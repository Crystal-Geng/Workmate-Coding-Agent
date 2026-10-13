import argparse
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from evals.schema import TrialResult


# 所有比例同时保留分子分母，零分母输出 N/A
def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percent": 100 * numerator / denominator if denominator else None,
        "display": f"{numerator}/{denominator} ({100 * numerator / denominator:.2f}%)"
        if denominator
        else f"{numerator}/{denominator} (N/A)",
    }


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "min": ordered[0],
        "max": ordered[-1],
    }


def paired_task_differences(results, left, right, value):
    repeats = defaultdict(dict)
    for row in results:
        if row.branch and row.status != "invalid" and row.comparison_eligible:
            repeats[(row.case_id, row.repeat_id)][row.branch] = row
    tasks = defaultdict(list)
    for (case_id, _), pair in repeats.items():
        if left in pair and right in pair:
            left_value = value(pair[left])
            right_value = value(pair[right])
            if left_value is not None and right_value is not None:
                tasks[case_id].append(left_value - right_value)
    return {case_id: statistics.fmean(values) for case_id, values in tasks.items()}


def cluster_bootstrap(task_differences, seed, samples=10_000):
    values = list(task_differences.values())
    if not values:
        return {"task_count": 0, "estimate": None, "ci_95": [None, None], "samples": samples}
    generator = random.Random(seed)
    estimates = sorted(
        statistics.fmean(generator.choice(values) for _ in values) for _ in range(samples)
    )
    low = estimates[int(samples * 0.025)]
    high = estimates[min(samples - 1, int(samples * 0.975))]
    return {
        "task_count": len(values),
        "estimate": statistics.fmean(values),
        "ci_95": [low, high],
        "samples": samples,
    }


def quality_conclusion(interval, margin):
    low, high = interval["ci_95"]
    if low is None:
        return "insufficient_evidence"
    if low >= -margin:
        return "supports_noninferiority"
    if high < -margin:
        return "does_not_support_noninferiority"
    return "insufficient_evidence"


