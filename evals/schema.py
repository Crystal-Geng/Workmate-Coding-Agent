from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 拒绝绝对路径、父目录和跨平台歧义路径
def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"unsafe relative path: {value!r}")
    return value


class Fixture(StrictModel):
    source: str
    destination: str = "."

    @model_validator(mode="after")
    # 校验复制来源与目标均为相对路径
    def validate_paths(self) -> Fixture:
        relative_path(self.source)
        relative_path(self.destination)
        return self


class GraderSpec(StrictModel):
    kind: Literal[
        "exists",
        "absent",
        "exact",
        "hash",
        "json",
        "csv",
        "pytest",
        "events",
        "changes",
        "composite",
    ]
    path: str | None = None
    source: str | None = None
    expected: Any = None
    fields: dict[str, Any] = Field(default_factory=dict)
    preserve_other_fields: bool = False
    sources: list[str] = Field(default_factory=list)
    operation: Literal["sum", "group_sum", "count", "join", "json_sum"] = "sum"
    column: str = "amount"
    group_by: str = "region"
    event: str | None = None
    event_fields: dict[str, Any] = Field(default_factory=dict)
    minimum: int = Field(default=1, ge=1)
    allowed: list[str] = Field(default_factory=list)
    test_id: str | None = None
    children: list[GraderSpec] = Field(default_factory=list)
    constraint: bool = False

    @model_validator(mode="after")
    # 检查各验收器必须的配置并拒绝路径穿越
    def check_configuration(self) -> GraderSpec:
        for path in [self.path, self.source, *self.sources, *self.allowed]:
            if path is not None:
                relative_path(path)
        if self.kind in {"exists", "absent", "exact", "hash", "json", "csv"} and not self.path:
            raise ValueError("file grader requires path")
        if self.kind == "exact" and self.source is None and not isinstance(self.expected, str):
            raise ValueError("exact requires expected text or pristine source")
        if self.kind == "json" and not self.fields and "expected" not in self.model_fields_set:
            raise ValueError("json requires expected value or fields")
        if self.kind == "hash" and not self.source:
            raise ValueError("hash requires pristine source")
        if self.kind == "json" and self.preserve_other_fields and not self.source:
            raise ValueError("preservation requires pristine source")
        if self.kind == "csv" and not self.sources:
            raise ValueError("computed grader requires fixture sources")
        if self.kind == "events" and not self.event:
            raise ValueError("events requires event")
        if self.kind == "pytest" and self.test_id not in {
            "sort",
            "date",
            "filter",
            "dedupe",
            "pipeline",
            "long_context_code",
        }:
            raise ValueError("pytest test_id is not in the evaluator allowlist")
        if self.kind == "composite" and not self.children:
            raise ValueError("empty composite")
        return self


FaultKind = Literal[
    "runtime_error",
    "rate_limited",
    "timeout",
    "schema_error",
    "provider_stream",
    "subagent",
    "mcp_disconnect",
]


class FaultSpec(StrictModel):
    kind: FaultKind
    target: str = "write_file"
    planned_injections: Literal[1] = 1
    mode: Literal["transient", "safety"] = "transient"


class CompactSpec(StrictModel):
    checkpoint: str
    branches: list[Literal["A", "B", "C"]] = Field(default_factory=lambda: ["A", "B", "C"])
    context_window: int = Field(default=200_000, gt=0)
    output_reserve: int = Field(default=8192, gt=0)
    c_budget_rule: Literal["not_above_b_first_request"] = "not_above_b_first_request"

    @model_validator(mode="after")
    # 检查检查点与必需的对照分支
    def check_checkpoint(self) -> CompactSpec:
        relative_path(self.checkpoint)
        if not {"A", "B"}.issubset(self.branches) or len(set(self.branches)) != len(self.branches):
            raise ValueError("compact requires unique A and B branches")
        if self.branches[:2] != ["A", "B"]:
            raise ValueError("compact branches must run A then B, followed by optional C")
        return self


class DatasetMetadata(StrictModel):
    split: Literal["development", "formal"]
    task_type: Literal[
        "requirement_change",
        "exact_information",
        "multi_file_code",
        "workflow",
        "document_processing",
    ]
    source: Literal["synthetic", "repository_trace"]
    source_note: str
    context_length_bucket: Literal["short", "medium", "long"]
    key_fact_positions: list[Literal["early", "middle", "late"]]
    dataset_version: str


class EvalCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    name: str
    category: str
    goal: str = Field(min_length=1)
    runner_type: Literal["agent_runner", "ipc"] = "agent_runner"
    fixtures: list[Fixture]
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_steps: int = Field(default=20, gt=0, le=1000)
    graders: list[GraderSpec] = Field(min_length=1)
    required_events: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    fault_spec: FaultSpec | None = None
    compact_spec: CompactSpec | None = None
    turns: list[str] = Field(default_factory=list)
    permission: Literal["approve", "deny_outside"] = "approve"
    reconnect: bool = False
    script: list[list[dict[str, Any]]] = Field(default_factory=list)
    child_script: list[list[dict[str, Any]]] = Field(default_factory=list)

    required_history_facts: list[str] = Field(default_factory=list)
    metadata: DatasetMetadata | None = None

    @model_validator(mode="after")
    # 校验专项任务必须使用对应执行链
    def check_special_cases(self) -> EvalCase:
        for path in self.forbidden_changes:
            relative_path(path)
        if (self.reconnect or self.category == "permission") and self.runner_type != "ipc":
            raise ValueError("permission/reconnect cases require real IPC")
        if self.fault_spec and self.runner_type == "ipc":
            raise ValueError("fault injection is currently supported by agent_runner only")
        if self.compact_spec and self.runner_type != "agent_runner":
            raise ValueError("compact branches use AgentRunner")
        return self


class GraderResult(StrictModel):
    check: str
    passed: bool
    expected: Any = None
    actual: Any = None
    reason: str | None = None
    constraint: bool = False


class TrialResult(StrictModel):
    case_id: str
    trial_id: str
    category: str
    started_at: datetime
    repeat_id: int = 0
    cohort: str = "baseline"
    ended_at: datetime | None = None
    duration_seconds: float = 0
    git_commit: str | None = None
    git_dirty: bool = False
    provider: str = "fake"
    model: str = "scripted"
    configuration: dict[str, Any] = Field(default_factory=dict)
    framework_self_test: bool = True
    run_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    session_id: str | None = None
    runner_type: Literal["agent_runner", "ipc"]
    status: Literal["passed", "failed", "invalid"] = "invalid"
    passed: bool = False
    grader_results: list[GraderResult] = Field(default_factory=list)
    failure_type: str | None = None
    failure_reason: str | None = None
    fault_type: str | None = None
    fault_mode: str | None = None
    fault_status: Literal["none", "triggered", "not_triggered", "unsupported"] = "none"
    planned_injections: int = 0
    injection_count: int = 0
    recovery_attempts: int = 0
    human_intervention: bool = False
    safely_terminated: bool | None = None
    boundary_preserved: bool | None = None
    branch: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    token_count_method: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    events_path: str
    trace_path: str
    artifacts_path: str
    input_hash: str | None = None
    budget_tokens: int | None = None
    budget_matched: bool | None = None
    comparison_eligible: bool = True
    preparation_seconds: float = 0
    continuation_seconds: float = 0
    grader_seconds: float = 0
    termination_reason: str | None = None
    infrastructure_failure: bool = False
    evaluator_error: bool = False
