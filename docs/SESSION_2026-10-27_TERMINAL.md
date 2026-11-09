# 2026-10-27 终端交互与结果查看

> 以下记录按当时日期保留；文件数量与名称不是当前导航。有关内容已合并，最新学习入口以根README为准。

## 需求与范围

用户希望把运行集中在终端：先选离线/真实与设置，再输入问题或使用预设，直接看到回答，快捷查trace/run，并同步已有说明。先读README、学习指南、架构、Loop、速查、STATUS、维护/清理记录，再检查cli/main、保存格式、预算和离线验收契约。

本次只完善输入/展示与单次执行入口，不引入Web前端、LangChain/LangGraph、额外Agent或新依赖。应用元数据与依赖维持0.35.0/AgentScope2.0.8，用本轮日期和新源码指纹区分接口修订；旧0.35归档/摘要不覆盖。真实.env、模型/工具/证据核心实现与原学习图保持。开始前在reports/terminal-20261027保存不含.env的before.zip、关键hash和checkpoint。

## 第一步：共享一次任务的执行器

cli.main拆为create_parser、execute_args、result_exit_code，原异步execute和save_run继续处理同一链路。execute_args返回(run,目录)，普通命令仍打印metrics JSON；--interactive/-i或普通终端无参数进入菜单。显式菜单模式只能额外给--output-dir，拒绝把--live等参数悄悄忽略；自动化无参数仍是原默认离线任务。

## 第二步：菜单与只读查看

新增terminal.py：run_terminal管理菜单，collect_task只组装参数；显示模式/预算/等价命令后才运行。默认离线1搜/1页；真实读页0搜/1页、120秒/700字符/1800输出token、零重试/不开缩稿，并要求输入LIVE。支持local/MCP、真实HTTP/Edge/无正文、有限自定义预算、预设/自定义问题与最多3个来源URL。

inspect_run直接展示保存回答、来源、警告和程序计数；show_trace展示应用trace及已有模型/MCP元数据。open_local用系统默认程序打开固定的report.md、run.json或目录，不拼Shell。没有关联程序时给绝对路径。显示外部文字前移除终端控制字符，不修改保存原件。

browse_history递归列最近20个可读run，先限20MiB/检查必要字段，损坏文件跳过；只读、不load_dotenv、不构造模型。每题新Session，菜单while不是Agent Loop，run也不是续跑检查点。Windows start.bat自动定位目录，以UTF-8调用现有.venv，不安装依赖。

## 第三步：实际操作中修正边界

- 重复搜索预设若预算1会在第二请求前停止；现在明确询问是否改为2，回归核对2条trace/1后端尝试/第二条拦截。
- 0页时预设切到固定搜索摘要，不向未注册读页工具发请求；0搜索时用登记URL的固定读页；两类预算同时0则返回设置，不发起任务。
- q/EOF/Ctrl+C、坏数字、非法/空URL、设置取消、无证据strict3、默认程序打开失败、坏历史记录都需保留明确提示；终端正常退出返回最近实际任务码，查看历史不改变它。
- 初次沙箱测试遇到临时目录删除权限，以及Windows asyncio回环socketpair卡在事件循环初始化。没有改业务来绕过：改用项目内临时目录、在允许本地回环的沙箱外跑固定测试；诊断后停止了卡住的本次进程。非公共网络故障，不是模型/搜索问题。

## 已实际完成的操作

用真实终端执行start.bat，依次选择离线/MCP/默认预算/默认题/开始，直接看到回答；再选trace、回主页、历史选同一题，最后退出0。保存原件为reports/20261027T070136-0ed8fafa/run.json和report.md：2应用工具、1搜索、1读页、1正文、2RPC、3固定模型调用、strict0。未运行真实模型、公共搜索、公共网页或Edge。

首批12项终端回归1.523秒通过；随后补0页/0搜索和零工具预算两项。完整测试、静态检查、四步验收、源码/文档同步与最终状态以[本轮结果](../reports/terminal-20261027/result.json)和[checkpoint](../reports/terminal-20261027/checkpoint.json)为准；未完成时checkpoint会列下一步。

## 文档与下次维护

