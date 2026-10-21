#!/usr/bin/env python3
"""Hourly watcher for official WeChat (Windows) releases.

What it does when a new version shows up:
  1. download the official installer (URL comes from the version manifest);
  2. publish it as a GitHub Release (permanent archive, free hosting);
  3. optionally mirror the file to your own object storage via a
     signed-ticket endpoint you control (credentials never touch this repo,
     everything comes from repository Secrets);
  4. optionally push a notification to a Feishu group bot;
  5. record the new version in versions.json and commit it back.

Everything is config-driven: point the Secrets at your own endpoints and the
same code works for anyone. Installers themselves belong to Tencent.

Version source: the Scoop Extras "wechat" manifest -- community maintained,
always points at the official download URLs.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

MANIFEST_URL = "https://raw.githubusercontent.com/ScoopInstaller/Extras/master/bucket/wechat.json"
STATE_FILE = Path("versions.json")
DOWNLOAD_DIR = Path("downloads")
RETRY = 3
DOWNLOAD_TIMEOUT = 60 * 30  # big file, slow link: half an hour cap

# Official CDN candidates, tried in order; the manifest URL is the last resort.
OFFICIAL_VERSIONED = "https://dldir1.qq.com/weixin/Windows/WeChatWin_{v}.exe"
OFFICIAL_LATEST = "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe"


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_manifest() -> dict:
    for i in range(RETRY):
        try:
            r = requests.get(MANIFEST_URL, timeout=30)
            r.raise_for_status()
            m = r.json()
            version = m["version"]
            url = m["url"]
            if isinstance(url, list):
                url = url[0]
            # scoop manifests may carry the installer under architecture.64bit
            arch = (m.get("architecture") or {}).get("64bit") or {}
            if arch.get("url"):
                url = arch["url"]
            if isinstance(url, list):
                url = url[0]
            return {"version": version, "url": url.split("#")[0]}
        except Exception as e:  # noqa: BLE001 - want the full chain in logs
            log(f"manifest fetch failed ({i + 1}/{RETRY}): {e}")
            time.sleep(10 * (i + 1))
    sys.exit("cannot read version manifest")


def pe_file_version(path: Path) -> str:
    """Read the FileVersion resource (e.g. 4.1.15.56) from a PE file.

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


def download(version: str, manifest_url: str) -> Path:
    """Download the installer for exactly `version`.

    Candidates in order: official versioned URL, official always-latest URL,
    the manifest URL (community mirror). Whichever source wins, the PE
    FileVersion must match `version` -- protects against the always-latest
    endpoint lagging behind the manifest, and against repacked mirrors.
    Stored under the WeChatWin_<version>.exe name (stable, versioned).
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dest = DOWNLOAD_DIR / f"WeChatWin_{version}.exe"
    candidates = [OFFICIAL_VERSIONED.format(v=version), OFFICIAL_LATEST, manifest_url]
    errors = []
    for url in candidates:
        for i in range(RETRY):
            try:
                log(f"downloading from {url} (try {i + 1})")
                fetch_to(url, dest)
                got = pe_file_version(dest)
                if got != version:
                    raise RuntimeError(f"PE version mismatch: want {version}, file says {got!r} ({url})")
                log(f"downloaded {dest.name} ({dest.stat().st_size >> 20} MB, PE version {got})")
                return dest
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url}: {e}")
                log(f"download failed ({i + 1}/{RETRY}): {e}")
                time.sleep(15 * (i + 1))
    sys.exit("all download candidates failed:\n" + "\n".join(errors))


def release(path: Path, version: str) -> str:
    """Publish the installer as a GitHub Release. Returns the release URL."""
    env = dict(os.environ)
    tag = f"v{version}"
    run = lambda *args: subprocess.run(  # noqa: E731
        args, check=True, capture_output=True, text=True, env=env
    )
    try:
        existing = run("gh", "release", "view", tag, "--json", "url").stdout
        url = json.loads(existing)["url"]
        log(f"release {tag} already exists: {url}")
        return url
    except subprocess.CalledProcessError:
        pass  # no such release yet - normal path
    out = run(
        "gh", "release", "create", tag, str(path),
        "--title", f"WeChat Windows {version}",
        "--notes", f"Official installer for WeChat {version}, archived automatically.",
    )
    url = out.stdout.strip().splitlines()[-1]
    return url


def mirror(path: Path) -> str:
    """Mirror the file to your own bucket via your own signed-ticket endpoint.

    Contract (all values from Secrets, nothing hardcoded):
      POST $UPLOAD_TICKET_URL   header { $UPLOAD_TOKEN_HEADER: $UPLOAD_TOKEN }
      body {"filename": "..."}  -> {"data": {"token", "key", "upload_url"}}
      then multipart POST to upload_url with fields token/key + file.
    """
    endpoint = os.environ.get("UPLOAD_TICKET_URL", "")
    header = os.environ.get("UPLOAD_TOKEN_HEADER", "")
    token = os.environ.get("UPLOAD_TOKEN", "")
    if not (endpoint and header and token):
        return "skipped (mirror not configured)"
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
            up.raise_for_status()
            confirmed = up.json().get("key", "")
            if confirmed and confirmed != ticket["key"]:
                return f"failed (storage confirmed a different key: {confirmed})"
            return "ok"
        except Exception as e:  # noqa: BLE001
            log(f"mirror failed ({i + 1}/{RETRY}): {e}")
            time.sleep(20 * (i + 1))
    return "failed (see log above)"


def feishu_sign(secret: str) -> tuple[str, dict]:
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    sign = base64.b64encode(mac).decode()
    return ts, {"timestamp": ts, "sign": sign}


def notify(version: str, release_url: str, mirror_status: str) -> None:
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        log("notify skipped (no webhook configured)")
        return
    payload: dict = {
        "msg_type": "text",
        "content": {
            "text": (
                f"WeChat Windows new version: {version}\n"
                f"Release: {release_url}\n"
                f"Mirror: {mirror_status}"
            )
        },
    }
    secret = os.environ.get("FEISHU_SECRET", "")
    if secret:
        ts, extra = feishu_sign(secret)
        webhook = f"{webhook}&timestamp={ts}&sign={extra['sign']}"
    try:
        r = requests.post(webhook, json=payload, timeout=15)
        log(f"notify -> HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {e}")


def save_state(version: str, path: Path) -> None:
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    history = state.setdefault("history", [])
    history.append({"version": version, "file": path.name, "at": int(time.time())})
    state["latest"] = version
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
    m = fetch_manifest()
    version, url = m["version"], m["url"]
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if state.get("latest") == version and not force:
        log(f"no change (latest={version})")
        return
    log(f"new version detected: {version}")
    path = download(version, url)
    release_url = release(path, version)
    mirror_status = mirror(path)
    log(f"mirror: {mirror_status}")
    notify(version, release_url, mirror_status)
    save_state(version, path)
    commit_state(version)
    log("done")


if __name__ == "__main__":
    main()
