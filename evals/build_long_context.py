from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.build_cases import changes, tool_write, write

ROOT = Path(__file__).resolve().parent
VERSION = "long-context-v2"


FORMAL = [
    (
        "requirement_change",
        "export handoff",
        ["输出改为 JSON，不再生成 YAML", "目录改为 exports/final", "所有时间使用 UTC"],
        {"format": "json", "directory": "exports/final", "timezone": "UTC"},
    ),
    (
        "requirement_change",
        "incident notification",
        ["通知渠道最终改为 email", "收件人仅 ops 与 legal", "使用模板 incident-v3"],
        {"channel": "email", "recipients": ["ops", "legal"], "template": "incident-v3"},
    ),
    (
        "requirement_change",
        "retention policy",
        ["最终保留期为 45 天", "legal-hold 项必须排除", "删除方式为 soft"],
        {"days": 45, "exclude": ["legal-hold"], "delete_mode": "soft"},
    ),
    (
        "requirement_change",
        "pagination contract",
        ["废弃 offset，最终参数为 cursor", "每页固定 75 条", "next_cursor 为 null 时停止"],
        {"parameter": "cursor", "page_size": 75, "stop_on": "next_cursor:null"},
    ),
    (
        "requirement_change",
        "release target",
        ["最终目标环境为 staging-eu", "部署必须带 --safe", "回滚脚本为 deploy/rollback.sh"],
        {"target": "staging-eu", "flag": "--safe", "rollback": "deploy/rollback.sh"},
    ),
    (
        "requirement_change",
        "auditor access",
        ["最终角色名为 auditor", "仅允许 read 与 export", "明确禁止 delete"],
        {"role": "auditor", "permissions": ["read", "export"], "deny": ["delete"]},
    ),
    (
        "exact_information",
        "payment reconciliation",
        ["交易号必须为 TXN-7Q4-2026", "金额为 18432.75", "币种为 CNY", "手续费 32.75 不计入净额"],
        {
            "transaction": "TXN-7Q4-2026",
            "gross": 18432.75,
            "currency": "CNY",
            "excluded_fee": 32.75,
        },
    ),
    (
        "exact_information",
        "shipment manifest",
        ["批次号 LOT-AX19", "SKU 为 PART-8841", "合格数量 317", "破损的 4 件不得计入"],
        {"lot": "LOT-AX19", "sku": "PART-8841", "accepted": 317, "excluded_damaged": 4},
    ),
    (
        "exact_information",
        "deployment invocation",
        [
            "配置路径 /srv/workmate/prod.toml",
            "端口 7443",
            "参数 --read-only",
            "health 路径 /readyz",
        ],
        {
            "config": "/srv/workmate/prod.toml",
            "port": 7443,
            "flag": "--read-only",
            "health": "/readyz",
        },
    ),
    (
        "exact_information",
        "credit note",
        ["贷项单 CN-2026-0417", "关联发票 INV-99107", "冲减 219.40", "税额 13.16 单列"],
        {"credit_note": "CN-2026-0417", "invoice": "INV-99107", "amount": 219.40, "tax": 13.16},
    ),
    (
        "exact_information",
        "source citation",
        ["引用报告 RPT-ALPHA-17", "使用第 42 页", "表格编号 T-6", "附录草稿数字不得采用"],
        {"report": "RPT-ALPHA-17", "page": 42, "table": "T-6", "excluded": "appendix-draft"},
    ),
    (
        "exact_information",
        "database migration",
        [
            "迁移编号 20260923_07",
            "表名 audit_events",
            "索引 idx_audit_actor_ts",
            "legacy_actor 为空时跳过",
        ],
        {
            "migration": "20260923_07",
            "table": "audit_events",
            "index": "idx_audit_actor_ts",
            "skip_when": "legacy_actor:null",
        },
    ),
    (
        "workflow",
        "schema rollout",
        [
            "备份与 dry-run 已完成，不得重复",
            "旧的 inplace 方案已失败",
            "只剩应用 migration.sql 与记录校验",
        ],
        {
            "already_done": ["backup", "dry-run"],
            "forbidden_retry": "inplace",
            "remaining": ["apply migration.sql", "record validation"],
        },
    ),
    (
        "workflow",
        "customer dispatch",
        ["邮件已发送，禁止再次发送", "delivery_id 为 DL-8821", "只剩归档回执与关闭工单"],
        {
            "side_effect_done": "email sent",
            "delivery_id": "DL-8821",
            "remaining": ["archive receipt", "close ticket"],
        },
    ),
    (
        "workflow",
        "restore procedure",
        ["快照校验已通过", "rsync 覆盖方案失败且禁止重试", "只剩挂载只读副本和运行 smoke test"],
        {
            "already_done": ["snapshot verification"],
            "forbidden_retry": "rsync overwrite",
            "remaining": ["mount read-only copy", "run smoke test"],
        },
    ),
    (
        "workflow",
        "release completion",
        [
            "版本 v3.8.2 已打 tag，不得重打",
            "上传 registry 曾超时但已经成功",
            "只剩更新 changelog 和发布说明",
        ],
        {
            "immutable_side_effect": "tag v3.8.2",
            "upload": "completed",
            "remaining": ["update changelog", "publish notes"],
        },
    ),
    (
        "workflow",
        "refund case",
        ["退款 RF-391 已提交，禁止再次提交", "支付网关返回 accepted", "只剩写审计记录和通知客服"],
        {
            "side_effect_done": "refund RF-391 accepted",
            "remaining": ["write audit record", "notify support"],
        },
    ),
    (
        "workflow",
        "warehouse import",
        [
            "阶段 extract 与 normalize 已完成",
            "旧的 latin1 重跑会破坏数据，禁止使用",
            "只剩 load 与 reconcile",
        ],
        {
            "already_done": ["extract", "normalize"],
            "forbidden_retry": "latin1 rerun",
            "remaining": ["load", "reconcile"],
        },
    ),
    (
        "document_processing",
        "regional totals",
        [
            "north.csv 是北区权威来源 1280.50",
            "south.json 是南区权威来源 940.25",
            "邮件中的 950 是旧值",
            "结果按两区求和",
        ],
        {"north": 1280.50, "south": 940.25, "total": 2220.75, "ignored": "email:950"},
    ),
    (
        "document_processing",
        "policy exception",
        [
            "主政策上限 30 天",
            "法务附件对 case-L17 给出 90 天例外",
            "例外优先于主政策",
            "其他案例仍为 30 天",
        ],
        {
            "default_days": 30,
            "case": "case-L17",
            "effective_days": 90,
            "precedence": "legal attachment",
        },
    ),
    (
        "document_processing",
        "vendor comparison",
        ["报价 A 为 12400 且含税", "报价 B 为 11900 但另加 8% 税", "比较含税总额", "运费均为零"],
        {"vendor_a_total": 12400, "vendor_b_total": 12852, "winner": "A"},
    ),
    (
        "document_processing",
        "incident chronology",
        ["监控告警时间 09:12", "人工确认 09:18", "回滚开始 09:26", "聊天记录中的 09:08 是时区错误"],
        {
            "timeline": ["09:12 alert", "09:18 confirmed", "09:26 rollback"],
            "ignored": "09:08 timezone error",
        },
    ),
    (
        "document_processing",
        "inventory conflict",
        ["仓库扫描记录 86 件", "ERP 显示 91 件", "盘点规则以扫描为准", "其中 3 件隔离品不计可用"],
        {"authoritative": "warehouse scan", "on_hand": 86, "quarantined": 3, "available": 83},
    ),
    (
        "document_processing",
        "compliance mapping",
        [
            "控制项 AC-2 对应证据 access.csv",
            "控制项 AU-6 对应 evidence/audit.log",
            "旧清单把两者写反",
            "最终映射以审计复核为准",
        ],
        {"AC-2": "access.csv", "AU-6": "evidence/audit.log", "ignored": "old reversed list"},
    ),
]


