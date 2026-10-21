# 维护说明（给维护者 / 新会话）

> 中性纪律：**本仓库公开**——代码、README、commit message、本文件里都不得出现
> 具体业务品牌、域名、桶名。所有指向"我们自己的服务"的值只进 GitHub Secrets。

## 它怎么跑（2026-10-22 二版：参照 wechat-windows-versions 的排他做法重做检测）

- `.github/workflows/watch.yml`：每小时（第 17 分）+ 手动触发（可勾 force 全量重跑）。
- `watcher.py` 排他链：**刮官网 `pc.weixin.qq.com` 拿当前官方直链**（dldir1v6.qq.com/
  weixin/Universal/Windows/...exe）→ **HEAD 拿 Last-Modified / Content-Length** →
  与 `versions.json` 里记的上次值比对 → **没动就退出（每小时成本 = 1 GET + 1 HEAD，
  零下载）**；动了才下载官方原包 → PE FileVersion 提取版本号 → Release（备注带
  Source/SHA256/Size/PE 版本/Last-Modified）→ 可选镜像 → 可选通知 → 状态回写。
- 版本号格式 `\d+(\.\d+){2,3}`（PE FileVersion，如 4.1.15.1000；市场版本 .56 时
  PE 内部号可能是 .1000——**Release tag 用 PE 号**，与市场版本可能末段不同）。
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
`{"data":{"token","key","upload_url"}}`；随后 multipart `token`/`key`/`file`
直传 `upload_url`。凭证是**单键、短时效、只增**的——令牌泄露的最坏结果是
别人多传一个新对象，删改不了任何已有内容。

## 常见维护动作

- **验证全链**：Actions → watch → Run workflow → 勾 force。
- **换版本源**：`watcher.py` 顶部 `MANIFEST_URL`。
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
- 镜像上传是单请求 multipart（非分片续传），跨境慢链路靠 3 次重试 + 30 分钟
  上限扛；失败只标记不拦通知（Release 永远先成，手动从 Release 补镜像即可）。
  首跑全链约 22 分钟（大头是 244MB 下载 + Release 附件上传）。
- Scoop manifest 更新通常在官方发版后数小时内；对"每小时级"的监测需求足够。
- `python watcher.py --pe-version <文件>` 可独立查任一 exe 的资源版本（排障用）。
