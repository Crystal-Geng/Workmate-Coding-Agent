import argparse
import asyncio
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from evals.faults.base import FaultState
from evals.faults.mcp_faults import LocalMcp
from evals.faults.provider_faults import StreamClient
from evals.faults.subagent_faults import SubagentFault
from evals.runners.agent_runner import EvaluationAgentRunner
from evals.runners.providers import OfflineSdkClient, ScriptedProvider, UsageRecorder
from evals.schema import EvalCase
from workmate.core.config import WorkmateConfig
from workmate.core.events.bus import EventBus
from workmate.core.llm.provider import AnthropicProvider
from workmate.core.permissions.manager import PermissionManager
from workmate.core.trace.record import TraceRecord
from workmate.core.trace.writer import TraceWriter


# 在创建任何真实客户端前检查显式 live 门禁和凭证
def make_provider(request, case, fault):
    if request["provider"] == "fake":
        if fault.kind == "provider_stream":
            client = StreamClient(OfflineSdkClient(case), fault)
            return AnthropicProvider(request["model"], client=client)
        return ScriptedProvider(case)
    if not request.get("live"):
        raise ValueError("real provider requires --live")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is required; no fallback to fake")
    provider = AnthropicProvider(request["model"])
    if fault.kind == "provider_stream":
        provider._client = StreamClient(provider._client, fault)
    return provider


# 在独立进程中收集完整总线事件、trace、usage 和故障触发事实
async def execute(request, case, daemon):
    state_root = Path(request["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    fault = (
        FaultState(case.fault_spec.kind, case.fault_spec.target)
        if case.fault_spec
        else FaultState()
    )
    fault.journal = str(state_root / "fault.json")
    fault.persist()
    inner = make_provider(request, case, fault)
    subfault = SubagentFault(inner, fault) if fault.kind == "subagent" else None
    recorder = UsageRecorder(
        subfault or inner,
        call_limit=request.get("call_limit"),
        input_token_limit=request.get("input_token_limit"),
    )
    recorder.model = request["model"]
    config = WorkmateConfig(port=request.get("port", 7437))
    config.agent.max_steps = case.max_steps
    config.llm.default_model = request["model"]
    config.logging.file = ""
    config.logging.level = "WARNING"
    config.trace.file = request["trace_path"]
    bus = EventBus()
    trace = TraceWriter(Path(request["trace_path"]))
    captured = []
    result = {}
    local = None
    events_file = Path(request["events_path"]).open("w")

    # 同步落盘事件，使超时或中断后仍能复盘实际发生的工作
    async def observe(event):
        data = event.model_dump(mode="json")
        captured.append(data)
        events_file.write(json.dumps(data, ensure_ascii=False) + "\n")
        events_file.flush()
        if subfault:
            await subfault.observe(event)
        if not daemon:
            trace.emit(
                TraceRecord(
                    ts=data.get("ts", ""),
                    direction="CORE",
                    layer="event",
                    kind="event",
                    run_id=data.get("run_id"),
                    data=data,
                )
            )

    bus.subscribe(observe)
    permission = PermissionManager(timeout_s=10)

    # 普通任务的评测策略自动批准；权限专项任务使用独立 IPC 客户端
    async def approve(event):
        if event.type == "permission.requested":
            permission.respond(event.tool_use_id, "allow_once")

    if not daemon:
        bus.subscribe(approve)
    try:
        if "local_mcp" in case.tags:
            local = LocalMcp(fault if fault.kind == "mcp_disconnect" else None)
            local_tool = await local.start()
        else:
            local_tool = None
        if daemon:
            from workmate.core.app import CoreApp

            # 注入现有 CoreApp 使用的依赖，不复制或替换协议处理逻辑
            def runner_factory(*args, **kwargs):
                return EvaluationAgentRunner(
                    *args,
                    provider=recorder,
                    fault_state=fault,
                    local_tool=local_tool,
                    workspace=Path.cwd(),
                    **kwargs,
                )

            app = CoreApp()
            app._bus.subscribe(observe)
            with (
                patch("workmate.core.app.get_config", return_value=config),
                patch("workmate.core.app.AnthropicProvider", return_value=recorder),
                patch("workmate.core.app.AgentRunner", runner_factory),
            ):
                await app.run()
        else:
            from evals.runners.agent_runner import run

            await trace.start()
            result = await run(
                case,
                config,
                recorder,
                bus,
                trace,
                permission,
                state_root,
                fault,
                local_tool,
                request.get("branch"),
                Path(request["fixture_root"]),
                request.get("compact_budget"),
            )
    except Exception as exc:
        from evals.runners.compact_runner import CompactionFailed, UnsupportedFullHistory
        from evals.runners.providers import EvaluationBudgetExceeded

        key = (
            "agent_error"
            if isinstance(exc, (CompactionFailed, UnsupportedFullHistory, EvaluationBudgetExceeded))
            else "worker_error"
        )
        result[key] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, UnsupportedFullHistory):
            result.update(error_type="unsupported_context", comparison_eligible=False)
        elif isinstance(exc, CompactionFailed):
            result["error_type"] = "compaction_failed"
        elif isinstance(exc, EvaluationBudgetExceeded):
            result.update(error_type="budget_exceeded", comparison_eligible=False)
    finally:
        if local:
            await local.close()
        if not daemon:
            await trace.stop()
        events_file.close()
        sessions = [e["session_id"] for e in captured if e["type"] == "session.created"]
        runs = [e["run_id"] for e in captured if e["type"] == "run.started"]
        result.update(
            session_id=sessions[0] if sessions else None,
            run_ids=runs,
            run_id=runs[-1] if runs else None,
            fault=fault.record(),
            usage=recorder.totals(),
            **recorder.counts,
        )
        if fault.kind == "provider_stream" and fault.injections:
            result["usage"]["complete"] = False
        Path(request["worker_result"]).write_text(json.dumps(result, indent=2))


# 使用评测专用路径映射隔离全局记忆，不修改宿主 HOME 或生产源文件
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    case = EvalCase.model_validate(request["case"])
    original_expanduser = Path.expanduser
    isolated_home = Path(request["state_root"])

    # 将生产代码对用户配置目录的读取限定到该试次的独立状态目录
    def expanduser(path):
        name = str(path)
        if name == "~" or name.startswith("~/"):
            return isolated_home / name[2:] if name != "~" else isolated_home
        return original_expanduser(path)

    with ExitStack() as stack:
        stack.enter_context(patch.object(Path, "expanduser", expanduser))
        if case.runner_type == "ipc" and not args.daemon:
            from evals.runners.ipc_runner import run

            evidence = asyncio.run(run(args.request, case))
            path = Path(request["worker_result"])
            data = json.loads(path.read_text()) if path.exists() else {}
            data["ipc"] = evidence
            path.write_text(json.dumps(data, indent=2))
        else:
            asyncio.run(execute(request, case, args.daemon))


if __name__ == "__main__":
    main()
