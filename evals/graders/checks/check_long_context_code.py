import importlib.util
import json
import os
from pathlib import Path


def test_multi_file_contract():
    root = Path(os.environ["EVAL_WORKSPACE"])
    scenario = (root / "scenario.txt").read_text().strip()
    spec = importlib.util.spec_from_file_location("solution", root / "module.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = json.loads((root / "config.json").read_text())
    if scenario == "tax":
        assert module.solve([10, 20]) == 33.0
        assert config == {"tax_rate": 0.1, "round_digits": 2}
    elif scenario == "parser":
        assert module.solve(" A,B , c ") == ["a", "b", "c"]
        assert config == {"delimiter": ",", "lowercase": True}
    elif scenario == "auth":
        assert module.solve("auditor", "export") is True
        assert module.solve("auditor", "delete") is False
        assert config == {"auditor": ["read", "export"]}
    elif scenario == "cache":
        assert module.solve("GET", "/x", {"b": 2, "a": 1}) == "GET:/x:a=1&b=2"
        assert config == {"sort_query": True, "separator": ":"}
    elif scenario == "allocation":
        assert module.solve(5, [2, 4]) == [2, 3]
        assert config == {"allow_partial": True, "preserve_order": True}
    elif scenario == "version":
        assert module.solve("v2.4.1") == (2, 4, 1)
        assert config == {"prefix": "v", "parts": 3}
    elif scenario == "absolute":
        assert module.solve(-7) == 7
        assert config == {"mode": "absolute"}
    else:
        raise AssertionError(f"unknown scenario: {scenario}")
