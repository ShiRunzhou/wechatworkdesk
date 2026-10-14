# WeChatWorkDesk 网站部署

网站地址：`https://desk.plantisland.cn`。事件处理模块使用服务器的一份数据库，各设备登录同一网址查看和判断。电脑原有本地数据库是迁移前副本，当前不做双向合并；正式启用后以网站为准。

## 上传与分析

电脑继续用 WeFlow 导出，每次将一个对象的 Markdown 和附件压缩为 ZIP，保留相对目录结构，然后在网站更新分析模块选择对象并上传。单次200MB，解压后512MB；大资料按对象和时段拆分。MD 单文件也支持，但不包含图片和附件内容。语音转文字必须在导出中已经存在，网站不新增语音转写。

网站分析各对象最近一次上传的完整资料；时间范围在电脑导出时选定。历史上传仅按需检索。相同路径和内容复用快照；新版本保留；重新分析沿用事项标识和人工状态。网站不宣称消息级增量，也不自动按上传日期筛选聊天正文。上传文件不构成执行聊天指令的授权。

API 请求由服务器发出。模型接口需要在网站重新配置，电脑上的 Windows 加密密钥不会迁移，本地模型 localhost 地址在网站中指服务器。服务器使用独立 Fernet 主密钥保护多个模型接口密钥。

Agent 使用同一轮上下文、检索及提交协议，可由 SSH 后执行 `docker exec wechatworkdesk python -m workdesk.flow --data /var/lib/wechatworkdesk ...`，或者已认证的 HTTP 接口。网站 POST 接口必须附上 `Origin: https://desk.plantisland.cn`。

## 运行与访问

Python 3.12 容器运行为 UID10001，绑定服务器本机127.0.0.1:8770；600MB 内存限制、只读程序目录。Nginx负责HTTPS。所有页面、接口和附件均要求独立网站账号。密码只保存加盐 PBKDF2 哈希。网页登录使用浏览器的账号密码提示；服务器重启后同一账号继续可用。

代码：`/opt/wechatworkdesk-prepared`；数据：`/var/lib/wechatworkdesk`；认证与模型加密主密钥：`/etc/wechatworkdesk`。启动服务 `wechatworkdesk.service`。现有 PlantIsland 网站及其证书独立保留。新站点配置必须先通过 Nginx 校验才重载。

首次上线前：在 plantisland.cn 的权威 DNS 添加 desk A 42.193.174.4；配置仅包含 ACME 验证路径的HTTP站点；申请 desk 证书；替换为 deploy/nginx-domain.conf.example；验证证书与登录后正式使用。

## 备份与恢复

每天北京时间04:30生成私有备份，保留最新3份成功备份（控制40GB磁盘用量）。包含 SQLite 在线一致快照、被数据库引用的资料快照和模型配置。模型加密主密钥及网站登录配置需单独安全保存。备份不含旧设备路径、临时文件或正在运行的分析任务。

备份目录：`/var/backups/wechatworkdesk`；服务和定时器：`wechatworkdesk-backup.service/timer`。这是同一台服务器上的备份；尚未配置异地备份。

恢复前停止工作台、另存当前数据，将备份解压至相同数据路径、恢复私有密钥和权限，再启动并检查源文件哈希及事项状态。备份源路径与数据库对应，不能任意改盘符；换数据根目录需要重新映射 sources.snapshot_path。旧运行任务不会恢复，应创建新一轮分析。
