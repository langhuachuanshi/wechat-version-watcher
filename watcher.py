#!/usr/bin/env python3
"""Hourly watcher for official WeChat (Windows) releases.

What it does each hour (exclusivity first -- an unchanged version costs one
page GET + one HEAD and nothing else):
  1. scrape the official site (pc.weixin.qq.com) for the current installer
     link, HEAD the CDN for its Last-Modified / Content-Length;
  2. compare against the last-seen values in versions.json -- unchanged
     means done, zero download (same approach as the well-known
     wechat-windows-versions archives);
  3. changed means Tencent shipped: download the official installer, read
     the marketing version from its unpacked layout, publish it as a GitHub
     Release (notes carry source URL + SHA256 + size + Last-Modified);
  4. record the new state in versions.json and commit it back BEFORE any
     mirror work -- whatever happens to the mirror afterwards can only
     cost a retry next hour, never a re-run of download -> release;
  5. optionally mirror the file to your own object storage via a
     signed-ticket endpoint you control (credentials never touch this repo,
     everything comes from repository Secrets). A failed mirror leaves a
     mirror_pending flag in the state, and later runs self-heal: they pull
     the asset back from our own GitHub Release and re-upload it;
  6. optionally push a notification to a Feishu group bot.

Everything is config-driven: point the Secrets at your own endpoints and the
same code works for anyone. Installers themselves belong to Tencent.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import quote

import requests

OFFICIAL_PAGE = "https://pc.weixin.qq.com/"  # official site carries the current 4.x download link
LINK_RE = r"https://dldir1v6\.qq\.com/weixin/Universal/Windows/[^\s\"'<>,]+\.exe"
STATE_FILE = Path("versions.json")
DOWNLOAD_DIR = Path("downloads")
RETRY = 3
BACKOFF_S = 15  # linear backoff between retry attempts
CHUNK = 1 << 20  # 1 MiB streaming / hashing chunk
MIN_INSTALLER_SIZE = 10 << 20
CONNECT_TIMEOUT = 15  # connect timeout for every request
PAGE_TIMEOUT = 30  # per-op cap for page / HEAD / ticket / webhook requests
QUIET_TIMEOUT = 120  # per-op cap for big transfers: a stalled stream aborts
#  in two minutes instead of blocking for half an hour (the old flat 1800s
#  timeout let one dead upload eat a third of the whole job)
FETCH_BUDGET_S = 6 * 60  # wall-clock cap for one download attempt
DOWNLOAD_BUDGET_S = RETRY * FETCH_BUDGET_S  # whole download stage
DETECT_BUDGET_S = 3 * 60  # page GET + HEAD are quick; budget just bounds retries
MIRROR_BUDGET_S = 12 * 60  # whole mirror stage (ticket + upload)
MIRROR_AUTO_RETRIES = 10  # self-heal attempts per version before we stop
#  burning bandwidth hourly and wait for a FORCE run
HISTORY_CAP = 100  # keep versions.json from growing forever

T = TypeVar("T")


def log(msg: str) -> None:
    print(msg, flush=True)


def retry(stage: str, budget_s: int, attempt: Callable[[], T]) -> T | None:
    """Run attempt() up to RETRY times with linear backoff, inside budget_s
    wall-clock seconds. Returns its result, or None when every attempt
    failed or the budget ran out -- the caller decides how loud to be.
    """
    started = time.monotonic()
    for i in range(RETRY):
        if i and time.monotonic() - started >= budget_s:
            log(f"{stage}: budget exhausted after {i} attempt(s), giving up")
            return None
        try:
            return attempt()
        except Exception as e:  # noqa: BLE001 - want the full chain in logs
            log(f"{stage} failed ({i + 1}/{RETRY}): {e}")
            time.sleep(BACKOFF_S * (i + 1))
    return None


def detect_official() -> dict:
    """Scrape the official site for the current installer link, then HEAD it.

    Change detection costs one page GET + one HEAD per hour: the CDN's
    Last-Modified header only moves when Tencent ships a new build, so an
    unchanged version never downloads anything (the exclusivity trick, same
    approach as the well-known wechat-windows-versions archives).
    """
    def once() -> dict:
        page = requests.get(OFFICIAL_PAGE, timeout=(CONNECT_TIMEOUT, PAGE_TIMEOUT))
        page.raise_for_status()
        m = re.search(LINK_RE, page.text)
        if not m:
            raise RuntimeError("official download link not found on the page")
        url = m.group(0)
        h = requests.head(url, timeout=(CONNECT_TIMEOUT, PAGE_TIMEOUT), allow_redirects=True)
        h.raise_for_status()
        return {
            "url": url,
            "last_modified": h.headers.get("Last-Modified", ""),
            "size": int(h.headers.get("Content-Length", 0) or 0),
        }

    detected = retry("official detect", DETECT_BUDGET_S, once)
    if detected is None:
        sys.exit("cannot detect official installer")
    return detected


def pe_file_version(path: Path) -> str:
    """Read the FileVersion resource (e.g. 4.1.15.1000) from a PE file.

    Also usable standalone: `python watcher.py --pe-version <file>` -- handy
    when double-checking what some installer actually contains.
    """
    import pefile

    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    for entry in getattr(pe, "DIRECTORY_ENTRY_RESOURCE", []).entries:
        if entry.id != pefile.RESOURCE_TYPE["RT_VERSION"]:
            continue
        for e2 in entry.directory.entries:
            for e3 in e2.directory.entries:
                data = e3.data.struct
                raw = pe.get_memory_mapped_image()[data.OffsetToData : data.OffsetToData + data.Size]
                # UTF-16 resource blob: look for FileVersion / ProductVersion,
                # then the first dotted-quad number right after the key.
                text = raw.decode("utf-16-le", errors="ignore")
                for key in ("FileVersion", "ProductVersion"):
                    i = text.find(key)
                    if i < 0:
                        continue
                    m = re.search(r"\d+(\.\d+){2,3}", text[i + len(key) : i + len(key) + 40])
                    if m:
                        return m.group(0)
    return ""


def fetch_to(url: str, dest: Path, budget_s: int) -> None:
    """Stream url to dest. Aborts (and lets the caller retry) when the
    stream stalls beyond QUIET_TIMEOUT or the transfer exceeds budget_s."""
    deadline = time.monotonic() + budget_s
    with requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, QUIET_TIMEOUT)) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if time.monotonic() > deadline:
                    raise RuntimeError("download budget exhausted mid-stream")
                f.write(chunk)
    size = dest.stat().st_size
    if size < MIN_INSTALLER_SIZE:
        raise RuntimeError(f"downloaded file too small: {size}")


def extract_market_version(path: Path) -> str:
    """Unpack the installer and read the marketing version (e.g. 4.1.15.56).

    The installer is a 7z self-extractor wrapping install.7z; inside sits a
    version-named directory carrying the marketing version -- the same number
    the installed WeChatWin.dll reports and downstream hook tables key on.
    (The installer's own PE FileVersion ends in an internal build segment,
    e.g. 4.1.15.1000, which does NOT follow the marketing version -- so PE is
    only a sanity check, never the name.) Approach shared with the reference
    archives; needs 7z on PATH (preinstalled on GitHub runners).
    """
    build = DOWNLOAD_DIR / "unpack"
    if build.exists():
        shutil.rmtree(build)
    outer = build / "exe"
    inner = build / "inner"
    outer.mkdir(parents=True)
    inner.mkdir(parents=True)

    def unpack(archive: Path, out: Path) -> None:
        r = subprocess.run(["7z", "x", str(archive), f"-o{out}", "-y"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"7z unpack failed: {r.stderr.strip()[-200:]}")

    try:
        unpack(path, outer)
        archive = outer / "install.7z"
        if not archive.exists():
            raise RuntimeError("install.7z not found inside the installer")
        unpack(archive, inner)
        for p in sorted(inner.iterdir()):
            if p.is_dir() and re.fullmatch(r"\d+(\.\d+)+", p.name):
                return p.name
        raise RuntimeError("no version-named directory inside install.7z")
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)


def download(url: str) -> tuple[Path, str, str]:
    """Download the official installer and learn its marketing version.

    Guards: the file must be big enough, its PE FileVersion must be a sane
    dotted number, and the marketing version comes from unpacking (see
    extract_market_version). Returns (file, marketing_version, pe_version).
    """
    def once() -> tuple[Path, str, str]:
        tmp = DOWNLOAD_DIR / "official.exe"
        log(f"downloading from {url}")
        fetch_to(url, tmp, FETCH_BUDGET_S)
        pe = pe_file_version(tmp)
        if not re.fullmatch(r"\d+(\.\d+){2,3}", pe or ""):
            raise RuntimeError(f"no sane PE FileVersion in file: {pe!r}")
        version = extract_market_version(tmp)
        dest = DOWNLOAD_DIR / f"WeChatWin_{version}.exe"
        tmp.replace(dest)
        log(f"downloaded {dest.name} ({dest.stat().st_size >> 20} MB, "
            f"market {version}, PE FileVersion {pe})")
        return dest, version, pe

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    got = retry("download", DOWNLOAD_BUDGET_S, once)
    if got is None:
        sys.exit(f"cannot download installer from {url}")
    return got


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def release_notes(path: Path, version: str, source_url: str, last_modified: str,
                  sha: str, pe: str) -> str:
    return (
        f"Official installer for WeChat {version}, archived automatically.\n"
        f"Source: {source_url}\n"
        f"SHA256: {sha}\n"
        f"Size: {path.stat().st_size} bytes\n"
        f"PE FileVersion: {pe}\n"
        f"Last Modified: {last_modified}\n"
    )


def release(path: Path, version: str, source_url: str, last_modified: str = "",
            sha: str = "", pe: str = "") -> str:
    """Publish the installer as a GitHub Release. Returns the release URL.

    Notes carry source URL + SHA256 + size + PE version + CDN Last-Modified
    (consumers -- and downstream packaging -- verify against them). If the
    release already exists (force rerun) the notes are refreshed, the asset
    is not re-uploaded.
    """
    env = dict(os.environ)
    tag = f"v{version}"
    notes = release_notes(path, version, source_url, last_modified, sha, pe)

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args, check=True, capture_output=True, text=True, env=env)

    try:
        url = json.loads(run("gh", "release", "view", tag, "--json", "url").stdout)["url"]
        log(f"release {tag} already exists: {url} (refreshing notes)")
        run("gh", "release", "edit", tag, "--notes", notes)
        return url
    except subprocess.CalledProcessError:
        pass  # no such release yet - the normal path
    out = run(
        "gh", "release", "create", tag, str(path),
        "--title", f"WeChat Windows {version}",
        "--notes", notes,
    )
    return out.stdout.strip().splitlines()[-1]


def mirror_configured() -> bool:
    return all(
        os.environ.get(k, "")
        for k in ("UPLOAD_TICKET_URL", "UPLOAD_TOKEN_HEADER", "UPLOAD_TOKEN")
    )


def mirror(path: Path) -> tuple[str, str, bool]:
    """Mirror the file to your own bucket via your own signed-ticket endpoint.

    Contract (all values from Secrets, nothing hardcoded):
      POST $UPLOAD_TICKET_URL   header { $UPLOAD_TOKEN_HEADER: $UPLOAD_TOKEN }
      body {"filename": "..."}  -> {"data": {"token", "key", "upload_url"}}
      then multipart POST to upload_url with fields token/key + file.

    Same-key semantics: qiniu has DEPRECATED insertOnly -- a re-upload to the
    same key overwrites (verified in practice). No pre-check needed: the
    hourly exclusivity upstream means uploads only run when the official CDN
    timestamp moved, and a new marketing version always carries a new name.
    The "file exists" answer stays as a fallback in case anti-overwrite is
    ever enabled on the bucket.

    Returns (status_text_chinese, public_url, ok); the public url is empty
    when MIRROR_PUBLIC_BASE is unset or nothing was uploaded.
    """
    if not mirror_configured():
        return "未配置镜像", "", False
    endpoint = os.environ["UPLOAD_TICKET_URL"]
    header = os.environ["UPLOAD_TOKEN_HEADER"]
    token = os.environ["UPLOAD_TOKEN"]

    def pub_url(key: str) -> str:
        base = os.environ.get("MIRROR_PUBLIC_BASE", "").rstrip("/")
        return f"{base}/{key}" if base and key else ""

    def once() -> tuple[str, str, bool]:
        r = requests.post(
            endpoint, json={"filename": path.name},
            headers={header: token}, timeout=(CONNECT_TIMEOUT, PAGE_TIMEOUT),
        )
        r.raise_for_status()
        body = r.json()
        ticket = body.get("data") or body
        with open(path, "rb") as f:
            up = requests.post(
                ticket["upload_url"],
                data={"token": ticket["token"], "key": ticket["key"]},
                files={"file": f},
                timeout=(CONNECT_TIMEOUT, QUIET_TIMEOUT),
            )
        # qiniu form-upload errors come back as JSON ({"error", "code"}); fall
        # back to sniffing the raw body when a proxy answers with plain text.
        try:
            reply: dict | None = up.json()
        except ValueError:
            reply = None
        err = str(reply.get("error", "")) if isinstance(reply, dict) else up.text[:512]
        code = reply.get("code") if isinstance(reply, dict) else None
        # 同名重传实测是覆盖（insertOnly 已废弃）；"file exists" 是桶端若开
        # 防覆盖时的兜底识别——不当失败，如实上报并提示比对 SHA。
        if "file exists" in err.lower() or code == 614:
            return "镜像同名已存在（不覆盖；若新包 SHA 不同需人工删旧重传）", pub_url(ticket["key"]), True
        up.raise_for_status()
        confirmed = reply.get("key", "") if isinstance(reply, dict) else ""
        if confirmed != ticket["key"]:
            raise RuntimeError(f"storage confirmed a different key: {confirmed!r}")
        return "已上传镜像", pub_url(ticket["key"]), True

    got = retry("mirror", MIRROR_BUDGET_S, once)
    if got is None:
        return "镜像上传失败（见运行日志）", "", False
    return got


def fetch_release_asset(version: str) -> Path:
    """Pull our own archived asset back from the GitHub Release (fast from
    Actions runners), so a pending mirror can be healed without re-scraping
    the official site or re-downloading from Tencent."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dest = DOWNLOAD_DIR / f"WeChatWin_{version}.exe"
    subprocess.run(
        ["gh", "release", "download", f"v{version}", "--dir", str(DOWNLOAD_DIR), "--clobber"],
        check=True, capture_output=True, text=True,
    )
    if dest.stat().st_size < MIN_INSTALLER_SIZE:
        raise RuntimeError(f"asset too small after download: {dest.stat().st_size}")
    return dest


