import pytest
from evals.run_eval import load_cases, validate_case
from evals.schema import EvalCase, GraderSpec
from pydantic import ValidationError


# 功能：验证 JSON 往返与配置静态检查
# 设计：从实际 pilot 配置读取，覆盖未来任务文件与 schema 的漂移
def test_roundtrip(pilot):
    assert EvalCase.model_validate_json(pilot.model_dump_json()) == pilot
    validate_case(pilot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_steps", 0),
        ("timeout_seconds", -1),
        ("runner_type", "shell"),
        ("graders", []),
        ("case_id", "../escape"),
        ("unknown", 1),
    ],
)
# 功能：拒绝无效预算、未授权 runner 与多余配置
# 设计：独立变异真实案例的单个字段，确保错误在执行前暴露
def test_reject_bad_case(pilot, field, value):
    data = pilot.model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        EvalCase.model_validate(data)


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "exists", "path": "../secret"},
        {"kind": "hash", "path": "a"},
        {"kind": "pytest", "test_id": "bash -c malicious"},
        {"kind": "composite", "children": []},
        {"kind": "events"},
    ],
)
# 功能：拒绝路径逃逸和任意宿主命令
# 设计：直接构造危险或缺少关键字段的验收配置，不执行任何命令
def test_reject_bad_grader(data):
    with pytest.raises(ValidationError):
        GraderSpec.model_validate(data)


# 功能：验证正式任务集数量、类别分布和全部 fixture
# 设计：静态校验所有 JSON，避免需要运行模型才能发现配置问题
def test_all_case_inventory():
    from collections import Counter

    cases = load_cases("e2e")
    assert Counter(c.category for c in cases) == {
        "files": 4,
        "summary": 4,
        "code_fix": 4,
        "multi_tool": 3,
        "subagent": 3,
        "permission": 2,
        "multi_turn": 2,
        "reconnect": 2,
    }
    assert len(load_cases("compact")) == 10
    assert len(load_cases("faults")) == 21
    formal = load_cases("long-formal")
    development = load_cases("long-dev")
    assert len(formal) == 30 and len(development) == 5
    assert Counter(case.metadata.task_type for case in formal) == {
        "requirement_change": 6,
        "exact_information": 6,
        "multi_file_code": 6,
        "workflow": 6,
        "document_processing": 6,
    }
    for case in load_cases("all"):
        validate_case(case)
