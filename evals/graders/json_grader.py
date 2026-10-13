import copy

from evals.graders.base import GradeContext, confined, load_json, result
from evals.schema import GraderSpec


# 从原始 JSON 构造期望值并检查无关字段完整保留
def grade(spec: GraderSpec, ctx: GradeContext):
    expected = spec.expected
    if spec.preserve_other_fields:
        expected = copy.deepcopy(load_json(confined(ctx.fixtures, spec.source).read_text()))
        expected.update(spec.fields)
    elif spec.fields:
        expected = spec.fields
    path = confined(ctx.workspace, spec.path)
    try:
        actual = load_json(path.read_text())
    except (OSError, ValueError, UnicodeError) as exc:
        return result(spec, expected, str(exc), False)
    if spec.fields and not spec.preserve_other_fields and isinstance(actual, dict):
        actual = {key: actual[key] for key in spec.fields if key in actual}
    return result(spec, expected, actual)
