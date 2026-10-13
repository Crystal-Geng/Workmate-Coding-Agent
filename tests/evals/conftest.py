from pathlib import Path
from types import SimpleNamespace

import pytest
from evals.run_eval import load_cases


@pytest.fixture
# 提供含真实 fixture 的固定 pilot 案例
def pilot():
    return load_cases("pilot", "e2e-01")[0]


@pytest.fixture
# 配置默认离线的最小单试次选项
def options(tmp_path: Path):
    return SimpleNamespace(
        provider="fake", model="scripted", live=False, trials=1, suite="pilot", output_dir=tmp_path
    )
