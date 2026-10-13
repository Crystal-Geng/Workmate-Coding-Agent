import argparse
import copy
import json
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# 确定性写入 fixture 或任务定义，不产生运行结果
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if not isinstance(value, str)
        else value
    )


# 为离线框架自测生成真实 write_file 工具请求
def tool_write(path, text):
    return {"name": "write_file", "input": {"path": path, "content": text}}


# 对固定自测 Python 脚本做 shell 引用，脚本不会来自模型回答
def python_call(code):
    return {"name": "bash", "input": {"command": "python -c " + shlex.quote(code)}}


# 先定义输入和验收配置，最后赋予面向 Agent 的目标
def case(number, category, inputs, graders, goal, script, **extra):
    cid = f"e2e-{number:02d}"
    inputs = {"protected.txt": "immutable fixture\n", **inputs}
    for path, content in inputs.items():
        write(ROOT / "fixtures" / cid / path, content)
    config = dict(
        case_id=cid,
        name=goal[:45],
        category=category,
        fixtures=[{"source": cid}],
        graders=graders,
        forbidden_changes=["protected.txt"],
        required_events=[],
        timeout_seconds=120,
        max_steps=20,
        tags=[],
        script=script,
    )
    config.update(extra)
    config["goal"] = goal
    if number in {1, 2, 3, 4, 5, 6, 9, 10}:
        config["tags"].append("pilot")
    return config


# 生成允许变动的最终文件清单和不可变文件约束
def changes(*paths):
    return {"kind": "changes", "allowed": list(paths), "constraint": True}


# 构造固定数据输入，不在 Agent 工作目录内放置标准答案
def data_inputs():
    return {
        "north.csv": "id,region,amount\na,north,12.5\nb,south,7.5\n",
        "south.csv": "id,region,amount\nc,north,4\nd,east,9\n",
        "jan.json": {"amount": 11, "note": "keep"},
        "feb.json": {"amount": 23, "note": "keep"},
    }


# 为汇总自测编写独立于 grader 的读取与计算工具脚本
def summary_script(operation, output="summary.json"):
    if operation == "json_sum":
        code = "import json; from pathlib import Path; "
        code += "answer={'total':sum(json.loads(Path(p).read_text())['amount'] for p in ['jan.json','feb.json'])}; "
    else:
        code = "import csv,json; from pathlib import Path; "
        code += "rows=[r for p in ['north.csv','south.csv'] for r in csv.DictReader(open(p))]; "
        if operation == "group_sum":
            code += "answer={g:sum(float(r['amount']) for r in rows if r['region']==g) for g in {r['region'] for r in rows}}; "
        elif operation == "join":
            code += "answer={'ids':sorted(set(r['id'] for r in rows))}; "
        else:
            code += "answer={'total':sum(float(r['amount']) for r in rows)}; "
    return [[python_call(code + f"Path({output!r}).write_text(json.dumps(answer))")]]


