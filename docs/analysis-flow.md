# 更新与分析流程

入口：工作事项台右上角“更新与分析”，或双击 **启动更新分析台.cmd**。与事项台共用127.0.0.1:8770服务。

## 日常使用

1. 选择会话；默认恢复上次范围。会话来自已登记对象，“刷新会话”取得WeFlow实际会话列表。搜索及联系人/群聊筛选只调整显示，后台始终使用external_id。
2. 选择最近7天、30天或自定义起止时间，均为北京时间。本次补充需求可留空。
3. 选择模型API或Agent，点击“更新并分析／更新并准备分析”。“仅更新资料”保留独立更新用法。
4. 查看各会话新增、跳过、失败与缺口。API结果通过原Store提交到工作事项列表；Agent模式点击“查看Agent请求”复制到你选择的聊天，不自动向其他聊天发消息。

任一会话更新失败时，本轮先不分析；“从已保存进度继续”只重试失败会话，起止时刻固定。停止不会删除已保存文件；模型请求正在进行时需等该请求返回。服务重启后任务标记可继续。
更新时会探测本机服务；如果未运行且D:\weflow\WeFlow.exe存在，自动启动这个已安装的程序。接口仍关闭或数据库未就绪时报告具体操作要求，不修改WeFlow设置或安装文件。

API端点未配置时可以先使用Agent模式。未配置的真实模型和Bonsai尚未做分析质量验收，不以模拟测试声称达到昨晚的效果。

## 分析设置

填写兼容Chat Completions的基础地址（如http://127.0.0.1:1234/v1）和服务返回的model标识。本机无认证服务可留空密钥。远端地址使用https；“保存并测试连接”只请求models清单，不发送真实聊天。
密钥保存在data/flow/settings.json，以当前Windows用户DPAPI加密；页面只返回是否已保存，任务和Agent包均不包含密钥。

默认需求已保存专业主任工作范围、排除项、时间线核对、依据定位和人工状态保护。模型可按需提出补查关键词；程序执行只读检索并继续分析，无需原生工具调用。
允许读取的背景资料可设置具体文件或目录；本机首次默认接入昨晚使用的工作概况、资料导航文件（存在时）。目录最多500个候选文件，支持文本、CSV、DOCX、XLSX、PPTX、PDF文字层。资料来源在每轮快照中可核查，原文件不修改。
其他任务的信息需要由Agent或用户保存为本地背景文档，再加入允许读取范围；独立工作台没有自动读取所有Codex聊天的能力。

模型适配选项：输入材料上限和每段字符数、最大输出长度、超时、补查轮数、temperature、输出长度参数、JSON mode、图片能力及额外JSON参数。Bonsai的思考等参数可在接入时填写。字符上限不是精确token计数。消息、背景或候选结果过大时会停止并报告，不能默默丢弃一部分后声称全读。

## 程序如何准备资料

- 更新范围只限制本次消息。已获授权的同对象历史快照仍可回查，跨会话检索限定于本轮所选对象。
- 每轮生成去重消息正文和关联附件清单；旧格式可能重复的记录标注待核对。音频不生成、不下载；复用已有文字，未转写语音报告缺口。
- 新材料采用独立不可变快照；历史source_id复用。同一会话按external_id对应原对象，出现多个对象映射时停止核实。
- 默认复用已成功分析的消息。新增消息、人工状态/反馈、确认背景、默认需求、模型、图片能力或背景资料范围变化时重新准备相关消息。“重新分析所选时段”可强制复核。
- 模型按消息分段分析，返回补查词后由程序搜索相关历史段落。选定附件单独读取：Office只提取正文/单元格文字，不执行宏、不重算公式；PDF只提取文字层，扫描图片和图表未自动OCR。可用的本机Codex依赖运行时为PDF提供pypdf；不存在时报告缺口，Agent仍可读取。
- 程序也会依据本段出现的答辩、实习等工作关键词预取相关历史，模型不提出补查时仍有历史参考。补查文件名时可读取匹配的历史Office/PDF文字，每次最多两份；更大范围交给Agent或分轮处理。
- 图片能力默认关闭，启用后以兼容image_url的本地数据编码传给配置模型。表情动画、超过12MB图片和不支持文件类型标记未读。
- 读取记录由程序根据实际向模型提供的段落/图片生成；未提供正文的目录条目不能成为依据。每个源文件报告read/partial/unread。模型结果引用未提供资料、其他对象或人工完成状态时拒绝发布。
- 分段和补查结果保存在私有任务目录；模型请求失败后沿用已完成的检查点。更换模型设置会保留材料并开始新的分析尝试。发布前再次校验，整轮调用Store.submit原子提交，保留反馈、状态和reference_code。
- 依据行号须在实际提供范围内，消息时间须出现在提供段落中；程序核对定位，但不能自动保证每个语义判断都正确。
- 分析期间背景或事项变化时阻止发布，保留草稿；继续任务会取得最新判断并重新核对。提交已成功但保存进度中断时，恢复已发布结果，不再次请求模型。

## Agent入口

生成任务后可以使用同一份上下文和读取入口：

```powershell
& '.\.venv\Scripts\python.exe' -m workdesk.flow context <job_id> --out <context.json>
& '.\.venv\Scripts\python.exe' -m workdesk.flow search <job_id> --query '事项 人物 日期'
& '.\.venv\Scripts\python.exe' -m workdesk.flow read <job_id> --source-id <source_id> --start-line 1 --end-line 150
& '.\.venv\Scripts\python.exe' -m workdesk.flow submit <job_id> --result <result.json>
```

上述命令支持全局--data用于隔离模拟环境。Agent应遵循agent-workflow.md，读取上下文给出的snapshot_path、报告真实覆盖范围，提交与原result-template.json一致的结构。也可使用原workdesk run submit提交，页面下次检查会识别已发布轮次。取得文件路径不代表已经阅读。附件文字提取能力不足时使用Agent自己的文件工具。

## 文件位置与接口

- E:\wechatrecord\increments：增量文档及图片，原texts保留。
- data/flow/jobs/<job_id>：状态、冻结上下文、Agent请求、处理检查点与API原始结构结果。
- data/flow/processed.json：成功发布的消息指纹，失败任务不推进分析进度。
- data/sources：不可变资料快照；data/workdesk.sqlite3仍使用原对象、事项、反馈和阅读轮次表。

GET /api/pipeline/config、/sessions、/jobs、/jobs/<id>、/jobs/<id>/context、/jobs/<id>/agent-request。
POST /api/pipeline/config、/model-test、/jobs、/jobs/<id>/retry、/stop、/search、/read、/submit。
修改接口仍检查本机Host与Origin、拒绝跨站请求；读取接口仅按本轮source_id访问快照，模型没有任意SQL、路径读取或命令执行入口。

本机网络直接连接；远端先直连。只读探测在传输失败时单请求通过127.0.0.1:1080重试。可能已经到达服务端的分析POST不会自动重复，HTTP应用错误不会切换代理。没有修改系统或全局代理。

兼容请求与图片输入格式参考：[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[图片输入](https://developers.openai.com/api/docs/guides/images-vision)。不同服务的可选参数需要按其支持情况设置。
