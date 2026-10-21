# wechat-version-watcher

Hourly watcher that archives official **WeChat for Windows** installers to
GitHub Releases. When a new version appears it downloads the official
installer, publishes it as a Release, optionally mirrors it to your own
object storage, and optionally pings a Feishu bot.

每小时监测微信 Windows 官方新版本：下载官方安装包归档到 GitHub Releases，
可选镜像到你自己的对象存储、推送飞书机器人通知。

- **Version source**: the community [Scoop Extras](https://github.com/ScoopInstaller/Extras)
  `wechat` manifest -- always points at official download URLs.
- **Archive**: every new version becomes a Release with the original,
  unmodified installer attached.
- **No hardcoded backends**: mirror endpoint, auth header, token and webhook
  all come from repository Secrets -- point them at whatever you own.
- **State**: `versions.json` (committed back by the workflow) remembers the
  last seen version; an unchanged version costs a single HTTP GET.

## Setup

1. Enable Actions (they are on by default).
2. To mirror to your own bucket, set these repository Secrets (all three or none):
   - `UPLOAD_TICKET_URL` -- your endpoint that returns a signed single-key
     upload ticket: `POST` with header `<UPLOAD_TOKEN_HEADER>: <UPLOAD_TOKEN>`,
     body `{"filename": "..."}` -> `{"data": {"token", "key", "upload_url"}}`;
     then the workflow multipart-POSTs `token`/`key`/`file` to `upload_url`.
   - `UPLOAD_TOKEN_HEADER` -- the header name your endpoint expects.
   - `UPLOAD_TOKEN` -- the token value.
3. To get notified, set `FEISHU_WEBHOOK` (Feishu group custom bot webhook URL)
   and optionally `FEISHU_SECRET` (if the bot uses signing).
4. Test the whole chain: Actions -> **watch** -> *Run workflow* -> tick
   **Force a full run**.

The hourly schedule needs no configuration. Runs that find no new version
take well under a minute.

## Why not just fetch the official site?

The official download page does not expose a stable version number. The Scoop
manifest is maintained daily by a large community and pins the official URL
for each release -- it is the most reliable public version signal.

## Legal

The installers are Tencent's; this repository only automates downloading and
archiving what the official endpoint serves, for compatibility-testing
purposes. The code is MIT (see LICENSE).
