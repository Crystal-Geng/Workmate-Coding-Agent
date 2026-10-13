import hashlib
import json
from pathlib import Path

from evals.graders.base import confined
from evals.graders.pytest_grader import ALLOWED

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FREEZE_PATH = ROOT / "cases" / "e2e" / "FROZEN.json"
VERSION = "e2e-v2"
FORMAL_CONFIGURATION = {
    "suite": "e2e",
    "provider": "anthropic",
    "model": "deepseek-flash",
    "live": True,
    "trials": 3,
    "statistics_seed": 20260923,
    "max_live_trials": 72,
    "max_live_calls": 1800,
    "stop_input_tokens": 3000000,
}


def freeze_paths(cases):
    paths = set()
    for case in cases:
        paths.add(ROOT / "cases" / "e2e" / f"{case.case_id}.json")
        for fixture in case.fixtures:
            source = confined(FIXTURES, fixture.source)
            paths.update(
                path
                for path in ([source] if source.is_file() else source.rglob("*"))
                if path.is_file()
            )
        if case.compact_spec:
            paths.add(confined(FIXTURES, case.compact_spec.checkpoint))
        pending = list(case.graders)
        while pending:
            spec = pending.pop()
            pending.extend(spec.children)
            for source in [spec.source, *spec.sources]:
                if source:
                    paths.add(confined(FIXTURES, source))
            if spec.kind == "pytest":
                paths.add(ALLOWED[spec.test_id])
    return sorted(paths)


def build_freeze(cases):
    files = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in freeze_paths(cases)
    }
    content = {
        "dataset_version": VERSION,
        "case_count": len(cases),
        "configuration": FORMAL_CONFIGURATION,
        "files": files,
    }
    content["sha256"] = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return content


def write_freeze(cases):
    frozen = build_freeze(cases)
    FREEZE_PATH.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
    return frozen


def verify_freeze(cases):
    if not FREEZE_PATH.is_file():
        raise ValueError("e2e dataset is not frozen")
    frozen = json.loads(FREEZE_PATH.read_text())
    expected = build_freeze(cases)
    if frozen != expected:
        raise ValueError("e2e dataset freeze mismatch; create a new dataset version")
    return frozen


def verify_formal_configuration(options):
    actual = {
        "suite": options.suite,
        "provider": options.provider,
        "model": options.model,
        "live": options.live,
        "trials": options.trials,
        "statistics_seed": options.statistics_seed,
        "max_live_trials": options.max_live_trials,
        "max_live_calls": options.max_live_calls,
        "stop_input_tokens": options.stop_input_tokens,
    }
    if actual != FORMAL_CONFIGURATION:
        raise ValueError(
            "formal e2e configuration mismatch: "
            + json.dumps({"expected": FORMAL_CONFIGURATION, "actual": actual}, sort_keys=True)
        )


def main():
    from evals.run_eval import load_cases, validate_case

    cases = load_cases("e2e")
    for case in cases:
        validate_case(case)
    frozen = write_freeze(cases)
    print(f"Frozen {len(cases)} cases: {frozen['sha256']}")


if __name__ == "__main__":
    main()