# 创建四种真实代码缺陷及评测器私有 pytest 测试
def code_cases():
    definitions = {
        "sort": (
            "def solve(values):\n    return sorted(values, key=str)\n",
            "def solve(values):\n    return sorted(values)\n",
            "assert module.solve([10, 2, -3, 2]) == [-3, 2, 2, 10]\n"
            "    assert module.solve([]) == []\n"
            "    data = [5, 1]; module.solve(data); assert data == [5, 1]",
            "修复 solution.py 的数值排序；升序、保留重复值且不修改输入列表。",
        ),
        "date": (
            "from datetime import datetime\ndef solve(value):\n    return datetime.strptime(value, '%Y-%d-%m').date().isoformat()\n",
            "from datetime import datetime\ndef solve(value):\n    return datetime.strptime(value, '%Y-%m-%d').date().isoformat()\n",
            "assert module.solve('2024-02-29') == '2024-02-29'\n"
            "    assert module.solve('2026-12-03') == '2026-12-03'\n"
            "    with pytest.raises(ValueError): module.solve('2025-02-29')",
            "修复 solution.py 的 ISO 日期解析，输入 YYYY-MM-DD，非法日期抛 ValueError。",
        ),
        "filter": (
            "def solve(rows):\n    return [r for r in rows if r.get('active')]\n",
            "def solve(rows):\n    return [r for r in rows if r.get('active') is True]\n",
            "rows = [{'active': True}, {'active': 'false'}, {}, {'active': False}, {'active': 1}]\n"
            "    assert module.solve(rows) == [rows[0]]\n"
            "    assert len(rows) == 5",
            "修复 solution.py，只有 active 字段严格为布尔 True 的记录被保留，不能修改输入。",
        ),
        "dedupe": (
            "def solve(values):\n    return sorted(set(values))\n",
            "def solve(values):\n    return list(dict.fromkeys(values))\n",
            "assert module.solve(['b', 'a', 'b', 'c', 'a']) == ['b', 'a', 'c']\n"
            "    assert module.solve([]) == []",
            "修复 solution.py 去重逻辑，保留首次出现顺序，支持空列表。",
        ),
    }
    cases = []
    for number, (name, (bug, fix, checks, goal)) in enumerate(definitions.items(), 9):
        test = "import importlib.util\nimport os\nfrom pathlib import Path\nimport pytest\n\n"
        test += "# 功能：验收真实工作目录中的代码修复\n# 设计：测试存于评测器目录，防止 Agent 改写验收规则\n"
        test += "def test_solution():\n"
        test += "    spec = importlib.util.spec_from_file_location('solution', Path(os.environ['EVAL_WORKSPACE']) / 'solution.py')\n"
        test += (
            "    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    "
            + checks
            + "\n"
        )
        write(ROOT / "graders" / "checks" / f"check_{name}.py", test)
        cases.append(
            case(
                number,
                "code_fix",
                {"solution.py": bug},
                [{"kind": "pytest", "test_id": name}, changes("solution.py")],
                goal,
                [[tool_write("solution.py", fix)]],
            )
        )
    return cases


