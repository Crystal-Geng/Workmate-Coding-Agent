import json

from evals.graders.base import GradeContext, snapshot
from evals.graders.composite import grade
from evals.schema import GraderSpec


# 功能：验证 composite 同时检查正确产物和受保护文件
# 设计：先验收成功，再只破坏一项条件，排除组合逻辑错误
def test_composite(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    spec = GraderSpec(
        kind="composite",
        children=[
            GraderSpec(kind="exact", path="a.txt", expected="hello"),
            GraderSpec(kind="absent", path="bad.txt"),
        ],
    )
    ctx = GradeContext(tmp_path, tmp_path)
    assert all(r.passed for r in grade(spec, ctx))
    (tmp_path / "bad.txt").touch()
    assert [r.passed for r in grade(spec, ctx)] == [True, False]


# 功能：只允许指定 JSON 字段变化，无关嵌套字段不能丢失
# 设计：使用不同的原始与最终文件，避免验收器读取已经修改的答案来源
def test_json_preserves_other_fields(tmp_path):
    original = {"enabled": False, "nested": {"keep": [1, 2]}}
    (tmp_path / "source.json").write_text(json.dumps(original))
    spec = GraderSpec(
        kind="json",
        path="out.json",
        source="source.json",
        fields={"enabled": True},
        preserve_other_fields=True,
    )
    ctx = GradeContext(tmp_path, tmp_path)
    (tmp_path / "out.json").write_text(json.dumps({**original, "enabled": True}))
    assert grade(spec, ctx)[0].passed
    (tmp_path / "out.json").write_text('{"enabled":true}')
    assert not grade(spec, ctx)[0].passed


# 功能：CSV 答案由原始数据独立计算且检测篡改
# 设计：包含小数及多个输入文件，覆盖拼接和数字转换
def test_csv_independent_oracle(tmp_path):
    for name, text in [("a.csv", "amount\n1.5\n"), ("b.csv", "amount\n2.25\n")]:
        (tmp_path / name).write_text(text)
    spec = GraderSpec(kind="csv", path="out.json", sources=["a.csv", "b.csv"])
    ctx = GradeContext(tmp_path, tmp_path)
    (tmp_path / "out.json").write_text('{"total":3.75}')
    assert grade(spec, ctx)[0].passed
    (tmp_path / "out.json").write_text('{"total":3}')
    assert not grade(spec, ctx)[0].passed


# 功能：哈希检查覆盖字节变动，目录约束覆盖未授权新增文件
# 设计：内容只相差换行仍必须失败，防止文本规范化掩盖修改
def test_hash_and_unauthorized_changes(tmp_path):
    (tmp_path / "original").write_bytes(b"same\n")
    (tmp_path / "copy").write_bytes(b"same\n")
    ctx = GradeContext(tmp_path, tmp_path, initial=snapshot(tmp_path))
    spec = GraderSpec(kind="hash", path="copy", source="original")
    assert grade(spec, ctx)[0].passed
    (tmp_path / "copy").write_bytes(b"same")
    assert not grade(spec, ctx)[0].passed
    assert not grade(GraderSpec(kind="changes", allowed=[]), ctx)[0].passed


# 功能：required event 不能被文字回答或错误状态事件冒充
# 设计：相同事件类型分别使用 failed 与 success 字段验证匹配条件
def test_event_fields(tmp_path):
    spec = GraderSpec(kind="events", event="subagent.finished", event_fields={"status": "success"})
    ctx = GradeContext(
        tmp_path, tmp_path, events=[{"type": "subagent.finished", "status": "failed"}]
    )
    assert not grade(spec, ctx)[0].passed
    ctx.events[0]["status"] = "success"
    assert grade(spec, ctx)[0].passed


# 功能：最终产物的符号链接不能让 grader 读出工作目录之外的文件
# 设计：链接到临时根外的已有文件，校验器必须拒绝路径解析
def test_symlink_cannot_escape(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "secret").write_text("hidden")
    (root / "out").symlink_to(tmp_path / "secret")
    results = grade(
        GraderSpec(kind="exact", path="out", expected="hidden"), GradeContext(root, root)
    )
    assert not results[0].passed
    assert results[0].constraint


# 功能：JSON 布尔值不能被数字冒充，必需 null 字段不能缺失
# 设计：比较 Python 相等但 JSON 语义不同的值，覆盖静默误判边界
def test_json_types_and_required_null(tmp_path):
    path = tmp_path / "out.json"
    ctx = GradeContext(tmp_path, tmp_path)
    path.write_text('{"enabled": 1}')
    assert not grade(GraderSpec(kind="json", path="out.json", fields={"enabled": True}), ctx)[
        0
    ].passed
    path.write_text("{}")
    assert not grade(GraderSpec(kind="json", path="out.json", fields={"required": None}), ctx)[
        0
    ].passed
    path.write_text('{"required": null}')
    assert grade(GraderSpec(kind="json", path="out.json", fields={"required": None}), ctx)[0].passed
    path.write_text('{"enabled": false, "enabled": true}')
    assert not grade(GraderSpec(kind="json", path="out.json", fields={"enabled": True}), ctx)[
        0
    ].passed


# 功能：未授权 write_file 写入即使最终恢复原文件也不能通过约束验收
# 设计：使用真实事件结构并保持最终快照不变，覆盖单纯哈希检查无法发现的行为
def test_restored_unauthorized_write_is_detected(tmp_path):
    (tmp_path / "protected.txt").write_text("original")
    ctx = GradeContext(
        tmp_path,
        tmp_path,
        initial=snapshot(tmp_path),
        events=[
            {
                "type": "tool.call_started",
                "tool_name": "write_file",
                "tool_use_id": "call",
                "params": {"path": "protected.txt", "content": "changed"},
            },
            {"type": "tool.call_finished", "tool_name": "write_file", "tool_use_id": "call"},
        ],
    )
    check = grade(GraderSpec(kind="changes", allowed=["answer.txt"]), ctx)[0]
    assert not check.passed and "observed write_file:protected.txt" in check.actual


# 功能：只伪造 completed 文本而未实现计算脚本时不能通过多工具验收
# 设计：所有输入和表面产物齐全，但重新执行脚本不产生汇总，私有 pytest 应失败
def test_pipeline_rejects_fake_completion(tmp_path):
    from evals.run_eval import load_cases, prepare_workspace

    workspace = tmp_path / "workspace"
    prepare_workspace(load_cases("e2e", "e2e-13")[0], workspace)
    (workspace / "summary.json").write_text('{"total":33}')
    (workspace / "process.py").write_text("print('completed')\n")
    check = grade(
        GraderSpec(kind="pytest", test_id="pipeline"), GradeContext(workspace, workspace)
    )[0]
    assert not check.passed and check.actual["exit_code"] == 1