def heal_mirror(state: dict) -> None:
    """Retry a mirror that failed in an earlier run: re-download our own
    GitHub Release asset and upload it again. Keeps mirror_pending until an
    upload actually succeeds; stops after MIRROR_AUTO_RETRIES so a dead
    route doesn't pull ~250MB every hour forever."""
    version = state.get("latest", "")
    if not mirror_configured():
        log("mirror credentials removed; dropping pending flag")
        clear_pending(state)
        write_state(state)
        commit_state(f"drop mirror pending ({version})")
        return
    attempts = int(state.get("mirror_attempts", 0))
    if attempts >= MIRROR_AUTO_RETRIES:
        log(f"mirror for {version} still failing after {attempts} attempts; "
            "waiting for a FORCE run")
        return
    log(f"retrying pending mirror for {version} (attempt {attempts + 1}/{MIRROR_AUTO_RETRIES})")
    try:
        asset = fetch_release_asset(version)
    except Exception as e:  # noqa: BLE001
        log(f"release asset fetch failed, will retry next hour: {e}")
        return
    sha = sha256_of(asset)
    status, mirror_url, ok = mirror(asset)
    log(f"mirror retry: {status} {mirror_url}")
    state["mirror_attempts"] = attempts + 1
    if ok:
        clear_pending(state)
    write_state(state)
    outcome = "ok" if ok else f"failed (attempt {attempts + 1})"
    commit_state(f"mirror {version} {outcome}")
    if ok:
        repo = os.environ.get("GH_REPO", "")
        release_url = f"https://github.com/{repo}/releases/tag/v{version}" if repo else ""
        notify(version, sha, release_url, f"{status}（补传）", mirror_url)