# 生成 24 项固定端到端任务，其中 8 项标为 pilot
def e2e_cases():
    cases = []
    cases.append(
        case(
            1,
            "files",
            {"source.txt": "Hello WorkMate\n"},
            [
                {"kind": "exact", "path": "output/greeting.txt", "source": "e2e-01/source.txt"},
                changes("output/greeting.txt"),
            ],
            "读取 source.txt，逐字复制到 output/greeting.txt，只创建此文件，保留其他文件。",
            [
                [
                    python_call(
                        "from pathlib import Path; Path('output').mkdir(exist_ok=True); Path('output/greeting.txt').write_bytes(Path('source.txt').read_bytes())"
                    )
                ]
            ],
        )
    )
    config = {"enabled": False, "owner": "ops", "nested": {"retries": 4}, "labels": ["a", "b"]}
    cases.append(
        case(
            2,
            "files",
            {"settings.json": config},
            [
                {
                    "kind": "json",
                    "path": "settings.json",
                    "source": "e2e-02/settings.json",
                    "fields": {"enabled": True},
                    "preserve_other_fields": True,
                },
                changes("settings.json"),
            ],
            "将 settings.json 的 enabled 改为 true，精确保留所有其他字段。",
            [
                [
                    python_call(
                        "import json; from pathlib import Path; p=Path('settings.json'); d=json.loads(p.read_text()); d['enabled']=True; p.write_text(json.dumps(d))"
                    )
                ]
            ],
        )
    )
    cases.append(
        case(
            3,
            "files",
            {"notes.txt": "first\n", "entry.txt": "second\n"},
            [
                {"kind": "exact", "path": "notes.txt", "expected": "first\nsecond\n"},
                changes("notes.txt"),
            ],
            "将 entry.txt 追加到 notes.txt 恰好一次，禁止修改 protected.txt。",
            [
                [
                    python_call(
                        "from pathlib import Path; p=Path('notes.txt'); p.write_text(p.read_text()+Path('entry.txt').read_text())"
                    )
                ]
            ],
        )
    )
    cases.append(
        case(
            4,
            "files",
            {"draft.txt": "draft\n", "keep.txt": "retain\n"},
            [
                {
                    "kind": "composite",
                    "children": [
                        {"kind": "absent", "path": "draft.txt"},
                        {"kind": "hash", "path": "keep.txt", "source": "e2e-04/keep.txt"},
                    ],
                },
                changes("draft.txt"),
            ],
            "删除 draft.txt；keep.txt 与 protected.txt 字节内容必须保持原样。",
            [[python_call("from pathlib import Path; Path('draft.txt').unlink()")]],
        )
    )
    goals = {
        "sum": "汇总 north.csv 和 south.csv 的 amount，写 summary.json，格式为 total 字段的对象。",
        "group_sum": "按 region 汇总两个 CSV 的 amount，写 summary.json，键为 region，值为合计。",
        "join": "读取两个 CSV，合并去重 id 并按字典序排序，写 summary.json 的 ids 数组。",
        "json_sum": "读取 jan.json 与 feb.json，将 amount 求和写 summary.json 的 total 字段。",
    }
    for number, operation in enumerate(goals, 5):
        sources = (
            ["jan.json", "feb.json"] if operation == "json_sum" else ["north.csv", "south.csv"]
        )
        cases.append(
            case(
                number,
                "summary",
                data_inputs(),
                [
                    {
                        "kind": "csv",
                        "path": "summary.json",
                        "operation": operation,
                        "column": "id" if operation == "join" else "amount",
                        "sources": [f"e2e-{number:02d}/{p}" for p in sources],
                    },
                    changes("summary.json"),
                ],
                goals[operation] + " 输入文件全部保持不变。",
                summary_script(operation),
            )
        )
    cases.extend(code_cases())
    for number, operation in zip(range(13, 16), ["sum", "group_sum", "join"], strict=True):
        action = summary_script(operation)[0][0]
        command = action["input"]["command"]
        code = shlex.split(command)[2]
        cases.append(
            case(
                number,
                "multi_tool",
                data_inputs(),
                [
                    {
                        "kind": "csv",
                        "path": "summary.json",
                        "operation": operation,
                        "column": "id" if operation == "join" else "amount",
                        "sources": [f"e2e-{number:02d}/north.csv", f"e2e-{number:02d}/south.csv"],
                    },
                    {"kind": "exists", "path": "process.py"},
                    {"kind": "pytest", "test_id": "pipeline"},
                    {"kind": "exact", "path": "execution.txt", "expected": "completed\n"},
                    changes("process.py", "summary.json", "execution.txt"),
                ],
                goals[operation]
                + "先读取数据，再创建 process.py 执行计算。运行脚本，打印 completed 并将标准输出保存到 execution.txt。保留输入。",
                [
                    [{"name": "read_file", "input": {"path": "north.csv"}}],
                    [tool_write("process.py", code + "; print('completed')\n")],
                    [{"name": "bash", "input": {"command": "python process.py > execution.txt"}}],
                ],
            )
        )
    for number, operation in zip(range(16, 19), ["sum", "group_sum", "join"], strict=True):
        cases.append(
            case(
                number,
                "subagent",
                data_inputs(),
                [
                    {
                        "kind": "csv",
                        "path": "summary.json",
                        "operation": operation,
                        "column": "id" if operation == "join" else "amount",
                        "sources": [f"e2e-{number:02d}/north.csv", f"e2e-{number:02d}/south.csv"],
                    },
                    {
                        "kind": "events",
                        "event": "subagent.finished",
                        "event_fields": {"status": "success"},
                    },
                    changes("summary.json"),
                ],
                "必须调用真实前台 SubAgent 完成以下计算，并核对其文件产物：" + goals[operation],
                [
                    [
                        {
                            "name": "spawn_agent",
                            "input": {
                                "description": "compute fixture summary",
                                "prompt": goals[operation],
                            },
                        }
                    ]
                ],
                child_script=summary_script(operation),
                required_events=["subagent.started", "subagent.finished"],
            )
        )
    cases.append(
        case(
            19,
            "permission",
            {"source.txt": "approved write\n"},
            [
                {"kind": "exact", "path": "approved.txt", "source": "e2e-19/source.txt"},
                changes("approved.txt"),
            ],
            "用 write_file 将 source.txt 的内容写到 approved.txt，等待客户端批准。",
            [[tool_write("approved.txt", "approved write\n")]],
            runner_type="ipc",
            required_events=["permission.requested", "permission.granted"],
        )
    )
    cases.append(
        case(
            20,
            "permission",
            {},
            [
                {"kind": "exact", "path": "receipt.txt", "expected": "denied\n"},
                {"kind": "absent", "path": "unauthorized.txt", "constraint": True},
                changes("receipt.txt"),
            ],
            "先申请用 write_file 向 ../boundary.txt 写入 changed。客户端将拒绝，拒绝后禁止绕过或再写外部文件，仅写本地 receipt.txt 内容 denied 加换行，然后结束。",
            [[tool_write("../boundary.txt", "changed")], [tool_write("receipt.txt", "denied\n")]],
            runner_type="ipc",
            permission="deny_outside",
            required_events=["permission.requested", "permission.denied"],
        )
    )
    for number, folder in [(21, "delivery"), (22, "archive")]:
        cases.append(
            case(
                number,
                "multi_turn",
                {"source.txt": "history respected\n"},
                [
                    {
                        "kind": "exact",
                        "path": f"{folder}/result.txt",
                        "source": f"e2e-{number:02d}/source.txt",
                    },
                    changes(f"{folder}/result.txt"),
                ],
                "现在读取 source.txt，按上一轮指定的目录和文件名完成逐字复制。",
                [
                    [
                        python_call(
                            f"from pathlib import Path; Path('{folder}').mkdir(exist_ok=True); Path('{folder}/result.txt').write_bytes(Path('source.txt').read_bytes())"
                        )
                    ]
                ],
                turns=[
                    f"记住约束：输出目录是 {folder}，文件名 result.txt。其他文件不可改。此轮不要执行，只确认。"
                ],
                required_events=["session.resumed", "session.waiting_for_input"],
            )
        )
    for number, content in [(23, "one\n"), (24, "two\n")]:
        cases.append(
            case(
                number,
                "reconnect",
                {"entry.txt": content, "journal.txt": "start\n"},
                [
                    {"kind": "exact", "path": "journal.txt", "expected": "start\n" + content},
                    changes("journal.txt"),
                ],
                "读取 entry.txt，追加到 journal.txt 恰好一次。客户端可能断线，不要重复追加，完成后结束。",
                [
                    [
                        python_call(
                            "import time; time.sleep(0.2); from pathlib import Path; p=Path('journal.txt'); p.write_text(p.read_text()+Path('entry.txt').read_text())"
                        )
                    ]
                ],
                runner_type="ipc",
                reconnect=True,
            )
        )
    return sorted(cases, key=lambda c: c["case_id"])


