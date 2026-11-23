"""Updates for the Windows app, the one installed with ClipKontrast-Setup.exe.

Every few hours the app reads a small file, latest.json, published with each new version. If it names a newer
version, the dashboard offers "Update now". Most updates change only ClipKontrast's own code: a zip of a few MB is
downloaded, checked against the sha256 in latest.json, unpacked next to the running code as "app.new", and the app
restarts. The launcher (packaging/windows/ClipKontrast.cmd) swaps the folders before starting again. When an update
also needs new libraries (a different requirements.txt), the whole installer is downloaded and run instead, and it
starts ClipKontrast again when it is done. Clips, settings and keys live in the data folder, which neither touches.

A ClipKontrast run from a git folder or the zip is not affected: studio/updates.py looks after those.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

from . import config

EVERY = 6 * 3600                      # how often latest.json is read
RESTART_CODE = 75                     # the launcher swaps in app.new and starts again
INSTALLER_CODE = 76                   # the launcher runs the downloaded installer
ON = config.INSTALLED and bool(config.APP_VERSION) and config.env("CLIPKONTRAST_UPDATE_CHECK", "1") != "0"
UA = {"User-Agent": "ClipKontrast-app-update"}
_state = {"checked": 0.0, "result": None, "manifest": None, "reached": False}
_job = {"phase": "idle", "progress": 0.0, "error": ""}
_lock = threading.Lock()
_started = False


def _version(v: str) -> tuple:
    """"2026.11.19.2" -> (2026, 11, 19, 2), for comparing. Anything that isn't numbers compares as oldest."""
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(p) for p in parts[:6]) if parts else (0,)


def deps_here() -> str:
    """The libraries this installation was built with (written by the build next to Python)."""
    try:
        return (config.INSTALL_DIR / "python" / "DEPS").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _valid(m) -> bool:
    if not isinstance(m, dict):
        return False
    # https only; plain http just from this computer itself, which is how the build's own test serves an update
    https = lambda u: isinstance(u, str) and u.startswith(("https://", "http://127.0.0.1:"))        # noqa: E731
    return (isinstance(m.get("version"), str) and https(m.get("app_zip")) and https(m.get("installer"))
            and re.fullmatch(r"[0-9a-f]{64}", str(m.get("app_sha256", ""))) is not None
            and re.fullmatch(r"[0-9a-f]{64}", str(m.get("installer_sha256", ""))) is not None)


def _get(url: str, timeout=30):
    import httpx
    return httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)


def check(force: bool = False):
    """Read latest.json. Returns what the dashboard should say, or None when this copy is the newest."""
    if not ON:
        return None
    with _lock:
        if not force and time.time() - _state["checked"] < EVERY:
            return _state["result"]
        _state["checked"] = time.time()
        try:
            r = _get(config.UPDATE_MANIFEST, timeout=20)
            m = json.loads(r.content.decode("utf-8-sig")) if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 - offline: asked again next time
            _state["reached"] = False
            return _state["result"]
        _state["reached"] = m is not None
        if not _valid(m) or _version(m["version"]) <= _version(config.APP_VERSION):
            _state.update(result=None, manifest=None)
            return None
        notes = [{"title": str(n)[:140], "date": ""} for n in (m.get("notes") or [])[:20] if str(n).strip()]
        _state["manifest"] = m
        _state["result"] = {"current": config.APP_VERSION, "latest": m["version"][:40], "behind": None, "notes": notes,
                            "url": "", "restart": False, "installed": True,
                            "full": bool(m.get("deps")) and m.get("deps") != deps_here()}
        return _state["result"]


def latest():
    return _state["result"] if ON else None


def reached() -> bool:
    """Whether the last look got an answer (False: offline, or the file couldn't be read)."""
    return bool(_state["reached"])


def progress() -> dict:
    return dict(_job)


def _download(url: str, dest: Path, sha: str):
    """Download to dest, checking it is exactly the file latest.json describes."""
    import httpx
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    with httpx.stream("GET", url, headers=UA, timeout=httpx.Timeout(60, connect=20), follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    _job["progress"] = min(0.95, got / total)
    if h.hexdigest() != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("The download didn't match. Nothing was changed. Try again in a while.")
    tmp.replace(dest)


def _unpack(zip_path: Path, into: Path):
    """The new code into `into`, refusing anything that would land outside it."""
    if into.exists():
        shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    root = into.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (into / name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("The update file isn't valid. Nothing was changed.")
        z.extractall(into)
    if not (into / "studio" / "server.py").is_file():
        raise RuntimeError("The update file isn't valid. Nothing was changed.")


def _quit(code: int):
    """Leave a moment for the answer to reach the browser, then stop with the code the launcher acts on. Recordings
    running now carry on after the restart (studio/live.py: closing the app stops them, an update doesn't)."""
    try:
        (config.DATA_DIR / "resume-recordings").write_text("1", encoding="utf-8")
    except OSError:
        pass

    def go():
        time.sleep(1.5)
        sys.stdout.flush()
        os._exit(code)
    threading.Thread(target=go, daemon=True).start()


def apply() -> dict:
    """Start updating, in the background. The page follows it with progress()."""
    if not ON:
        raise RuntimeError("Updates are only for the installed ClipKontrast app.")
    m = _state["manifest"]
    if not m:
        check(force=True)
        m = _state["manifest"]
    if not m:
        raise RuntimeError("You already have the newest version.")
    if _job["phase"] in ("downloading", "restarting"):
        return progress()
    full = bool(m.get("deps")) and m.get("deps") != deps_here()
    _job.update(phase="downloading", progress=0.0, error="")

    def work():
        upd = config.INSTALL_DIR / "update"
        try:
            if full:
                _download(m["installer"], upd / "ClipKontrast-Setup.exe", m["installer_sha256"])
                _job.update(phase="restarting", progress=1.0)
                _quit(INSTALLER_CODE)
            else:
                z = upd / "app.zip"
                _download(m["app_zip"], z, m["app_sha256"])
                _unpack(z, config.INSTALL_DIR / "app.new")
                (config.INSTALL_DIR / "app.new" / "VERSION").write_text(m["version"][:40], encoding="utf-8")
                z.unlink(missing_ok=True)
                _job.update(phase="restarting", progress=1.0)
                _quit(RESTART_CODE)
        except Exception as e:  # noqa: BLE001 - shown on the page; the running copy is untouched
            shutil.rmtree(config.INSTALL_DIR / "app.new", ignore_errors=True)
            _job.update(phase="error", error=str(e)[:300])

    threading.Thread(target=work, daemon=True).start()
    return progress()


def start():
    global _started
    if not ON or _started:
        return
    _started = True

    def loop():
        time.sleep(20)                 # let the app finish starting first
        while True:
            try:
                check()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(EVERY)

    threading.Thread(target=loop, daemon=True, name="app-update").start()
