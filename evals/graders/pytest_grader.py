import os
import subprocess
import sys
from pathlib import Path

from evals.graders.base import GradeContext, result
from evals.schema import GraderSpec

TESTS = Path(__file__).with_name("checks")
ALLOWED = {
    name: TESTS / f"check_{name}.py"
    for name in ("sort", "date", "filter", "dedupe", "pipeline", "long_context_code")
}


# 只执行代码中列出的评测器私有测试，禁用模型控制的 pytest 配置及插件
def grade(spec: GraderSpec, ctx: GradeContext):
    test = ALLOWED[spec.test_id]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "EVAL_WORKSPACE": str(ctx.workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-c",
        "/dev/null",
        "--confcutdir",
        str(TESTS),
        "-p",
        "no:cacheprovider",
        str(test),
    ]
    try:
        proc = subprocess.run(
            command, cwd=TESTS, env=env, capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return result(spec, "pytest exit 0", "pytest timeout", False)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"grader pytest failed to execute: {proc.stdout} {proc.stderr}")
    return result(
        spec,
        "pytest exit 0",
        {"exit_code": proc.returncode, "output": (proc.stdout + proc.stderr)[-6000:]},
        proc.returncode == 0,
    )