在已有13份主文档内补操作、入口函数、20模块结构与关系图、菜单/CLI默认值区别、trace与历史只读边界，并更新演示/交付/恢复入口。不新增综合指南。旧专题及历史结果仍按原日期理解；10-25真实样本的研究核心保持，但不能说旧样本验证了本次菜单源码。

下次先看STATUS、本记录和checkpoint，已完成项不重做；只因新可复现的使用障碍继续小步改。先亲手练默认离线/MCP和历史查看，再自行决定是否运行真实题；不要为了证明菜单重复付费任务。若要更丰富的终端界面，先以实际缺陷判断，当前不需要另加前端或框架。

## 收尾验证

完整343测试44.884秒通过，Ruff/98文件格式通过。新summary.json四步全部通过：9组回放、2工具/1搜/1页/1正文/2RPC/3固定模型调用、8绑定文件/9编辑行、交付审计。主文档13份完成同步；新增的是日期工作记录，综合指南没有增加。当前源码ZIP含195文件；只读新旧摘要、最终链接/保存材料审计、关键hash保护和精确包内容核对见result/verification。全程无真实模型/公共搜索/网页/浏览器请求。

## 同日后续修复：默认编辑器日志污染终端

用户实际反馈：结果菜单4打开run.json时，VS Code的main/node/electron缓存、扩展、字体、Jump List等日志穿插到研究菜单；输入0后重新出现主页。代码确认当时open_local直接从菜单进程调用os.startfile；0只是重绘菜单，不能修复外部编辑器。未在本机重新制造用户那一组VS Code内部警告，具体扩展/字体/版本原因未定位，不改其配置、扩展或系统隐私设置。

修复范围包括菜单3/4/5：open_local在Windows使用当前解释器旁的pythonw.exe提交os.startfile请求，DETACHED_PROCESS/独立进程组，stdin/stdout/stderr均接DEVNULL、close_fds=True、shell=False，10秒内等辅助进程提交，不等编辑器关闭。viewer_environment仅传系统/桌面上下文，不传模型key、PYTHONPATH、ELECTRON/VSCODE注入变量；非Windows也设置分离会话和三通道重定向。失败/非零/超时给路径与菜单6，不自动重试；成功仅称“已提交”，不认证GUI已显示。

初次真实子进程测试发现：仅把虚拟环境python.exe加DETACHED_PROCESS，重定向器仍可能为底层解释器分配另一控制台；改用pythonw后保存探针显示console_window=0、stdin_eof=true。另修正Windows环境变量键自动大写的测试预期，不是放宽隔离条件。初次失败日志viewer-tests-initial.txt保留，最终9项viewer-tests.txt通过，含真实噪声子进程、不能读菜单输入、不收到模型key、特殊路径参数、跨平台参数、超时/失败、只读显示和坏文件处理。

新增结果菜单6：通过load_saved_run读取保存的run.json，保留已有大小/结构检查，终端只读展示；不启动编辑器、不加载.env、不调用模型，读取失败仍能继续菜单。仍保留2看trace与4外部打开两种方式。

实际终端操作：读取已有reports/20261027T070136-0ed8fafa/run.json，主页3→历史1→菜单4成功提交默认打开，后续5.006秒空轮询没有终端输出；再菜单6显示完整JSON→0→0，退出0。未视觉认证VS Code渲染，也未重跑研究题。第一次自动权限审核超时，按工具明确允许重试一次后通过，无待授权阻塞。操作记录见reports/viewer-fix-20261027/manual-operation.json。

完整352测试50.058秒通过、Ruff与99文件格式通过，主文档13份已同步；本轮没有新增综合说明或新框架/依赖。研究核心、CLI、.env/锁文件、原eval与旧记录保持。新四步/源码包/最终审计以[后续修复结果](../reports/viewer-fix-20261027/result.json)和[最新checkpoint](../reports/viewer-fix-20261027/checkpoint.json)为准，不覆盖最初terminal-20261027材料。

应用元数据仍0.35.0，多个源码修订靠hash和日期快照区分。最初终端summary源码与原release-v035不同，当前继续变化后检查器可能拒绝该旧摘要；保留对应source-terminal.zip/旧readback并回到对应源码核验，不修改原归档或旧hash来冒充当前。最新默认命令指向本轮summary；这是既有完整性保护的边界，本次不扩展摘要协议。

