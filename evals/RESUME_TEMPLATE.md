# 真实测评后简历成果模板

以下内容只能在完成 live 正式评测后，用对应证据包中的数值填写；方括号不得用 scripted/fake 结果替换。

## 精简版

- 设计并实现 WorkMate 长上下文 A/B/C 配对评测：在 [30] 个冻结任务、[3] 次重复上，以 OS 沙箱隔离评测私有资料，并用任务级聚类 bootstrap 报告质量与资源区间。
- 在 usage 完整率 [X/Y] 的真实模型试验中，摘要压缩 B 相对完整历史 A 的全程输入 Token 差为 [均值与 95% CI]，成功率差为 [百分点与 95% CI]；按预先确认的 [5pp] 非劣界限，结论为 [支持/不支持/证据不足]。
- 相对同首请求预算的合法截断 C，B 的成功率差为 [百分点与 95% CI]，关键约束保持率为 [X/Y]，严重违规 [N] 次；首请求估算降幅 [算术平均] 与 [汇总比例] 分开报告。

## 证据引用

- 数据集版本与冻结摘要：[dataset_version] / [sha256]
- Git commit 与 dirty 状态：[commit] / [dirty]
- Provider 与模型：[provider] / [model]
- 计划、完成、invalid、基础设施故障：[planned] / [completed] / [invalid] / [infra]
- usage 完整率及潜在偏差：[coverage] / [limitations]
- 正式报告路径或内部归档编号：[evidence_package]
- 运行日期、统计种子、非劣界限：[date] / [seed] / [margin]

不得写“质量无损”来替代证据不足，不得把首请求 bytes/4 估算写成全程 Token 或费用节省；价格结论必须附运行当日模型计费规则和完整 usage 覆盖。
