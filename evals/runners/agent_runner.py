from pathlib import Path

from evals.faults.tool_faults import ToolFault
from evals.runners.sandbox_tools import SandboxedSpawnAgentTool, install_sandbox_tools
from workmate.core.runner import AgentRunner
from workmate.core.session.manager import SessionManager
from workmate.core.session.store import SessionStore


class EvaluationAgentRunner(AgentRunner):
    # 只在评测适配器中包装注册表，生产默认构造行为保持原样
    def __init__(self, *args, fault_state=None, local_tool=None, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault_state = fault_state
        self.local_tool = local_tool
        self.workspace = workspace

    # 在已有 registry 扩展点安装一次性故障与本地 MCP 工具
    def _build_registry(self, *args, **kwargs):
        registry = super()._build_registry(*args, **kwargs)
        if self.workspace is None:
            raise RuntimeError("evaluation sandbox workspace is required")
        install_sandbox_tools(registry, self.workspace)
        provider = kwargs.get("provider")
        bus = kwargs.get("bus")
        run_id = kwargs.get("run_id")
        session = kwargs.get("session")
        store = kwargs.get("store")
        if provider is not None and bus is not None and run_id is not None:
            registry.register(
                SandboxedSpawnAgentTool(
                    provider=provider,
                    parent_bus=bus,
                    parent_run_id=run_id,
                    permission_manager=self._permission_manager,
                    max_steps=self._config.agent.max_steps,
                    task_registry=self._task_registry,
                    runs_dir=kwargs.get("child_runs_dir") or self._runs_dir,
                    session_id=session.id if session is not None and store is not None else "",
                    depth=0,
                    workspace=self.workspace,
                )
            )
        if self.local_tool:
            registry.register(self.local_tool)
        state = self.fault_state
        if state and state.kind in {"runtime_error", "rate_limited", "timeout", "schema_error"}:
            target = registry.get(state.target)
            if target:
                registry.register(ToolFault(target, state))
        return registry


# 使用真实 SessionManager 与 AgentRunner 执行一个全新 trial
async def run(
    case,
    config,
    provider,
    bus,
    trace,
    permission,
    state_root,
    fault_state,
    local_tool=None,
    branch=None,
    fixture_root=None,
    budget=None,
):
    store = SessionStore(state_root / "sessions")

    # 为同一 Session 的各轮创建生产 runner，Provider 状态只在当前 trial 内共享
    def factory():
        return EvaluationAgentRunner(
            config,
            provider=provider,
            bus=bus,
            trace=trace,
            permission_manager=permission,
            fault_state=fault_state,
            local_tool=local_tool,
            workspace=Path.cwd(),
            runs_dir=state_root / "runs",
        )

    manager = SessionManager(store, factory, bus, provider)
    session = await manager.create(mode="chat", title=case.name)
    preparation = {}
    if case.compact_spec:
        from evals.runners.compact_runner import prepare

        preparation = await prepare(
            case, branch, fixture_root, state_root, session, store, factory(), provider, bus, budget
        )
    for goal in case.turns + [case.goal]:
        await manager.send_message(session.id, goal)
    return {
        "session_id": session.id,
        "run_id": session.run_ids[-1],
        "run_ids": session.run_ids,
        **preparation,
    }
