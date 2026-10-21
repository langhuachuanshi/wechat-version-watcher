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
     the version from its PE FileVersion resource, publish it as a GitHub
     Release (notes carry source URL + SHA256 + size + Last-Modified);
  4. optionally mirror the file to your own object storage via a
     signed-ticket endpoint you control (credentials never touch this repo,
     everything comes from repository Secrets);
  5. optionally push a notification to a Feishu group bot;
  6. record the new state in versions.json and commit it back.

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

import requests

OFFICIAL_PAGE = "https://pc.weixin.qq.com/"  # official site carries the current 4.x download link
LINK_RE = r"https://dldir1v6\.qq\.com/weixin/Universal/Windows/[^\s\"'<>,]+\.exe"
STATE_FILE = Path("versions.json")
DOWNLOAD_DIR = Path("downloads")
RETRY = 3
DOWNLOAD_TIMEOUT = 60 * 30  # big file, slow link: half an hour cap


def log(msg: str) -> None:
    print(msg, flush=True)


def detect_official() -> dict:
    """Scrape the official site for the current installer link, then HEAD it.

    Change detection costs one page GET + one HEAD per hour: the CDN's
    Last-Modified header only moves when Tencent ships a new build, so an
    unchanged version never downloads anything (the exclusivity trick, same
    approach as the well-known wechat-windows-versions archives).
    """
    for i in range(RETRY):
        try:
            page = requests.get(OFFICIAL_PAGE, timeout=30)
            page.raise_for_status()
            m = re.search(LINK_RE, page.text)
            if not m:
                raise RuntimeError("official download link not found on the page")
            url = m.group(0)
            h = requests.head(url, timeout=30, allow_redirects=True)
            h.raise_for_status()
            return {
                "url": url,
                "last_modified": h.headers.get("Last-Modified", ""),
                "size": int(h.headers.get("Content-Length", 0) or 0),
            }
        except Exception as e:  # noqa: BLE001 - want the full chain in logs
            log(f"official detect failed ({i + 1}/{RETRY}): {e}")
            time.sleep(10 * (i + 1))
    sys.exit("cannot detect official installer")


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


def fetch_to(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    if dest.stat().st_size < 10 << 20:
        raise RuntimeError(f"downloaded file too small: {dest.stat().st_size}")


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
    try:
        subprocess.run(["7z", "x", str(path), f"-o{outer}", "-y"],
                       check=True, capture_output=True)
        archive = outer / "install.7z"
        if not archive.exists():
            raise RuntimeError("install.7z not found inside the installer")
        subprocess.run(["7z", "x", str(archive), f"-o{inner}", "-y"],
                       check=True, capture_output=True)
        for p in sorted(inner.iterdir()):
            if p.is_dir() and re.fullmatch(r"\d+(\.\d+)+", p.name):
                return p.name
        raise RuntimeError("no version-named directory inside install.7z")
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)