实现依据：[Python os.startfile](https://docs.python.org/3/library/os.html#os.startfile)说明按文件关联启动、提交即返回；[Python子进程分离标志](https://docs.python.org/3/library/subprocess.html#subprocess.DETACHED_PROCESS)与[Microsoft进程标志](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)说明控制台继承。这里使用标准库，不把这些系统功能当Agent能力。

下次优先在新终端运行start.bat检查菜单4或6；已连接旧终端的编辑器进程不会被新代码追溯断开，不强行终止用户编辑器。若出现其他桌面关联问题，先记录提交/超时与路径，再单独复现，不重做收费研究任务。

后续修复收尾：新summary四步全部passed，新源码包196文件；最新只读摘要核验/最终审计/配置与旧源码保护见viewer-fix目录result与verification。四步启动前一次自动审核超时，按允许的一次重试后已执行成功，不是未完成阻塞。

## 同日说明文档重组：自包含面试手册与核心循环图

用户指出演示提出追问却要求跨文件查答案、架构图混入终端辅助，以及SDK/Tavily/DDGS等首次概念解释不足。本次先对照九类主说明、原Agent Loop、7份专题、核心源码和安装的AgentScope2.0.8/DDGS9.16.0接口；查Tavily/DDGS/AgentScope官方资料，强调以当前接线与固定版本为准。

开始前保存reports/docs-reorganization-20261027/docs-before.zip（25份维护Markdown）和477项代码/配置/历史材料指纹。源码、测试、脚本、eval数据、.env、锁文件、许可、原学习图及旧reports均不修改；本轮不运行研究模型或搜索，也不为纯文档重复整套352测试。

第一步：重写现有面试手册，纳入完整五分钟脚本和70个问题，按介绍、SDK/服务、循环、预算、证据、模型、MCP、安全、缩稿、评测、终端与设计取舍组织。失败计预算、stop后读页、SDK拦截没有应用trace、null不是零费用，在演示位置直接给答案，并在同文件详细展开；不以跨文件链接代替解释。

第二步：将Agent Loop并入架构，按一题从初始化到循环后返回逐步讲述；重画核心PNG与SVG，突出模型/SDK、Session许可、后端和结果回流，终端/文件查看另列外围图和章节。用已有离线Sharp渲染1500×1230 PNG，实际查看中文、箭头与版面；另外三幅Mermaid分别解释时序、MCP与外围资源，不宣称安装了Mermaid编译器。

第三步：补SDK/API/框架/库/服务、DDGS/包名/类/服务区分、Pydantic数据模型、JSON/null、async/线程/锁、schema/hash等基础；保留并整理原字段合同。对照CLI还澄清live_model_fixture只是粗分类，固定搜索不保证reader离线；没有改标签或程序。

第四步：操作练习保留在学习指南，验收/交付/携带合入现有统一验收文档，7专题设计经过与原件合入来源文件；README、STATUS、维护与eval入口同步。主文档13→9，根Markdown16→5，docs仍8份（4主文档和4历史SESSION）；没有新增综合指南。

合并后检查99份Python没有引用待删文档，逐份与合并前ZIP核对hash、去向锚点，再原生PowerShell删除11份已并入文件（4份分散主指南+7专题）。原文在快照和旧源码ZIP中保持，非保留重定向空壳。映射见merge-map.json；后续链接、锚点、命令解析、图示、材料审计与保护核对以本目录result/verification为准。

排版采用主题短段、分级标题、并列表格与有限代码块；重要答案留在当前位置，链接只用于核对源码/原件。最新恢复入口切到本轮checkpoint，旧SESSION数量与说法按历史解释，不当当前导航。本轮代码证据沿用同源码的352测试/99格式与viewer-fix四步结果；只读核验不等于新执行。

说明重组收尾：CommonMark解析核对149个本地链接目标及锚点，70题连续、20个模块与37个关键符号存在，示例JSON与CLI参数只解析不执行；主图1500×1230 PNG已查看，静态SVG保留。477项保护字节保持，材料审计通过，最新viewer-fix与0.20旧摘要只读核验通过。合并后14份维护Markdown（9主文档、4历史记录、eval入口），当前源码/文档包187文件；最后结果以docs-reorganization目录verification/result为准。
