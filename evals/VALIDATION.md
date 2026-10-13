# 长上下文评测实施验证记录

验证日期：2026-09-23。全部验证均为离线 fake/scripted；没有调用真实模型 API、没有执行正式 270 次测评、没有使用付费外部服务。历史结果目录未修改。

## 最终自动测试

相关完整回归：163 passed in 51.82s。

    .venv/bin/pytest -q tests/evals \
      tests/unit/test_runner.py tests/unit/test_loop.py \
      tests/unit/test_tool_registry.py tests/unit/test_invocation.py \
      tests/unit/test_tool_retry.py tests/unit/test_llm_provider.py \
      tests/unit/test_spawn_agent_tool.py tests/unit/test_permission_manager.py \
      tests/unit/test_session_store.py tests/unit/test_session_manager.py \
      tests/unit/test_compactor.py tests/unit/test_event_bus.py \
      tests/unit/test_event_writer.py tests/unit/test_trace_writer.py \
      tests/unit/test_tracing_provider.py tests/unit/test_mcp_tool.py \
      tests/unit/test_socket_client.py

最终静态检查：

    .venv/bin/ruff format evals src/workmate/core/llm/types.py \
      src/workmate/core/llm/provider.py tests/evals tests/unit/test_llm_provider.py
    .venv/bin/ruff check evals src/workmate/core/llm/types.py \
      src/workmate/core/llm/provider.py tests/evals tests/unit/test_llm_provider.py

结果：49 files unchanged；All checks passed。

## 隔离测试

当前 Landlock ABI 为 4。verify_isolation 在任何 suite 执行前运行，13 项检查全部通过：

- 正常 workspace 读、写、Python 命令；
- 文件工具拒绝绝对路径、父目录、越界符号链接和其他 trial；
- Shell 与 Python 拒绝读取 evaluator-private sentinel；
- 拒绝读取父进程 /proc 环境；
- 任务 Shell 不含 ANTHROPIC_API_KEY；
- TCP 与 UDP socket 均被拒绝。

tests/evals/test_isolation.py 在完整回归中再次主动执行同一边界。覆盖事实写入每个 run 的 isolation/isolation.json；不宣称证明所有内核逃逸不可能。

## 数据集与冻结

- long-dev：5 个任务，五种 task_type 各 1 个。
- long-formal：30 个任务，五种 task_type 各 6 个；short/medium/long 各 10 个。
- 每个任务有独立 case ID、fixture、检查点、隐藏验收和 synthetic 来源元数据。
- 检查点包含任务相关工具结果、需求变化、失败记录、已完成副作用、来源冲突和例外，不靠同一句无关文字重复。
- 正式计划为 30 × 3 × 3 = 270 trials。
- 两套 FROZEN.json 已按 long-context-v2 重建；运行前逐文件 SHA-256 与汇总 digest 校验。

配置验证：

    .venv/bin/python -m evals.run_eval --suite long-dev --dry-run
    # Validated 5 cases

    .venv/bin/python -m evals.run_eval --suite long-formal --trials 3 --dry-run
    # Validated 30 cases

## scripted 开发集冒烟

命令：

    .venv/bin/python -m evals.run_eval \
      --suite long-dev --trials 1 \
      --output-dir /tmp/workmate-long-dev-final

证据目录：

    /tmp/workmate-long-dev-final/20260923T040927-c28212a7

结果：15 个 A/B/C trial 全部落盘，无 invalid、无基础设施故障。A 5/5、B 5/5、C 0/5；C 的失败是 scripted provider 用来证明截断会丢关键事实的框架自测，不能解释为真实模型效果。报告中的 B_vs_A 与 B_vs_C 结论均为 framework_self_test_only。

fake provider 不返回 usage，因此 usage 完整率为 0/15，ordinary/cache/output/reasoning/total 均为 null，没有被填成零。逐调用 ledger 仍保留 30 个调用的 phase、输入哈希、耗时和潜在不可观测标记。

## 报告重建

对上述 run 执行：

    sha256sum <run>/summary.json
    .venv/bin/python -m evals.report <run>
    sha256sum <run>/summary.json

重建前后 SHA-256 均为：

    9c954bceda9f27eef5f317076e6af96e1446cdc6c168b76a3cdb4cba8f3f9153

这验证 summary 可由 manifest.json + trials.jsonl 确定性重建。bootstrap 固定数值测试还验证相同种子产生相同 95% 区间和非劣判定。

## 已验证的失败与账本语义

- failed、timeout、compaction_failed 和 budget/dependency 终止不会被静默删除。
- B 无首请求预算时 C 记 dependency_failed、comparison_eligible=false、infrastructure_failure=false。
- A 超窗口记 unsupported_context 并从普通配对推断排除。
- C 截断保持合法 tool_use/tool_result 配对；无法满足预算时显式 budget_matched=false。
- B 摘要调用以 phase=summary 进入同一 usage ledger，不与 continuation 重复累计。
- Provider request ID 在 SDK 返回时透传；本地 call ID 和请求 SHA-256 始终记录。
- 基础设施故障单列为 invalid；任务困难、摘要失败和任务超时保留为实验结果。
- live call/token cap 和 usage-observability stop reason 写回 manifest，即使实验未完成也生成报告。

## 尚未完成

未执行任何 live 开发集或正式集，因此没有真实模型成功率、非劣结论、B 对 C 优势、实际全程 Token、缓存效果或费用结论。README 中的 live 命令必须由用户先确认模型、Token cap、账户计费规则与 5pp 非劣界限后运行。
