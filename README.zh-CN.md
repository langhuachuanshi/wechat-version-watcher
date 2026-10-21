# wechat-version-watcher

[English](README.md) | [简体中文](README.zh-CN.md)

每小时自动监测 **微信 Windows 官方**新版本：下载官方安装包归档到 GitHub Releases，
可选镜像到你自己的对象存储、推送飞书群机器人通知。

## 工作方式

- **变更检测（排他）**：先刮官网拿当前官方下载直链，再对 CDN 发一次 HEAD——
  `Last-Modified` 没动本轮即结束。**无新版本的小时成本 = 1 次页面 GET + 1 次
  HEAD，零下载**。
- **市场版本号**：安装包经 7z 两层解包，读包内版本目录（如 `4.1.15.56`）——与
  装机后 `WeChatWin.dll` 报的号同口径。安装包自身的 PE FileVersion 末段是内部
  build 号（市场版 `4.1.15.56` 的包内是 `4.1.15.1000`），只做合理性校验、写进
  Release 备注，不用于命名。
- **归档**：每个新版本发一个 Release，附件为**未改动的官方原包**；备注带来源
  URL、SHA256、大小、PE FileVersion 与 CDN Last-Modified。
- **镜像（可选）**：经你自己控制的签票端点上传到你的对象存储（凭证不出现在
  本仓库，全走 Secrets）。注意：七牛已废弃 `insertOnly`，**同名重传会覆盖**；
  上游的排他机制保证正常运行不会撞名。
- **通知（可选）**：向飞书群自定义机器人发一条中文消息：版本号、SHA256、
  GitHub 下载链接、镜像下载链接、镜像状态。
- **零硬编码后端**：端点、令牌、webhook 全部来自仓库 Secrets，指向你自己的
  服务即可。
- **状态**：`versions.json`（工作流跑完自动提交回仓库）记录上次见到的
  Last-Modified/URL 与版本历史。

## 配置

1. 开启 Actions（默认开启）。每小时定时无需配置；无新版本的一轮耗时不到一分钟。
2. 要镜像到你自己的桶，设置以下仓库 Secrets（前三项要么全配要么全不配）：
   - `UPLOAD_TICKET_URL` -- 你的签票端点：`POST` 带头
     `<UPLOAD_TOKEN_HEADER>: <UPLOAD_TOKEN>`，体 `{"filename": "..."}` ->
     `{"data": {"token", "key", "upload_url"}}`；随后工作流以 multipart 的
     `token`/`key`/`file` 直传 `upload_url`。
   - `UPLOAD_TOKEN_HEADER` -- 端点要求的令牌头名。
   - `UPLOAD_TOKEN` -- 令牌值。
   - `MIRROR_PUBLIC_BASE`（可选）-- 如 `https://your-cdn.example.com`；配置后
     通知里会带镜像下载链接（`<base>/<key>`）。
3. 要收通知，设置 `FEISHU_WEBHOOK`（飞书群自定义机器人 webhook 地址），机器人
   开了签名校验再设 `FEISHU_SECRET`。
4. 全链自测：Actions -> **watch** -> *Run workflow* -> 勾选
   **Force a full run**。

## 为什么刮官网而不是调版本接口？

微信 Windows 没有公开的版本接口。官网首页始终带着当前安装包直链，CDN 的 HEAD
响应里 `Last-Modified` 只在发新版时移动——两者合起来是最便宜可靠的第一方信号
（与知名的 wechat-windows-versions 系列存档同思路）。市场版本号只在确认有变更
后才需要，所以是下载后解包读取，而不是去猜带版本的 URL。

## 声明

安装包版权归腾讯所有；本仓库仅自动下载与归档官方端点发放的原始文件，用于
兼容性测试。代码以 MIT 发布（见 LICENSE）。
