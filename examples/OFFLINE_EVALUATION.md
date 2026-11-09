# 离线回放结果（模拟数据）

使用真实 AgentScope 2.0.8 Agent / Toolkit，模型与证据为脚本预置。
这是控制流回归验证，不是模型质量、事实准确率、提示注入防御成功率或真实成本测试。

| 场景 | 规则检查 | 后端搜索尝试 | 模拟模型调用 | 停止原因 | 未知引用 ID 数 |
|---|---|---:|---:|---|---:|
| normal | PASS | 2 | 3 | agent_completed | 0 |
| budget_1 | PASS | 1 | 2 | search_budget | 0 |
| duplicate | PASS | 1 | 3 | duplicate_query | 0 |
| duplicate_ablation | PASS | 2 | 3 | no_new_evidence | 0 |
| empty | PASS | 1 | 2 | no_new_evidence | 0 |
| search_error | PASS | 1 | 2 | search_error | 0 |
| invalid_citation | PASS | 2 | 3 | agent_completed | 1 |
| injection | PASS | 2 | 3 | agent_completed | 0 |
| no_tool | PASS | 0 | 1 | agent_completed | 0 |

## 能说明什么

固定的重复查询样例中，开启查询早停后后端搜索尝试由 2 次降到 1 次；模型调用仍为 3 次。
关闭查询早停时，第二次搜索之后仍会触发‘无新增证据’停止。因此不能声称无限循环或通用成本下降 50%。
injection 场景仅验证恶意文本可以作为数据穿过工具接口；脚本模型不会执行恶意指令，不能用它证明真实模型安全。
invalid_citation 的 PASS 表示成功发现故意植入的错误引用，不表示该报告可以交付。
