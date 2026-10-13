import json

from evals.run_eval import execute_trial, load_cases, run_suite
from evals.runners.compact_runner import legal_history, truncate_legal
from evals.token_counting import EstimatedTokenCounter, request_shape


# 功能：压缩两侧完整请求使用同一计数方法和范围
# 设计：固定系统提示与工具，只缩短历史，检查 Unicode 与工具 schema 都计入
def test_same_request_scope():
    counter = EstimatedTokenCounter()
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    before = request_shape([{"role": "user", "content": "长历史" * 100}], "system", tools, "model")
    after = request_shape([{"role": "user", "content": "摘要"}], "system", tools, "model")
    assert counter.count(before) > counter.count(after)
    assert counter.count(after) > counter.count({"messages": after["messages"]})
    assert counter.method.startswith("estimated:")


# 功能：真实 Compactor 的 silent_bus 调用被单独采集且 A/B/C 使用独立 Session
# 设计：运行一个离线长历史案例，只验证统计口径和执行隔离，不评价真实模型能力
async def test_compact_branches_capture_summary_usage(options):
    options.suite = "compact"
    output = await run_suite(load_cases("compact", "compact-01"), options)
    rows = [json.loads(s) for s in (output / "trials.jsonl").read_text().splitlines()]
    assert [r["branch"] for r in rows] == list("ABC")
    assert len({r["session_id"] for r in rows}) == 3
    assert all(r["token_count_method"] == rows[0]["token_count_method"] for r in rows)
    b = rows[1]
    assert any(c["phase"] == "summary" for c in b["usage"]["calls"])
    assert b["usage"]["summary"]["total_input_tokens"] is None
    summary = json.loads((output / "summary.json").read_text())
    assert summary["summary_call_usage"]["total_input_tokens"] is None
    assert b["tokens_before"] > b["tokens_after"]


async def test_c_without_b_budget_is_preserved_as_dependency_failure(tmp_path, options):
    for folder in ("artifacts", "events", "traces"):
        (tmp_path / folder).mkdir()
    case = load_cases("compact", "compact-01")[0]
    row = await execute_trial(
        case,
        "baseline-1-C",
        tmp_path,
        options,
        {"git_commit": "test", "git_dirty": False},
        branch="C",
        budget=None,
        repeat_id=1,
    )
    assert row.status == "failed"
    assert row.failure_type == "dependency_failed"
    assert row.comparison_eligible is False
    assert row.infrastructure_failure is False


def test_truncation_never_orphans_tool_messages():
    history = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "value"}],
        },
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "ack"},
    ]
    selected, matched = truncate_legal(history, lambda rows: len(rows), 3)
    assert matched and selected == history[-2:]
    assert legal_history(selected)
    assert not legal_history(history[2:])
