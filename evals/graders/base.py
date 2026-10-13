from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.schema import GraderResult, GraderSpec, relative_path


# 将路径解析到指定根目录并拒绝符号链接逃逸
def confined(root: Path, name: str) -> Path:
    relative_path(name)
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes evaluation root: {name}")
    return path


# 计算文件字节哈希以检查不可变输入
def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 记录工作目录中的文件和符号链接状态
def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): ("symlink:" + str(p.readlink()) if p.is_symlink() else digest(p))
        for p in root.rglob("*")
        if p.is_file() or p.is_symlink()
    }


@dataclass
class GradeContext:
    workspace: Path
    fixtures: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    initial: dict[str, str] = field(default_factory=dict)


# 按 JSON 类型比较，避免 Python 将 true 与数字 1 判为相等
def json_equal(expected, actual):
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and expected.keys() == actual.keys()
            and all(json_equal(value, actual[key]) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(json_equal(a, b) for a, b in zip(expected, actual, strict=True))
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual


# 拒绝非标准 JSON 数值与重复字段，防止含糊产物绕过验收
def load_json(text):
    # 非标准 NaN 与 Infinity 不是有效验收产物
    def reject_constant(value):
        raise ValueError(f"non-standard JSON constant: {value}")

    # 重复键没有可复现的跨解析器含义
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


# 生成包含期望、实际结果和可读原因的验收记录
def result(spec: GraderSpec, expected: Any, actual: Any, passed: bool | None = None):
    ok = json_equal(expected, actual) if passed is None else passed
    return GraderResult(
        check=f"{spec.kind}:{spec.path or spec.event or spec.test_id or ''}",
        passed=ok,
        expected=expected,
        actual=actual,
        reason=None if ok else "final state does not satisfy the check",
        constraint=spec.constraint,
    )
