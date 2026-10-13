import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from evals.graders import grade_case
from evals.graders.base import GradeContext, confined, snapshot
from evals.isolation import verify_isolation
from evals.report import write_report
from evals.schema import EvalCase, GraderResult, TrialResult

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
FIXTURES = ROOT / "fixtures"


def validate_fixture_source(source: Path) -> None:
    paths = [source, *source.rglob("*")] if source.is_dir() else [source]
    if any(path.is_symlink() for path in paths):
        raise ValueError("fixture symlinks are forbidden")
    polluted = next(
        (
            path
            for path in paths
            if path.name in {"__pycache__", ".pytest_cache"} or path.suffix == ".pyc"
        ),
        None,
    )
    if polluted:
        raise ValueError(f"fixture contains generated artifact: {polluted.relative_to(FIXTURES)}")


# 加载固定 JSON 任务集并应用确定性筛选
def load_cases(suite="pilot", case_id=None, category=None):
    aliases = {"pilot": "e2e", "long-dev": "long_dev", "long-formal": "long_formal"}
    folders = ["e2e", "faults", "compact"] if suite == "all" else [aliases.get(suite, suite)]
    cases = [
        EvalCase.model_validate_json(p.read_text())
        for folder in folders
        for p in sorted((ROOT / "cases" / folder).glob("*.json"))
        if p.name != "FROZEN.json"
    ]
    if suite == "pilot":
        cases = [c for c in cases if "pilot" in c.tags]
    cases = [
        c
        for c in cases
        if (not case_id or c.case_id == case_id) and (not category or c.category == category)
    ]
    if not cases:
        raise ValueError("no matching evaluation cases")
    if len({c.case_id for c in cases}) != len(cases):
        raise ValueError("duplicate case_id")
    return cases


# 复制 fixture 到全新目录，拒绝符号链接与冲突文件
def prepare_workspace(case, workspace):
    workspace.mkdir(parents=True, exist_ok=False)
    for fixture in case.fixtures:
        source = confined(FIXTURES, fixture.source)
        if not source.exists():
            raise FileNotFoundError(f"missing fixture: {fixture.source}")
        validate_fixture_source(source)
        target = confined(workspace, fixture.destination)
        if source.is_dir():
            for p in source.rglob("*"):
                if p.is_file():
                    dst = target / p.relative_to(source)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        raise ValueError(f"fixture collision: {dst}")
                    shutil.copyfile(p, dst)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError(f"fixture collision: {target}")
            shutil.copyfile(source, target)
    return snapshot(workspace)


# 静态检查 fixture、验收配置和检查点，不启动 Provider 或执行 pytest 命令
def validate_case(case):
    from evals.graders.csv_grader import expected_value
    from evals.graders.pytest_grader import ALLOWED

    for fixture in case.fixtures:
        source = confined(FIXTURES, fixture.source)
        if not source.exists():
            raise ValueError(f"missing fixture {source}")
        validate_fixture_source(source)
    pending = list(case.graders)
    while pending:
        spec = pending.pop()
        pending.extend(spec.children)
        for source in [spec.source, *spec.sources]:
            if source and not confined(FIXTURES, source).is_file():
                raise ValueError(f"missing grader source {source}")
        if spec.kind == "csv":
            expected_value(spec, GradeContext(ROOT, FIXTURES))
        if spec.kind == "json" and spec.source:
            json.loads(confined(FIXTURES, spec.source).read_text())
        if spec.kind == "pytest" and not ALLOWED[spec.test_id].is_file():
            raise ValueError("missing allowlisted pytest test")
    if case.compact_spec:
        history = json.loads(confined(FIXTURES, case.compact_spec.checkpoint).read_text())
        if not history or any(m.get("role") not in {"user", "assistant"} for m in history):
            raise ValueError("invalid checkpoint messages")