def download(url: str) -> tuple[Path, str]:
    """Download the official installer and learn its marketing version.

    Guards: the file must be big enough, its PE FileVersion must be a sane
    dotted number, and the marketing version comes from unpacking (see
    extract_market_version). Returns (file, marketing_version).
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    tmp = DOWNLOAD_DIR / "official.exe"
    for i in range(RETRY):
        try:
            log(f"downloading from {url} (try {i + 1})")
            fetch_to(url, tmp)
            pe = pe_file_version(tmp)
            if not re.fullmatch(r"\d+(\.\d+){2,3}", pe or ""):
                raise RuntimeError(f"no sane PE FileVersion in file: {pe!r}")
            version = extract_market_version(tmp)
            dest = DOWNLOAD_DIR / f"WeChatWin_{version}.exe"
            tmp.rename(dest)
            log(f"downloaded {dest.name} ({dest.stat().st_size >> 20} MB, "
                f"market {version}, PE FileVersion {pe})")
            return dest, version
        except Exception as e:  # noqa: BLE001
            log(f"download failed ({i + 1}/{RETRY}): {e}")
            time.sleep(15 * (i + 1))
    sys.exit(f"cannot download installer from {url}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_notes(path: Path, version: str, source_url: str, last_modified: str, sha: str) -> str:
    return (
        f"Official installer for WeChat {version}, archived automatically.\n"
        f"Source: {source_url}\n"
        f"SHA256: {sha}\n"
        f"Size: {path.stat().st_size} bytes\n"
        f"PE FileVersion: {pe_file_version(path)}\n"
        f"Last Modified: {last_modified}\n"
    )


def release(path: Path, version: str, source_url: str, last_modified: str = "", sha: str = "") -> str:
    """Publish the installer as a GitHub Release. Returns the release URL.

    Notes carry source URL + SHA256 + size + PE version + CDN Last-Modified
    (consumers -- and downstream packaging -- verify against them). If the
    release already exists (force rerun) the notes are refreshed, the asset
    is not re-uploaded.
    """
    env = dict(os.environ)
    tag = f"v{version}"
    notes = release_notes(path, version, source_url, last_modified)
    run = lambda *args: subprocess.run(  # noqa: E731
        args, check=True, capture_output=True, text=True, env=env
    )
    try:
        url = json.loads(run("gh", "release", "view", tag, "--json", "url").stdout)["url"]
        log(f"release {tag} already exists: {url} (refreshing notes)")
        run("gh", "release", "edit", tag, "--notes", notes)
        return url
    except subprocess.CalledProcessError:
        pass  # no such release yet - normal path
    out = run(
        "gh", "release", "create", tag, str(path),
        "--title", f"WeChat Windows {version}",
        "--notes", notes,
    )
    url = out.stdout.strip().splitlines()[-1]
    return url


def mirror(path: Path) -> tuple[str, str]:
    """Mirror the file to your own bucket via your own signed-ticket endpoint.

    Contract (all values from Secrets, nothing hardcoded):
      POST $UPLOAD_TICKET_URL   header { $UPLOAD_TOKEN_HEADER: $UPLOAD_TOKEN }
      body {"filename": "..."}  -> {"data": {"token", "key", "upload_url"}}
      then multipart POST to upload_url with fields token/key + file.

    Tickets are single-key insert-only: an existing object is NEVER
    overwritten (the storage answers "file exists") -- that rejection IS the
    existence check, reported honestly instead of pre-checking. Returns
    (status_text_chinese, public_url); url is empty when MIRROR_PUBLIC_BASE
    is unset or nothing was uploaded.
    """
    endpoint = os.environ.get("UPLOAD_TICKET_URL", "")
    header = os.environ.get("UPLOAD_TOKEN_HEADER", "")
    token = os.environ.get("UPLOAD_TOKEN", "")
    if not (endpoint and header and token):
        return "未配置镜像", ""

    def pub_url(key: str) -> str:
        base = os.environ.get("MIRROR_PUBLIC_BASE", "").rstrip("/")
        return f"{base}/{key}" if base and key else ""

    for i in range(RETRY):
        try:
            r = requests.post(
                endpoint, json={"filename": path.name},
                headers={header: token}, timeout=30,
            )
            r.raise_for_status()
            body = r.json()
            ticket = body.get("data") or body
            up = requests.post(
                ticket["upload_url"],
                data={"token": ticket["token"], "key": ticket["key"]},
                files={"file": open(path, "rb")},
                timeout=DOWNLOAD_TIMEOUT,
            )
            text = up.text[:512]
            # insert-only 同名不覆盖：七牛回 file exists（可能 2xx 带 error 体）。
            # 不当失败——如实上报"已存在"，是否同包看通知里的 SHA256 比对。
            if "file exists" in text or '"code":614' in text:
                return "镜像同名已存在（不覆盖；若新包 SHA 不同需人工删旧重传）", pub_url(ticket["key"])
            up.raise_for_status()
            confirmed = up.json().get("key", "")
            if confirmed != ticket["key"]:
                raise RuntimeError(f"storage confirmed a different key: {confirmed!r}")
            return "已上传镜像", pub_url(ticket["key"])
        except Exception as e:  # noqa: BLE001
            log(f"mirror failed ({i + 1}/{RETRY}): {e}")
            time.sleep(20 * (i + 1))
    return "镜像上传失败（见运行日志）", ""


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
        from urllib.parse import quote
        webhook = f"{webhook}{sep}timestamp={ts}&sign={quote(extra['sign'])}"
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        log(f"notify -> HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {e}")


def save_state(version: str, path: Path, detected: dict) -> None:
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    history = state.setdefault("history", [])
    history.append({"version": version, "file": path.name, "at": int(time.time())})
    state["latest"] = version
    # 排他记账：官方 CDN 的 Last-Modified / Size——下一小时 HEAD 一比就知道有没有新版
    state["last_modified"] = detected.get("last_modified", "")
    state["size"] = detected.get("size", 0)
    state["url"] = detected.get("url", "")
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def commit_state(version: str) -> None:
    subprocess.run(["git", "config", "user.name", "watcher"], check=True)
    subprocess.run(["git", "config", "user.email", "watcher@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", str(STATE_FILE)], check=True)
    subprocess.run(["git", "commit", "-m", f"track {version}"], check=True, capture_output=True)
    subprocess.run(["git", "push"], check=True)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--pe-version":
        print(pe_file_version(Path(sys.argv[2])) or "(no version resource found)")
        return
    force = os.environ.get("FORCE", "") == "1"
    detected = detect_official()
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    # 排他（exclusivity）：官方 CDN 的 Last-Modified 没动就不下载——
    # 每小时成本 = 官网 1 GET + CDN 1 HEAD，零流量零下载。
    unchanged = (
        state.get("last_modified") == detected["last_modified"]
        and state.get("url") == detected["url"]
        and detected["last_modified"] != ""
    )
    if unchanged and not force:
        log(f"no change (Last-Modified={detected['last_modified']}, latest={state.get('latest')})")
        return
    log(f"change detected: Last-Modified={detected['last_modified']} size={detected['size']}")
    path, version = download(detected["url"])
    sha = sha256_of(path)  # 算一次全链复用（Release 备注与飞书通知同一份）
    release_url = release(path, version, detected["url"], detected.get("last_modified", ""), sha)
    mirror_status, mirror_url = mirror(path)
    log(f"mirror: {mirror_status} {mirror_url}")
    notify(version, sha, release_url, mirror_status, mirror_url)
    save_state(version, path, detected)
    commit_state(version)
    log("done")


if __name__ == "__main__":
    main()
