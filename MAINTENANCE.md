# 维护说明（给维护者 / 新会话）

> 中性纪律：**本仓库公开**——代码、README、commit message、本文件里都不得出现
> 具体业务品牌、域名、桶名。所有指向"我们自己的服务"的值只进 GitHub Secrets。

## 它怎么跑（2026-11-11 四版：先记账后镜像 + 镜像挂起自愈 + 分片续传上传）

- `.github/workflows/watch.yml`：每小时（第 17 分）+ 手动触发（可勾 force 全量重跑），
  `timeout-minutes: 40`。
- `watcher.py` 排他链：**刮官网 `pc.weixin.qq.com` 拿当前官方直链**（dldir1v6.qq.com/
  weixin/Universal/Windows/...exe）→ **HEAD 拿 Last-Modified / Content-Length** →
  与 `versions.json` 里记的上次值比对 → **没动就退出（每小时成本 = 1 GET + 1 HEAD，
  零下载）**；动了才下载官方原包 → PE FileVersion 合理性校验 + 7z 解包取市场版本 →
  Release（备注带 Source/SHA256/Size/PE 版本/Last-Modified）→ **立即提交状态
  （`track X`，含 `mirror_pending`）** → 可选镜像（成功再补一刀 `mirror X ok`）→
  可选通知 → 结束。
- **镜像挂起自愈**：镜像失败/被取消时 `mirror_pending` 已随 `track X` 落库，下一小时
  排他检测照常走"没动"分支，发现挂起标记就从**自己的 GitHub Release 资产**拉回来重传
  （不重新刮官网、不重新下载腾讯包），自动补传最多 10 次（`mirror_attempts` 计数），
  超限后停手等手动 FORCE。补传成功会清标记并另发一条"补传"飞书通知。
- **快速失败**：大传输超时 = (connect 15s, 读 120s；注意 **HTTPS 写路径实际沿用 connect
  槽位的 15s**——urllib3 发送请求体期间不会把 socket 超时切换到 read 值，2026-11-11
  本地已复现实证，见下"已知取舍"），单次下载另受 6 分钟墙钟预算约束，镜像阶段整段
  20 分钟预算——任何停滞都在分钟级暴露并重试，整轮耗时远小于 job 的 40 分钟上限
  （二版时代一个死链上传能独占 1800s、把 job 拖到 50 分钟超时取消，且取消发生在
  状态提交前 → 整夜重跑循环）。
- **版本号 = 市场版本**（如 4.1.15.56，与装机后 Weixin.dll 报的、适配链偏移表用的同口径）：
  下载后 **7z 两层解包**读包内版本目录（参考仓同款做法；runner 预装 7z）。安装包自身
  PE FileVersion 末段是内部 build 号（4.1.15.**56** 的包装着 4.1.15.**1000**）——PE 只做
  合理性校验、写进 Release 备注，**不用来命名**（2026-10-22 二版定的口径，头版用 PE 号
  归档过 v4.1.15.1000，已重归档为 v4.1.15.56 并清掉七牛错名对象）。
- 一版教训（已过时但留档）：dldir1.qq.com/weixin/Windows/* 不是 4.x 通道
  （WeChatSetup.exe 是 3.x、无版本化路径）；4.x 官方在 **dldir1v6** + **Universal/Windows**。

## Secrets（仓库设置里配，值永不外泄）

| Secret | 值（维护者自己填） | 缺省行为 |
|---|---|---|
| `UPLOAD_TICKET_URL` | 自己的签票端点 | 不镜像，只发 Release |
| `UPLOAD_TOKEN_HEADER` | 端点要求的令牌头名 | 同上 |
| `UPLOAD_TOKEN` | 令牌值 | 同上 |
| `FEISHU_WEBHOOK` | 飞书群自定义机器人 webhook | 不通知 |
| `FEISHU_SECRET` | 机器人加签密钥（没配加签就不设） | — |

签票契约（对端点的要求）：`POST` 带上令牌头、body `{"filename"}` →
`{"data":{"token","key","upload_url"}}`；随后优先对 `upload_url` 的主机发七牛
**分片续传 v2**（init/PUT 分片/complete，桶名从凭证 policy 解析），主机不认 v2
路径时回退 multipart `token`/`key`/`file` 单请求直传 `upload_url`。凭证是
**单键、短时效、只增**的——令牌泄露的最坏结果是别人多传一个新对象，删改不了
任何已有内容。

## 常见维护动作

- **验证全链**：Actions → watch → Run workflow → 勾 force。
- **换版本源**：`watcher.py` 顶部 `OFFICIAL_PAGE` + `LINK_RE`。
- **想让它盯别的目录/前缀**：镜像 key 由签票端点决定（端点按 filename 定 key），
  本仓库只上传 `filename` 本身。
- 下载目录 `downloads/` 已被 .gitignore 忽略（工作区临时产物，跑完即删）。

## 已知取舍（2026-10-22 首跑实测）

- **官方 4.x 直链未确认**：dldir1 没有版本化路径（404）；`WeChatSetup.exe` 还在发
  3.9.x；`WeixinSetup.exe` 候选首跑未命中。**当前实际下载源 = manifest 指向的
  社区镜像件**（cscnk52 release），由 PE 版本核验把关（4.1.15.56 的包，PE
  FileVersion 实测为 4.1.15.1000——市场版本与内部 build 号不同段，所以核验口径是
  **前三段相等**）。哪天官方 4.x 直链确认了，把它排到候选最前面即可。
- **PE 核验不是摆设**：首跑它拦下了"官方端点还在发 3.9.12"的错包——没有这道闸，
  3.9 的安装包就被当成 4.1.15.56 存进去了。
- **镜像上传 = 七牛分片续传 v2（2026-11-11 四版）**：4 MiB 一片、每片独立短请求
  （新连接）+ 单片三次重试，最后 complete 合并。三版的全量单请求 multipart 在跨境
  慢链路上结构性必死：HTTPS 下 urllib3 发请求体时 write 走的是 connect 槽位超时
  （15s，配置的 read=120s 根本到不了写路径——本地已复现实证），260MB 单流推完 TCP
  初始窗口即断。4.2.0.52 九连灭（三小时三次运行、每次 ~17s 断、零进度），而同期的
  短请求（签票 POST、TLS 握手）全部正常——短连接能过、长流过不去，这正是分片要
  解决的形态。桶名从上传凭证 policy 的 `scope` 解析；签票端点若不支持 v2 路径
  （返回 400/404/405 或无 uploadId），自动回退旧的单请求 multipart。分片 PUT 的
  超时两个槽位都给 90s（就是为了让 connect 槽位也罩得住整片写入）。
  2026-11-05 的 4.2.0.13 曾靠"快速失败 + 每小时自愈"扛过去（运气好碰上一条能走完
  全程的链路）；失败只标记不拦通知（Release 永远先成）。
- Scoop manifest 更新通常在官方发版后数小时内；对"每小时级"的监测需求足够。
- `python watcher.py --pe-version <文件>` 可独立查任一 exe 的资源版本（排障用）。
