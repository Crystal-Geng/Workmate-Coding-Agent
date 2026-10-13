import dataclasses
import hashlib
import json
import time
from collections import defaultdict
from types import SimpleNamespace

from workmate.core.llm.types import LlmResponse, ToolCallBlock


class ScriptedProvider:
    # 保存评测器脚本，只用于验证框架，绝不表示模型能力
    def __init__(self, case):
        self.case = case
        self.root = None
        self.steps = defaultdict(int)

    # 通过真实工具执行预定义脚本，不直接写入验收产物
    async def chat(self, messages, tool_schemas, bus, run_id, **kwargs):
        if run_id == "compact":
            history = str(messages[0]["content"])
            summary = "\n".join(
                line for line in history.splitlines() if line.startswith("Constraint:")
            )
            return LlmResponse(stop_reason="end_turn", text=summary or "Scripted summary.")
        if self.root is None:
            self.root = run_id
        script = self.case.script if run_id == self.root else self.case.child_script
        # 多轮约束首轮只确认，后续 run 使用同一主脚本
        if self.case.turns:
            if len([m for m in messages if m["role"] == "user"]) == 1:
                return LlmResponse(stop_reason="end_turn", text="Constraints recorded (fake).")
            script = self.case.script
        visible = json.dumps(messages, ensure_ascii=False)
        if self.case.compact_spec and (
            "Constraint:" not in visible
            or any(fact not in visible for fact in self.case.required_history_facts)
        ):
            return LlmResponse(stop_reason="end_turn", text="No checkpoint constraints (fake).")
        index = self.steps[run_id]
        if "local_mcp" in self.case.tags and index == len(script):
            self.steps[run_id] += 1
            results = [
                b
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
                if b.get("type") == "tool_result"
            ]
            if results and not results[-1].get("is_error"):
                value = results[-1]["content"]
                output = "mcp.txt"
                content = value + "\n"
                if "mcp_append" in self.case.tags:
                    content = results[0]["content"] + content
                if "mcp_json" in self.case.tags:
                    output = "mcp.json"
                    content = json.dumps({"value": value})
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(
                            id=f"mcp-output-{run_id}",
                            name="write_file",
                            input={"path": output, "content": content},
                        )
                    ],
                )
            return LlmResponse(stop_reason="end_turn", text="Local MCP unavailable (fake).")
        self.steps[run_id] += 1
        actions = script[index] if index < len(script) else []
        calls = [
            ToolCallBlock(id=f"eval-{run_id}-{index}-{i}", name=a["name"], input=a.get("input", {}))
            for i, a in enumerate(actions)
        ]
        return LlmResponse(
            stop_reason="tool_use" if calls else "end_turn",
            tool_calls=calls,
            text="Scripted framework self-test.",
        )


class OfflineSdkStream:
    # 提供离线 SDK 形状，供真实 AnthropicProvider 的流重试自测
    def __init__(self, response):
        self.response = response

    # 返回离线流对象
    async def __aenter__(self):
        return self

    # 离线流没有外部资源需要关闭
    async def __aexit__(self, *args):
        return False

    @property
    # 返回至少两个片段以实际覆盖中途迭代异常
    def text_stream(self):
        return self.chunks()

    # 按片段输出自测文本
    async def chunks(self):
        yield "Scripted "
        yield "SDK stream."

    # 构造与 SDK 一致的消息结构，usage 为零且报告明确为 fake
    async def get_final_message(self):
        return SimpleNamespace(
            stop_reason=self.response.stop_reason,
            content=[
                SimpleNamespace(type="tool_use", id=t.id, name=t.name, input=t.input)
                for t in self.response.tool_calls
            ],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )


class OfflineSdkClient:
    # 对同一请求复用响应，避免把 Provider 重试误当成下一步 Agent 决策
    def __init__(self, case):
        self.case = case
        self.responses = {}
        self.messages = SimpleNamespace(stream=self.stream)

    # 按请求消息中 assistant 数量选择脚本响应
    def stream(self, **kwargs):
        key = json.dumps(kwargs, sort_keys=True)
        if key not in self.responses:
            step = sum(m["role"] == "assistant" for m in kwargs["messages"])
            actions = self.case.script[step] if step < len(self.case.script) else []
            self.responses[key] = LlmResponse(
                stop_reason="tool_use" if actions else "end_turn",
                tool_calls=[
                    ToolCallBlock(id=f"sdk-{step}-{i}", name=a["name"], input=a.get("input", {}))
                    for i, a in enumerate(actions)
                ],
            )
        return OfflineSdkStream(self.responses[key])


class EvaluationBudgetExceeded(RuntimeError):
    pass


