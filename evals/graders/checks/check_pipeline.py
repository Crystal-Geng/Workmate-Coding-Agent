import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


# 功能：产出的计算脚本必须能在干净输入目录中重现已经验收的汇总结果
# 设计：不给脚本预先生成的答案文件，独立执行后比较结果与 stdout，防止伪造执行记录
def test_pipeline(tmp_path):
    workspace = Path(os.environ["EVAL_WORKSPACE"])
    for name in ("north.csv", "south.csv", "jan.json", "feb.json", "process.py"):
        shutil.copyfile(workspace / name, tmp_path / name)
    result = subprocess.run(
        [sys.executable, "process.py"], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "completed\n"
    assert json.loads((tmp_path / "summary.json").read_text()) == json.loads(
        (workspace / "summary.json").read_text()
    )
