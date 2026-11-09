# 2026-10-23 最终版整体检查与恢复记录

> 以下记录按当时日期保留；文件数量与名称不是当前导航。有关内容已合并，最新学习入口以根README为准。

用户要求连续完成重要改进，逐步留档，最终整理现有基础说明，以项目结构和函数调用关系为重点；不再新增同类入门说明文件。本文件仅为工作记录。保持AgentScope2.0.8、单Agent、搜索/读页两种业务能力，保留历史证据与原图，不改真实.env。

## 恢复入口

先读本文件、reports/finalization-v035/checkpoint.json，再查看当前源码与测试输出。完成项不要重做真实请求；启动记录/结果都保存在该目录。基线为baseline.json，历史reports/eval/SESSION/许可/原图/.env受保护；代码允许修复，最终另存差异。

## 范围与进度

1. 整体检查：已读主循环、模型、工具、搜索/读页/MCP、配置与历史缺口，继续覆盖评测/编辑辅助模块。最终版聚焦可靠执行、预算、证据一致性与可学习结构，不扩展框架/新Agent。
2. 模型失败诊断：计划保存每次适配器调用的安全状态/类别，失败不记为零用量；不记录远端原始错误、头、密钥或思考。旧0.34 BadRequest根因仍未知。
3. 证据事务：复现不合法元数据造成正文先提交、搜索坏项造成部分提交；验证后再提交完整观察与状态。
4. 配置/真实运行：核对模型参数、客户端关闭、URL边界，先离线后有界真实验证；不更换旧计划/hash。
5. 文档：最后重写/精简已有README、学习指南、架构指南、Agent Loop、概念速查及面试入口；按职责分工，历史按链接查，不向基础文档继续堆版本补丁。
6. 完成条件：重要故障回归、完整测试/Ruff/四步离线、旧摘要读回、受保护原件与独立源码快照，真实观察与程序检查分开记录。

## 初始观察

当前0.34.0，历史315测试；不能当作本次已重跑。模型只记录成功解析的响应，缺少安全请求错误诊断。read_page先写page_evidence后生成观察，selection_warnings=None等可能使观察失败但正文已保留。search_web边遍历边提交，坏对象可留下先前来源。以上需要最小复现和回归后再定修复。

2026-10-23查阅DeepSeek官方Chat Completions/Thinking Mode说明，确认工具与思考模式有协议要求；文档不能用来追溯判定0.34旧错误的具体原因。后续诊断依据仍须实际安全记录。

## 第一步：模型诊断与证据一致性（已完成）

ObservedOpenAIChatModel逐次记录适配器尝试（包括重试/取消），白名单保留HTTP状态/错误类别/参数名；远端正文与密钥不保存，SDK重试日志另做定点脱敏。模型失败或重试后成功但失败用量未知时，总用量null；成功响应的已知用量仍可单独查看。新增model_diagnostics.py，run schema8/report renderer5，旧renderer1～4保持。主模型增加显式--model-thinking，默认provider-default，disabled只接受既有已验证官方DeepSeek范围；不自动切换。后端构造先于客户端，避免无效搜索配置时创建无用客户端。

search_web先暂存完整批次再提交；read_page先校验13项元数据类型/有限JSON树、深拷贝并生成可序列化观察，再提交正文。异常元数据统一invalid_page_metadata；未知reader错误文案统一page_reader_error。端口0不再被归一化为默认端口。旧可编辑review schema1错误类型返回invalid_*，不抛AttributeError/TypeError。

reports/finalization-v035/atomic-before-after.json从冻结0.34源码重现：失败搜索残留来源1→0；失败读页残留正文1→0。全部为固定输入，无网络。boundaries-final.txt为11项新回归通过；full-first.txt为324项首轮回归（3项新报告文字/版本断言不匹配，已更新，待完整重跑）。原始旧报告没有回写。

复现器首次误把sources字典的URL键当source_id，读页被unknown_source_id提前拦截；改用Source.id后重新执行，才得到上述旧版残留1/新版0的对照。该错误仅在临时复现命令，不在生产代码，两个版本各自实际读页后比较。

## 第二步：完整回归和一次有界真实任务

应用版本0.35.0；uv lock/sync均离线，仅自身版本更新。新增CLI思考选项传递/客户端关闭及非live提前拒绝测试，累计13新回归；tests-code-final.txt为328测试通过，ruff-code.txt/format-code.txt为96文件通过。

冻结live-plan.json绑定源码af48ceba16dffd48e9c11f9aaec33fc690c30abeb36a11a260416023acecdf83；一次真实完整CLI题目为Python3.13 asyncio.gather，0搜索/1页/MCP/Edge/主模型disabled，最多3适配器尝试、零重试/缩稿，700字符/1800输出token/90秒。不使用两个保留题。live-executor/launch/exit/stdout/stderr/run/report保存。实际2次正常模型响应；MCP后端ConnectTimeout，1读页尝试、0正文；模型诚实拒绝无证据断言，260字符，无引用，strict退出3。tokens输入3033/输出202。不是实时研究成功，也不能用短字数判质量通过。不重复付费任务。