# 汇总有效任务、故障恢复、约束与压缩对照，保留无效试次
def summarize(results, manifest):
    valid = [r for r in results if r.status != "invalid"]
    groups = defaultdict(list)
    for r in valid:
        groups[r.category].append(r)
    faults = [
        r
        for r in valid
        if r.fault_status == "triggered"
        and r.fault_mode == "transient"
        and not r.human_intervention
    ]
    planned_cases = {
        p["case_id"]
        for p in manifest.get("plan", [])
        if p.get("cohort") == "baseline" and not p.get("branch")
    }
    stable = 0
    eligible = 0
    for case_id in planned_cases:
        planned = [
            p for p in manifest["plan"] if p["case_id"] == case_id and p["cohort"] == "baseline"
        ]
        trials = [r for r in results if r.case_id == case_id and not r.fault_type and not r.branch]
        if len(planned) == 3:
            eligible += 1
            stable += (
                len(trials) == 3
                and len({r.trial_id for r in trials}) == 3
                and all(r.status == "passed" for r in trials)
            )
    branches = {b: [r for r in valid if r.branch == b] for b in "ABC"}
    rates = {b: ratio(sum(r.passed for r in rows), len(rows)) for b, rows in branches.items()}
    constraints = [g for r in valid for g in r.grader_results if g.constraint]
    pairs = []
    paired = defaultdict(dict)
    for r in results:
        if r.branch:
            paired[(r.case_id, r.trial_id.rsplit("-", 1)[0])][r.branch] = r
    for pair in paired.values():
        if all(
            b in pair and pair[b].status != "invalid" and pair[b].comparison_eligible for b in "AB"
        ):
            pairs.append(pair)
    safety = [r for r in valid if r.safely_terminated is not None]
    seed = manifest.get("statistics", {}).get("seed", 20260923)
    margin = manifest.get("configuration", {}).get("quality_margin_pp", 5.0)
    ba_quality = cluster_bootstrap(
        paired_task_differences(results, "B", "A", lambda row: 100.0 * row.passed), seed
    )
    bc_quality = cluster_bootstrap(
        paired_task_differences(results, "B", "C", lambda row: 100.0 * row.passed), seed + 1
    )

    def usage_value(row):
        return row.usage.get("total_input_tokens") if row.usage.get("complete", False) else None

    def task_duration(row):
        return max(0.0, row.duration_seconds - row.grader_seconds)

    ba_usage = cluster_bootstrap(paired_task_differences(results, "B", "A", usage_value), seed + 2)
    bc_usage = cluster_bootstrap(paired_task_differences(results, "B", "C", usage_value), seed + 3)
    ba_duration = cluster_bootstrap(
        paired_task_differences(results, "B", "A", task_duration), seed + 4
    )
    bc_duration = cluster_bootstrap(
        paired_task_differences(results, "B", "C", task_duration), seed + 5
    )
    compaction_rows = [
        row for row in results if row.branch and row.tokens_before and row.tokens_after is not None
    ]
    token_keys = (
        "ordinary_input_tokens",
        "total_input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    )

    def known_sum(rows, key, section=None):
        values = []
        for row in rows:
            source = row.usage.get(section, {}) if section else row.usage
            value = source.get(key)
            if value is not None:
                values.append(value)
        return sum(values) if values else None

    def phase_sum(rows, key, phase):
        calls = [
            call
            for row in rows
            for call in row.usage.get("calls", [])
            if call.get("phase") == phase
        ]
        if not calls:
            return 0
        if any(
            call.get("normalized_usage") is None or call["normalized_usage"].get(key) is None
            for call in calls
        ):
            return None
        return sum(call["normalized_usage"][key] for call in calls)

    summary = {
        "framework_self_test": not manifest.get("live", False),
        "provider": manifest.get("provider"),
        "model": manifest.get("model"),
        "configuration": manifest.get("configuration", {}),
        "git_commit": manifest.get("git_commit"),
        "git_dirty": manifest.get("git_dirty"),
        "planned_trials": manifest["planned_trials"],
        "completed_trials": len(results),
        "valid_trials": len(valid),
        "invalid_trials": len(results) - len(valid),
        "missing_trials": manifest["planned_trials"] - len(results),
        "task_success_rate": ratio(sum(r.passed for r in valid), len(valid)),
        "all_three_success_rate": ratio(stable, eligible),
        "by_category": {
            c: ratio(sum(r.passed for r in rows), len(rows)) for c, rows in groups.items()
        },
        "by_cohort": {
            cohort: ratio(
                sum(r.passed for r in valid if bool(r.fault_type) == (cohort == "fault")),
                sum(bool(r.fault_type) == (cohort == "fault") for r in valid),
            )
            for cohort in ("baseline", "fault")
        },
        "fault_planned_injections": sum(p["cohort"] == "fault" for p in manifest.get("plan", [])),
        "fault_actual_injections": sum(r.injection_count for r in results),
        "fault_status_counts": dict(Counter(r.fault_status for r in results if r.fault_type)),
        "recovery_rate": ratio(sum(r.passed for r in faults), len(faults)),
        "safely_terminated": ratio(sum(r.safely_terminated for r in safety), len(safety)),
        "boundary_preserved": ratio(sum(bool(r.boundary_preserved) for r in safety), len(safety)),
        "branch_success_rates": rates,
        "quality_inference": {
            "B_minus_A_percentage_points": ba_quality,
            "B_minus_C_percentage_points": bc_quality,
            "noninferiority_margin_pp": margin,
            "B_vs_A_conclusion": (
                "framework_self_test_only"
                if not manifest.get("live", False)
                else quality_conclusion(ba_quality, margin)
            ),
            "B_vs_C_conclusion": (
                "framework_self_test_only"
                if not manifest.get("live", False)
                else (
                    "supports_B_superiority"
                    if bc_quality["ci_95"][0] is not None and bc_quality["ci_95"][0] > 0
                    else "insufficient_evidence"
                )
            ),
        },
        "resource_inference": {
            "complete_usage_only": True,
            "B_minus_A_total_input_tokens": ba_usage,
            "B_minus_C_total_input_tokens": bc_usage,
            "B_minus_A_task_seconds": ba_duration,
            "B_minus_C_task_seconds": bc_duration,
        },
        "paired_B_minus_A_percentage_points": (
            100 * sum(p["B"].passed - p["A"].passed for p in pairs) / len(pairs) if pairs else None
        ),
        "paired_valid_count": len(pairs),
        "paired_net_successes_B_minus_A": sum(p["B"].passed - p["A"].passed for p in pairs),
        "key_constraint_rate": ratio(sum(g.passed for g in constraints), len(constraints)),
        "branch_constraint_rates": {
            b: ratio(
                sum(g.passed for r in rows for g in r.grader_results if g.constraint),
                sum(g.constraint for r in rows for g in r.grader_results),
            )
            for b, rows in branches.items()
        },
        "compaction": [
            {
                "case_id": r.case_id,
                "trial_id": r.trial_id,
                "branch": r.branch,
                "before": r.tokens_before,
                "after": r.tokens_after,
                "method": r.token_count_method,
                "reduction": ratio(r.tokens_before - r.tokens_after, r.tokens_before)
                if r.tokens_before and r.tokens_after is not None
                else ratio(0, 0),
            }
            for r in results
            if r.branch
        ],
        "first_request_reduction_estimate": {
            branch: {
                "per_trial_ratio_distribution": distribution(
                    [
                        (r.tokens_before - r.tokens_after) / r.tokens_before
                        for r in compaction_rows
                        if r.branch == branch and r.tokens_before
                    ]
                ),
                "aggregate_ratio": ratio(
                    sum(
                        r.tokens_before - r.tokens_after
                        for r in compaction_rows
                        if r.branch == branch
                    ),
                    sum(r.tokens_before for r in compaction_rows if r.branch == branch),
                ),
                "method": "complete request JSON UTF-8 bytes / 4, rounded up",
            }
            for branch in "ABC"
        },
        "duration_seconds": sum(r.duration_seconds for r in results),
        "duration_distribution": distribution([r.duration_seconds for r in results]),
        "task_duration_distribution": distribution([task_duration(r) for r in results]),
        "preparation_duration": distribution([r.preparation_seconds for r in results]),
        "continuation_duration": distribution([r.continuation_seconds for r in results]),
        "grader_duration": distribution([r.grader_seconds for r in results]),
        "observed_usage": {key: known_sum(results, key) for key in token_keys},
        "usage_complete": bool(results) and all(r.usage.get("complete", False) for r in results),
        "usage_completeness": ratio(
            sum(r.usage.get("complete", False) for r in results), len(results)
        ),
        "infrastructure_failures": sum(r.infrastructure_failure for r in results),
        "execution": manifest.get("execution", {}),
        "serious_violations": sum(
            not g.passed for r in valid for g in r.grader_results if g.constraint
        ),
        "failures": dict(Counter(r.failure_type or "unknown" for r in results if not r.passed)),
        "failed_trials": [
            {
                "case_id": r.case_id,
                "trial_id": r.trial_id,
                "status": r.status,
                "reason": r.failure_reason,
                "events": r.events_path,
                "trace": r.trace_path,
                "artifacts": r.artifacts_path,
            }
            for r in results
            if not r.passed
        ],
    }
    summary["summary_call_usage"] = {key: phase_sum(results, key, "summary") for key in token_keys}
    summary["branch_usage"] = {
        b: {
            "totals": {key: known_sum(rows, key) for key in token_keys},
            "complete_subset_totals": {
                key: known_sum([row for row in rows if row.usage.get("complete", False)], key)
                for key in token_keys
            },
            "total_input_distribution": distribution(
                [
                    r.usage["total_input_tokens"]
                    for r in rows
                    if r.usage.get("total_input_tokens") is not None
                ]
            ),
            "coverage": ratio(sum(r.usage.get("complete", False) for r in rows), len(rows)),
        }
        for b, rows in branches.items()
    }
    summary["failed_retry_tokens"] = None
    summary["reread_extra_tokens"] = None
    summary["by_fault_type"] = {}
    for kind in sorted({r.fault_type for r in results if r.fault_type}):
        rows = [r for r in faults if r.fault_type == kind]
        summary["by_fault_type"][kind] = {
            "recovery_rate": ratio(sum(r.passed for r in rows), len(rows)),
            "actual_injections": sum(r.injection_count for r in results if r.fault_type == kind),
            "recovery_attempts": sum(r.recovery_attempts for r in results if r.fault_type == kind),
        }
    summary["agent_success_rate"] = (
        None if summary["framework_self_test"] else summary["task_success_rate"]
    )
    return summary


# 从已落盘试次重新生成 JSON 和可读 Markdown 报告
def write_report(directory: Path):
    manifest = json.loads((directory / "manifest.json").read_text())
    results = []
    corrupt_rows = []
    path = directory / "trials.jsonl"
    for index, line in enumerate(path.read_text().splitlines() if path.exists() else [], 1):
        try:
            results.append(TrialResult.model_validate_json(line))
        except ValueError as exc:
            corrupt_rows.append({"line": index, "reason": str(exc)})
    summary = summarize(results, manifest)
    summary["corrupt_result_rows"] = corrupt_rows
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    label = (
        "FAKE / SCRIPTED：仅评测框架自测，不代表真实 Agent 成功率"
        if summary["framework_self_test"]
        else "LIVE Agent 评测"
    )
    lines = [
        f"# {label}",
        "",
        f"Provider: {summary['provider']} / Model: {summary['model']}",
        f"Git: {summary['git_commit']} / dirty={summary['git_dirty']}",
        f"计划 {summary['planned_trials']}；完成 {summary['completed_trials']}；"
        f"有效 {summary['valid_trials']}；无效 {summary['invalid_trials']}；"
        f"未完成 {summary['missing_trials']}",
        "",
    ]
    for key in (
        "task_success_rate",
        "all_three_success_rate",
        "recovery_rate",
        "safely_terminated",
        "boundary_preserved",
        "key_constraint_rate",
    ):
        lines.append(f"- {key}: {summary[key]['display']}")
    lines.extend(
        [
            "",
            "## 分类与对照",
            "",
            "```json",
            json.dumps(
                {
                    k: summary[k]
                    for k in (
                        "by_category",
                        "by_cohort",
                        "fault_planned_injections",
                        "fault_actual_injections",
                        "fault_status_counts",
                        "branch_success_rates",
                        "quality_inference",
                        "resource_inference",
                        "paired_B_minus_A_percentage_points",
                        "paired_valid_count",
                        "branch_constraint_rates",
                        "compaction",
                        "first_request_reduction_estimate",
                        "observed_usage",
                        "summary_call_usage",
                        "branch_usage",
                        "failed_retry_tokens",
                        "reread_extra_tokens",
                        "by_fault_type",
                        "usage_complete",
                        "usage_completeness",
                        "duration_seconds",
                        "duration_distribution",
                        "task_duration_distribution",
                        "preparation_duration",
                        "continuation_duration",
                        "grader_duration",
                        "infrastructure_failures",
                        "execution",
                        "failures",
                    )
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "成本为已观测 usage；失败流和重新读取的因果成本未暴露，不补造数字。",
            "",
            "## 失败及无效试次",
            "",
        ]
    )
    lines.extend(
        f"- {r['case_id']}/{r['trial_id']}: {r['status']} — {r['reason']} "
        f"[events]({r['events']}) [trace]({r['trace']}) [产物]({r['artifacts']})"
        for r in summary["failed_trials"]
    )
    if corrupt_rows:
        lines.append(f"结果文件存在损坏行：{corrupt_rows}")
    (directory / "summary.md").write_text("\n".join(lines) + "\n")
    return summary


# 提供脱离模型调用的独立报告重建入口
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    write_report(args.directory)


if __name__ == "__main__":
    main()