CODE = [
    (
        "tax",
        [
            "solve 接收价格列表并按 10% 税率返回含税总额",
            "结果保留两位小数",
            "config.json 必须同步税率与精度",
        ],
        "def solve(values):\n    return sum(values)\n",
        "def solve(values):\n    return round(sum(values) * 1.1, 2)\n",
        {"tax_rate": 0.1, "round_digits": 2},
    ),
    (
        "parser",
        [
            "solve 按逗号拆分字符串",
            "每项去空格并转小写",
            "config.json 必须同步 delimiter 与 lowercase",
        ],
        "def solve(value):\n    return value.split(';')\n",
        "def solve(value):\n    return [part.strip().lower() for part in value.split(',')]\n",
        {"delimiter": ",", "lowercase": True},
    ),
    (
        "auth",
        [
            "auditor 只允许 read 与 export",
            "delete 必须返回 False",
            "module.py 与 config.json 的角色契约必须一致",
        ],
        "def solve(role, action):\n    return True\n",
        "def solve(role, action):\n    return role == 'auditor' and action in {'read', 'export'}\n",
        {"auditor": ["read", "export"]},
    ),
    (
        "cache",
        [
            "键格式为 METHOD:path:sorted_query",
            "query 参数按键排序并用 & 连接",
            "config.json 同步 separator 与 sort_query",
        ],
        "def solve(method, path, params):\n    return path\n",
        "def solve(method, path, params):\n    query = '&'.join(f'{key}={params[key]}' for key in sorted(params))\n    return f'{method}:{path}:{query}'\n",  # noqa: E501
        {"sort_query": True, "separator": ":"},
    ),
    (
        "allocation",
        ["按订单顺序分配库存", "库存不足时允许最后一单部分满足", "不得分配超过总库存"],
        "def solve(stock, orders):\n    return orders\n",
        "def solve(stock, orders):\n    result = []\n    for order in orders:\n        take = min(stock, order)\n        result.append(take)\n        stock -= take\n    return result\n",  # noqa: E501
        {"allow_partial": True, "preserve_order": True},
    ),
    (
        "version",
        ["输入必须以 v 开头", "解析恰好三个整数部分", "返回整数 tuple 并同步 config.json"],
        "def solve(value):\n    return value\n",
        "def solve(value):\n    parts = value.removeprefix('v').split('.')\n    if not value.startswith('v') or len(parts) != 3:\n        raise ValueError(value)\n    return tuple(map(int, parts))\n",  # noqa: E501
        {"prefix": "v", "parts": 3},
    ),
]


