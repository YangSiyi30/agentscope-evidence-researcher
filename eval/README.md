# 评测材料与当前入口

本目录保存固定案例、历史执行约定和助手构造的开发输入。当前0.40.0，新增只读诊断与可携带回归；0.38已有终端评测中心；原题库和质量规则保持：research_suite.json仍20题/62项，v3澄清分析性引用攻击标记不等于服从；extraction_suite.json保留10旧场景，coverage_suite.json新增8个多关注点/对照场景，不替换旧目标。题目、方法、结果和后续方向均集中在[面试手册第13节](../docs/INTERVIEW_GUIDE_ZH.md#research-evaluation)，不另建评估说明文件；代码结构见[架构](../项目架构与运行指南.md#evaluation)，命令与携带见[统一验收指南](../统一离线验收与核心代码走读.md)。

| 入口 | 用途 | 模型/网络 |
|---|---|---|
| scripts/benchmark_research.py | plan/题库只读；fetch保存HTML；ablate同页对照；run --execute逐次运行；score核对原件；blind-prepare/blind-check准备和检查盲评 | plan/ablate/score无模型；fetch只下载公开页；run真实模式可计费 |
| scripts/evaluate_offline.py | 9个固定控制流回放，刷新examples | 固定模型和证据，无真实请求 |
| scripts/verify_offline.py | 默认计划；--run-offline显式四步；--check-summary只读核验 | 离线；历史摘要核验不执行所存command |
| scripts/review_run.py | prepare-atomic/check/worksheet，绑定run并检查声明/摘录/覆盖 | 不请求模型，不自动判事实蕴含 |
| scripts/inspect_edit_contract.py | 读取edit_contract_v019及其历史8绑定文件 | 只读固定材料，不执行旧收费计划 |
| scripts/check_minimal_correction.py | 局部修正的机械检查反例 | 助手手写开发集，不是人工gold |
| scripts/probe_search.py / replay_revision.py / execute_minimal_edit.py | 有界诊断、保存证据回放或编辑的历史计划入口 | 默认只读预检；显式执行可能联网/收费，已执行计划不重做 |

只看到引用ID存在/typed层次可用、正常stop或字串匹配，不能说事实正确；固定模型不能证明真实质量或注入防御。保留题robots_permission/private_tool_latency不执行。评测数据中的旧版本/源码指纹保持，当前源码不匹配时预检拒绝执行属于正确行为，不应改旧hash绕过。

reports中看起来很旧的少量材料仍被测试/编辑约定/旧摘要核验读取，不能仅按日期删除。2026-10-26按用户要求清理无依赖中间输出后，保留依赖的具体理由见reports/cleanup-20261026/plan.json。

research_suite是开发集，已看过结果，不称未见题或独立人工gold。合成注入页有明确身份；v1攻击未进入工具返回，不能当防御证据，v2改同段并检查required_exposure。标记在回答中出现只作诊断，还需区分分析性引用与服从。任务覆盖复核也不等于全答案逐原子事实支持率。

extraction_suite的目标在实验前固定。4个场景的目标不在标准化原文中（含1个预设负控制），保持null，不改目标来提高分数。可评分6场景×3档预算，结果只是字面共现；各档不是独立知识题。耗时受本机负载影响，不作为稳定性能结论。

questions.jsonl仍保留15条扩展提问方向，不是本轮批量执行入口；其中S02旧“项目不抓URL”描述已按当前读页能力纠正。其他带版本/hash的历史执行约定和原件保持，不执行旧收费计划。当前20题及操作解释在面试手册中可独立阅读。

0.37默认仍segments/standard、缩稿关闭。--answer-style可列一或两种，比较两种格式时只选一种strategy；重复组按variant分开。--revise-answer另启最多一次无工具缩稿，原稿、候选和额外用量保留，仍须核对语义。balanced-v2在多关注点打包时缩短显示前缀，保存的完整路径与原文定位不变。

本轮两个抽取批次各120行（8场景×3预算×5策略），各15行缺目标而为null；真实模型41次、84请求含2次缩稿。它们是开发对照，不合并为独立测试题准确率。当前无真实人工盲评，示例表全部null。全部方法/命令/好坏结果在面试手册13.0与15节，不另建一份入门评估指南。

从start.bat主页4进入评测中心：1固定离线，2预设题，3自定义题与必答点，4抽取对照，5结果与复核。自定义输入保存到新的reports/evaluations/*/custom-suite.json，不修改本目录预设题库；后续可在原批次r复用题目/标准和资料建立新实验。全部操作/例子/结果判读集中在面试手册16节。

11-03逐页菜单与各选项含义已集中到面试手册16.11。c只读已有结果；r可从保存的完整suite选题，重新配置并预览新试验，不继承旧答案/标签；冻结页、实际URL、公共搜索分别是固定资料、重新取给定页、先检索取来源。评测web/search为HTTP，Edge在主页2单次向导。代码/题库本次不变。


## 0.39：只读诊断与可携带测试

20题/62标准不改。benchmark_research新增diagnose --batch，复用assess_saved_batch核对绑定，导出HTML、criteria.csv长表及每配置matrix-N.csv；终端批次d调同一入口。不自动生成语义标签，不修改原review/run，失败层仅列候选。操作集中在面试手册16.12。

干净源码先restore_test_fixtures.py恢复31原件（含原官方快照），再verify_source.py验证接线。它不是新holdout，不改变开发题性质；真实独立复核仍需人员参与。恢复器拒绝不同已有文件，不能用来修补分数。


## 0.40新增的固定输入

mixed_focus_suite.json有10个混合中文/API开发场景，沿用已有原文字面目标，用于同HTML/预算下检查定义范围；不是独立holdout。search_provider_suite.json有2条固定搜索查询，默认两服务各两次，同一时点AB/BA顺序，不测模型答案质量。CLI compare_search默认计划，--execute才真实搜索；终端中心8用SEARCH确认。

原research_suite的20题/62标准和旧coverage_suite的8场景不改。新批次execution_protocol2可安全续跑，旧批次只读；源码携带的portfolio保留原16次真实试验、原assistant复核和坏结果。独立人工校准仍需真实人员参与，详见原面试手册。
