import importlib.util
import os
from pathlib import Path


# 功能：验收真实工作目录中的代码修复
# 设计：测试存于评测器目录，防止 Agent 改写验收规则
def test_solution():
    spec = importlib.util.spec_from_file_location(
        "solution", Path(os.environ["EVAL_WORKSPACE"]) / "solution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.solve([10, 2, -3, 2]) == [-3, 2, 2, 10]
    assert module.solve([]) == []
    data = [5, 1]
    module.solve(data)
    assert data == [5, 1]