DEV = [
    (
        "requirement_change",
        "dev output rename",
        ["最终输出名是 handoff.json", "旧名 draft.json 已废弃"],
        {"output": "handoff.json", "deprecated": "draft.json"},
    ),
    (
        "exact_information",
        "dev service config",
        ["服务名 workmate-index", "重试次数 4", "超时 17 秒"],
        {"service": "workmate-index", "retries": 4, "timeout_seconds": 17},
    ),
    (
        "workflow",
        "dev upload",
        ["分片上传已完成不得重复", "只剩合并与校验 SHA-256"],
        {"already_done": ["chunk upload"], "remaining": ["merge", "verify sha256"]},
    ),
    (
        "document_processing",
        "dev source conflict",
        ["正式表值为 61", "草稿值 58 作废", "最终使用正式表"],
        {"value": 61, "source": "final table", "ignored": 58},
    ),
]


def checkpoint(facts, bucket, subject):
    pairs = {"short": 10, "medium": 30, "long": 70}[bucket]
    positions = {
        round(index * (pairs - 1) / max(1, len(facts) - 1)): fact
        for index, fact in enumerate(facts)
    }
    record_types = (
        "source inventory",
        "requirement diff",
        "failed approach",
        "completed side effect",
        "authority check",
        "exception review",
        "artifact inspection",
        "validation result",
        "pending action",
        "conflict resolution",
    )
    history = []
    for index in range(pairs):
        if index in positions:
            history.extend(
                [
                    {"role": "user", "content": "Constraint: " + positions[index]},
                    {"role": "assistant", "content": "已记录；该项覆盖更早的冲突记录。"},
                ]
            )
        tool_id = f"history-{index + 1:03d}"
        record_type = record_types[index % len(record_types)]
        history.extend(
            [
                {
                    "role": "user",
                    "content": (
                        f"继续 {subject}：检查第 {index + 1} 份 {record_type} 记录，"
                        "区分权威资料、失败方案、已完成副作用和待办项。"
                    ),
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "read_file",
                            "input": {"path": f"records/{record_type}-{index + 1:03d}.txt"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": (
                                f"Synthetic {record_type} for {subject}; record LOG-{index + 1:03d}. "  # noqa: E501
                                f"Cross-check status={index % 4}; this record does not override a later Constraint."  # noqa: E501
                            ),
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": (
                        f"已核对 LOG-{index + 1:03d}；保留其来源关系与执行状态，"
                        "最终决定仍以最新 Constraint 和明确例外为准。"
                    ),
                },
            ]
        )
    return history


def regular_case(case_id, split, task_type, title, facts, expected, bucket):
    fixture = f"long/{case_id}"
    write(ROOT / "fixtures" / fixture / "source.txt", f"Synthetic source packet for {title}.\n")
    history = checkpoint(facts, bucket, title)
    write(ROOT / "fixtures" / "checkpoints-long" / f"{case_id}.json", history)
    return {
        "case_id": case_id,
        "name": title,
        "category": "long_context",
        "goal": "根据历史中的最终要求完成剩余任务，将精确结果写入 result.json；不要采用已废弃值或重复已完成副作用。",  # noqa: E501
        "fixtures": [{"source": fixture}],
        "timeout_seconds": 180,
        "max_steps": 20,
        "graders": [
            {"kind": "json", "path": "result.json", "expected": expected, "constraint": True},
            changes("result.json"),
        ],
        "forbidden_changes": ["source.txt"],
        "tags": ["long_context", split],
        "compact_spec": {
            "checkpoint": f"checkpoints-long/{case_id}.json",
            "branches": ["A", "B", "C"],
        },
        "required_history_facts": facts,
        "metadata": {
            "split": split,
            "task_type": task_type,
            "source": "synthetic",
            "source_note": "Purpose-built synthetic task; not represented as a real project trace.",
            "context_length_bucket": bucket,
            "key_fact_positions": ["early", "middle", "late"],
            "dataset_version": VERSION,
        },
        "script": [[tool_write("result.json", json.dumps(expected, ensure_ascii=False))]],
    }


