"""What a bug report from the clip maker carries besides the person's own words: which app and computer, what is
installed, the last problems ClipKontrast hit, and the end of its log. Shown to the person before it is sent.

Paths are cut down so the computer's user name never leaves it ("C:\\Users\\Rahul\\..." becomes "~\\...")."""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path

from . import config

LOG_LINES = 120
MAX_CHARS = 18000


def _scrub(text: str) -> str:
    home = str(Path.home())
    if home and len(home) > 3:
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    text = re.sub(r"(?i)([a-z]:\\users\\)[^\\/\s]+", r"\1<user>", text)          # any other Windows user folder
    text = re.sub(r"/(home|Users)/[^/\s]+", r"/\1/<user>", text)
    text = re.sub(r"(?i)(key|token|secret|password)(\s*[=:]\s*)\S+", r"\1\2<hidden>", text)   # never a key from a log line
    return text


def app_line() -> str:
    """One line: app version, system, memory."""
    from . import transcribe
    system = platform.system()
    if system == "Windows":
        system = f"Windows {platform.release()} ({platform.version()})"
    elif system == "Darwin":
        system = f"macOS {platform.mac_ver()[0]}"
    ram = f"{transcribe.RAM_GB:.0f} GB RAM" if transcribe.RAM_GB else "RAM unknown"
    kind = "installed app" if config.INSTALLED else "zip / source"
    return (f"ClipKontrast {config.APP_VERSION or 'dev'} ({kind}) · {system} · {platform.machine()} · {ram} · "
            f"{os.cpu_count() or '?'} CPUs · Python {sys.version.split()[0]}")[:200]


def _setup() -> list:
    from . import render, tools, whisper
    try:
        from . import media
        js = bool(media._js_runtimes())
    except Exception:  # noqa: BLE001
        js = False
    ffmpeg = shutil.which("ffmpeg") or ("found" if not tools.missing() else "")
    used = [k for k, v in render._hw_ok.items() if v] or ["processor (libx264)"]
    failed = [k for k, v in render._hw_ok.items() if v is False]
    from . import fetch_model
    fm = fetch_model.state
    speech = ("ready" if fetch_model.ready() else f"downloading ({fm['percent']}%)" if fm["doing"]
              else "not downloaded" + (f" (last problem: {fm['error']})" if fm["error"] else ""))
    return [f"ffmpeg: {'yes' if ffmpeg else 'NOT FOUND'}",
            f"Speech model (English): {speech}",
            f"Deno (for YouTube): {'yes' if js else 'no'}",
            f"Whisper (Hindi): {'installed' if whisper.installed() else 'not installed'}",
            "Hinglish captions model: " + ((f"downloaded, but {whisper.hl_state['error']}" if whisper.hl_state["error"] else "ready")
                                           if whisper.hinglish_ready() else
                                           f"downloading {whisper.hl_state['percent']}%" if whisper.hl_state["doing"] else
                                           f"not downloaded ({whisper.hl_state['error']})" if whisper.hl_state["error"] else
                                           "not downloaded yet"),
            f"AI: {'set up' if config.ai_ready() else 'not set up'}",
            f"Video encoder: {', '.join(used)}" + (f" (tried and failed: {', '.join(failed)})" if failed else "")]


def _problems() -> list:
    """The last videos and clips that went wrong, newest first."""
    from . import jobs
    out = []
    for j in sorted(list(jobs._jobs.values()), key=lambda j: j.get("created", 0), reverse=True)[:25]:
        title = (j.get("title") or (j.get("source") or {}).get("url") or "")[:90]
        when = time.strftime("%d %b %H:%M", time.localtime(j.get("created", 0)))
        if j.get("error"):
            where = f" (at {j['error_where']})" if j.get("error_where") else ""
            out.append(f"[{when}] video \"{title}\": {j['error'][:400]}{where}")
        for c in j.get("clips") or []:
            if c.get("status") == "error" and c.get("error"):
                out.append(f"[{when}] clip {c.get('id')} of \"{title}\" ({c.get('layout', '')}, {c.get('style', '')}): {c['error'][:300]}")
        if len(out) >= 12:
            break
    return out[:12]


def _recent() -> list:
    """The last few videos: how long they were, how long each step took, and where the transcript came from, so a
    slow computer shows which step is slow."""
    from . import jobs
    out = []
    for j in sorted(list(jobs._jobs.values()), key=lambda j: j.get("created", 0), reverse=True):
        if j.get("kind") != "video" or j.get("status") not in ("done", "error", "running"):
            continue
        t = j.get("timings") or {}
        steps = ", ".join(f"{k} {v // 60}m{v % 60:02d}s" for k, v in t.items())
        out.append(f"{(j.get('duration') or 0) / 60:.0f} min video, {j.get('status')}, took {j.get('took', '?')} s, "
                   f"{len(j.get('clips') or [])} clips, language {j.get('lang') or '?'}, transcript {j.get('transcript_from') or 'own'}"
                   + (f" | {steps}" if steps else ""))
        if len(out) >= 5:
            break
    return out


def _log_tail() -> str:
    f = config.DATA_DIR / "logs" / "clipkontrast.log"
    try:
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 60000))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-LOG_LINES:]
        return "\n".join(lines)
    except OSError:
        return ""


def details() -> str:
    """Everything technical a report carries, as plain text the person can read before sending."""
    parts = ["Setup:", *("  " + x for x in _safe(_setup))]
    from . import jobs
    parts += [f"  Clips made at once: {jobs.render_workers()}"]
    recent = _safe(_recent)
    if recent:
        parts += ["", "Recent videos:", *("  " + r for r in recent)]
    probs = _safe(_problems)
    parts += ["", "Recent problems:", *(("  " + p) for p in probs)] if probs else ["", "Recent problems: none"]
    tail = _safe(_log_tail, "")
    if tail:
        parts += ["", f"End of the log (last {LOG_LINES} lines):", tail]
    return _scrub("\n".join(parts))[-MAX_CHARS:]


def _safe(fn, empty=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a report goes out even if one part of it can't be gathered
        return [f"(couldn't read: {str(e)[:80]})"] if empty is None else empty
