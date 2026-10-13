import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class FaultState:
    kind: str | None = None
    target: str = ""
    injections: int = 0
    recovery_attempts: int = 0
    supported: bool = True
    journal: str | None = None
    recovered: bool = False

    # 将注入事实同步持久化，保留进程超时前已经发生的故障
    def persist(self):
        if self.journal:
            Path(self.journal).write_text(json.dumps(self.record()))

    # 在目标位置最多触发一次，只统计首次成功之前的恢复尝试
    def hit(self) -> bool:
        if self.recovered:
            return False
        if self.injections:
            self.recovery_attempts += 1
            self.persist()
            return False
        self.injections = 1
        self.persist()
        return True

    # 结束当前故障的尝试计数，后续正常调用不算恢复尝试
    def complete(self):
        self.recovered = True
        self.persist()

    # 输出明确区分未触发与不支持的故障状态
    def record(self) -> dict:
        return {
            **asdict(self),
            "status": (
                "unsupported"
                if not self.supported
                else "none"
                if not self.kind
                else "triggered"
                if self.injections
                else "not_triggered"
            ),
        }
