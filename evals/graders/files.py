from evals.graders.base import GradeContext, confined, digest, result
from evals.schema import GraderSpec


# 验收文件存在性、精确内容与原始字节哈希
def grade(spec: GraderSpec, ctx: GradeContext):
    path = confined(ctx.workspace, spec.path)
    if spec.kind in {"exists", "absent"}:
        return result(spec, spec.kind == "exists", path.exists())
    if spec.kind == "hash":
        expected = digest(confined(ctx.fixtures, spec.source))
        actual = digest(path) if path.is_file() else None
    else:
        expected = confined(ctx.fixtures, spec.source).read_text() if spec.source else spec.expected
        actual = path.read_text() if path.is_file() else None
    return result(spec, expected, actual)
