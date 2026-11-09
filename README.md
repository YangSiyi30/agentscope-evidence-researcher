# AgentScope Evidence Researcher

给一个问题，在硬预算内搜索、读页、回答，并保存可复核的证据和 trace。基于 AgentScope 2.0.8，当前应用 **0.39.0**；只有一个研究 Agent、两个只读工具，不要求 LangChain/LangGraph。

```text
问题 → AgentScope Agent ↔ 模型
               ↓ 工具请求       ↑ 观察回流
          ResearchSession → 搜索 / 读页后端
          来源、预算、去重、原子提交
               ↓
       回答 + 证据 + trace → 保存 run
               ↓ 循环之外
       绑定核对 → 人工标准复核 → 诊断网页 / CSV矩阵
```

## 先跑起来

当前机器在项目目录执行 `.\start.bat`。主页1是离线演示；主页4是评测中心。真实模式要明确输入 LIVE，可能产生费用。

新机器有 Python 3.13 与 uv 后：

```powershell
uv sync --locked --extra browser
uv run --no-sync python scripts/restore_test_fixtures.py
uv run --no-sync as-research --interactive
```

第二条命令从随源码提供的约0.7 MB数据包恢复31份固定测试原件和网页快照，不下载网页、不调用模型；不同的已有文件会被拒绝覆盖。browser为可选静态Edge读取依赖，只有HTTP路径时可以不选；已有环境同步应保留已用的extra。

## 三分钟演示与可讲的设计

1. 主页1一路回车，观察搜索→读页→回答；结果页2看trace，6直接读JSON。失败也消耗后端尝试额度，被拦截的请求不消耗。
2. 主页4→1跑固定评测→批次选d，打开自包含诊断网页，查看待判定、标准与改进建议。fixture回答预写，不能拿它宣称模型准确。
3. 已有真实批次用c比较原回答，d导出逐项矩阵。要新做实验才用r；固定题目、材料、预算和一个变量，再确认LIVE。

代码重点是 [ResearchSession](src/as_researcher/researcher.py) 的资源许可与原子提交、[evidence.py](src/as_researcher/evidence.py) 的摘要/正文引用分层，以及 [diagnostics.py](src/as_researcher/diagnostics.py) 的只读诊断。引用存在不等于结论被支持，规则信号不等于已确认根因。

## 一个正结果与一个负结果

0.37历史小开发组：4题×2格式×2次，共16 trials，使用当时同一组冻结网页与预算。

| 指标 | standard | compact |
|---|---:|---:|
| 长度达标 | 3/8 | 6/8 |
| 原助手完整语义复核通过 | 6/8 | 6/8 |
| 门槛与语义同时通过 | 3/8 | 5/8 |

结论：精简提示在这组中改善长度，语义通过数没有改善，比较题仍会误读范围，因此保持 **segments / standard / 缩稿关闭**。四题是开发题，含一题合成注入，不是独立holdout或人工gold；新诊断还分别展示普通问答1/6对3/6、对抗题各2/2，避免混成一个“准确率”。完整正负结果见面试手册第15节。

## 验证与学习入口

```powershell
uv run --no-sync python scripts/restore_test_fixtures.py
uv run --no-sync python -m unittest discover -s tests -t .
uv run --no-sync python scripts/verify_source.py
```

本轮441项离线回归通过；实际测量应用源码的语句覆盖90.1%、分支83.3%，coverage综合88.0%。覆盖率说明哪些代码被执行，不能证明回答正确。已在独立安装、无真实.env的干净副本验证；GitHub Actions配置已加入，远程runner结果需发布后另看，不提前标CI绿。

| 想了解什么 | 读现有文件 |
|---|---|
| 从零操作与学习顺序 | [学习指南](从零上手与面试学习指南.md) |
| **项目结构、核心循环与函数关系** | [架构与运行指南](项目架构与运行指南.md) |
| 面试讲稿、80问、20题、所有评测与新诊断操作 | [面试总手册](docs/INTERVIEW_GUIDE_ZH.md)第16.12节 |
| SDK/Tavily/DDGS、对象和字段 | [概念与数据结构速查](项目概念与数据结构速查.md) |
| 新机器、CI、覆盖率、历史原件与交付 | [统一验收指南](统一离线验收与核心代码走读.md)第10节 |
| 来源与贡献 / 当前事实 / 后续任务 | [来源记录](docs/UPSTREAM_AND_CHANGES.md)、[STATUS](docs/STATUS.md)、[NEXT](docs/NEXT_STEPS_ZH.md) |

完整工作原件在本机reports；源码交付包不包含真实.env、.venv或全部reports，数据包仅恢复测试必需子集。缺少旧全批次时，不要把历史链接缺失当成需要重新收费运行。

限制：尚无真实独立人工校准、未见保留集结论或Tavily服务对照；不宣称生产级隔离、多用户服务、Agent断点恢复或整体领先。参考 [Local Deep Researcher](https://github.com/langchain-ai/local-deep-researcher)，[MIT归属](LICENSE)保留；实现与说明有AI协助，个人贡献应如实展示理解与验证。

本地Git基线已建立，远程发布未执行。本轮记录及当前源码交付位置见[STATUS](docs/STATUS.md)。

当前[0.39源码交付包](reports/release-v039/source-v039.zip)不含密钥、环境与全部工作原件；本机可直接打开[本轮诊断示例](reports/completion-20261109/style-diagnostics-v039/index.html)。