在后续调整错误传递前已冻结live-executed-source.zip；这次实际源码与最后交付源码分别保留，不替换历史计划。发现MCP后端超时到模型观察只剩MCPBackendError，下一步统一读页网络错误类别，并用模拟HTTP/协议及不调用模型的组件探针验证。

## 第三步：读页网络分类与独立组件（已完成）

page_error_code统一HTTP超时/连接/状态错误；服务器用相同安全类别返回，MCP桥保留已知PageReadError，协议/传输另记page_transport_error；本地/服务器/桥接六类错误一致，无原始错误文案和自动重试。新增1回归（总新增14），原运输错误测试需更新为新稳定类别，RPC中原安全RuntimeError与层次仍保留。首轮329测试有该一项旧断言失败（tests-final.txt）；修正后mcp-parity-final.txt五项通过，待最后全套重跑。

network-component-plan/results记录0模型：三官方HTML均成功；一次DDG返回1结果（不是权威性/稳定性保证）；Edge固定HTML抽取通过。mcp-browser-component-plan/results另做一次0模型/1页实际MCP+Edge官方HTML，保存7000字符/12片段，RPC成功。它们与之前付费整链失败分别记录，不能合并成端到端成功，也不能确定最初ConnectTimeout根因。页面原文片段另存mcp-browser-page.json。

## 第四步：统一现有说明（已完成初稿）

重写既有README、学习指南、架构、Agent Loop、概念速查；架构添加整体结构、MCP时序、证据提交流程三图，Loop另有循环时序，共4图。逐项覆盖19个源码模块（含包标记）及关键函数/对象/调用者，Python/消息/JSON基础结合项目从零解释。面试/交付/验收/贡献/状态/后续说明同步精简，旧专题与SESSION作为历史保留。未新建综合入门指南，只有本SESSION为本轮工作记录。

## 第五步：最后验收（已通过）

tests-release.txt：329测试41.352秒通过；ruff-release/format-release：检查通过、96文件格式一致。相比旧315新增14测试，不新增无关框架。四步offline-delivery-v035/summary.json全部passed_program_checks_only：9回放、真实stdio但固定模型/证据链、8历史绑定/9编辑行、交付审计（1443文件/1108本地链接/94份可重现报告）。0.34只读核验matching_frozen_source_archive，0.35为matching_workspace_source，两者verified/0模型/0子进程。

scope-check.json：1246份受保护原件字节保持、101第三方完整lock条目保持；19个源码模块（含包标记），修改10个现有生产模块，新增model_diagnostics，另更新观察器/回归断言。production-diff.txt对比冻结0.34生产源码。README/STATUS/面试交付等同步当前结果，eval/README仍作为原件保留并标为历史入口，不用其中旧当前提示代替STATUS。

release-v035源码候选先用于完整审计；最终在全部记录更新后刷新并冻结同一新归档，旧0.34及更早ZIP始终不改。源码包250载荷（旧白名单247＋新模块/测试/本SESSION），不含真实.env/依赖/reports。下一步只需最终归档字节检查与审计完成标记，不应重复真实任务或已通过测试。

最后人工复核输入补查发现：旧schema1若supported证据的source_id误写成列表，后续集合比较仍会抛TypeError。已在既有类型回归内补这个反例，入口增加source_id字符串校验。该补查只影响离线人工复核，主循环未改。先前summary.json为最终化中的候选检查，绑定修改前源码；保留该原文件/候选读回，并另存source-before-final-review-guard.zip及清单。最后全套与四步将另存summary-final.json，主要说明全部指向此最终摘要，绝不回写候选hash。

## 最终收尾结果

最后source_id保护后tests-release-guard.txt为329项/41.498秒全部通过，Ruff/96文件格式通过。最终四步摘要为reports/offline-delivery-v035/summary-final.json，全部passed；readback-v035-final.json为verified/current workspace，旧0.34读回仍按冻结源码。先前summary.json与readback-v035-candidate.json仅是修复前中间候选，不作为当前源码证明；其独立源码副本保留，只读入口会按指纹拒绝不匹配。

最新说明均指最终摘要。最后源码ZIP在此记录更新后刷新，250文件逐字节与工作区/manifest核对；历史保护/依赖/应用版本/测试/摘要/真实与组件结果/19模块文档覆盖/4图及交付审计汇总到[final-verification](../reports/finalization-v035/final-verification.json)，最终状态见[checkpoint](../reports/finalization-v035/checkpoint.json)。没有新的待实现重要功能；外部服务稳定性、语义真实性与生产部署隔离属于已写明的边界，不标成程序已解决。

用户后续学习应先看架构图和模块表，再沿main→run_research→Agent→Session工具→backend→观察→下一轮模型解释。当前阶段不继续堆框架/工具/新基础文档；维护只由可复现缺陷或明确新需求触发。新真实请求必须新计划，不能重做本轮唯一收费任务；旧.env/图片/SESSION/原件与旧源码包保持。
