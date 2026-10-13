# 长上下文 A/B/C 可复现评测

本目录包含原有 e2e/fault/compact 回归套件，以及隔离后的 long-dev、long-formal。默认使用 fake/scripted provider，只验证框架；真实模型调用必须显式指定 --live --provider anthropic、预算上限和停止条件。

历史结果 /home/ubuntu/gcl/workmate-eval-results/compact-deepseek-flash-1x-v1/20260915T100927-8044c06a 只读保留，不会被新入口读取或覆盖。历史 94.40% 仅表示“首个续跑完整请求 JSON 的 UTF-8 字节数÷4（向上取整）”的平均估算降幅，不含摘要开销，不代表全程 Token 或费用下降。

## 数据集与比较规则

- long-dev：5 个合成开发任务，五类各 1 个。
- long-formal：30 个相互独立的合成任务，需求变更、精确信息、多文件代码、长流程续做、资料处理各 6 个；短/中/长上下文各 10 个。
- 历史含合法配对的 tool_use/tool_result、来源核对、需求变更、失败方案、已完成副作用、例外和干扰记录；关键事实分布在早/中/晚位置。所有任务都标记为 synthetic，不冒充真实项目轨迹。
- 正式计划固定为 30 task × 3 repeat × 3 branch = 270 trials。每个 trial 从同一只读 fixture 复制到全新工作区。
- A 使用完整历史；超过配置窗口时记 unsupported_context，不混入普通配对比较。
- B 每个 repeat 重新调用生产 Compactor；摘要失败保留为 compaction_failed，不补跑。
- C 取“不超过该 repeat 的 B 首请求实际估算大小”的最长合法历史后缀，不留下孤立 tool_use/tool_result。最小合法请求仍超预算时 budget_matched=false；B 未产生预算时 C 记 dependency_failed 且不进入配对推断。
- 奇数 repeat 的实际顺序为 A/B/C，偶数为 B/C/A；B 必须先于依赖它的 C。统计种子和实际顺序写入 manifest。
- 模型、工具、fixture、步数、超时和 grader 在三分支间相同。系统提示、工具 schema 与当前任务均计入首请求，且不参与历史截断。

build_long_context.py 是确定性生成源。cases/long_dev/FROZEN.json 与 cases/long_formal/FROZEN.json 保存数据集版本、案例、fixture、检查点及关键评测代码的 SHA-256。运行前会 fail closed 校验冻结状态；修改后必须提升版本并重新冻结，不能覆盖旧报告或按结果删题。

## OS 隔离边界

任务工具在每次调用时进入 Linux Landlock ABI 4 + x86_64 seccomp 沙箱：

- 只允许读写当前 trial workspace；评测器私有案例、grader、完整检查点、运行配置、其他分支和其他 trial 均未授权。
- 系统运行库只读；Shell/Python 进程继承最小环境，任务环境没有 API key。
- seccomp 拒绝 socket、ptrace、process_vm 和 pidfd_getfd；默认不能联网，也不能读取宿主 /proc。
- read/write/list/bash 以及所有嵌套 SubAgent 使用同一沙箱工具；未注册宿主 RAG 或 MCP。带 local_mcp 标签的旧故障案例只连接显式合成服务，不构成长上下文资料通道。
- Provider 和隐藏 grader 位于可信评测器侧。模型调用可联网，但任务工具不能；完整 evaluator request 位于 workspace 外的 evaluator-private-request.json，Agent 无权限读取。
- Landlock/seccomp 不可用或主动测试失败时，suite 在创建试次前终止，不降级为 cwd、提示词或 Python 路径检查。

启动时主动使用独立哨兵验证：正常读写与 Python 命令、文件工具绝对路径和 ../、符号链接、其他 trial、Shell/Python 私有读取、父进程 /proc、API key 环境、TCP 与 UDP。结果写入 isolation/isolation.json。这证明列出的攻击面在当前内核配置下被阻止，不声称排除了所有内核逃逸。

## 指标、账本与统计

每个 Provider 调用保存本地 call ID、可用时的服务 request ID、phase、请求 SHA-256、单调时钟耗时、原始 usage、标准化 usage、失败和潜在不可观测消耗。账本覆盖 B 摘要、全部续跑调用、子 Agent 和可观测失败；SDK 内部重试、响应丢失及“重新读取导致的额外成本”若不可观测，保持 null 并写明限制。