def code_case(case_id, scenario, facts, buggy, fixed, config, bucket, split="formal"):
    fixture = f"long/{case_id}"
    write(ROOT / "fixtures" / fixture / "scenario.txt", scenario + "\n")
    write(ROOT / "fixtures" / fixture / "module.py", buggy)
    write(ROOT / "fixtures" / fixture / "config.json", {"status": "stale"})
    write(
        ROOT / "fixtures" / "checkpoints-long" / f"{case_id}.json",
        checkpoint(facts, bucket, scenario),
    )
    return {
        "case_id": case_id,
        "name": f"multi-file {scenario} contract",
        "category": "long_context",
        "goal": "依据历史中的最终接口契约修复 module.py 和 config.json，保持函数名 solve；不要修改 scenario.txt。",  # noqa: E501
        "fixtures": [{"source": fixture}],
        "timeout_seconds": 180,
        "max_steps": 20,
        "graders": [
            {"kind": "pytest", "test_id": "long_context_code", "constraint": True},
            changes("module.py", "config.json"),
        ],
        "forbidden_changes": ["scenario.txt"],
        "tags": ["long_context", split],
        "compact_spec": {
            "checkpoint": f"checkpoints-long/{case_id}.json",
            "branches": ["A", "B", "C"],
        },
        "required_history_facts": facts,
        "metadata": {
            "split": split,
            "task_type": "multi_file_code",
            "source": "synthetic",
            "source_note": "Purpose-built synthetic multi-file task with evaluator-private tests.",
            "context_length_bucket": bucket,
            "key_fact_positions": ["early", "middle", "late"],
            "dataset_version": VERSION,
        },
        "script": [[tool_write("module.py", fixed), tool_write("config.json", json.dumps(config))]],
    }


def freeze_paths(folder):
    paths = sorted((ROOT / "cases" / folder).glob("*.json"))
    paths += sorted((ROOT / "fixtures" / "checkpoints-long").glob("*.json"))
    paths += sorted(path for path in (ROOT / "fixtures" / "long").rglob("*") if path.is_file())
    paths += [
        ROOT / "build_long_context.py",
        ROOT / "schema.py",
        ROOT / "run_eval.py",
        ROOT / "report.py",
    ]
    return [path for path in paths if path.name != "FROZEN.json"]


def freeze(folder):
    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in freeze_paths(folder)
    }
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    write(
        ROOT / "cases" / folder / "FROZEN.json",
        {"dataset_version": VERSION, "sha256": digest, "files": hashes},
    )


def verify_freeze(folder):
    path = ROOT / "cases" / folder / "FROZEN.json"
    if not path.is_file():
        raise ValueError(f"dataset is not frozen: {folder}")
    frozen = json.loads(path.read_text())
    hashes = {
        str(item.relative_to(ROOT)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in freeze_paths(folder)
    }
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if (
        frozen.get("dataset_version") != VERSION
        or frozen.get("files") != hashes
        or frozen.get("sha256") != digest
    ):
        raise ValueError(f"dataset freeze mismatch: {folder}; create a new dataset version")
    return frozen


def main():
    formal = []
    for index, (task_type, title, facts, expected) in enumerate(FORMAL, 1):
        bucket = ("short", "medium", "long")[(index - 1) % 3]
        formal.append(
            regular_case(
                f"lc-formal-{index:02d}", "formal", task_type, title, facts, expected, bucket
            )
        )
    for offset, (scenario, facts, buggy, fixed, config) in enumerate(CODE, 25):
        bucket = ("short", "medium", "long")[(offset - 1) % 3]
        formal.append(
            code_case(f"lc-formal-{offset:02d}", scenario, facts, buggy, fixed, config, bucket)
        )
    dev = [
        regular_case(
            f"lc-dev-{index:02d}",
            "development",
            *spec,
            ("short", "medium", "long", "medium")[index - 1],
        )
        for index, spec in enumerate(DEV, 1)
    ]
    dev.append(
        code_case(
            "lc-dev-05",
            "absolute",
            ["module.py 的 solve 返回输入的绝对值", "config.json 的 mode 为 absolute"],
            "def solve(value):\n    return value\n",
            "def solve(value):\n    return abs(value)\n",
            {"mode": "absolute"},
            "short",
            "development",
        )
    )
    for folder, cases in (("long_formal", formal), ("long_dev", dev)):
        for item in cases:
            write(ROOT / "cases" / folder / f"{item['case_id']}.json", item)
        freeze(folder)


if __name__ == "__main__":
    main()
