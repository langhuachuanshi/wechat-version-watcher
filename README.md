# wechat-version-watcher

[English](README.md) | [简体中文](README.zh-CN.md)

Hourly watcher that archives official **WeChat for Windows** installers to
GitHub Releases, optionally mirrors them to your own object storage, and
optionally pings a Feishu bot.

## How it works

- **Change detection (exclusivity)**: scrapes the official site for the
  current installer link and HEADs the CDN -- when the `Last-Modified`
  header has not moved, the run is over. An unchanged hour costs one page
  GET + one HEAD, zero download.
- **Marketing version**: the installer is unpacked (7z, two layers) and the
  version is read from the version-named directory inside -- the same number
  the installed `WeChatWin.dll` reports. The installer's own PE FileVersion
  ends in an internal build segment (e.g. `4.1.15.1000` for a `4.1.15.56`
  release) and is kept as a sanity check in the release notes only.
- **Archive**: every new version becomes a Release with the original,
  unmodified installer attached; notes carry source URL, SHA256, size, PE
  FileVersion and the CDN Last-Modified.
- **Mirror (optional)**: uploads to your own bucket through a signed
  single-key ticket endpoint you control (credentials never appear in this
  repository). Note: qiniu has deprecated `insertOnly` -- a same-key
  re-upload overwrites; the hourly exclusivity upstream makes that a
  non-issue in normal operation.
- **Notify (optional)**: sends a Feishu group-bot message in Chinese with
  the version, SHA256, GitHub download link, mirror link and mirror status.
- **No hardcoded backends**: every endpoint, token and webhook comes from
  repository Secrets -- point them at whatever you own.
- **State**: `versions.json` (committed back by the workflow) remembers the
  last seen Last-Modified/URL and the version history.

## Setup

1. Enable Actions (they are on by default). The hourly schedule needs no
   configuration; a run that finds no new version takes well under a minute.
2. To mirror to your own bucket, set these repository Secrets (the first
   three or none):
   - `UPLOAD_TICKET_URL` -- your endpoint that returns a signed single-key
     upload ticket: `POST` with header `<UPLOAD_TOKEN_HEADER>: <UPLOAD_TOKEN>`,
     body `{"filename": "..."}` -> `{"data": {"token", "key", "upload_url"}}`;
     then the workflow multipart-POSTs `token`/`key`/`file` to `upload_url`.
   - `UPLOAD_TOKEN_HEADER` -- the header name your endpoint expects.
   - `UPLOAD_TOKEN` -- the token value.
   - `MIRROR_PUBLIC_BASE` (optional) -- e.g. `https://your-cdn.example.com`;
     when set, the notification includes the mirror download link
     (`<base>/<key>`).
3. To get notified, set `FEISHU_WEBHOOK` (Feishu group custom bot webhook
   URL) and optionally `FEISHU_SECRET` (if the bot uses signing).
4. Test the whole chain: Actions -> **watch** -> *Run workflow* -> tick
   **Force a full run**.

## Why scrape the homepage instead of a version API?

WeChat for Windows has no public version API. The official homepage always
carries the current installer link, and the CDN answers HEAD with a moving
`Last-Modified` -- together they are the cheapest reliable first-party
signal (approach shared with the well-known wechat-windows-versions
archives). The marketing version is only needed once a change is confirmed,
so it is read by unpacking the downloaded file instead of guessing URLs.

## Legal

The installers are Tencent's; this repository only automates downloading and
archiving what the official endpoint serves, for compatibility-testing
purposes. The code is MIT (see LICENSE).