标准化字段分开保存 ordinary input、cache read、cache creation、output 和 provider 可用时的 reasoning；total_input_tokens 按 Anthropic 当前三个互斥输入字段求和。任何字段缺失都为 null，不填零、不用估算冒充实际值。报告同时给出 usage 完整率、完整子集、失败/超时前账本、准备/续跑/grader 耗时和终止原因；无可靠价格映射时不输出费用。

质量以 task 为聚类单位：先在同一 case/repeat 内计算配对差，再对同一 task 的 repeats 求均值，最后以固定种子做 10,000 次任务级 percentile bootstrap 95% 区间。B−A 暂定非劣界限为 5 个百分点，正式 live 必须通过 --confirm-quality-margin-pp 明确确认。区间下界未达到 -margin 时只报告证据不足或不支持，不能把“无显著差异”写成“质量无损”。B−C 仅在区间下界大于 0 时报告支持 B 优势。

首请求缩减是辅助估算，报告分别给出逐 trial 比例的算术分布和汇总字节估算后的总体比例，不与全程实际 usage 混用。资源差异只用 usage 完整的配对子集，并报告覆盖率和潜在偏差。

## 离线验证命令

    # 校验冻结数据，不启动 Agent、grader 或 Provider
    .venv/bin/python -m evals.run_eval --suite long-dev --dry-run
    .venv/bin/python -m evals.run_eval --suite long-formal --trials 3 --dry-run

    # 5 × 1 × 3 = 15 个 scripted 框架试次；不是模型效果
    .venv/bin/python -m evals.run_eval --suite long-dev --trials 1

    # 相关回归
    .venv/bin/pytest -q tests/evals

    # 从原始 trials.jsonl 确定性重建
    .venv/bin/python -m evals.report /tmp/workmate-evals/<run-id>

CLI 退出码：全部验收通过为 0；存在 failed/invalid 为 1；配置错误为 2；Ctrl+C 为 130。scripted 的 C 分支可按设计失败，因此框架自测退出码可能为 1。

## 真实模型命令与预算门禁

本次实施没有执行以下命令。调用前请按账户价格、计划模型和可接受 Token 上限确认预算；入口不会读取 .env，也不会因检测到 key 自动运行。

开发集一次 repeat 共 15 trials，最少约 20 次模型调用（15 次续跑首调用 + 5 次 B 摘要），最多 305 次：

    ANTHROPIC_API_KEY=... .venv/bin/python -m evals.run_eval \
      --suite long-dev --trials 1 --live --provider anthropic \
      --model claude-sonnet-4-6 \
      --max-live-trials 15 --max-live-calls 305 --stop-input-tokens <TOKEN_CAP>

正式集固定 270 trials。若每个分支一次续跑，约 360 次调用（270 + 90 次摘要）；按 20 steps 的保守理论上限为 5,490 次。真实值取决于任务实际步数：

    ANTHROPIC_API_KEY=... .venv/bin/python -m evals.run_eval \
      --suite long-formal --trials 3 --live --provider anthropic \
      --model claude-sonnet-4-6 --confirm-quality-margin-pp 5 \
      --max-live-trials 270 --max-live-calls 5490 --stop-input-tokens <TOKEN_CAP>

--max-live-calls 是硬调用上限；--stop-input-tokens 使用已观测实际输入 Token，在达到阈值后停止下一次调用。usage 丢失时 live suite 也会停止，manifest 保存 stop reason、已完成数量和未完成计划。Token 费用只能在运行后按账本中普通/缓存输入和输出分别乘以运行当日、对应模型的官方计费规则计算；代码不硬编码价格。

## 证据包

每个新 run 独占 <output-dir>/<timestamp>-<uuid>/：

    manifest.json        # Git/dirty、配置、冻结输入哈希、统计种子、实际顺序、停止状态
    isolation/           # 主动越界测试与哨兵证据
    trials.jsonl         # 原始逐试次结果，逐行 flush + fsync
    summary.json/.md     # 可从原始记录重建的机器/人工报告
    events/              # 脱敏 bus 事件；不含 API key
    traces/              # 请求/响应 trace，可能含任务内容但不含凭据
    artifacts/           # 每试次独立 workspace，以及 workspace 外的评测器私有运行记录

运行产物可能包含任务内容和模型响应，应按内部评测资料管理；不要公开上传。只有 live 报告可以支撑真实 Agent 质量或资源主张。见 AUDIT.md、VALIDATION.md 和 RESUME_TEMPLATE.md。
