"""The editor's library: each person's own fonts, sounds and media (pictures, GIFs, videos, memes), uploaded once and
used in any clip: a font for the big text or the captions, a sound as a sound effect, a picture or video laid over
the clip.

Every file is checked and made into one safe, known format before it is kept: fonts must open as fonts, sounds become
AAC audio, pictures become PNG, GIFs and videos become H.264 video. Nothing is ever run, and nothing a person uploads
is served as anything but its own kind.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config
from .media import NO_WINDOW

KINDS = {"font": (".ttf", ".otf"), "sound": (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm", ".mp4"),
         "media": (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mkv", ".m4v")}
MAX_MB = {"font": 15, "sound": 40, "media": 200}
MAX_ITEMS = 300
SOUND_MAX_S, VIDEO_MAX_S = 180, 120
ID = re.compile(r"^[0-9a-f]{12}$")
_lock = threading.Lock()


class LibraryError(ValueError):
    pass


def root(uid: str) -> Path:
    return config.user_dir(uid) / "library"


def _index(uid: str) -> list:
    f = root(uid) / "index.json"
    try:
        items = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
    except (OSError, ValueError):
        items = []
    return [i for i in items if isinstance(i, dict) and ID.match(str(i.get("id", "")))]


def _save(uid: str, items: list):
    d = root(uid)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "index.json.part"
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    tmp.replace(d / "index.json")


def public(item: dict) -> dict:
    """What the app sees of an item: never a path on this computer."""
    out = {k: item.get(k) for k in ("id", "kind", "name", "family", "type", "dur", "w", "h", "audio", "added")}
    out["url"] = f"/api/library/{item['id']}/file"
    if item.get("thumb"):
        out["thumb"] = f"/api/library/{item['id']}/thumb"
    return out


def listing(uid: str) -> list:
    return [public(i) for i in sorted(_index(uid), key=lambda x: -x.get("added", 0))]


def get(uid: str, iid: str) -> dict | None:
    if not ID.match(str(iid or "")):
        return None
    return next((i for i in _index(uid) if i["id"] == iid), None)


def path(uid: str, iid: str, thumb=False) -> Path | None:
    i = get(uid, iid)
    if not i:
        return None
    f = root(uid) / (i.get("thumb") if thumb else i["file"]) if (i.get("thumb") or not thumb) else None
    return f if f and f.is_file() else None


def _probe(f: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(f)],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    try:
        d = json.loads(p.stdout or "{}")
    except ValueError:
        d = {}
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"dur": float((d.get("format") or {}).get("duration") or 0), "w": int(v.get("width") or 0), "h": int(v.get("height") or 0),
            "video": bool(v), "audio": any(s.get("codec_type") == "audio" for s in streams)}


def _ff(args, timeout=600):
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise LibraryError("That file couldn't be read. Try another one.")


def add(uid: str, kind: str, filename: str, upload: Path) -> dict:
    """Check an uploaded file and keep it in the library, made into its kind's safe format. The upload is removed."""
    try:
        if kind not in KINDS:
            raise LibraryError("Pick fonts, sounds or media.")
        ext = Path(filename or "").suffix.lower()
        if ext not in KINDS[kind]:
            raise LibraryError(f"That isn't a {'font' if kind == 'font' else 'sound' if kind == 'sound' else 'picture or video'} file "
                               f"PapiClips can use ({', '.join(KINDS[kind])}).")
        if upload.stat().st_size > MAX_MB[kind] * 1024 * 1024:
            raise LibraryError(f"That file is too big. The limit is {MAX_MB[kind]} MB.")
        with _lock:
            items = _index(uid)
        if len(items) >= MAX_ITEMS:
            raise LibraryError("Your library is full. Delete some things you don't use first.")
        iid = secrets.token_hex(6)
        d = root(uid) / kind
        d.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[\x00-\x1f<>\"\\]+", "", Path(filename).stem)[:60].strip() or kind
        item = {"id": iid, "kind": kind, "name": name, "added": time.time()}
        if kind == "font":
            try:
                from PIL import ImageFont
                f = ImageFont.truetype(str(upload), 40)
                fam, style = f.getname()
            except Exception:  # noqa: BLE001 - not a font
                raise LibraryError("That file isn't a font PapiClips can open.")
            dest = d / f"{iid}{ext}"
            shutil.copyfile(upload, dest)
            from .render import ass_family
            item.update(file=f"font/{dest.name}", family=ass_family(dest, " ".join(x for x in (fam, style) if x and x != "Regular") or name)[:60],
                        type=ext[1:])
        elif kind == "sound":
            info = _probe(upload)
            if not info["audio"]:
                raise LibraryError("That file has no sound in it.")
            if info["dur"] > SOUND_MAX_S:
                raise LibraryError(f"Sounds can be up to {SOUND_MAX_S // 60} minutes long.")
            dest = d / f"{iid}.m4a"
            _ff(["-i", str(upload), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(dest)])
            item.update(file=f"sound/{dest.name}", dur=round(info["dur"], 2), type="sound")
        else:
            info = _probe(upload)
            if not info["video"] or not info["w"]:
                raise LibraryError("That file isn't a picture or video PapiClips can open.")
            still = ext in (".png", ".jpg", ".jpeg", ".webp")
            if still:
                dest = d / f"{iid}.png"
                _ff(["-i", str(upload), "-frames:v", "1", "-vf", "scale='min(1080,iw)':-2", str(dest)])
                item.update(file=f"media/{dest.name}", type="image", w=info["w"], h=info["h"])
            else:
                dur = info["dur"] or 0
                if dur > VIDEO_MAX_S:
                    raise LibraryError(f"Videos can be up to {VIDEO_MAX_S // 60} minutes long. Trim it first.")
                dest = d / f"{iid}.mp4"
                audio = info["audio"] and ext != ".gif"
                args = ["-i", str(upload), "-vf", "scale='min(1080,iw)':-2:flags=lanczos,fps=30,format=yuv420p", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart"]
                args += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"] if audio else ["-an"]
                _ff(args + [str(dest)])
                got = _probe(dest)
                item.update(file=f"media/{dest.name}", type="gif" if ext == ".gif" else "video", w=got["w"], h=got["h"],
                            dur=round(got["dur"], 2), audio=audio)
            th = d / f"{iid}.thumb.jpg"
            try:
                _ff(["-i", str(dest), "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "4", str(th)], timeout=60)
                item["thumb"] = f"media/{th.name}"
            except LibraryError:
                pass
        with _lock:
            items = _index(uid)
            items.append(item)
            _save(uid, items)
        return public(item)
    finally:
        upload.unlink(missing_ok=True)


def delete(uid: str, iid: str) -> bool:
    with _lock:
        items = _index(uid)
        it = next((i for i in items if i["id"] == iid), None)
        if not it:
            return False
        for k in ("file", "thumb"):
            if it.get(k):
                (root(uid) / it[k]).unlink(missing_ok=True)
        _save(uid, [i for i in items if i["id"] != iid])
    return True


def resolve(uid: str, ed: dict) -> dict:
    """The library files an edit uses, by id, for making the clip: {id: {path, kind, family, type, dur, audio}}. Ones
    deleted since are left out (the clip is made without them)."""
    if not uid or not ed:
        return {}
    want = set()
    for k in ("hook_font", "cap_font"):
        if ed.get(k):
            want.add(ed[k])
    for s in ed.get("sfx") or []:
        if str(s.get("kind", "")).startswith("lib:"):
            want.add(s["kind"][4:])
    for m in ed.get("media") or []:
        want.add(m.get("id"))
    out = {}
    try:
        items = {i["id"]: i for i in _index(uid)}
    except ValueError:
        return {}
    for iid in want:
        it = items.get(iid)
        if it and (root(uid) / it["file"]).is_file():
            out[iid] = {"path": str(root(uid) / it["file"]), "kind": it["kind"], "family": it.get("family", ""),
                        "type": it.get("type", ""), "dur": it.get("dur", 0), "audio": bool(it.get("audio"))}
    return out
