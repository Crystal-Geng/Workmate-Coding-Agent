# 长上下文评测代码审查

审查日期：2026-09-23。历史结果目录已确认存在并保持只读：
/home/ubuntu/gcl/workmate-eval-results/compact-deepseek-flash-1x-v1/20260915T100927-8044c06a。

## 已证实的问题

| 位置 | 原行为 | 风险与处理 |
| --- | --- | --- |
| evals/run_eval.py | evaluator request 写入完整 case（含 graders、script）和 fixture/checkpoint 绝对路径；worker 与任务工具共享宿主文件系统。 | Agent 可通过文件或 Shell 读取私有资料和其他 trial。现在 request 留在 workspace 外，所有任务工具由 Landlock+seccomp 强制限制；启动先做主动越界测试。 |
| evals/runners/agent_runner.py | 直接使用生产 read/write/list/bash 和 SpawnAgent 子注册表。 | cwd 与 Python 路径检查不是安全边界。现在根 Agent 与递归 SubAgent 都覆盖为 sandbox_tools。 |
| evals/runners/compact_runner.py | A/B/C 没有正式的合法消息截断检查；C 的预算匹配和 B 失败规则不完整。 | C 现在仅选合法后缀，显式记录 budget_tokens/budget_matched；B 无预算时 C 记 dependency_failed，不补跑。A 超窗单列。 |
| evals/runners/providers.py | 账本只足以支持首请求估算；usage 缺失可能在汇总时看成零，摘要、失败、缓存和请求标识不完整。 | Provider 边界记录摘要/续跑/子 Agent 的逐调用 ledger、请求哈希、服务 request ID（若提供）、原始/标准化 usage、耗时和不可观测标记；缺失保持 null。 |
| evals/report.py | 主要给成功率、简单配对差和首请求估算，没有任务聚类区间、全程资源差、usage 覆盖和正式结论规则。 | 新增 task-level paired cluster bootstrap、固定种子、非劣判定、完整 usage 子集、资源/耗时差、严重违规、基础设施故障和确定性重建。fake 结论固定为 framework_self_test_only。 |
| evals/cases/compact | 10 个字段替换型任务不足以支撑 30 个独立正式任务。 | 新增 5 个开发任务、30 个正式任务和冻结哈希；五类任务及三种长度均衡覆盖，全部明确标为合成。 |
| 历史报告解释 | 94.40% 来自完整首请求 JSON UTF-8 bytes/4 的平均估算降幅，未计摘要。 | 新报告保留该辅助口径但同时给出算术分布与汇总比例，不把它写成全程 Token 或费用节省。 |

## 可复用能力

- 生产 Compactor、AgentRunner、Session/Store、EventBus、TraceWriter 和 SubAgent 调用链保持不变。
- 既有 case schema、fixture 复制、grader、逐行 JSONL + fsync、Git/文件哈希、fake provider 和离线故障测试继续复用。
- 现有 EstimatedTokenCounter 继续只用于同方法的首请求估算；实际成本优先使用 provider usage。
- 隐藏 grader 仍在任务结束后由可信评测器运行，不复制进 workspace。

## 环境事实与边界

当前主机是 x86_64 Linux 6.8，Landlock ABI=4。bwrap、Docker、Podman 不存在；无特权 user namespace 的 unshare 失败。因此采用最小可用的 Landlock 文件规则加 seccomp syscall 过滤，而没有新增容器依赖。隔离不可用时 verify_isolation 抛错，正式评测 fail closed。

当前评测没有注册 RAG 或宿主 MCP；旧 local_mcp 故障案例使用显式合成本地服务。主动测试记录这一事实，但不把“当前未注册”表述成所有未来外部工具都已隔离。若以后向任务注册新工具，必须先让其服从同一边界并增加攻击测试。

Provider 位于可信 worker，需要 API key 和网络；模型可请求的 read/write/list/bash 实际执行在无 key、无 socket、仅 workspace 可见的子进程。评测器私有 request、grader、检查点、日志和其他 trial 不在允许规则中。隐藏 grader 在 Agent 完成后运行。

## 尚未消除的不确定性

- 生产 AnthropicProvider 内部流重试没有逐 attempt usage；响应丢失可能已经计费但服务未返回 usage。账本将这类成本标为潜在不可观测，不伪造数值。
- provider cache 的实际命中由服务端决定；只记录返回字段，不声称控制缓存。
- Landlock/seccomp 的主动测试覆盖已列攻击面，不是形式化证明，也不防御未知内核漏洞；当前 syscall 表仅支持 x86_64。
- 数据集没有可合法复用的真实 WorkMate 长会话，因此全部采用明确标注的合成任务。真实外部效度需要另行数据治理，不得把本集合包装成真实项目轨迹。
- 本次未授权 live 调用，所以尚无真实模型质量、全程 Token 或费用结论。

## 最小实现与验收映射

1. OS 隔离：sandbox.py、sandbox_tools.py、isolation.py；验收为哨兵攻击测试全部通过且正常工具可用。
2. 公平分支与账本：compact_runner.py、providers.py、worker.py、run_eval.py、schema.py；验收为合法截断、B→C 依赖、null usage、摘要入账和停止条件测试。
3. 数据集冻结：build_long_context.py、long_dev、long_formal、长上下文 grader；验收为 5/30 数量、五类/三长度分布、270 计划与 SHA-256 校验。
4. 可复算报告：report.py；验收为固定数值 bootstrap、fake 禁止效果结论、原始 JSONL 重建哈希一致。
5. 回归：tests/evals 加相关 unit 组合；不改与任务无关的生产行为。
