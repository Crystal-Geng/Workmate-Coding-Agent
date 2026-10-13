import copy
import json
import time

from evals.graders.base import confined
from evals.token_counting import request_shape
from workmate.core.compact.compactor import Compactor
from workmate.core.context import ExecutionContext
from workmate.core.llm.provider import _SYSTEM_PROMPT
from workmate.core.task.manager import TaskManager
from workmate.core.trace.provider import TracingProvider


class CompactionFailed(RuntimeError):
    pass


class UnsupportedFullHistory(RuntimeError):
    pass


def _blocks(message, kind):
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if block.get("type") == kind]


def legal_history(messages):
    if messages and messages[0].get("role") != "user":
        return False
    pending = set()
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            return False
        if role == "assistant":
            pending.update(block.get("id") for block in _blocks(message, "tool_use"))
        for block in _blocks(message, "tool_result"):
            tool_id = block.get("tool_use_id")
            if tool_id not in pending:
                return False
            pending.remove(tool_id)
    return not pending


def truncate_legal(history, count, budget):
    candidates = [
        history[index:] for index in range(len(history) + 1) if legal_history(history[index:])
    ]
    matching = [candidate for candidate in candidates if count(candidate) <= budget]
    return (max(matching, key=len), True) if matching else ([], False)


# 从不可变检查点构造分支历史，B 调用生产 Compactor，C 截去最早的完整消息对
async def prepare(
    case, branch, fixture_root, state_root, session, store, runner, provider, bus, budget=None
):
    history = json.loads(confined(fixture_root, case.compact_spec.checkpoint).read_text())
    started = time.monotonic()
    if not isinstance(history, list) or not history:
        raise ValueError("empty compact checkpoint")
    provider.before_history = copy.deepcopy(history)
    selected = copy.deepcopy(history)
    registry = runner._build_registry(
        TaskManager(state_root / "counting-tasks"),
        session=session,
        store=store,
        run_id="counting",
        provider=provider,
        bus=bus,
        session_id=session.id,
    )
    counter = provider.counter
    schemas = registry.tool_schemas()

    # 对每个分支使用相同系统提示、工具和后续目标计算完整请求
    def count(messages):
        return counter.count(
            request_shape(
                messages + [{"role": "user", "content": case.goal}],
                _SYSTEM_PROMPT,
                schemas,
                provider.model,
            )
        )

    if (
        branch == "A"
        and count(history) + case.compact_spec.output_reserve > case.compact_spec.context_window
    ):
        raise UnsupportedFullHistory("full-history branch exceeds configured context window")
    if branch == "B":
        context = ExecutionContext(
            run_id="checkpoint", goal=case.goal, max_steps=case.max_steps, prefill_messages=selected
        )
        compactor = Compactor(bus, store.session_dir(session.id), session.id)
        compacted = await compactor.compact(context, TracingProvider(provider, runner._trace))
        if compacted is None:
            raise CompactionFailed("compaction did not produce a summary")
        selected = context.messages
    elif branch == "C":
        if budget is None:
            raise ValueError("C requires successful B request token budget")
        selected, budget_matched = truncate_legal(selected, count, budget)
    else:
        budget_matched = None
    if branch == "B":
        budget_matched = True
    for message in selected:
        store.append_message(session.id, message["role"], message["content"])
    return {
        "checkpoint_messages": len(history),
        "selected_messages": len(selected),
        "budget_tokens": budget,
        "budget_matched": budget_matched,
        "preparation_seconds": time.monotonic() - started,
        "comparison_eligible": branch != "C" or budget_matched,
    }
