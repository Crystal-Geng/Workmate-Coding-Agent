from datetime import UTC, datetime

from evals.report import cluster_bootstrap, quality_conclusion, ratio, summarize
from evals.schema import TrialResult


# 创建只用于统计测试的明确状态记录
def trial(status="passed", **kwargs):
    return TrialResult(
        case_id="case",
        trial_id="baseline-1",
        category="files",
        started_at=datetime.now(UTC),
        runner_type="agent_runner",
        status=status,
        passed=status == "passed",
        events_path="events",
        trace_path="trace",
        artifacts_path="artifacts",
        **kwargs,
    )


# 功能：所有百分比带分子分母且空分母显示 N/A
# 设计：使用简单整数比率和零分母，避免浮点格式掩盖统计逻辑错误
def test_ratio():
    assert ratio(1, 4) == {
        "numerator": 1,
        "denominator": 4,
        "percent": 25,
        "display": "1/4 (25.00%)",
    }
    assert ratio(0, 0)["percent"] is None
    assert "N/A" in ratio(0, 0)["display"]


# 功能：invalid 不计入成功率，未触发故障不计入恢复率
# 设计：混合成功、失败、无效、未触发和人工介入五类试次
def test_valid_and_recovery_denominators():
    rows = [
        trial(fault_type="runtime_error", fault_mode="transient", fault_status="not_triggered"),
        trial("invalid"),
        trial(
            "failed",
            fault_type="runtime_error",
            fault_mode="transient",
            fault_status="triggered",
            injection_count=1,
        ),
        trial(
            fault_type="runtime_error",
            fault_mode="transient",
            fault_status="triggered",
            human_intervention=True,
        ),
    ]
    summary = summarize(rows, {"planned_trials": 4, "live": False})
    assert summary["usage_completeness"]["numerator"] == 0
    assert summary["observed_usage"]["total_input_tokens"] is None
    assert summary["task_success_rate"]["numerator"] == 2
    assert summary["task_success_rate"]["denominator"] == 3
    assert summary["recovery_rate"]["numerator"] == 0
    assert summary["recovery_rate"]["denominator"] == 1
    assert summary["agent_success_rate"] is None
    assert summary["quality_inference"]["B_vs_A_conclusion"] == "framework_self_test_only"
    assert summary["quality_inference"]["B_vs_C_conclusion"] == "framework_self_test_only"


# 功能：未完成的第三次试验不能被计为三次全部成功
# 设计：计划三次但只完成两次，验证计划表参与可靠性指标
def test_three_trials_requires_complete_plan():
    plan = [{"case_id": "case", "cohort": "baseline"}] * 3

    summary = summarize([trial(), trial()], {"planned_trials": 3, "plan": plan})
    assert summary["all_three_success_rate"]["numerator"] == 0
    assert summary["all_three_success_rate"]["denominator"] == 1
    assert summary["missing_trials"] == 1


def test_cluster_bootstrap_and_noninferiority_are_deterministic():
    first = cluster_bootstrap({"a": -4.0, "b": -4.0, "c": -4.0}, seed=17, samples=200)
    second = cluster_bootstrap({"a": -4.0, "b": -4.0, "c": -4.0}, seed=17, samples=200)
    assert first == second
    assert first["estimate"] == -4.0 and first["ci_95"] == [-4.0, -4.0]
    assert quality_conclusion(first, margin=5.0) == "supports_noninferiority"
