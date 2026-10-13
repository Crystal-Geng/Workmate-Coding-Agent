import json
import subprocess
import sys

import pytest
from evals.e2e_freeze import FORMAL_CONFIGURATION, verify_formal_configuration, verify_freeze
from evals.run_eval import (
    load_cases,
    main,
    prepare_workspace,
    run_suite,
    validate_case,
    validate_formal_git,
    worker_env,
)
from evals.sandbox import clean_environment


# 功能：fixture 与不同 trial 之间没有文件状态泄漏
# 设计：修改第一次复制的文件后再复制第二份，检查原始字节和额外产物
def test_fixture_isolation(tmp_path, pilot):
    first, second = tmp_path / "first", tmp_path / "second"
    prepare_workspace(pilot, first)
    (first / "source.txt").write_text("changed")
    (first / "answer.txt").write_text("leak")
    prepare_workspace(pilot, second)
    assert (second / "source.txt").read_text() == "Hello WorkMate\n"
    assert not (second / "answer.txt").exists()


def test_generated_fixture_artifacts_are_rejected(tmp_path, pilot, monkeypatch):
    fixture = tmp_path / "polluted"
    (fixture / "__pycache__").mkdir(parents=True)
    (fixture / "source.txt").write_text("clean source\n")
    (fixture / "__pycache__" / "source.pyc").write_bytes(b"generated")
    monkeypatch.setattr("evals.run_eval.FIXTURES", tmp_path)
    case = pilot.model_copy(deep=True)
    case.fixtures[0].source = "polluted"

    with pytest.raises(ValueError, match="generated artifact"):
        validate_case(case)
    with pytest.raises(ValueError, match="generated artifact"):
        prepare_workspace(case, tmp_path / "workspace")


def test_sandbox_python_does_not_write_bytecode(tmp_path):
    (tmp_path / "fixture_module.py").write_text("VALUE = 1\n")
    subprocess.run(
        [sys.executable, "-c", "import fixture_module"],
        cwd=tmp_path,
        env=clean_environment(tmp_path),
        check=True,
    )
    assert not (tmp_path / "__pycache__").exists()


def test_e2e_v2_freeze_matches_all_cases():
    cases = load_cases("e2e")
    frozen = verify_freeze(cases)
    assert frozen["case_count"] == 24
    assert frozen["configuration"] == FORMAL_CONFIGURATION
    assert not any(
        "__pycache__" in path or path.endswith(".pyc") or ".pytest_cache" in path
        for path in frozen["files"]
    )


def test_formal_configuration_and_git_are_exact(options):
    for name, value in FORMAL_CONFIGURATION.items():
        setattr(options, name, value)
    verify_formal_configuration(options)
    options.expected_git_commit = "abc"
    validate_formal_git(options, {"git_commit": "abc", "git_dirty": False})
    with pytest.raises(ValueError, match="clean git worktree"):
        validate_formal_git(options, {"git_commit": "abc", "git_dirty": True})


def test_filtered_e2e_dry_run_uses_full_freeze():
    assert main(["--suite", "e2e", "--case", "e2e-01", "--dry-run"]) == 0


# 功能：end_turn 但没有正确产物时判定 failed
# 设计：空脚本返回正常结束，真实生产 AgentRunner success 不能绕过 grader
async def test_end_turn_is_not_acceptance(pilot, options):
    pilot.script = []
    output = await run_suite([pilot], options)
    row = json.loads((output / "trials.jsonl").read_text())
    assert row["status"] == "failed" and not row["evaluator_error"]
    events = [json.loads(s) for s in open(row["events_path"])]
    assert any(e["type"] == "run.finished" and e["status"] == "success" for e in events)


# 功能：不同 trial 使用独立 Session 和工作目录
# 设计：连续执行相同脚本，两次产物正确且 session_id 不能相同
async def test_trials_isolate_sessions(pilot, options):
    options.trials = 2
    output = await run_suite([pilot], options)
    rows = [json.loads(s) for s in (output / "trials.jsonl").read_text().splitlines()]
    assert all(r["passed"] for r in rows)
    assert len({r["session_id"] for r in rows}) == 2
    assert len({r["artifacts_path"] for r in rows}) == 2


# 功能：grader 自身故障标为 invalid，之前已完成结果不丢失
# 设计：第二次验收主动抛出异常，检查落盘结果和成功率分母
async def test_grader_error_persists_earlier_trial(pilot, options, monkeypatch):
    from evals.graders import grade_case

    calls = 0

    # 第二次调用模拟评测器自身故障
    def flaky(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("broken grader")
        return grade_case(*args)

    monkeypatch.setattr("evals.run_eval.grade_case", flaky)
    options.trials = 2
    output = await run_suite([pilot], options)
    rows = [json.loads(s) for s in (output / "trials.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["passed", "invalid"]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["task_success_rate"]["denominator"] == 1
    assert summary["invalid_trials"] == 1


# 功能：未指定 live 时真实 Provider 不会被调用且子进程不继承凭证
# 设计：直接验证 CLI 门禁与环境 allowlist，宿主是否配置密钥不影响结果
def test_live_gate_and_clean_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    with pytest.raises(SystemExit) as exc:
        main(["--provider", "anthropic", "--case", "e2e-01"])
    assert exc.value.code == 2
    assert "ANTHROPIC_API_KEY" not in worker_env(False)
    assert main(["--provider", "anthropic", "--dry-run"]) == 0


@pytest.mark.parametrize("case_id", ["e2e-19", "e2e-20", "e2e-23"])
# 功能：审批和重连通过真实 daemon 与 TCP IPC 完成
# 设计：启动隔离子进程和脚本 Provider，外部 grader 同时核验事件与最终文件
async def test_real_daemon_ipc(case_id, options):
    output = await run_suite(load_cases("e2e", case_id), options)
    row = json.loads((output / "trials.jsonl").read_text())
    assert row["status"] == "passed", row["failure_reason"]
    assert row["runner_type"] == "ipc" and row["session_id"]


# 功能：真实任务超时会保留结果且标为 failed，而非评测器 invalid
# 设计：执行本地休眠命令触发进程预算，验收超时分类与已写事件
async def test_timeout_records_failed_trial(pilot, options):
    pilot.timeout_seconds = 1.5
    pilot.script = [[{"name": "bash", "input": {"command": "sleep 10"}}]]
    output = await run_suite([pilot], options)
    row = json.loads((output / "trials.jsonl").read_text())
    assert row["status"] == "failed" and row["failure_type"] == "timeout"
    assert not row["evaluator_error"]


# 功能：取消评测后已完成结果和当前中断记录仍可生成报告
# 设计：先完成一个正常试次，再取消本地休眠任务，模拟 Ctrl+C 的 asyncio 取消路径
async def test_cancellation_preserves_results(pilot, options):
    import asyncio

    slow = pilot.model_copy(deep=True)
    slow.case_id = "slow-case"
    slow.script = [[{"name": "bash", "input": {"command": "sleep 10"}}]]
    task = asyncio.create_task(run_suite([pilot, slow], options))
    output = None
    try:
        async with asyncio.timeout(10):
            while True:
                paths = list(options.output_dir.glob("*/events/slow-case-baseline-1.jsonl"))
                if paths and "run.started" in paths[0].read_text():
                    output = paths[0].parents[1]
                    break
                await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    rows = [json.loads(s) for s in (output / "trials.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["passed", "invalid"]
    assert rows[1]["failure_type"] == "interrupted"
    assert (output / "summary.json").is_file()
