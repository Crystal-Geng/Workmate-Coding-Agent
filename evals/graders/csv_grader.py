import csv
from collections import defaultdict
from decimal import Decimal

from evals.graders.base import GradeContext, confined, load_json, result
from evals.schema import GraderSpec


# 只根据评测器保存的原始 fixture 独立计算 CSV 或 JSON 汇总答案
def expected_value(spec: GraderSpec, ctx: GradeContext):
    if spec.operation == "json_sum":
        values = [load_json(confined(ctx.fixtures, p).read_text()) for p in spec.sources]
        return {"total": sum(v[spec.column] for v in values)}
    rows = []
    for source in spec.sources:
        with confined(ctx.fixtures, source).open(newline="") as stream:
            rows.extend(csv.DictReader(stream))
    if spec.operation == "count":
        return {"count": len(rows)}
    if spec.operation == "sum":
        return {"total": float(sum(Decimal(row[spec.column]) for row in rows))}
    if spec.operation == "group_sum":
        groups = defaultdict(Decimal)
        for row in rows:
            groups[row[spec.group_by]] += Decimal(row[spec.column])
        return {k: float(v) for k, v in sorted(groups.items())}
    return {"ids": sorted({row[spec.column] for row in rows})}


# 比较最终 JSON 汇总文件与独立计算答案
def grade(spec: GraderSpec, ctx: GradeContext):
    expected = expected_value(spec, ctx)
    try:
        actual = load_json(confined(ctx.workspace, spec.path).read_text())
    except (OSError, ValueError, UnicodeError) as exc:
        return result(spec, expected, str(exc), False)
    return result(spec, expected, actual)