class UsageRecorder:
    # 在 Provider 边界采集返回 usage，覆盖 Compactor 的独立 silent_bus
    def __init__(self, inner, counter=None, call_limit=None, input_token_limit=None):
        from evals.token_counting import EstimatedTokenCounter

        self.counter = counter or EstimatedTokenCounter()
        self.inner = inner
        self.calls = []
        self.before_history = None
        self.counts = {}
        self.call_limit = call_limit
        self.input_token_limit = input_token_limit

    def _request(self, messages, tool_schemas, kwargs):
        from evals.token_counting import request_shape

        request = request_shape(messages, kwargs.get("system", ""), tool_schemas, self.model)
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return request, hashlib.sha256(raw.encode()).hexdigest()

    # 分开记录摘要与后续请求成本，失败调用不猜测 token 数
    async def chat(self, messages, tool_schemas, bus, run_id, **kwargs):
        from evals.token_counting import request_shape

        request, request_hash = self._request(messages, tool_schemas, kwargs)
        if self.call_limit is not None and len(self.calls) >= self.call_limit:
            raise EvaluationBudgetExceeded("live call limit reached")
        observed_inputs = sum(
            call["normalized_usage"]["total_input_tokens"]
            for call in self.calls
            if call["normalized_usage"] is not None
            and call["normalized_usage"].get("total_input_tokens") is not None
        )
        if self.input_token_limit is not None and observed_inputs >= self.input_token_limit:
            raise EvaluationBudgetExceeded("live input-token stop reached")
        if self.before_history is not None and run_id != "compact" and not self.counts:
            counter = self.counter
            base = self.before_history + [messages[-1]]
            common = (request["system"][0]["text"], tool_schemas, self.model)
            self.counts = {
                "tokens_before": counter.count(request_shape(base, *common)),
                "tokens_after": counter.count(request_shape(messages, *common)),
                "token_count_method": counter.method,
                "input_hash": request_hash,
            }
        phase = "summary" if run_id == "compact" else "continuation"
        started = time.monotonic()
        try:
            response = await self.inner.chat(messages, tool_schemas, bus, run_id, **kwargs)
        except Exception:
            self.calls.append(
                {
                    "call_id": f"call-{len(self.calls) + 1}",
                    "provider_request_id": None,
                    "phase": phase,
                    "request_hash": request_hash,
                    "duration_seconds": time.monotonic() - started,
                    "failed": True,
                    "raw_usage": None,
                    "normalized_usage": None,
                    "potential_unobserved_usage": True,
                }
            )
            raise
        raw = dataclasses.asdict(response.usage) if response.usage else None
        normalized = None
        if raw is not None:
            normalized = {
                "ordinary_input_tokens": raw.get("input_tokens"),
                "cache_read_input_tokens": raw.get("cache_read_input_tokens"),
                "cache_creation_input_tokens": raw.get("cache_creation_input_tokens"),
                "output_tokens": raw.get("output_tokens"),
                "reasoning_tokens": raw.get("reasoning_tokens"),
            }
            input_keys = (
                "ordinary_input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
            inputs = [normalized[key] for key in input_keys]
            normalized["total_input_tokens"] = (
                sum(inputs) if all(value is not None for value in inputs) else None
            )
        self.calls.append(
            {
                "call_id": f"call-{len(self.calls) + 1}",
                "provider_request_id": getattr(response, "request_id", None),
                "phase": phase,
                "request_hash": request_hash,
                "duration_seconds": time.monotonic() - started,
                "failed": False,
                "raw_usage": raw,
                "normalized_usage": normalized,
                "potential_unobserved_usage": raw is None,
            }
        )
        return response

    # 汇总已知 usage 并显式标记不可观测的重试和重新读取成本
    def totals(self):
        keys = (
            "ordinary_input_tokens",
            "total_input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
        )

        def aggregate(calls):
            if not calls:
                return {key: 0 for key in keys}
            return {
                key: (
                    sum(call["normalized_usage"][key] for call in calls)
                    if all(
                        call["normalized_usage"] is not None
                        and call["normalized_usage"].get(key) is not None
                        for call in calls
                    )
                    else None
                )
                for key in keys
            }

        totals = aggregate(self.calls)
        totals["summary"] = aggregate([call for call in self.calls if call["phase"] == "summary"])
        totals["calls"] = self.calls
        totals["complete"] = bool(self.calls) and all(
            call["normalized_usage"] is not None for call in self.calls
        )
        totals["mapping"] = {
            "total_input_tokens": "ordinary + cache_read + cache_creation; provider fields treated as disjoint",  # noqa: E501
            "output_tokens": "provider output_tokens; reasoning remains separate when exposed",
        }
        totals["automatic_retry_usage_observable"] = False
        totals["limitations"] = (
            "failed responses, internal SDK retries, and causal reread cost may be unobservable"
        )
        return totals