def feishu_sign(secret: str) -> tuple[str, dict]:
    # Feishu's scheme (official sample): the string "timestamp\nsecret" is the
    # HMAC *key* with an empty message -- NOT key=secret (that yields 19021).
    ts = str(int(time.time()))
    mac = hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(mac).decode()
    return ts, {"timestamp": ts, "sign": sign}


def notify(version: str, sha: str, release_url: str, mirror_status: str, mirror_url: str) -> None:
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        log("notify skipped (no webhook configured)")
        return
    lines = [
        f"微信 Windows 新版本：{version}",
        f"SHA256：{sha}",
        f"下载（GitHub）：{release_url}",
    ]
    if mirror_url:
        lines.append(f"下载（镜像）：{mirror_url}")
    lines.append(f"镜像状态：{mirror_status}")
    payload: dict = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    secret = os.environ.get("FEISHU_SECRET", "")
    if secret:
        ts, extra = feishu_sign(secret)
        sep = "&" if "?" in webhook else "?"
        # sign is base64: quote it, otherwise '+'/'=' get eaten by URL parsing
        webhook = f"{webhook}{sep}timestamp={ts}&sign={quote(extra['sign'])}"
    try:
        r = requests.post(webhook, json=payload, timeout=(CONNECT_TIMEOUT, PAGE_TIMEOUT))
        log(f"notify -> HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def save_state(state: dict, version: str, path: Path, detected: dict) -> None:
    history = state.setdefault("history", [])
    history.append({"version": version, "file": path.name, "at": int(time.time())})
    state["history"] = history[-HISTORY_CAP:]
    state["latest"] = version
    # 排他记账：官方 CDN 的 Last-Modified / Size——下一小时 HEAD 一比就知道有没有新版
    state["last_modified"] = detected.get("last_modified", "")
    state["size"] = detected.get("size", 0)
    state["url"] = detected.get("url", "")
    if mirror_configured():
        state["mirror_pending"] = True  # cleared (with mirror_attempts) once uploaded
        state["mirror_attempts"] = 0
    write_state(state)


def clear_pending(state: dict) -> None:
    state.pop("mirror_pending", None)
    state.pop("mirror_attempts", None)


def commit_state(message: str) -> None:
    subprocess.run(["git", "config", "user.name", "watcher"], check=True)
    subprocess.run(["git", "config", "user.email", "watcher@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", str(STATE_FILE)], check=True)
    subprocess.run(["git", "commit", "-m", message], check=True, capture_output=True)
    subprocess.run(["git", "push"], check=True)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--pe-version":
        print(pe_file_version(Path(sys.argv[2])) or "(no version resource found)")
        return
    force = os.environ.get("FORCE", "") == "1"
    detected = detect_official()
    state = load_state()
    # 排他（exclusivity）：官方 CDN 的 Last-Modified 没动就不下载——
    # 每小时成本 = 官网 1 GET + CDN 1 HEAD，零流量零下载。
    unchanged = (
        state.get("last_modified") == detected["last_modified"]
        and state.get("url") == detected["url"]
        and detected["last_modified"] != ""
    )
    if unchanged and not force:
        if state.get("mirror_pending"):
            heal_mirror(state)
        else:
            log(f"no change (Last-Modified={detected['last_modified']}, latest={state.get('latest')})")
        return
    log(f"change detected: Last-Modified={detected['last_modified']} size={detected['size']}")
    path, version, pe = download(detected["url"])
    sha = sha256_of(path)  # 算一次全链复用（Release 备注与飞书通知同一份）
    release_url = release(path, version, detected["url"], detected.get("last_modified", ""), sha, pe)
    # 先记账再镜像：状态落库（含 mirror_pending）之后，镜像再怎么失败/超时
    # 也只会触发下一小时的补传，绝不会把 download -> release 整条流水线
    # 重新拉起来（旧顺序曾因取消发生在提交前而陷入整夜重跑循环）。
    save_state(state, version, path, detected)
    commit_state(f"track {version}")
    status, mirror_url, ok = mirror(path)
    log(f"mirror: {status} {mirror_url}")
    if ok:
        clear_pending(state)
        write_state(state)
        commit_state(f"mirror {version} ok")
    notify(version, sha, release_url, status, mirror_url)
    log("done")


if __name__ == "__main__":
    main()