# 记录版本、dirty 状态和评测输入哈希，使本地未提交实现也可辨识
def provenance():
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO))
    files = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in ROOT.rglob("*")
        if p.is_file() and p.suffix in {".py", ".json", ".csv", ".txt"}
    }
    return {"git_commit": commit, "git_dirty": dirty, "eval_file_hashes": files}


def validate_formal_git(options, git):
    if not options.expected_git_commit:
        raise ValueError("formal run requires --expected-git-commit")
    if git["git_dirty"]:
        raise ValueError("formal run requires a clean git worktree")
    if git["git_commit"] != options.expected_git_commit:
        raise ValueError(
            f"formal run commit mismatch: expected {options.expected_git_commit}, "
            f"got {git['git_commit']}"
        )


# 只向工作子进程传入必要环境，fake 模式不会获得外部凭证或用户配置
def worker_env(live):
    env = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "PYTHONPATH": str(REPO) + os.pathsep + str(REPO / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    if live:
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            if name in os.environ:
                env[name] = os.environ[name]
    return env


# 执行单次隔离任务，Agent 失败和评测器故障分别归类
async def execute_trial(
    case,
    trial_id,
    output,
    options,
    git,
    branch=None,
    budget=None,
    repeat_id=0,
    cohort="baseline",
    call_limit=None,
    input_token_limit=None,
):
    base = output / "artifacts" / case.case_id / trial_id
    workspace = base / "workspace"
    events = output / "events" / f"{case.case_id}-{trial_id}.jsonl"
    trace = output / "traces" / f"{case.case_id}-{trial_id}.jsonl"
    result = TrialResult(
        case_id=case.case_id,
        trial_id=trial_id,
        repeat_id=repeat_id,
        cohort=cohort,
        category=case.category,
        started_at=datetime.now(UTC),
        git_commit=git["git_commit"],
        git_dirty=git["git_dirty"],
        provider=options.provider,
        model=options.model,
        framework_self_test=not options.live,
        runner_type=case.runner_type,
        branch=branch,
        events_path=str(events),
        trace_path=str(trace),
        artifacts_path=str(workspace),
        configuration={
            "max_steps": case.max_steps,
            "timeout_seconds": case.timeout_seconds,
            "auto_compaction_threshold": 0.0,
        },
    )
    if case.fault_spec:
        result.fault_type = case.fault_spec.kind
        result.fault_mode = case.fault_spec.mode
        result.planned_injections = 1
        result.fault_status = "not_triggered"
    start = time.monotonic()
    proc = None
    timed_out = False
    initial = None
    try:
        initial = prepare_workspace(case, workspace)
        if branch == "C" and budget is None:
            result.status = "failed"
            result.failure_type = "dependency_failed"
            result.failure_reason = "B did not produce a first-request budget; C was not run"
            result.termination_reason = "dependency_failed"
            result.comparison_eligible = False
            return result
        if case.permission == "deny_outside":
            (base / "boundary.txt").write_text("external boundary sentinel\n")
        request = {
            "case": case.model_dump(),
            "provider": options.provider,
            "model": options.model,
            "live": options.live,
            "state_root": str(base / "state"),
            "fixture_root": str(FIXTURES),
            "events_path": str(events),
            "trace_path": str(trace),
            "worker_result": str(base / "worker-result.json"),
            "branch": branch,
            "compact_budget": budget,
            "repeat_id": repeat_id,
            "call_limit": call_limit,
            "input_token_limit": input_token_limit,
        }
        request_path = base / "evaluator-private-request.json"
        request_path.write_text(json.dumps(request))
        with (base / "worker.log").open("w") as log:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "evals.runners.worker",
                str(request_path),
                cwd=workspace,
                env=worker_env(options.live),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(proc.wait(), case.timeout_seconds)
            except TimeoutError:
                timed_out = True
                await stop_process(proc)
        data_path = Path(request["worker_result"])
        data = json.loads(data_path.read_text()) if data_path.exists() else {}
        rows = (
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        for field in (
            "session_id",
            "run_id",
            "run_ids",
            "usage",
            "tokens_before",
            "tokens_after",
            "token_count_method",
            "input_hash",
            "budget_tokens",
            "budget_matched",
            "comparison_eligible",
            "preparation_seconds",
        ):
            if field in data:
                setattr(result, field, data[field])
        fault_path = base / "state" / "fault.json"
        fault = data.get("fault") or (
            json.loads(fault_path.read_text()) if fault_path.exists() else {}
        )
        result.injection_count = fault.get("injections", 0)
        result.recovery_attempts = fault.get("recovery_attempts", 0)
        result.fault_status = fault.get("status", result.fault_status)
        if not timed_out and (proc.returncode or data.get("worker_error") or not data):
            raise RuntimeError(data.get("worker_error") or "worker/daemon failed; see worker.log")
        result.continuation_seconds = sum(
            call.get("duration_seconds", 0)
            for call in result.usage.get("calls", [])
            if call.get("phase") == "continuation"
        )
        grader_started = time.monotonic()
        result.grader_results = grade_case(case, GradeContext(workspace, FIXTURES, rows, initial))
        result.grader_seconds = time.monotonic() - grader_started
        finished = [e for e in rows if e["type"] == "run.finished"]
        steps = sum(e["type"] == "step.started" for e in rows)
        result.grader_results.append(
            GraderResult(
                check="budget",
                passed=not timed_out and bool(finished) and steps <= case.max_steps,
                expected={
                    "timeout_seconds": case.timeout_seconds,
                    "max_steps_per_trial": case.max_steps,
                },
                actual={"timed_out": timed_out, "total_steps": steps},
            )
        )
        if case.permission == "deny_outside":
            boundary = base / "boundary.txt"
            actual = boundary.read_text() if boundary.is_file() else None
            result.grader_results.append(
                GraderResult(
                    check="boundary:outside_workspace",
                    passed=actual == "external boundary sentinel\n",
                    expected="external boundary sentinel\n",
                    actual=actual,
                    constraint=True,
                )
            )
        if case.reconnect:
            ipc = data.get("ipc", {})
            ok = (
                ipc.get("reconnected")
                and ipc.get("replayed_count", 0) > 0
                and ipc.get("submitted_runs") == 1
                and len(finished) == 1
                and ipc.get("replay_run_id") == result.run_id
                and ipc.get("disconnected_before_finish")
            )
            result.grader_results.append(
                GraderResult(
                    check="ipc:reconnect_same_run",
                    passed=bool(ok),
                    expected="one run with replay",
                    actual=ipc,
                )
            )
        result.passed = all(g.passed for g in result.grader_results)
        result.status = "passed" if result.passed else "failed"
        if data.get("agent_error"):
            result.passed = False
            result.status = "failed"
        if not result.passed:
            result.failure_type = "timeout" if timed_out else "acceptance_failed"
            reasons = [e.get("reason") for e in finished if e.get("reason")]
            if reasons and not timed_out:
                result.failure_type = reasons[-1]
            if data.get("agent_error"):
                result.failure_type = data.get("error_type", "compaction_failed")
            result.termination_reason = result.failure_type
            result.failure_reason = "; ".join(
                g.check for g in result.grader_results if not g.passed
            )
        if case.permission == "deny_outside" or (
            case.fault_spec and case.fault_spec.mode == "safety"
        ):
            result.safely_terminated = bool(finished) and not timed_out
            result.boundary_preserved = all(g.passed for g in result.grader_results if g.constraint)
    except asyncio.CancelledError:
        result.failure_type, result.failure_reason = "interrupted", "evaluation interrupted"
        result.evaluator_error = True
        raise
    except Exception as exc:
        result.status = "invalid"
        result.passed = False
        result.evaluator_error = True
        result.infrastructure_failure = True
        result.failure_type = "evaluator_error"
        result.failure_reason = f"{type(exc).__name__}: {exc}"
    finally:
        if proc:
            await stop_process(proc)
        result.ended_at = datetime.now(UTC)
        result.duration_seconds = time.monotonic() - start
        with (output / "trials.jsonl").open("a") as stream:
            stream.write(result.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return result


# 终止该试次的整个进程组，防止超时 bash 或 daemon 遗留副作用
async def stop_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 3)
    except TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.wait()


# 先跑无故障基线，再跑故障版本；压缩分支依次 A/B/C 且相互隔离
async def run_suite(cases, options):
    output = options.output_dir.resolve() / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    for folder in ("artifacts", "events", "traces"):
        (output / folder).mkdir(parents=True)
    isolation = await verify_isolation(output / "isolation")
    git = provenance()
    if getattr(options, "formal", False):
        validate_formal_git(options, git)
    statistics_seed = getattr(options, "statistics_seed", 20260923)
    plan = []
    jobs = []
    for cohort in ("baseline", "fault"):
        for case in cases:
            if cohort == "fault" and not case.fault_spec:
                continue
            selected = case.model_copy(deep=True)
            if cohort == "baseline":
                selected.fault_spec = None
            for trial in range(1, options.trials + 1):
                branches = [None]
                if case.compact_spec:
                    branches = list(case.compact_spec.branches)
                    if trial % 2 == 0:
                        branches = ["B", "C", "A"]
                for branch in branches:
                    trial_id = f"{cohort}-{trial}" + (f"-{branch}" if branch else "")
                    plan.append(
                        {
                            "case_id": case.case_id,
                            "trial_id": trial_id,
                            "cohort": cohort,
                            "branch": branch,
                            "repeat_id": trial,
                        }
                    )
                    jobs.append((selected, trial_id, branch, trial, cohort))
    manifest = {
        **git,
        "provider": options.provider,
        "model": options.model,
        "live": options.live,
        "formal": getattr(options, "formal", False),
        "freeze_sha256": getattr(options, "freeze_sha256", None),
        "expected_git_commit": getattr(options, "expected_git_commit", None),
        "planned_trials": len(plan),
        "plan": plan,
        "isolation": isolation,
        "configuration": {
            "trials": options.trials,
            "suite": options.suite,
            "quality_margin_pp": getattr(options, "quality_margin_pp", 5.0),
            "max_live_trials": getattr(options, "max_live_trials", None),
            "max_live_calls": getattr(options, "max_live_calls", None),
            "stop_input_tokens": getattr(options, "stop_input_tokens", None),
        },
        "statistics": {
            "seed": statistics_seed,
            "cluster": "case_id",
            "interval": "percentile bootstrap 95%",
        },
        "cases": [c.model_dump(exclude={"script", "child_script"}) for c in cases],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    budgets = {}
    calls_used = 0
    input_tokens_used = 0
    stop_reason = None
    try:
        for case, trial_id, branch, repeat_id, cohort in jobs:
            call_cap = getattr(options, "max_live_calls", None)
            token_cap = getattr(options, "stop_input_tokens", None)
            if options.live and (
                (call_cap is not None and calls_used >= call_cap)
                or (token_cap is not None and input_tokens_used >= token_cap)
            ):
                stop_reason = "live_budget_reached"
                break
            key = (case.case_id, trial_id.rsplit("-", 1)[0])
            row = await execute_trial(
                case,
                trial_id,
                output,
                options,
                git,
                branch,
                budgets.get(key),
                repeat_id,
                cohort,
                None if call_cap is None else call_cap - calls_used,
                None if token_cap is None else token_cap - input_tokens_used,
            )
            calls_used += len(row.usage.get("calls", []))
            observed_input = row.usage.get("total_input_tokens")
            if observed_input is not None:
                input_tokens_used += observed_input
            elif options.live:
                stop_reason = "usage_observability_lost"
            if branch == "B":
                budgets[key] = row.tokens_after
            print(f"{case.case_id}/{trial_id}: {row.status}", flush=True)
            if stop_reason:
                break
    finally:
        manifest["execution"] = {
            "calls_used": calls_used,
            "observed_input_tokens": input_tokens_used,
            "stop_reason": stop_reason,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        write_report(output)
        print(f"Report: {output / 'summary.md'}", flush=True)
    return output


# 提供默认离线的命令行入口，配置检查不会访问模型或外部服务
def main(argv=None):
    parser = argparse.ArgumentParser(description="Reproducible WorkMate Agent evaluations")
    parser.add_argument(
        "--suite",
        choices=["pilot", "e2e", "faults", "compact", "long-dev", "long-formal", "all"],
        default="pilot",
    )
    parser.add_argument("--case")
    parser.add_argument("--category")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    parser.add_argument("--statistics-seed", type=int, default=20260923)
    parser.add_argument("--model")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/workmate-evals"))
    parser.add_argument(
        "--fault",
        choices=[
            "runtime_error",
            "rate_limited",
            "timeout",
            "schema_error",
            "provider_stream",
            "subagent",
            "mcp_disconnect",
        ],
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-quality-margin-pp", type=float)
    parser.add_argument("--max-live-trials", type=int)
    parser.add_argument("--max-live-calls", type=int)
    parser.add_argument("--stop-input-tokens", type=int)
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--expected-git-commit")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        if options.trials <= 0:
            raise ValueError("--trials must be positive")
        cases = load_cases(options.suite, options.case, options.category)
        if options.fault:
            cases = [c for c in cases if c.fault_spec and c.fault_spec.kind == options.fault]
            if not cases:
                raise ValueError("--fault filters the fault suite; use --suite faults")
        for case in cases:
            validate_case(case)
        if options.suite in {"long-dev", "long-formal"}:
            from evals.build_long_context import verify_freeze

            verify_freeze(options.suite.replace("-", "_"))
        options.freeze_sha256 = None
        if options.suite == "e2e":
            from evals.e2e_freeze import verify_freeze

            options.freeze_sha256 = verify_freeze(load_cases("e2e"))["sha256"]
        if options.formal:
            from evals.e2e_freeze import verify_formal_configuration

            if options.case or options.category:
                raise ValueError("formal e2e run cannot filter cases")
            verify_formal_configuration(options)
            validate_formal_git(options, provenance())
        if options.dry_run:
            print(f"Validated {len(cases)} cases; no providers, graders, or agents executed.")
            return 0
        if options.provider != "fake" and not options.live:
            raise ValueError("real provider requires explicit --live")
        if options.live and options.provider == "fake":
            raise ValueError("--live requires --provider anthropic")
        if options.live and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY missing; no requests made and no fake fallback")
        if options.live:
            planned = options.trials * sum(
                (2 if case.fault_spec else 1)
                * (len(case.compact_spec.branches) if case.compact_spec else 1)
                for case in cases
            )
            if options.max_live_trials is None or options.max_live_trials < planned:
                raise ValueError(f"--max-live-trials must cover the frozen plan ({planned})")
            if not options.max_live_calls or not options.stop_input_tokens:
                raise ValueError("live runs require --max-live-calls and --stop-input-tokens")
            if options.suite == "long-formal" and options.confirm_quality_margin_pp is None:
                raise ValueError("formal live run requires --confirm-quality-margin-pp")
        options.quality_margin_pp = (
            options.confirm_quality_margin_pp
            if options.confirm_quality_margin_pp is not None
            else 5.0
        )
        options.model = options.model or (
            "scripted" if options.provider == "fake" else "claude-sonnet-4-6"
        )
        output = asyncio.run(run_suite(cases, options))
        summary = json.loads((output / "summary.json").read_text())
        return 1 if summary["failed_trials"] else 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("Interrupted; completed trials and report have been saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
