import json

from evals.isolation import verify_isolation


async def test_os_isolation_attack_surface(tmp_path):
    evidence = await verify_isolation(tmp_path / "isolation")
    assert evidence["passed"]
    assert all(evidence["checks"].values())
    saved = json.loads((tmp_path / "isolation" / "isolation.json").read_text())
    assert saved == evidence
    assert (tmp_path / "isolation" / "workspace" / "output" / "result.txt").is_file()
    assert (
        tmp_path / "isolation" / "evaluator-private" / "sentinel.txt"
    ).read_text() == "PRIVATE-SENTINEL\n"
