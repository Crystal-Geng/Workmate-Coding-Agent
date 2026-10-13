import fnmatch

from evals.graders.base import GradeContext, result
from evals.schema import GraderSpec


# 检查真实持久化事件类型与指定字段，不接受模型文字作为事件
def grade(spec: GraderSpec, ctx: GradeContext):
    found = [
        e
        for e in ctx.events
        if fnmatch.fnmatch(e.get("type", ""), spec.event)
        and all(e.get(k) == v for k, v in spec.event_fields.items())
    ]
    return result(spec, f">= {spec.minimum}", len(found), len(found) >= spec.minimum)