# 为七类故障各准备三个场景，运行器自动先生成无故障基线
def fault_cases(e2e):
    cases = []
    for kind in (
        "runtime_error",
        "rate_limited",
        "timeout",
        "schema_error",
        "provider_stream",
        "subagent",
        "mcp_disconnect",
    ):
        for index in range(1, 4):
            base = copy.deepcopy(e2e[index - 1] if kind != "subagent" else e2e[14 + index])
            base["case_id"] = f"fault-{kind}-{index}"
            base["name"] = f"{kind} scenario {index}: {base['name']}"
            base["tags"] = ["fault"]
            target = "write_file" if index == 1 else "bash"
            if kind in {"runtime_error", "rate_limited", "timeout", "schema_error"} and index == 1:
                base["script"] = [[tool_write("output/greeting.txt", "Hello WorkMate\n")]]
            if kind == "subagent":
                target = "spawn_agent"
            if kind == "mcp_disconnect":
                base["fixtures"] = [{"source": "e2e-01"}]
                base["graders"] = [
                    {"kind": "exact", "path": "mcp.txt", "expected": "local-mcp-value-37\n"},
                    changes("mcp.txt"),
                ]
                base["required_events"] = []
                base["goal"] = (
                    f"场景 {index}：调用 eval__lookup 读取唯一数据来源，将结果加换行保存到 mcp.txt。服务不可用时请报告失败，不猜测数据。"
                )
                base["script"] = [[{"name": "eval__lookup", "input": {}}]]
                # 后续脚本从真实 MCP 工具结果读取数据，断连时不会造出成功产物
                base["tags"].append("local_mcp")
                if index == 2:
                    write(ROOT / "fixtures" / "mcp-update" / "mcp.txt", "existing\n")
                    base["fixtures"].append({"source": "mcp-update"})
                    base["graders"][0]["expected"] = "existing\nlocal-mcp-value-37\n"
                    base["script"].insert(0, [{"name": "read_file", "input": {"path": "mcp.txt"}}])
                    base["tags"].append("mcp_append")
                    base["goal"] = (
                        "读取 mcp.txt 的现有内容，调用 eval__lookup，将返回值加换行恰好追加一次。不可用时报告失败，不猜测数据。"
                    )
                elif index == 3:
                    base["graders"] = [
                        {
                            "kind": "json",
                            "path": "mcp.json",
                            "expected": {"value": "local-mcp-value-37"},
                        },
                        changes("mcp.json"),
                    ]
                    base["tags"].append("mcp_json")
                    base["goal"] = (
                        "调用 eval__lookup，将返回字符串作为 value 字段保存到 mcp.json。不可用时报告失败，不猜测数据。"
                    )
                target = "eval__lookup"
            base["fault_spec"] = {"kind": kind, "target": target}
            cases.append(base)
    return cases


