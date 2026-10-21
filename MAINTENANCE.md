# 维护说明（给维护者 / 新会话）

> 中性纪律：**本仓库公开**——代码、README、commit message、本文件里都不得出现
> 具体业务品牌、域名、桶名。所有指向"我们自己的服务"的值只进 GitHub Secrets。

## 它怎么跑

- `.github/workflows/watch.yml`：每小时（第 17 分）+ 手动触发（可勾 force 全量重跑）。
- `watcher.py` 五步：读 Scoop Extras 的 wechat manifest 拿版本与官方直链 →
  与 `versions.json`（状态文件，跑完自动 commit 回仓库）比对 → 新版本才往下走：
  下载 → `gh release create`（tag `v<版本>`，附件=原始安装包）→ 可选镜像 →
  可选通知 → 写状态并 push。
- 没有新版本时整轮只是一次 GET，分钟级消耗极小。

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

## 已知取舍

- 镜像上传是单请求 multipart（非分片续传），跨境慢链路靠 3 次重试 + 30 分钟
  上限扛；失败只标记不拦通知（Release 永远先成，手动从 Release 补镜像即可）。
- Scoop manifest 更新通常在官方发版后数小时内；对"每小时级"的监测需求足够。
