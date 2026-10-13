import pytest
from evals.faults.base import FaultState
from evals.faults.mcp_faults import LocalMcp
from evals.faults.provider_faults import StreamClient
from evals.faults.tool_faults import ToolFault
from evals.runners.providers import OfflineSdkClient

from workmate.core.events.bus import EventBus
from workmate.core.llm.provider import AnthropicProvider
from workmate.core.llm.types import ToolCallBlock
from workmate.core.tools.base import BaseTool, ToolResult
from workmate.core.tools.invocation import invoke_tool
from workmate.core.tools.registry import ToolRegistry


class CountingTool(BaseTool):
    name = "count"
    description = "count calls"
    input_schema = {}

    # 初始化实际执行次数
    def __init__(self):
        self.calls = 0

    # 记录工具实际副作用次数
    async def invoke(self, params):
        self.calls += 1
        return ToolResult("done")


@pytest.mark.parametrize("kind", ["runtime_error", "rate_limited"])
# 功能：一次性工具故障由生产重试恢复，写入副作用仅发生一次
# 设计：只将测试退避设为零，仍执行真实 invoke_tool 循环
async def test_tool_retry_once(kind, monkeypatch):
    monkeypatch.setattr("workmate.core.tools.invocation._RETRY_BASE_S", 0)
    state = FaultState(kind, "count")
    inner = CountingTool()
    registry = ToolRegistry()
    registry.register(ToolFault(inner, state))
    response = await invoke_tool(registry, ToolCallBlock("id", "count", {}), EventBus(), "run")
    assert not response.is_error
    assert state.injections == 1 and state.recovery_attempts == 1
    assert inner.calls == 1


# 功能：超时错误不被误报为现有工具层自动重试
# 设计：使用生产 invoke_tool 的 TimeoutError 捕获分支检查实际执行计数
async def test_timeout_is_not_automatically_retried():
    inner, state = CountingTool(), FaultState("timeout", "count")
    registry = ToolRegistry()
    registry.register(ToolFault(inner, state))
    response = await invoke_tool(registry, ToolCallBlock("id", "count", {}), EventBus(), "run")
    assert response.error_type == "timeout"
    assert state.recovery_attempts == 0 and inner.calls == 0


# 功能：底层 stream 中途异常被真实 Provider 内部重试捕获
# 设计：仅使用离线 SDK 对象，验证重试后保留工具调用而非外层重试 chat
async def test_provider_retries_internal_stream(pilot, monkeypatch):
    monkeypatch.setattr("workmate.core.llm.provider._RETRY_BACKOFF_S", (0, 0, 0))
    state = FaultState("provider_stream")
    provider = AnthropicProvider("scripted", client=StreamClient(OfflineSdkClient(pilot), state))
    response = await provider.chat([{"role": "user", "content": pilot.goal}], [], EventBus(), "run")
    assert response.tool_calls
    assert state.injections == 1 and state.recovery_attempts == 1


# 功能：本地 MCP 真实断连后不伪造客户端重连能力
# 设计：直接测试 TCP EOF 与后续同一连接调用，禁止替生产客户端补连接
async def test_mcp_real_disconnect():
    state = FaultState("mcp_disconnect")
    local = LocalMcp(state)
    try:
        tool = await local.start()
        first = await tool.invoke({})
        second = await tool.invoke({})
        assert first.is_error and second.is_error
        assert state.injections == 1
    finally:
        await local.close()


# 功能：一次性参数错误进入真实校验层且不自动重试
# 设计：首次调用失败后显式发第二次调用，区分工具重试与模型再次请求
async def test_schema_fault_runs_in_validation_layer():
    inner, state = CountingTool(), FaultState("schema_error", "count")
    registry = ToolRegistry()
    registry.register(ToolFault(inner, state))
    first = await invoke_tool(registry, ToolCallBlock("first", "count", {}), EventBus(), "run")
    assert first.error_type == "schema_error"
    assert inner.calls == 0 and state.injections == 1 and state.recovery_attempts == 0
    second = await invoke_tool(registry, ToolCallBlock("second", "count", {}), EventBus(), "run")
    assert not second.is_error and inner.calls == 1
    assert state.injections == 1 and state.recovery_attempts == 1


# 功能：故障恢复后的普通工具调用不会继续增加恢复次数
# 设计：在真实重试成功后再执行一次正常调用，排除统计把所有后续调用都算成恢复
async def test_recovery_counter_stops_after_success(monkeypatch):
    monkeypatch.setattr("workmate.core.tools.invocation._RETRY_BASE_S", 0)
    inner, state = CountingTool(), FaultState("runtime_error", "count")
    registry = ToolRegistry()
    registry.register(ToolFault(inner, state))
    for call_id in ("first", "second"):
        await invoke_tool(registry, ToolCallBlock(call_id, "count", {}), EventBus(), "run")
    assert inner.calls == 2 and state.recovery_attempts == 1


# 功能：SubAgent 恢复按重新执行的子 run 计数，而非每轮模型调用
# 设计：第一次子 run 失败，第二次子 run 连续调用两轮，恢复尝试只能增加一次
async def test_subagent_recovery_counts_runs():
    from unittest.mock import AsyncMock

    from evals.faults.subagent_faults import SubagentFault

    from workmate.core.llm.types import LlmResponse

    state = FaultState("subagent", "spawn_agent")
    inner = AsyncMock()
    inner.chat.return_value = LlmResponse(stop_reason="end_turn", text="done")
    provider = SubagentFault(inner, state)
    provider.children.update({"first", "second"})
    with pytest.raises(RuntimeError):
        await provider.chat([], [], EventBus(), "first")
    for _ in range(2):
        await provider.chat([], [], EventBus(), "second")
    assert state.injections == 1 and state.recovery_attempts == 1
