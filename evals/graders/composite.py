from evals.graders import constraints, csv_grader, events_grader, files, json_grader, pytest_grader
from evals.graders.base import GradeContext, snapshot
from evals.schema import EvalCase, GraderResult, GraderSpec

GRADERS = {
    **dict.fromkeys(("exists", "absent", "exact", "hash"), files.grade),
    "json": json_grader.grade,
    "csv": csv_grader.grade,
    "events": events_grader.grade,
    "changes": constraints.grade,
    "pytest": pytest_grader.grade,
}


# 展开组合验收器并保留每个叶子检查的诊断信息
def grade(spec: GraderSpec, ctx: GradeContext) -> list[GraderResult]:
    if spec.kind == "composite":
        results = [r for child in spec.children for r in grade(child, ctx)]
        if spec.constraint:
            for item in results:
                item.constraint = True
        return results
    try:
        return [GRADERS[spec.kind](spec, ctx)]
    except ValueError as exc:
        if "path escapes evaluation root" not in str(exc):
            raise
        return [
            GraderResult(
                check=f"{spec.kind}:{spec.path}",
                passed=False,
                expected="artifact confined to workspace",
                actual=str(exc),
                reason="Agent artifact escapes workspace",
                constraint=True,
            )
        ]


# 合并任务产物检查、必需事件与不可变文件检查
def grade_case(case: EvalCase, ctx: GradeContext) -> list[GraderResult]:
    specs = list(case.graders)
    specs += [GraderSpec(kind="events", event=e) for e in case.required_events]
    protected = []
    actual = snapshot(ctx.workspace)
    for path in case.forbidden_changes:
        expected, value = ctx.initial.get(path), actual.get(path)
        protected.append(
            GraderResult(
                check=f"forbidden:{path}",
                passed=expected == value,
                expected=expected,
                actual=value,
                constraint=True,
                reason=None if expected == value else "protected file changed",
            )
        )
    return [r for spec in specs for r in grade(spec, ctx)] + protected