# 生成十个分散历史约束检查点与独立 A/B/C 对照任务
def compact_cases():
    cases = []
    for index in range(1, 11):
        cid = f"compact-{index:02d}"
        directory, field, order = (
            f"delivery_{index}",
            f"order_ref_{index}",
            f"ORDER-00{index:02d}-X9",
        )
        history = []
        constraints = [
            f"Constraint: 输出目录为 {directory}，输出文件 receipt.json。",
            "Constraint: 字段原名 old_order，稍后会修正。",
            "Constraint: 禁止修改 protected.txt 与 completed.txt，前序步骤已完成。",
            f"Constraint: 最终字段名已修正为 {field}，禁止使用 old_order。",
            f"Constraint: 订单 ID 必须逐字保留 {order}；金额整数为 {100000 + index}。",
            "Constraint: legacy.txt 方案已失败，禁止再次创建该文件；仅剩生成收据。",
        ]
        for block, constraint in enumerate(constraints):
            history += [
                {"role": "user", "content": constraint},
                {"role": "assistant", "content": "收到约束。"},
            ]
            for filler in range(8):
                history += [
                    {
                        "role": "user",
                        "content": f"历史资料 {block}/{filler}: "
                        + ("已讨论的背景资料，无新增要求。" * 60),
                    },
                    {"role": "assistant", "content": "背景资料已审阅，不改变约束。"},
                ]
        write(ROOT / "fixtures" / "checkpoints" / f"{cid}.json", history)
        write(ROOT / "fixtures" / cid / "protected.txt", "protected\n")
        write(ROOT / "fixtures" / cid / "completed.txt", "already done\n")
        expected = {field: order, "amount": 100000 + index, "status": "complete"}
        config = dict(
            case_id=cid,
            name=f"长上下文订单交付 {index}",
            category="compact",
            fixtures=[{"source": cid}],
            timeout_seconds=180,
            max_steps=20,
            graders=[
                {
                    "kind": "json",
                    "path": f"{directory}/receipt.json",
                    "expected": expected,
                    "constraint": True,
                },
                {"kind": "absent", "path": "legacy.txt", "constraint": True},
                changes(f"{directory}/receipt.json"),
            ],
            required_events=[],
            forbidden_changes=["protected.txt", "completed.txt"],
            tags=["compact"],
            compact_spec={"checkpoint": f"checkpoints/{cid}.json", "branches": ["A", "B", "C"]},
            script=[[tool_write(f"{directory}/receipt.json", json.dumps(expected))]],
        )
        config["goal"] = (
            "完成历史中剩余的收据任务，使用最终字段名、精确订单 ID 和金额，增加 status=complete。遵守所有历史约束。"
        )
        cases.append(config)
    return cases


# 分阶段构建 pilot 或完整固定任务集
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-only", action="store_true")
    args = parser.parse_args()
    e2e = e2e_cases()
    for item in e2e:
        if not args.pilot_only or "pilot" in item["tags"]:
            write(ROOT / "cases" / "e2e" / f"{item['case_id']}.json", item)
    if not args.pilot_only:
        for suite, cases in [("faults", fault_cases(e2e)), ("compact", compact_cases())]:
            for item in cases:
                write(ROOT / "cases" / suite / f"{item['case_id']}.json", item)


if __name__ == "__main__":
    main()
