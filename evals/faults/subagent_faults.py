from evals.faults.base import FaultState


class SubagentFault:
    # 使用真实 subagent.started 事件识别子执行链
    def __init__(self, inner, state: FaultState):
        self.inner, self.state = inner, state
        self.children = set()
        self.attempted_runs = set()

    # 记录生产事件中的子 run_id
    async def observe(self, event):
        data = event.model_dump()
        if data.get("type") == "subagent.started":
            self.children.add(data["run_id"])
        if data.get("type") == "subagent.finished" and data.get("status") == "success":
            self.state.complete()

    # 在首个真实子 Agent 的模型调用中失败，交由子 loop 与父工具处理
    async def chat(self, messages, tool_schemas, bus, run_id, **kwargs):
        if run_id in self.children and run_id not in self.attempted_runs:
            self.attempted_runs.add(run_id)
            if self.state.hit():
                raise RuntimeError("eval: first subagent execution failed")
        return await self.inner.chat(messages, tool_schemas, bus, run_id, **kwargs)
