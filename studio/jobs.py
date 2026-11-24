"""Job pipeline: get video -> audio -> transcript -> AI picks -> rendered clips. One heavy task at a time."""
import collections
import json
import os
import math
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid

import numpy as np
from pathlib import Path

from . import brain, config, creators, ctx, facecam, games, hashtags, load, media, quality, render, roman, safe, transcribe, usage, whisper
from . import tag as tagmod

class _FairQueue:
    """Whose video is next. Each person has their own line and the lines take turns, so someone who adds ten
    videos does not make everyone else wait behind all ten. Small tasks (re-making one clip) go first."""

    def __init__(self):
        self.cv = threading.Condition()
        self.quick = collections.deque()
        self.lines = {}                       # user -> their waiting videos, oldest first
        self.turn = collections.deque()       # users with something waiting, in the order they will be served
        self.epoch = 0                        # goes up whenever the line moves
        self._pos = (0.0, {})
        self.running = 0                      # videos being worked on right now
        self.last_start = 0.0
        self.held = False                     # videos are waiting only because the machine is near full

    def _room(self) -> bool:
        """May one more video start? One always may. More only while the machine has room, and a few seconds
        after the last one started, so its weight is already showing in the measurements."""
        if self.running == 0:
            return True
        return self.running < config.JOB_WORKERS and time.time() - self.last_start > 6 and not load.busy()

    def done(self):
        with self.cv:
            self.running = max(0, self.running - 1)
            self.cv.notify_all()

    def put(self, item, user=None):
        with self.cv:
            if user is None:
                self.quick.append(item)
            else:
                if user not in self.lines:
                    self.lines[user] = collections.deque()
                    self.turn.append(user)
                self.lines[user].append(item)
            self.epoch += 1
            self.cv.notify()

    def get(self, quick_only=False):
        with self.cv:
            while True:
                if self.quick:
                    self.epoch += 1
                    return self.quick.popleft()
                if not quick_only and self.turn:
                    if self._room():
                        break
                    if not self.held:
                        self.held = True
                        self.epoch += 1
                elif self.held:
                    self.held = False
                    self.epoch += 1
                self.cv.wait(1.0)             # looked at again every second: the line moves the moment there is room
            self.epoch += 1
            self.held = False
            self.running += 1
            self.last_start = time.time()
            user = self.turn.popleft()
            item = self.lines[user].popleft()
            if self.lines[user]:
                self.turn.append(user)
            else:
                del self.lines[user]
            return item

    def ahead(self) -> dict:
        """job id -> how many videos will start before it. Worked out at most once a second."""
        with self.cv:
            if time.time() - self._pos[0] < 1:
                return self._pos[1]
            out, lines, order, n = {}, {u: list(q) for u, q in self.lines.items()}, list(self.turn), 0
            depth = 0
            while order:
                nxt = []
                for u in order:
                    if depth < len(lines[u]):
                        out[lines[u][depth][1]] = n
                        n += 1
                        if depth + 1 < len(lines[u]):
                            nxt.append(u)
                order, depth = nxt, depth + 1
            self._pos = (time.time(), out)
            return out


_q = _FairQueue()
_live_q: "queue.Queue" = queue.Queue()   # live clips never wait behind a long video
_jobs: dict = {}
_by_user: dict = {}                       # user -> {job id: job}: listing one person's videos never scans everyone's
_lock = threading.RLock()
on_clip_ready: list = []   # callbacks (job, clip), run once when a clip first finishes

DEFAULTS = {"max_clips": 6, "min_len": 15, "max_len": 60, "style": "bounce", "layout": "blur", "quality": 720,
            "range_start": None, "range_end": None, "captions": True, "lang": "auto", "caption_lang": "auto",
            "zoom": True, "watermark": "", "tighten": True, "cam_hint": "auto",
            "cam_box": None, "game_box": None, "split": "balanced",
            "tag": False, "tag_handle": "", "tag_platforms": [], "tag_style": "clean", "tag_pos": "bottom_left",
            "exact": False}
EXACT_MIN, EXACT_MAX = 3, 180          # an exact moment: one clip of exactly the part chosen, 3 seconds to 3 minutes
TAG_KEYS = ("tag", "tag_handle", "tag_platforms", "tag_style", "tag_pos")      # the creator tag (studio/tag.py)
CAM_PRESETS = {"tl": [0.0, 0.0, 0.28, 0.28], "tr": [0.72, 0.0, 0.28, 0.28], "bl": [0.0, 0.72, 0.28, 0.28],
               "br": [0.72, 0.72, 0.28, 0.28], "bc": [0.36, 0.72, 0.28, 0.28]}
CLIP_LOOK = ("style", "layout", "captions", "zoom", "watermark", "tighten") + TAG_KEYS     # what each clip carries from its video's settings


def clean_watermark(v) -> str:
    """The clipper's @handle drawn on their clips: plain text, short, nothing that could change the captions."""
    import re as _re
    return _re.sub(r"[\x00-\x1f\x7f{}\\]", "", str(v or "")).strip()[:40]


LIMITS = {"max_clips": (1, 20), "min_len": (5, 120), "max_len": (10, 180), "quality": (360, 1080)}


def clean_box(v):
    """A box drawn on a frame of the video, as fractions of the picture [x, y, w, h], or None. Raises on nonsense."""
    if v is None:
        return None
    if not (isinstance(v, (list, tuple)) and len(v) == 4 and all(isinstance(n, (int, float)) and not isinstance(n, bool)
                                                                  and math.isfinite(n) for n in v)):
        raise ValueError("That box isn't valid.")
    x, y, w, h = (float(n) for n in v)
    if w < 0.03 or h < 0.03 or x < -0.001 or y < -0.001 or x + w > 1.001 or y + h > 1.001:
        raise ValueError("The box has to be inside the picture, and not tiny.")
    return [round(max(0.0, x), 4), round(max(0.0, y), 4), round(min(w, 1 - max(0.0, x)), 4), round(min(h, 1 - max(0.0, y)), 4)]


def clean_options(options) -> dict:
    """Only known settings, each inside its allowed range. Raises ValueError on nonsense."""
    out = {}
    for k, v in (options if isinstance(options, dict) else {}).items():
        if k in ("cam_box", "game_box"):
            out[k] = clean_box(v)
            continue
        if k == "split":
            out[k] = v if v in render.SPLITS else "balanced"
            continue
        if k == "tag":
            if not isinstance(v, bool):
                raise ValueError("The creator tag must be on or off.")
            out[k] = v
            continue
        if k == "exact":
            if not isinstance(v, bool):
                raise ValueError("'Exactly this part' must be on or off.")
            out[k] = v
            continue
        if k == "tag_handle":
            out[k] = tagmod.clean_handle(v)
            continue
        if k == "tag_platforms":
            out[k] = tagmod.clean_platforms(v)
            continue
        if k == "tag_style":
            out[k] = v if v in tagmod.STYLES else "clean"
            continue
        if k == "tag_pos":
            out[k] = v if v in tagmod.POSITIONS else "bottom_left"
            continue
        if k in ("captions", "zoom", "tighten"):
            if not isinstance(v, bool):
                raise ValueError({"captions": "Captions", "zoom": "Auto zoom", "tighten": "Cutting silences"}[k] + " must be on or off.")
            out[k] = v
            continue
        if k == "watermark":
            out[k] = clean_watermark(v)
            continue
        if k == "cam_hint":
            out[k] = v if v in CAM_PRESETS or v == "none" else "auto"
            continue
        if k not in DEFAULTS or v in ("", None):
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(float(v))))
        elif k == "style":
            v = v if v in render.STYLES else DEFAULTS["style"]
        elif k == "layout":
            v = v if v in render.LAYOUTS else DEFAULTS["layout"]
        elif k == "lang":
            v = v if v in whisper.LANGUAGES else "auto"
        elif k == "caption_lang":
            v = v if v in whisper.CAPTION_LANGS else "auto"
        else:                                    # range_start, range_end: seconds into the video
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError("That part of the video isn't valid.")
        out[k] = v
    if out.get("range_end") is not None and out["range_end"] <= out.get("range_start", 0):
        raise ValueError("The end of the chosen part must be after its start.")
    if out.get("exact"):
        if out.get("range_end") is None:
            raise ValueError("Choose where the moment starts and ends.")
        length = out["range_end"] - (out.get("range_start") or 0)
        if not EXACT_MIN <= length <= EXACT_MAX:
            raise ValueError(f"An exact moment is {EXACT_MIN} seconds to {EXACT_MAX // 60} minutes long. Choose a shorter part, "
                             "or let the AI find the best clips inside it.")
    return out


def job_dir(jid) -> Path:
    return Path(_jobs[jid]["dir"])


HIDDEN = ("dir", "user", "source_path", "recover")


def public(job) -> dict:
    """What the browser may see: no server paths, no owner id."""
    out = {k: v for k, v in job.items() if k not in HIDDEN}
    out["source"] = {k: v for k, v in job["source"].items() if k != "path"}
    out["clips"] = [{k: v for k, v in c.items() if k not in ("src", "fixed_words", "fixed_for", "hl_words", "hl_for", "en_words",
                                                              "en_for", "hi_words", "hi_for")} for c in job["clips"]]
    ai = job.get("ai") or {}
    out["ai"] = {"tokens": ai.get("tokens", 0), "note": ai.get("note", ""), "used": bool(ai.get("model"))}   # never the model, never raw errors
    if job.get("status") == "queued":
        n = _q.ahead().get(job["id"])
        if n:
            out["stage"] = (f"High demand right now: {n} video{'s' if n != 1 else ''} ahead of yours" if _q.held else
                            f"Waiting in line: {n} video{'s' if n != 1 else ''} ahead of yours")
        elif n == 0 and _q.held:
            out["stage"] = "High demand right now: yours is next"
    return out


def save(job):
    ctx.bump(job.get("user"))
    with _lock:
        d = Path(job["dir"])
        for _ in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:  # changed mid-dump by another thread
                time.sleep(0.05)
        else:
            return
        tmp = d / "job.json.tmp"
        try:
            tmp.write_text(text, encoding="utf-8")
        except OSError:
            return                                   # kept in memory; written with the next change
        for wait in (0.05, 0.2, 0.5, 1.0, 0):
            try:
                tmp.replace(d / "job.json")
                return
            except PermissionError:
                # An antivirus or Windows Search reading job.json a moment (WinError 5 / 32): a video must never
                # fail for it. Tried again; if it's still held, written next time something changes.
                if wait:
                    time.sleep(wait)


class Stopped(BaseException):
    """The person pressed Stop: raised in the worker making the video at its next step. A BaseException, so the
    many places that carry on past a failed step (another of YouTube's apps, a model that didn't answer) don't."""


_making: dict = {}           # video id -> the worker thread making it


def _set(job, **kw):
    if job.get("stop") and "status" not in kw and _making.get(job.get("id")) == threading.get_ident():
        raise Stopped()
    with _lock:
        job.update(kw)
    save(job)


STOPPED = "Stopped by you"


def stop(jid: str) -> dict:
    """Stop a video being made (or waiting its turn). The clips already made are kept; the rest aren't made. The
    worker stops at its next step (a download or a transcription finishes its current piece first)."""
    job = get(jid)
    if not job or job.get("status") not in ("queued", "running"):
        raise ValueError("This video isn't being made right now.")
    job["stop"] = True
    if job["status"] == "queued" and _making.get(jid) is None:
        _stopped(job)                      # not started yet: stopped now; the worker skips it when its turn comes
    else:
        _set(job, stage="Stopping…")
    return job


def _stopped(job):
    for c in job.get("clips", []):
        if c.get("status") in ("queued", "rendering"):
            c.update(status="error", error="Stopped before this clip was made. Press Re-make to make it.")
    done = any(c.get("status") == "done" for c in job.get("clips", []))
    with _lock:
        job.update(status="done" if done else "error", stage=STOPPED, progress=1.0 if done else job.get("progress", 0),
                   error=None if done else STOPPED + ".", resume=False)
    save(job)
    try:
        creators.log(job["creator"], f"Video stopped: {str(job.get('name') or '')[:80]}")
    except Exception:  # noqa: BLE001
        pass


def _remap(path, folder: Path):
    """Paths saved before data moved into per-user folders (or on another machine): find the file by name."""
    if path and not Path(path).exists():
        cand = folder / Path(str(path).replace("\\", "/")).name
        if cand.exists():
            return str(cand)
    return path


def _load_file(f: Path, uid: str):
    try:
        job = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return
    job.update(dir=str(f.parent), user=uid)
    job.setdefault("kind", "video")
    job.setdefault("creator", None)
    job.setdefault("options", {}).setdefault("captions", True)
    if job.get("source_path"):
        job["source_path"] = _remap(job["source_path"], f.parent)
    if job.get("source", {}).get("path"):
        job["source"]["path"] = _remap(job["source"]["path"], f.parent)
    if job.get("status") in ("queued", "running") and job.get("kind") != "live":
        # Picked up again as soon as the server is back (jobs.start), from the last finished part of the transcript.
        job.update(status="queued", stage="Picking up where it stopped", error=None, resume=True)
    if job.get("status") == "live":
        job.update(status="done", stage="Recording paused by a server update", was_live=True)
    buffered = job.get("kind") == "live" and any((f.parent / "buffer").glob("*.ts"))
    for c in job.get("clips", []):
        c.setdefault("captions", job["options"]["captions"])
        if c.get("src"):
            c["src"] = _remap(c["src"], f.parent / "clips")
        if c.get("status") in ("rendering", "queued"):
            if buffered and not c.get("src"):
                c["status"] = "queued"  # the stream is still on disk: live.recover() finishes these
                job["recover"] = True
            elif job.get("resume"):
                c["status"] = "queued"  # the video is being picked up again: this clip is made then
            elif job.get("kind") != "live" and (f.parent / "words.json").is_file():
                c["status"] = "queued"  # a single clip being re-made: jobs.start() makes it again
                job.setdefault("remake", []).append(c["id"])
            else:
                c.update(status="error", error="Interrupted by a restart. Open it and press Save and re-make.")
    _jobs[job["id"]] = job
    _by_user.setdefault(uid, {})[job["id"]] = job


def load_user(uid: str):
    d = config.USERS_DIR / uid
    for f in list((d / "jobs").glob("*/job.json")) + list((d / "creators").glob("*/*/*/job.json")):
        _load_file(f, uid)


def load_all():
    for d in config.USERS_DIR.iterdir():
        if d.is_dir():
            load_user(d.name)


def all_jobs() -> list:
    """Every user's jobs. Only for background work; never send this to a browser."""
    with _lock:
        return list(_jobs.values())


def list_jobs():
    u = ctx.uid()
    with _lock:
        return sorted((dict(j) for j in _by_user.get(u, {}).values()), key=lambda j: -j["created"])


def get(jid):
    """The job, only if it belongs to whoever this code is running for."""
    job = _jobs.get(jid)
    return job if job and job["user"] == ctx.uid() else None


def busy_count() -> int:
    u = ctx.uid()
    return sum(1 for j in list(_by_user.get(u, {}).values()) if j["kind"] == "video" and j["status"] in ("queued", "running"))


def waiting_total() -> int:
    return sum(1 for j in list(_jobs.values()) if j["kind"] == "video" and j["status"] == "queued")


def queue_epoch() -> int:
    return _q.epoch


def running_now() -> int:
    return _q.running


def create(source: dict, options: dict, enqueue=True, creator=None, kind="video") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    c = creators.get(creator) if creator else None
    if c:
        d = creators.root(c["id"]) / ("live" if kind == "live" else "videos") / jid
        base = {**DEFAULTS, **{k: c[k] for k in ("max_clips", "min_len", "max_len", "style", "layout", "captions", "lang", "caption_lang",
                                                 "zoom", "watermark", "tighten", "cam_hint") + TAG_KEYS if k in c}}
    else:
        d, base, creator = config.user_dir(ctx.uid()) / "jobs" / jid, dict(DEFAULTS), None
    opts = {**base, **clean_options(options)}
    if opts["min_len"] >= opts["max_len"]:
        opts["min_len"] = max(5, opts["max_len"] - 5)
    job = {"id": jid, "user": ctx.uid(), "kind": kind, "creator": creator, "dir": str(d), "created": time.time(), "source": source,
           "options": opts, "status": "queued", "stage": "Waiting", "progress": 0.0,
           "title": source.get("name", ""), "uploader": "", "duration": 0, "clips": [], "error": None, "ai": {}}
    d.mkdir(parents=True)
    with _lock:
        _jobs[jid] = job
        _by_user.setdefault(job["user"], {})[jid] = job
    save(job)
    if enqueue:
        _q.put(("job", jid, None), user=job["user"])
    return job


def enqueue(job):
    save(job)
    _q.put(("job", job["id"], None), user=job["user"])


def submit(fn):
    """Run any heavy task (a render, a cleanup) on the single worker, in order."""
    _q.put(("fn", ctx.bind(fn), None))


def submit_live(fn):
    """Live-stream renders and cleanups: their own worker, so a long video can never hold them up."""
    _live_q.put(ctx.bind(fn))


def _live_worker():
    while True:
        fn = _live_q.get()
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def delete(jid):
    with _lock:
        job = _jobs.pop(jid, None)
        if job:
            _by_user.get(job["user"], {}).pop(jid, None)
    if job:
        ctx.bump(job["user"])
        shutil.rmtree(job["dir"], ignore_errors=True)
    return bool(job)


def request_render(jid, cid, changes: dict):
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    new = dict(clip)
    for k in ("captions", "zoom", "watermark", "tighten") + TAG_KEYS:
        if k in changes:
            new.update(clean_options({k: changes[k]}))
    for k in ("start", "end"):
        if k in changes:
            new[k] = round(float(changes[k]), 2)
    if "src_offset" in clip:  # live clip: can only move inside the saved piece of stream
        new["start"] = max(new["start"], clip["src_offset"])
        new["end"] = min(new["end"], clip["src_end"])
    if not (math.isfinite(new["start"]) and math.isfinite(new["end"])) or new["start"] < 0:
        raise ValueError("Those times aren't valid.")
    if not 3 <= new["end"] - new["start"] <= 180:
        raise ValueError("A clip can be 3 to 180 seconds long.")
    for k in ("hook", "title", "description", "style", "layout"):
        if k in changes:
            new[k] = str(changes[k])[:{"hook": 60, "title": 100, "description": 2000}.get(k, 20)]
    for k in ("hook_x", "hook_y", "caption_x", "caption_y", "tag_x", "tag_y", "watermark_x", "watermark_y"):
        if k in changes and changes[k] not in ("", None):
            try:
                new[k] = round(float(changes[k]), 1)
            except (ValueError, TypeError):
                pass
    if "hashtags" in changes:
        new["hashtags"] = clean_hashtags(changes["hashtags"])
    if new.get("style") not in render.STYLES:
        new["style"] = DEFAULTS["style"]
    if new.get("layout") not in render.LAYOUTS:
        new["layout"] = DEFAULTS["layout"]
    clip.update(new, status="queued")
    save(job)
    _q.put(("clip", jid, cid))



def clean_hashtags(v) -> list:
    """Hashtags typed by a person ("#funny gaming") or sent by the page as a list: plain words, at most 8."""
    if isinstance(v, str):
        v = v.split()
    if not isinstance(v, list):
        raise ValueError("Hashtags must be words separated by spaces.")
    tags = [re.sub(r"\W", "", str(h))[:30] for h in v[:50] if isinstance(h, (str, int))]
    return [t for t in tags if t][:8]


def save_post_text(job, clip, changes: dict):
    """Change a clip's title, description and hashtags without making the clip again. Posting reads them
    from the clip when it uploads, so the next post (or one still waiting in the queue) uses the new text."""
    text = {k: str(changes[k] or "")[:{"title": 100, "description": 2000}[k]] for k in ("title", "description") if k in changes}
    if "hashtags" in changes:
        text["hashtags"] = clean_hashtags(changes["hashtags"])
    if clip.get("hashtags_in_description") and not clip.get("fixed_description") and text:
        desc = hashtags._TAG_IN_TEXT.sub("", text.get("description", clip.get("description") or "")).strip()
        line = " ".join("#" + t for t in text.get("hashtags", clip.get("hashtags") or []))
        text["description"] = ((desc + "\n\n" + line).strip() if desc else line)[:2000]
    with _lock:
        clip.update(text)
    save(job)
    side = Path(job["dir"]) / "clips" / f"{clip['id']}.json"
    try:
        if side.is_file():
            side.write_text(json.dumps({**json.loads(side.read_text(encoding="utf-8")), **text}, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def spoken_in(job, clip) -> str:
    """What is said during a clip, from the video's transcript."""
    try:
        words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return " ".join(str(w.get("w", "")) for w in words
                    if isinstance(w, dict) and clip["start"] - 0.5 <= _f(w.get("s")) <= clip["end"] + 0.5)


def _with_channel(job) -> dict:
    """The job, with the streamer's handle and platform: the video's own channel, else (an uploaded file, say) the
    creator's channel link."""
    if job.get("handle") or not job.get("creator"):
        return job
    c = creators.get(job["creator"]) or {}
    for p, url in (c.get("channels") or {}).items():
        h = tagmod.clean_handle(url)
        if h:
            return dict(job, handle=h, platform=job.get("platform") or p)
    return job


def render_one(job, clip, words):
    if job.get("stop") and job.get("status") in ("queued", "running"):
        clip.update(status="error", error="Stopped before this clip was made. Press Re-make to make it.")
        save(job)
        return
    d = Path(job["dir"]).resolve()
    clip["status"] = "rendering"
    save(job)
    try:
        if not clip.get("src") and not job.get("source_path"):
            _piece_again(job, clip)
        src, meta = _clip_source(job, clip)
        src = src.resolve()
        off = clip.get("src_offset", 0.0) if clip.get("src") else 0.0
        local = dict(clip, start=clip["start"] - off, end=clip["end"] - off, hook=roman.to_roman(clip.get("hook") or ""))
        local["tag"] = tagmod.spec(_with_channel(job), clip)          # the streamer's handle on it, if asked for
        near = _caption_words(job, clip, words, off, src) if clip.get("captions", True) else []
        # the size of the file this clip is really cut from (a piece can come in another size than the first one)
        out = render.render_clip(src, (d / "clips").resolve(), clip["id"], local, near, meta["width"] or job["width"],
                                 meta["height"] or job["height"])
        # The layout chosen stays the clip's choice; what was really used is kept beside it, with why if they differ.
        used = out.pop("layout", clip.get("layout"))
        note = ""
        if clip.get("layout") in render.FACECAM_LAYOUTS and used not in render.FACECAM_LAYOUTS:
            note = ("The camera fills the picture in this part of the stream, so the clip is zoomed onto it."
                    if clip.get("camera") == "full" else
                    "No webcam was found in this part of the stream, so the whole picture is shown.")
        clip.update(out, layout_used=used, layout_note=note, status="done", version=clip.get("version", 0) + 1, error=None)
        post = {k: clip.get(k) for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}
        (d / "clips" / f"{clip['id']}.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
        if clip["version"] == 1:
            clip.setdefault("made_at", time.time())
            creators.log(job["creator"], f"Clip ready (score {clip['score']}): {clip['title'] or clip['hook']}")
            save(job)
            for fn in on_clip_ready:
                try:
                    fn(job, clip)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=safe.friendly(e, 400))
        creators.log(job["creator"], f"Clip failed: {safe.friendly(e, 200)}")
    save(job)


def _pick_language(job, o, yt, audio, resuming=False):
    """(the language spoken, YouTube's transcript if it is still the one to use). Sets job["lang"]."""
    want = o.get("lang") or "auto"
    if want != "auto" and yt and yt[1].split("-")[0].lower() != want:
        yt = None                                  # YouTube's transcript is in another language: ours is written
        job["transcript_from"] = "own"
    if want != "auto":
        spoken = want
    elif yt:
        spoken = yt[1]
    elif resuming and job.get("lang"):
        spoken = job["lang"]
    else:
        spoken = _detect_language(job, audio)
    job["lang"] = spoken
    return spoken, yt


def _detect_language(job, audio) -> str:
    """Which language is spoken, told by Whisper from the liveliest half minute. English if it can't tell."""
    try:
        got = whisper.detect(audio, note=lambda t: _set(job, stage=t))
    except Exception:  # noqa: BLE001 - no Whisper: treated as English, as before
        got = None
    if not got:
        return "en"
    lang, _sure = got
    # Spoken Hindi and spoken Urdu sound the same; Whisper often calls Hindi "Urdu" and writes it in Urdu script,
    # which spells badly in Roman letters. A person who really wants Urdu can pick it.
    return "hi" if lang == "ur" else lang


def _hinglish_mode(job) -> bool:
    """Captions "as spoken" for a video in Hindi (or Urdu, which sounds the same): Hinglish, in Roman letters."""
    want = (job.get("options") or {}).get("caption_lang") or "auto"
    return want == "auto" and (job.get("lang") or "").lower().startswith(("hi", "ur"))


def _clip_sound(job, clip, src, off):
    """The clip's own sound with a second either side (16 kHz), and the video's second it starts at."""
    a = max(0.0, clip["start"] - off - 1.0)
    b = clip["end"] - off + 1.0
    wav = Path(job["dir"]) / f"{clip['id']}.sound.wav"
    try:
        media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(src), "-vn",
                   "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        return media.load_audio(wav), a + off
    finally:
        media.remove(wav)


def _clip_lang(job, clip, src, off, sound=None):
    """The language spoken in this clip, heard from its own sound (kept with the clip), or the video's when that
    can't be told. A video taken for Hindi can have English clips (or be wholly English, heard wrong): their captions
    must not go to the Hindi model."""
    span = [round(clip["start"], 2), round(clip["end"], 2)]
    if clip.get("lang_for") == span and clip.get("cap_lang"):
        return clip["cap_lang"]
    try:
        audio = sound[0] if sound else _clip_sound(job, clip, src, off)[0]
        got = whisper.clip_language(audio)
    except Exception:  # noqa: BLE001
        got = None
    lang = "hi" if got in ("hi", "ur") else got or (job.get("lang") or "en").split("-")[0]
    clip.update(cap_lang=lang, lang_for=span)
    return lang


def _hinglish_words(job, clip, src, off, sound=None):
    """This clip's words in Hinglish, written by the Hinglish model straight from the clip's own sound (timed on the
    video's clock), or None: the model isn't downloaded yet (it starts downloading now, in the background), or it
    couldn't hear anything. Kept with the clip, so remaking it (another style, say) doesn't listen again."""
    span = [round(clip["start"], 2), round(clip["end"], 2), whisper.HL_TAG]
    if clip.get("hl_for") == span and clip.get("hl_words"):
        return clip["hl_words"]
    if not whisper.hinglish_ready():
        whisper.fetch_hinglish_soon()
        return None
    try:
        audio, base = sound or _clip_sound(job, clip, src, off)
        got = whisper.hinglish(audio)
    except Exception as e:  # noqa: BLE001 - the captions are made the old way
        print(f"  Clip {clip['id']}: Hinglish captions model didn't run ({str(e)[-160:]})", flush=True)
        return None
    if len(got) < 2:
        return None
    words = [dict(w, s=round(w["s"] + base, 2), e=round(w["e"] + base, 2)) for w in got]
    clip.update(hl_words=words, hl_for=span)
    return words


def _hindi_words(job, clip, sound):
    """This clip's words heard again by Hindi Whisper, word by word, on the video's clock. Kept with the clip."""
    span = [round(clip["start"], 2), round(clip["end"], 2)]
    if clip.get("hi_for") == span and clip.get("hi_words"):
        return clip["hi_words"]
    try:
        audio, base = sound
        got = whisper.transcribe(audio, "hi")
    except Exception as e:  # noqa: BLE001 - the words the video was heard with
        print(f"  Clip {clip['id']}: Hindi speech model didn't run ({str(e)[-160:]})", flush=True)
        return None
    if len(got) < 2:
        return None
    words = [dict(w, s=round(w["s"] + base, 2), e=round(w["e"] + base, 2)) for w in got]
    clip.update(hi_words=words, hi_for=span)
    return words


def _english_words(job, clip, src, off, sound=None):
    """This clip's words heard again by the English speech model (Parakeet), timed on the video's clock: for an
    English clip in a video that was taken for Hindi, whose transcript is Hindi Whisper's. Kept with the clip."""
    span = [round(clip["start"], 2), round(clip["end"], 2)]
    if clip.get("en_for") == span and clip.get("en_words"):
        return clip["en_words"]
    try:
        audio, base = sound or _clip_sound(job, clip, src, off)
        got = transcribe.transcribe(audio)
    except Exception as e:  # noqa: BLE001 - the words as they were heard
        print(f"  Clip {clip['id']}: English speech model didn't run ({str(e)[-160:]})", flush=True)
        return None
    if len(got) < 2:
        return None
    words = [dict(w, s=round(w["s"] + base, 2), e=round(w["e"] + base, 2)) for w in got]
    clip.update(en_words=words, en_for=span)
    return words


def _caption_words(job, clip, words, off, src=None):
    want = (job.get("options") or {}).get("caption_lang")
    if want == "deva":
        # Hindi in its own script, as it was heard: no English fixing and no Roman letters. English words said in
        # it stay in English letters.
        return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
    always_en = want == "en"
    lang = job.get("lang") or ""
    clip["caption_by"] = "english" if (lang or "en").startswith("en") else "hindi"
    hindi_job = _hinglish_mode(job)
    # An "English" video can be a Hinglish streamer heard wrong: when Whisper is here (it is for anyone who clips
    # Hindi), its clips are checked on their own sound too, so their Hindi is never left to the English model.
    english_job_check = (src is not None and (want or "auto") == "auto" and (lang or "en").startswith("en")
                         and whisper.ENABLED and whisper.installed())
    if src is not None and (hindi_job or english_job_check):
        # Each clip's own sound says which language it really is.
        sound = None
        try:
            sound = _clip_sound(job, clip, src, off)
        except Exception:  # noqa: BLE001 - can't read it again: the video's language and words are used
            pass
        clip_lang = _clip_lang(job, clip, src, off, sound) if sound else ("hi" if hindi_job else "en")
        if clip_lang == "hi":
            lang = "hi"
            # Hindi said in this clip, in Roman letters, by the model made for exactly that: no spelling out of
            # Devanagari and no AI, so it's right even when the AI key is out of credit.
            hl = _hinglish_words(job, clip, src, off, sound)
            if hl:
                clip["caption_by"] = "hinglish-model"
                near = [w for w in hl if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
                return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in roman.words(near)]
            clip["caption_by"] = "hindi-rules"           # Hindi spelled in Roman letters by rules (and the AI, if it's on)
            if sound:
                # The Hinglish model couldn't write them (not downloaded yet, or it won't run on this computer): the
                # clip is heard again by Hindi Whisper, word by word, never left to a long video's quick rough pass
                # (whose timings are guessed), and spelled in Roman letters below as before.
                fine = _hindi_words(job, clip, sound)
                if fine:
                    words = fine
        elif clip_lang == "en" and sound and hindi_job:
            # English after all: heard again by the English model, and captioned as English
            en = _english_words(job, clip, src, off, sound)
            if en:
                words, lang = en, "en"
                clip["caption_by"] = "english"
    span = [round(clip["start"], 2), round(clip["end"], 2), ("en3" if always_en else "en2") + ("e" if lang == "en" else "")]
    if clip.get("fixed_for") == span and clip.get("fixed_words") and not any(
            roman.needs_roman(w.get("w", "")) for w in clip["fixed_words"]):
        raw = clip["fixed_words"]          # already tidied for this exact span: no second AI call
    else:
        raw = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
        try:
            usage.set_context(job.get("creator"), job.get("title") or "")
            raw = brain.english_captions(raw, lang, job.get("title") or "",
                                         youtube_english=lambda: _english_part(job, clip), always_english=always_en)
            clip.update(fixed_words=raw, fixed_for=span)
        except Exception:  # noqa: BLE001 - captions still render with the words as heard
            pass
    # the last word on it: whatever was said, nothing is drawn in Hindi or Urdu script
    return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in roman.words(raw)]


CLIP_MINUTES = float(config.env("PAPICLIPS_CLIP_MINUTES") or config.env("CLIPKONTRAST_CLIP_MINUTES", "15"))     # the longest one clip may take to make


def _wait_for_clips(job, futures, began, workers):
    """Wait for a video's clips, but not for ever. A clip that hangs (a download that stalls, an encoder that
    freezes) used to keep the whole video "in progress", and no other clip could be re-made. Now it is marked as
    failed after CLIP_MINUTES and the video finishes with the clips that did work."""
    left, given_up = list(futures), 0
    late = "This clip took too long and was stopped. Open it and press Save and re-make to try again."
    while left:
        still = []
        for f, clip in left:
            if f.done():
                f.result()
                continue
            t = began.get(clip["id"])
            if (t and time.time() - t > CLIP_MINUTES * 60) or (not t and given_up >= workers):
                given_up += 1
                clip.update(status="error", error=late)
                creators.log(job["creator"], f"Clip stopped after {CLIP_MINUTES:g} minutes: {clip.get('title') or clip.get('hook') or clip['id']}")
                save(job)
                continue
            still.append((f, clip))
        left = still
        if left:
            time.sleep(1)


def _english_part(job, clip):
    """YouTube's English captions for just this clip, for a video in another language. Fetched once per video."""
    if (job.get("source") or {}).get("type") != "url":
        return None
    f = Path(job["dir"]) / "words_en.json"
    if f.is_file():
        en = json.loads(f.read_text(encoding="utf-8"))
    else:
        try:
            en = media.youtube_english_words(job["source"]["url"]) or []
        except Exception:  # noqa: BLE001 - YouTube often refuses its translations: the AI translates instead
            en = []
        f.write_text(json.dumps(en), encoding="utf-8")       # asked once per video, refused or not
    return [w for w in en if clip["start"] - 0.5 <= w["s"] <= clip["end"] + 0.5]


LONG_VIDEO = 40 * 60   # above this, fetch only the pieces that become clips
PAD = 8                # seconds kept either side of a fetched piece, so clips can be re-trimmed


def _clip_source(job, clip):
    """The file this clip is cut from, and its size. A fetched piece that doesn't hold the clip's moment (YouTube sent
    another stretch, or a short one) used to reach the encoder with nothing to cut ("received no packets"): it is
    fetched again, cut exactly at the clip's time, and only if that still fails does the clip say so."""
    for attempt in (1, 2):
        src = (Path(clip["src"]) if clip.get("src") else Path(job["source_path"])).resolve()
        try:
            meta = media.probe(src)
        except Exception:  # noqa: BLE001 - an unreadable piece is fetched again below; the whole video, as known
            meta = ({"duration": 0, "width": 0, "height": 0} if clip.get("src") else
                    {"duration": job.get("duration") or clip["end"], "width": job.get("width") or 0,
                     "height": job.get("height") or 0})
        off = clip.get("src_offset", 0.0) if clip.get("src") else 0.0
        start, end = clip["start"] - off, min(clip["end"] - off, meta["duration"] or 0)
        if end - start >= 1.0 and start >= -0.5:
            clip["end"] = round(end + off, 2)
            return src, meta
        print(f"  Clip {clip['id']}: its piece doesn't hold its moment (piece {off:.1f}s + {meta['duration']:.1f}s, "
              f"clip {clip['start']:.1f}-{clip['end']:.1f}s); fetching it again", flush=True)
        if attempt == 2 or not clip.get("src"):
            break
        clip.pop("src", None)
        _piece_again(job, clip)
    raise RuntimeError("The part of the video downloaded for this clip doesn't hold its moment. "
                       "Press Save and re-make clip to try again.")


def _src_height(o: dict, layout=None) -> int:
    """How tall a video is fetched. A layout that shows the streamer's webcam cuts that small corner out and makes it
    a third of the clip, so it's fetched at 1080p whatever was chosen: from 720p the webcam came out blurry."""
    want = int(o.get("quality") or 720)
    if (layout or o.get("layout")) in render.FACECAM_LAYOUTS:
        want = max(want, config.MAX_SOURCE_HEIGHT)
    return want


def _piece_again(job, clip):
    """Remaking a clip whose own piece of the video was never fetched (its first try failed) and whose whole video
    is not here: fetch the piece now, cut exactly at the asked time, so no sound matching is needed."""
    src = job.get("source") or {}
    if src.get("type") != "url" or not src.get("url"):
        raise RuntimeError("The original video is no longer here, so this clip can't be made again. Add the video again.")
    d = Path(job["dir"])
    want = max(0.0, clip["start"] - PAD)
    until = clip["end"] + PAD if not job.get("duration") else min(job["duration"], clip["end"] + PAD)
    try:
        piece = media.download_section(src["url"], d / "clips", f"{clip['id']}.src", want, until,
                                       _src_height(job["options"], clip.get("layout")), exact=True)
        meta = media.probe(piece)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not fetch this part of the video: {safe.friendly(e, 250)}") from e
    clip.update(src=str(piece), src_offset=round(want, 2), src_end=round(want + meta["duration"], 2))
    clip["end"] = min(clip["end"], clip["src_end"])
    job.setdefault("width", meta["width"])
    job.setdefault("height", meta["height"])


def _fetch_piece(job, clip, full_audio):
    """Long videos: download just this clip's stretch and pin down exactly where it starts."""
    d = Path(job["dir"])
    o = job["options"]
    try:
        want = max(0.0, clip["start"] - PAD)
        piece = media.download_section(job["source"]["url"], d / "clips", f"{clip['id']}.src", want,
                                       min(job["duration"], clip["end"] + PAD), _src_height(o, clip.get("layout")))
        wav = d / "clips" / f"{clip['id']}.wav"
        media.extract_audio(piece, wav)
        part = media.load_audio(wav)
        media.remove(wav)
        meta = media.probe(piece)
        off = media.find_offset(full_audio, part, want)
        clip.update(src=str(piece), src_offset=round(off, 2), src_end=round(off + meta["duration"], 2))
        clip["start"], clip["end"] = max(clip["start"], clip["src_offset"]), min(clip["end"], clip["src_end"])
        job.setdefault("width", meta["width"])
        job.setdefault("height", meta["height"])
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=f"Could not fetch this part of the video: {safe.friendly(e, 250)}")


BLOCK = 10 * 60         # long videos are transcribed and clipped one block at a time
SECTION_PAD = 20        # seconds downloaded either side of a part of a long video asked for
PROGRESSIVE_FROM = 20 * 60
SCAN_REGION = 150       # scan mode: seconds of audio listened to around each lively moment
WHISPER_REGION = 90     # ...when Whisper listens (Hindi and other languages), which is slower


def render_workers() -> int:
    """How many clips are made at once. Each one is mostly waiting (its piece downloading, the AI tidying its
    captions) and then one ffmpeg, so a few at a time is quicker on any computer; fewer when memory is short."""
    cpus = os.cpu_count() or 4
    n = max(2, min(6, (cpus + 1) // 3))
    ram = getattr(transcribe, "RAM_GB", 0) or 0
    if ram and ram < 8:
        n = 2
    return n


def _mmss(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"
SCAN_SLICE = 15 * 60    # scan mode: every slice this long gets at least its liveliest moment


def _hot_regions(db, max_clips: int, region: int = None, per_clip: int = 3, boost=None, voice=None) -> list:
    """Scan mode: the lively parts of a long video, picked from its loudness alone (seconds from its start).

    The liveliest moment of every 15 minutes is taken, so no part of the video is skipped, and then the liveliest
    moments of the whole video until there are about three for every clip wanted. Only these parts are transcribed,
    which is far quicker than the whole video, and the AI then picks the best clips out of all of them at once."""
    region = region or SCAN_REGION
    n = len(db)
    if n < region * 2:
        return [(0, n)]
    # How much is happening: not loudness alone (a game's explosions and music are loud too) but a sudden jump in
    # it, a raised voice (2-4 kHz: shouting, screaming) and, when there is one, the chat suddenly reacting.
    k = 20                                              # how lively the 20 seconds around each second are
    live = np.convolve(brain.moment_scores(np.asarray(db, dtype=np.float32), (), voice, boost), np.ones(k) / k, mode="same")
    half = region // 2
    want = max(int(np.ceil(n / SCAN_SLICE)), max_clips * per_clip)
    want = min(want, max(1, int(n * 0.45) // region))      # never more than about half the video
    picked = []

    def free(c):
        return all(abs(c - p) >= region // 2 for p in picked)

    for s in range(0, n, SCAN_SLICE):                  # the liveliest moment of each slice
        part = live[s: s + SCAN_SLICE]
        if len(part) >= 30:
            c = s + int(np.argmax(part))
            if free(c):
                picked.append(c)
    for c in np.argsort(-live):                        # then the liveliest of the whole video
        if len(picked) >= want:
            break
        if free(int(c)):
            picked.append(int(c))
    regions = []
    for c in sorted(picked):
        a, b = max(0, c - half), min(n, c + half)
        if regions and a <= regions[-1][1] + 5:        # touching parts are listened to as one
            regions[-1] = (regions[-1][0], b)
        else:
            regions.append((a, b))
    return regions


def _covers(words: list, start: float, end: float) -> bool:
    """Is a borrowed transcript good enough to find clips with? It has to reach the end of the part being clipped
    and have a normal amount of talking in it; otherwise our own model writes the transcript."""
    inside = [w for w in words if start <= w["s"] <= end]
    minutes = max(0.5, (end - start) / 60)
    return len(inside) >= 25 * minutes and inside[-1]["s"] >= start + 0.85 * (end - start)


def _camera(job, clip):
    """How the streamer's camera shows in this clip: [x, y, w, h] of a webcam box (facecam on top), "full" when the
    camera fills the picture ("just chatting": zoomed onto it), or None (no camera: the whole picture is shown).
    Decided for each clip, because a stream changes: talking to the camera, then a game with the webcam in a corner."""
    path, span = clip.get("src"), None
    if not path:
        path, span = job.get("source_path"), (clip["start"], clip["end"])
    if not path or not Path(path).is_file():
        return None
    try:
        box = facecam.detect(path, ask=brain.look_for_facecam, span=span)
    except Exception:  # noqa: BLE001 - not knowing is not a failure: the whole picture is shown
        return None
    if not box or box.get("conf") != "high":
        return None
    if box.get("full"):
        return "full"
    return [box["x"], box["y"], box["w"], box["h"]]


# ---------------------------------------------------------------- where each streamer's webcam is, remembered
# A streamer's webcam sits in the same place in every clip, stream after stream. Looking for it in one clip alone can
# be fooled (a game character's face in a cutscene, a cinematic bar), so the place found in most clips is remembered
# per channel, and a clip whose own answer disagrees uses the remembered place instead.
CAM_AGREE = 0.5            # how much two boxes must overlap to be the same place


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _cam_key(job) -> str:
    """Whose webcam: the creator's workspace, else the channel the video is from."""
    if job.get("creator"):
        return "creator:" + job["creator"]
    up = re.sub(r"\s+", " ", str(job.get("uploader") or "")).strip().lower()
    return "channel:" + up if up else ""


def _cam_file():
    return config.user_dir(job_user()) / "facecams.json"


def job_user():
    return ctx.uid()


def _cams() -> dict:
    try:
        return json.loads(_cam_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remembered(job):
    """The remembered webcam box for this job's channel, as fractions of the picture [x, y, w, h], or None."""
    key = _cam_key(job)
    got = _cams().get(key) if key else None
    box = (got or {}).get("box")
    return box if isinstance(box, list) and len(box) == 4 else None


def _remembered_info(job) -> dict:
    key = _cam_key(job)
    return (_cams().get(key) if key else None) or {}


def _remember(job, frac_box, n: int, manual=False):
    key = _cam_key(job)
    if not key:
        return
    cams = _cams()
    if frac_box is None:
        cams[key] = {"box": None, "none": True, "manual": True, "at": time.time()}      # "this streamer has no webcam"
    else:
        cams[key] = {"box": [round(float(v), 4) for v in frac_box], "seen": int(n), "at": time.time(), "manual": bool(manual)}
    if len(cams) > 500:                                  # never grows without limit
        for k in sorted(cams, key=lambda k: cams[k].get("at", 0))[:len(cams) - 500]:
            cams.pop(k, None)
    try:
        f = _cam_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cams), encoding="utf-8")
    except OSError:
        pass


def _frac(box, w, h):
    return [box[0] / w, box[1] / h, box[2] / w, box[3] / h] if w and h else None


def _px(frac, w, h):
    return [int(round(frac[0] * w)), int(round(frac[1] * h)), int(round(frac[2] * w)), int(round(frac[3] * h))]


def _src_size(job, clip):
    """The size of the picture this clip is cut from."""
    path = clip.get("src") or job.get("source_path")
    if clip.get("src_w") and clip.get("src_h"):
        return clip["src_w"], clip["src_h"]
    try:
        meta = media.probe(path)
        return meta.get("width") or job.get("width"), meta.get("height") or job.get("height")
    except Exception:  # noqa: BLE001
        return job.get("width"), job.get("height")


def _learn(job, frac_box):
    """Each clip's own answer is counted; once two clips agree on a place that isn't the remembered one (or none
    is remembered yet), that place is remembered."""
    seen = job.setdefault("cam_seen", [])
    seen.append([round(v, 4) for v in frac_box])
    del seen[:-30]
    agree = [b for b in seen if _iou(b, frac_box) >= CAM_AGREE]
    mem = _remembered(job)
    if _remembered_info(job).get("manual"):
        return                                            # a place a person set is kept
    if len(agree) >= 2 and (not mem or _iou(mem, frac_box) < CAM_AGREE):
        med = [float(np.median([b[i] for b in agree])) for i in range(4)]
        _remember(job, med, len(agree))


def _use_frame(job, clip, w, h):
    """The part of the picture that is the game, and how the frame is shared, as set before the clips were made (or
    remembered for this streamer from the last time they were set)."""
    o = job.get("options") or {}
    info = _remembered_info(job)
    game = o.get("game_box") if o.get("game_box") else info.get("game")
    if isinstance(game, list) and len(game) == 4 and w and h:
        clip["game"] = _px(game, w, h)
    else:
        clip.pop("game", None)
    split = o.get("split") if o.get("split") not in (None, "balanced") or not info.get("split") else info.get("split")
    clip["split"] = split if split in render.SPLITS else "balanced"


def _save_frame(job):
    """Webcam and game set before the clips were made: remembered for this streamer, as if set by hand, once."""
    o = job.get("options") or {}
    if job.get("frame_saved") or not isinstance(o.get("cam_box"), list):
        return
    key = _cam_key(job)
    if not key:
        return
    _remember(job, o["cam_box"], 99, manual=True)
    cams = _cams()
    if key in cams:
        cams[key].update(game=o.get("game_box"), split=o.get("split") or "balanced")
        try:
            _cam_file().write_text(json.dumps(cams), encoding="utf-8")
        except OSError:
            pass
    job["frame_saved"] = True


def _use_camera(job, clip):
    """Find the camera for a clip with a facecam layout, and note what was found on it. A box that disagrees with
    where this streamer's webcam is remembered to be (or no box at all) gives way to the remembered place."""
    if clip.get("layout") not in render.FACECAM_LAYOUTS:
        return
    box = (job.get("options") or {}).get("cam_box")
    if isinstance(box, list):
        # Set on a frame of the video before the clips were made: this is the webcam, nothing is searched for.
        w, h = _src_size(job, clip)
        if w and h:
            clip.update(facecam=_px(box, w, h), camera="box", camera_from="manual")
            _use_frame(job, clip, w, h)
            _save_frame(job)
            return
    info = _remembered_info(job)
    if info.get("manual") and info.get("none"):           # set by hand: this streamer shows no webcam
        clip.pop("facecam", None)
        clip.update(camera="none", camera_from="manual")
        return
    cam = _camera(job, clip)
    clip.pop("facecam", None)
    w, h = _src_size(job, clip)
    hint = (job.get("options") or {}).get("cam_hint") or "auto"
    if hint == "none":
        clip.update(camera="none", camera_from="chosen")
        return
    if hint in CAM_PRESETS and w and h and cam != "full" and not info.get("manual"):
        # the corner was chosen: a box found anywhere else is not the webcam, and none found means the usual size there
        px = _px(CAM_PRESETS[hint], w, h)
        if not (isinstance(cam, list) and _iou(cam, px) > 0.05):
            cam = px
            clip["camera_from"] = "chosen"
    mem = _remembered(job)
    if isinstance(cam, list) and w and h and clip.get("camera_from") != "chosen":
        _learn(job, _frac(cam, w, h))
        mem = _remembered(job)
    if info.get("manual") and mem and w and h and cam != "full":
        cam = _px(mem, w, h)                              # set by hand: always this place
        clip["camera_from"] = "manual"
        mem = None
    if mem and w and h and cam != "full":
        mem_px = _px(mem, w, h)
        if not (isinstance(cam, list) and _iou(cam, mem_px) >= CAM_AGREE):
            cam = mem_px                                  # this clip's own look was fooled, or found nothing
            clip["camera_from"] = "remembered"
    clip["camera"] = "box" if isinstance(cam, list) else (cam or "none")
    if isinstance(cam, list):
        clip["facecam"] = cam
    _use_frame(job, clip, w, h)


CHECK_CLIPS = 8           # at most this many clips of a video have their facecam looked at again by the AI


def _cam_band(job, clip) -> bytes:
    """The facecam part of a clip that was made, from its middle, as a small JPEG."""
    f = Path(job["dir"]) / "clips" / str(clip.get("file") or "")
    if not f.is_file() or not isinstance(clip.get("facecam"), list):
        return b""
    if clip.get("layout_used") == "split":
        crop = f"crop={config.OUT_W}:{render.split_top(clip)}:0:0"
    elif clip.get("layout_used") == "gameplay":
        fw, fh = clip["facecam"][2], clip["facecam"][3]
        ph = max(2, int(round(render.PIP_W * fh / max(1, fw))))
        crop = f"crop={render.PIP_W}:{ph}:{config.OUT_W - render.PIP_W - render.PIP_MARGIN}:{render.PIP_MARGIN}"
    else:
        return b""
    try:
        meta = media.probe(f)
        at = max(0.5, (meta.get("duration") or 4) / 2)
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(f), "-frames:v", "1", "-vf",
                               f"{crop},scale=480:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                              capture_output=True, timeout=60, creationflags=media.NO_WINDOW).stdout or b""
    except Exception:  # noqa: BLE001
        return b""


def _check_cameras(job, words):
    """The AI looks at the facecam of each clip made with one (like an editor checking the cut before it's shown):
    a clip whose facecam shows the game is made again with the streamer's remembered webcam, or, when there is
    none, with the whole picture. Nothing changes when the AI can't look."""
    fixed = 0
    for c in [c for c in job["clips"] if c.get("status") == "done" and c.get("layout_used") in render.FACECAM_LAYOUTS
              and c.get("camera_from") != "manual"][:CHECK_CLIPS]:
        verdict = brain.is_streamer_cam(_cam_band(job, c))
        if verdict is not False:
            continue
        w, h = _src_size(job, c)
        mem = _remembered(job)
        mem_px = _px(mem, w, h) if mem and w and h else None
        if mem_px and _iou(mem_px, c["facecam"]) < CAM_AGREE:
            c.update(facecam=mem_px, camera="box", camera_from="checked")
        else:
            c.pop("facecam", None)
            c.update(camera="none", camera_from="checked")       # no trustworthy webcam here: the whole picture
        render_one(job, c, words)
        fixed += 1
    return fixed


def set_facecam(jid, frac_box, cid=None):
    """A webcam place set by hand on a frame (fractions of the picture [x, y, w, h]), or None for "no webcam":
    remembered for this streamer, and the clip (or, with no cid, every clip of the video) made again with it."""
    job = _jobs[jid]
    if frac_box is not None:
        if not (isinstance(frac_box, (list, tuple)) and len(frac_box) == 4):
            raise ValueError("Draw the box around the webcam.")
        x, y, w_, h_ = (float(v) for v in frac_box)
        if not all(math.isfinite(v) for v in (x, y, w_, h_)) or w_ < 0.04 or h_ < 0.04 or x < -0.01 or y < -0.01 \
                or x + w_ > 1.01 or y + h_ > 1.01:
            raise ValueError("Draw the box around the webcam, inside the picture.")
        frac_box = [max(0.0, x), max(0.0, y), min(w_, 1.0 - max(0.0, x)), min(h_, 1.0 - max(0.0, y))]
    _remember(job, frac_box, 99, manual=True)
    targets = [c for c in job["clips"] if (cid is None or c["id"] == cid) and c.get("status") in ("done", "error", "queued")]
    for c in targets:
        w, h = _src_size(job, c)
        if c.get("layout") not in render.FACECAM_LAYOUTS:
            c["layout"] = "split"                         # a webcam was drawn: the clip shows it on top
        if frac_box is None or not (w and h):
            c.pop("facecam", None)
            c.update(camera="none", camera_from="manual")
        else:
            c.update(facecam=_px(frac_box, w, h), camera="box", camera_from="manual")
        waiting = c.get("status") == "queued"           # already waiting to be made: it will use the new box
        c["status"] = "queued"
        save(job)
        if not waiting:
            _q.put(("clip", jid, c["id"]))
    return len(targets)


def facecam_frame(jid, cid) -> tuple:
    """A frame of the picture a clip is cut from (JPEG), with the webcam box in use as fractions, to draw on."""
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    path, span = clip.get("src"), None
    if not path:
        path, span = job.get("source_path"), (clip["start"], clip["end"])
    if not path or not Path(path).is_file():
        raise ValueError("The video this clip came from isn't on this computer any more.")
    jpeg = facecam._frame_jpeg(path, width=960)
    if not jpeg:
        raise ValueError("Couldn't read a picture from this clip's video.")
    w, h = _src_size(job, clip)
    box = clip.get("facecam")
    mem = _remembered(job)
    frac = _frac(box, w, h) if isinstance(box, list) and w and h else mem
    return jpeg, frac


def _agree_cameras(job, words):
    """After a video's clips are made: the place most clips found the webcam is the webcam. Clips whose own look
    disagreed are made again with it, so one cutscene can't leave a game character in the facecam."""
    boxed = [c for c in job["clips"] if c.get("status") == "done" and c.get("layout") in render.FACECAM_LAYOUTS]
    if _remembered_info(job).get("manual"):
        return 0
    found = [(c, c["facecam"]) for c in boxed if isinstance(c.get("facecam"), list) and c.get("camera_from") not in ("remembered", "chosen")]
    if len(found) < 2:
        return 0
    best = max(found, key=lambda cf: sum(1 for _c, b in found if _iou(b, cf[1]) >= CAM_AGREE))
    group = [b for _c, b in found if _iou(b, best[1]) >= CAM_AGREE]
    if len(group) < 2 or len(group) * 2 < len(found):
        return 0                                          # no clear majority: each clip keeps its own
    cons = [int(np.median([b[i] for b in group])) for i in range(4)]
    w, h = _src_size(job, boxed[0])
    if w and h:
        _remember(job, _frac(cons, w, h), len(group))
    fixed = 0
    for c in boxed:
        if c.get("camera") == "full":
            continue
        if isinstance(c.get("facecam"), list) and _iou(c["facecam"], cons) >= CAM_AGREE:
            continue
        c.update(facecam=cons, camera="box", camera_from="agreed")
        render_one(job, c, words)
        fixed += 1
    return fixed


def _facecam(job, path=None) -> list | None:
    """Where the webcam is in the source: once for the whole video, for every clip of it.

    Only a sure answer is used. A box that is a scoreboard drawn over the game would
    put a picture of a scoreboard in the corner of the clip. One frame is also shown to
    the AI, which tells a person from a game character better than any measurement can;
    what it says is measured again on the picture, so it cannot do any harm."""
    path = path or job.get("source_path")
    if not path or not Path(path).is_file():
        return None
    try:
        box = facecam.detect(path, ask=brain.look_for_facecam)
    except Exception:  # noqa: BLE001 - a missing facecam is not a failure
        return None
    if not box or box.get("conf") != "high":
        return None
    job["facecam"] = [box["x"], box["y"], box["w"], box["h"]]
    return job["facecam"]


def _run_job(job):
    import math
    from concurrent.futures import ThreadPoolExecutor
    d = Path(job["dir"])
    o = job["options"]
    src = job["source"]
    tmark = [time.time()]
    job["timings"] = {}

    def mark(step):
        """How long each step took, in seconds: kept with the video, and sent with a bug report."""
        now = time.time()
        job["timings"][step] = round(now - tmark[0])
        tmark[0] = now
    # Which game this is: what the creator set, or what the title says. It decides the extra rules the AI
    # reads and the tags every post carries.
    game = job.get("game") or ""
    if not game and job.get("creator"):
        try:
            game = (creators.get(job["creator"]) or {}).get("game") or ""
        except Exception:  # noqa: BLE001
            game = ""
    if not game:
        game = games.detect(job["title"], src.get("name"))[0] or ""
    if game and job.get("game") != game:
        job["game"] = game
    t0 = time.time()
    notes = []
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    resuming = bool(job.pop("resume", False)) and (d / "words.json").is_file()
    if resuming:   # after a restart: keep the transcript parts, clips and AI answers already made, and carry on
        _set(job, status="running", stage="Picking up where it stopped", error=None)
    else:
        _set(job, status="running", stage="Getting the audio", progress=0.03, error=None, clips=[], ai={}, done_blocks=0)
    if not transcribe.model_ready() and not transcribe.REMOTE:
        from . import fetch_model
        _set(job, stage="Getting the speech model (one time, about 480 MB)")
        fetch_model.ensure(lambda p: _set(job, stage=f"Getting the speech model (one time, about 480 MB): {p}%"))
        _set(job, stage="Getting the audio")
    fetch = ThreadPoolExecutor(1)
    video = None
    section = tuple(job["section"]) if job.get("section") and job.get("source_path") and Path(job["source_path"]).is_file() else None
    chat_rep = [None]                                   # the stream's chat replay, when there is one (media.ChatReplay)
    if src["type"] == "url":
        meta = media.info(src["url"])
        if meta["kind"] != "video":
            raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
        if ((o["range_end"] or meta["duration"]) - (o["range_start"] or 0)) > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That is longer than {config.MAX_VIDEO_HOURS:g} hours. Choose a shorter part of it under Options.")
        if meta["is_live"]:
            raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                               "put this link under Setup, and press Start recording instead.")
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"],
             handle=meta.get("handle", ""), platform=meta.get("platform", ""))
        if job.get("kind") != "live" and not o.get("exact") and meta.get("live_status") in ("was_live", "post_live"):
            chat_rep[0] = media.ChatReplay(src["url"], d)        # what viewers wrote, while the rest goes on
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        span = (o["range_end"] or meta["duration"]) - (o["range_start"] or 0)
        if o["range_end"] and meta["duration"] > 0 and span <= LONG_VIDEO and meta["duration"] - span > 120:
            # Only a part of a long video was asked for (a moment from a campaign's timestamps, say): just that
            # part is downloaded, with a little either side, and made into clips like a file of its own. A minute
            # of a three-hour stream takes seconds instead of downloading the whole stream.
            a_s = max(0.0, (o["range_start"] or 0) - SECTION_PAD)
            b_s = min(meta["duration"], o["range_end"] + SECTION_PAD)
            _set(job, stage="Getting that part of the video")
            part = media.download_section(src["url"], d, "source", a_s, b_s, _src_height(o), exact=True)
            info = media.probe(part)
            section = (a_s, b_s)
            _set(job, source_path=str(part), width=info["width"], height=info["height"], section=[a_s, b_s],
                 duration=round(info["duration"] or (b_s - a_s), 2))
            pieces, audio_file = False, part
        else:
            pieces = span > LONG_VIDEO or meta["duration"] <= 0
            if not pieces:  # shorter video: fetch all of it in the background while the audio is worked on
                video = fetch.submit(media.download, src["url"], d, _src_height(o))
            audio_file = media.download_audio(src["url"], d, lambda p: _set(job, progress=0.03 + 0.1 * p))
    else:
        pieces, audio_file = False, Path(src["path"])
        info = media.probe(audio_file)
        if info["duration"] > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video is longer than {config.MAX_VIDEO_HOURS:g} hours, which is the limit.")
        _set(job, source_path=str(audio_file), width=info["width"], height=info["height"], duration=info["duration"])
        if not job.get("thumb") and media.save_thumb(d, video=audio_file):
            _set(job, thumb=True)
    wav = d / "audio.wav"
    media.extract_audio(audio_file, wav)
    audio = media.load_audio(wav)
    media.remove(wav)
    if src["type"] == "url" and not section:
        media.remove(audio_file)
    if not job["duration"]:
        job["duration"] = len(audio) / 16000
    sec0 = section[0] if section else 0.0          # with only a part downloaded, times are counted from its start
    lo = max(0, int(((o["range_start"] or 0) - sec0) * 16000))
    hi = min(len(audio), int((o["range_end"] - sec0) * 16000)) if o["range_end"] else len(audio)
    span = (hi - lo) / 16000
    db = media.loudness_per_second(audio[lo:hi])
    voice = media.voice_per_second(audio[lo:hi])         # a raised voice: one of the signs something happened
    abs0 = sec0 + lo / 16000                             # the video's own second that db[0] is

    def chat_now(wait=0.0, s0=0, m=None):
        """The chat replay as read so far, counted like db from second s0 (m seconds of it): {"z", "msgs",
        "offset"} for brain.find_clips, or None when there's no chat (or too little to tell)."""
        if not chat_rep[0]:
            return None
        try:
            msgs = chat_rep[0].messages(wait)
            cz = brain.chat_signal(msgs, len(db), abs0)
        except Exception as e:  # noqa: BLE001 - the chat is a bonus: a video is never lost to it
            notes.append(f"The stream's chat replay couldn't be read ({str(e)[:80]}), so moments were picked without it.")
            chat_rep[0] = None
            return None
        if cz is None:
            return None
        m = len(db) - s0 if m is None else m
        job["chat_messages"] = len(msgs)
        return {"z": cz[s0: s0 + m], "msgs": [(t - abs0 - s0, who, text) for t, who, text in msgs], "offset": 0.0}

    vlock, vstate = threading.Lock(), {}
    mark("audio")

    def whole_video():
        """Wait for the background download once; retry it, or give up and use pieces."""
        with vlock:
            if section:                      # only a part was downloaded, and it is the video
                return job.get("source_path")
            if "path" not in vstate:
                path = None
                try:
                    path = video.result()
                except Exception as e:  # noqa: BLE001 - sites sometimes refuse a download
                    notes.append(f"Video download refused once ({str(e)[-80:]}), retrying")
                    try:
                        path = media.download(src["url"], d, _src_height(o))
                    except Exception as e2:  # noqa: BLE001
                        notes.append(f"Whole-video download failed ({str(e2)[-80:]}), fetching clip parts instead")
                if path:
                    info = media.probe(path)
                    job.update(source_path=str(path), width=info["width"], height=info["height"])
                vstate["path"] = path
            return vstate["path"]

    exact = {}      # clip id -> (from, to, words): our own model's words for just that clip
    rough = [False]  # the moments were found with the quick model: each clip's captions are written again, properly

    began = {}      # clip id -> when work on it started

    def make(clip, words_now):
        began[clip["id"]] = time.time()
        if src["type"] == "url" and (pieces or not whole_video()):
            _fetch_piece(job, clip, audio)
        if clip["status"] != "error":
            _use_camera(job, clip)          # webcam box, camera filling the picture, or none: for this clip
        yt_en = bool(yt and yt[1].lower().startswith("en"))
        hl_writes = _hinglish_mode(job) and whisper.hinglish_ready()   # it writes this clip's captions itself
        if clip["status"] != "error" and clip.get("captions", True) and (yt_en or rough[0]) and not hl_writes:
            # The clip was found with YouTube's transcript, or with the quick model. What is shown on screen comes
            # from our own full model, run on just this clip: full punctuation, nothing blanked out, timings to the word.
            a0, b0 = max(lo, int((clip["start"] - 1.5) * 16000)), min(hi, int((clip["end"] + 1.5) * 16000))
            try:
                fine = transcribe.transcribe(audio[a0:b0]) if yt_en else asr(audio[a0:b0])
                tw = [dict(w, s=round(w["s"] + a0 / 16000, 2), e=round(w["e"] + a0 / 16000, 2)) for w in fine]
            except Exception:  # noqa: BLE001 - the clip is still made, with YouTube's words
                tw = []
            if len(tw) >= 3:
                exact[clip["id"]] = (a0 / 16000, b0 / 16000, tw)
                words_now = tw
        if clip["status"] != "error":
            render_one(job, clip, words_now)

    renders = ThreadPoolExecutor(render_workers())
    futures = []
    step = BLOCK * 16000 if span > PROGRESSIVE_FROM else hi - lo
    blocks = [(x, min(hi, x + step)) for x in range(lo, hi, step)] or [(lo, hi)]
    words, tokens, model = [], 0, None
    done_blocks = 0
    yt = None
    if src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"]):
        try:
            yt = media.youtube_words(src["url"])
        except Exception:  # noqa: BLE001 - no transcript to borrow: ours is written as usual
            yt = None
        if yt and section:                   # counted from the start of the part that was downloaded
            yt = ([dict(w, s=round(w["s"] - section[0], 2), e=round(w["e"] - section[0], 2)) for w in yt[0]
                   if section[0] <= w["s"] <= section[1]], yt[1])
        if yt and not _covers(yt[0], lo / 16000, hi / 16000):
            yt = None
        job["transcript_from"] = "youtube" if yt else "own"
        job["lang"] = yt[1] if yt else ""          # the language spoken, when YouTube says (decides the captions)
    # The language spoken: the one picked for this video, else what YouTube says, else Whisper listens and tells.
    # It decides which speech model writes the transcript (English: Parakeet; anything else: Whisper) and how the
    # captions are written.
    spoken, yt = _pick_language(job, o, yt, audio[lo:hi], resuming)
    use_whisper = [not whisper.is_english(spoken)]
    if _hinglish_mode(job):
        whisper.fetch_hinglish_soon()       # the Hinglish captions model, the first time: in the background

    def asr(part, on_progress=None, quick=False):
        """The transcript of a part of the audio, by the speech model for the language spoken."""
        if use_whisper[0]:
            try:
                return whisper.transcribe(part, spoken.split("-")[0], on_progress, note=lambda t: _set(job, stage=t), quick=quick)
            except Exception as e:  # noqa: BLE001 - the English model still gives something to work with
                use_whisper[0] = False
                notes.append(f"Hindi speech model unavailable ({str(e)[-100:]}), used the English one")
        return transcribe.transcribe(part, on_progress)
    # Scan mode (long videos): look over the whole video first, then pick the best clips of all of it at once,
    # instead of clipping each part as it comes. Without YouTube's transcript only the lively parts are
    # transcribed, found from the loudness of every second, which is much quicker than the whole video.
    scan = span > PROGRESSIVE_FROM and job.get("kind") != "live" and o.get("scan", True)
    if scan and not yt:
        # Whisper (Hindi and other languages) is several times slower than the English model on an ordinary
        # computer, so it listens to shorter stretches around fewer lively moments: the same clips, far sooner.
        early = chat_now(20)                               # the chat read so far points at lively parts too
        boost = early["z"] if early else None
        hot = (_hot_regions(db, int(o["max_clips"]), WHISPER_REGION, 2, boost, voice) if use_whisper[0]
               else _hot_regions(db, int(o["max_clips"]), boost=boost, voice=voice))
        blocks = [(lo + a * 16000, min(hi, lo + b * 16000)) for a, b in hot]
        rough[0] = bool(use_whisper[0] and whisper.quick_model())
    prog_base, acc = [], 0
    for a_, b_ in blocks:
        prog_base.append(acc)
        acc += b_ - a_
    prog_total = max(1, acc)
    if resuming:
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        except ValueError:
            words = []
        done_blocks = min(int(job.get("done_blocks", 0)), len(blocks)) if words else 0
        tokens, model = int((job.get("ai") or {}).get("tokens", 0)), (job.get("ai") or {}).get("model")
        if done_blocks < len(blocks):     # words past the last finished part are transcribed again
            cut = blocks[done_blocks][0] / 16000
            words = [w for w in words if w["s"] < cut]
        for c in job["clips"]:            # clips found before the restart that were not finished yet
            if c.get("status") not in ("done", "error"):
                c["status"] = "queued"
                futures.append((renders.submit(ctx.bind(make), c, list(words)), c))
        save(job)
    # The transcript of the next part is written while the AI is still reading this one (the AI is a wait on the
    # network, the transcript is work on this machine), so neither waits for the other.
    ahead = ThreadPoolExecutor(2, thread_name_prefix="ahead")
    heard, now_on = {}, [done_blocks]

    def hear(i):
        if i < len(blocks) and i >= done_blocks and i not in heard:
            a_, b_ = blocks[i]
            last = [0.0]

            def tp(p, i=i, a_=a_, b_=b_):
                if now_on[0] == i and p - last[0] > 0.05:      # only the part being shown moves the bar
                    last[0] = p
                    secs = (b_ - a_) / 16000
                    heard_now = f": {_mmss(p * secs)} of {_mmss(secs)} heard" if secs >= 60 else ""
                    stage = job.get("stage", "").split(":")[0] + heard_now     # people can see it's moving
                    _set(job, stage=stage, progress=max(job["progress"], 0.14 + 0.78 * (prog_base[i] + p * (b_ - a_)) / prog_total))
            heard[i] = ahead.submit(ctx.bind(asr), audio[a_:b_], tp, rough[0])

    def take(found, shift):
        """Drop clips in ad breaks or static screens, then queue the rest to be made."""
        if found and statics:
            # A commercial or a "be right back" slate is not a clip. Any clip that sits mostly inside one
            # is dropped.
            keep = []
            for c in found:
                a_s, b_s = c["start"] + shift, c["end"] + shift
                inside = quality.static_overlap(a_s, b_s, statics)
                if inside > 0.5 * max(1.0, b_s - a_s):
                    notes.append(f"A clip from {round(a_s)}s was left out: it sat in an ad break or a static screen.")
                else:
                    keep.append(c)
            found = keep
        for c in found:
            c.update(id=f"clip{len(job['clips']) + 1:02d}", start=round(c["start"] + shift, 2),
                     end=round(c["end"] + shift, 2), style=o["style"], layout=o["layout"],
                     captions=o.get("captions", True), zoom=o.get("zoom", True), watermark=o.get("watermark", ""),
                     tighten=o.get("tighten", True), **{k: o.get(k, DEFAULTS[k]) for k in TAG_KEYS},
                     status="queued", version=0)
            # Only the facecam layouts use a webcam, so it is only looked for then, and only once: a
            # stream with no webcam is not looked for again on every clip of it.
            job["clips"].append(c)
            futures.append((renders.submit(ctx.bind(make), c, list(words)), c))

    # The ad breaks and static screens of the whole video, found once. It reads the video end to end,
    # so it is done here rather than for each clip, and only when asked for.
    statics = quality.static_ranges(job.get("source_path") or "") if config.SKIP_STATIC else []
    for bi, (a, b) in enumerate(blocks):
        if bi < done_blocks:
            continue                      # transcribed and checked by the AI before the restart
        shift = a / 16000
        where = f" (minute {round((a - lo) / 16000 / 60)} of {round(span / 60)})" if len(blocks) > 1 else ""
        if scan and not yt:
            where = f" (part {bi + 1} of {len(blocks)})"
        if yt:
            _set(job, stage="Reading the transcript" + where)
            bw = [dict(w, s=round(w["s"] - shift, 2), e=round(w["e"] - shift, 2)) for w in yt[0] if a / 16000 <= w["s"] < b / 16000]
        else:
            _set(job, stage=("Listening to the lively parts" if scan else "Writing the transcript") + where)
            now_on[0] = bi
            hear(bi)
            hear(bi + 1)
            bw = heard.pop(bi).result()
        _set(job, progress=max(job["progress"], 0.14 + 0.78 * (prog_base[bi] + (b - a)) / prog_total))
        words += [dict(w, s=round(w["s"] + shift, 2), e=round(w["e"] + shift, 2)) for w in bw]
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
        if scan:                          # the clips are picked once the whole video has been looked over
            job["done_blocks"] = bi + 1
            save(job)
            continue
        if o.get("exact"):                # the moment was chosen: no picking, only its words for the captions
            job["done_blocks"] = bi + 1
            save(job)
            continue
        _set(job, stage="Finding the best moments" + where, word_count=len(words))
        left = int(o["max_clips"]) - len(job["clips"])
        if left <= 0 or len(bw) < 8:
            job["done_blocks"] = bi + 1
            save(job)
            continue
        quota = max(1, math.ceil(left / (len(blocks) - bi)))
        s0 = int((a - lo) / 16000)
        found, ai = brain.find_clips(bw, db[s0: s0 + int((b - a) / 16000) + 1], (b - a) / 16000, job["title"],
                                     job["uploader"], quota, int(o["min_len"]), int(o["max_len"]), notes.append, game=game,
                                     voice=voice[s0: s0 + int((b - a) / 16000) + 1],
                                     chat=chat_now(30, s0, int((b - a) / 16000) + 1))
        tokens += ai.get("tokens", 0)
        model = ai.get("model") or model
        if ai.get("note"):
            notes.append(ai["note"])
        take(found, shift)
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        job["done_blocks"] = bi + 1       # a restart from here on carries on with the next part
        save(job)
    mark("transcript")
    if o.get("exact") and o.get("range_end") and not job["clips"]:
        # An exact moment: one clip of exactly the part chosen, on the clock of the file it's cut from. The AI only
        # writes its big text, title and post (when it's there): it doesn't move the start or the end.
        s_rel, e_rel = (o["range_start"] or 0) - sec0, o["range_end"] - sec0
        text = " ".join(w["w"] for w in words if s_rel - 0.5 <= w["s"] <= e_rel)
        _set(job, stage="Writing the big text and the title", progress=max(job["progress"], 0.9))
        # the AI key, or, when it isn't answering (or isn't set up), the free AI on this computer
        about = brain.describe(text, job["title"], job["uploader"], game=game)
        by = about.pop("_by", None)
        if by:
            model = by
        take([dict(about, start=round(max(0.0, s_rel), 2), end=round(e_rel, 2), reason="The exact moment you chose.")], 0.0)
        notes.append("Clipped exactly the part you chose." + (
            " The free AI on this computer wrote its big text and title." if str(by or "").startswith("ollama:") else
            "" if by else " No AI was answering, so it has no big text or title: add them on the clip."))
    if scan and not job["clips"] and words:
        _set(job, stage="Picking the best moments of the whole video", progress=max(job["progress"], 0.9))
        base = lo / 16000
        rel = [dict(w, s=round(w["s"] - base, 2), e=round(w["e"] - base, 2)) for w in words]
        found, ai = brain.find_clips(rel, db, span, job["title"], job["uploader"], int(o["max_clips"]),
                                     int(o["min_len"]), int(o["max_len"]), notes.append, game=game, voice=voice,
                                     chat=chat_now(60))
        tokens += ai.get("tokens", 0)
        model = ai.get("model") or model
        if ai.get("note"):
            notes.append(ai["note"])
        take(found, base)
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        save(job)
    if not job["clips"] and job.get("kind") != "live" and span >= 5:
        # The safety net: nothing was picked (the AI found nothing that stood out, or too little was heard to ask
        # it), so the liveliest parts are clipped. Someone who asked for clips never gets none back.
        base = lo / 16000
        rel = [dict(w, s=round(w["s"] - base, 2), e=round(w["e"] - base, 2)) for w in words]
        n = min(int(o["max_clips"]), 3, max(1, int(span // max(15, int(o["max_len"])))))
        late = chat_now()
        net = brain.liveliest_clips(rel, db, span, n, int(o["min_len"]), int(o["max_len"]), voice, late["z"] if late else None)
        if net:
            notes.append("Nothing stood out to the AI, so the liveliest parts were clipped.")
            job["safety_net"] = True
            take(net, base)
    mark("picking")
    if chat_rep[0]:
        if job.get("chat_messages"):
            notes.append(f"The stream's chat replay was read too ({job['chat_messages']} messages): moments viewers reacted to count more.")
        chat_rep[0].stop()
    if futures:
        _set(job, stage="Finishing the clips", progress=max(job["progress"], 0.92))
    _wait_for_clips(job, futures, began, renders._max_workers)
    try:
        if _agree_cameras(job, words):
            notes.append("The facecam was set the same in every clip.")
        if _check_cameras(job, words):
            notes.append("A facecam showing the game was fixed.")
    except Exception:  # noqa: BLE001 - the clips are already made; this only improves them
        traceback.print_exc()
    mark("clips")
    if any(c.get("caption_by") == "hindi-rules" for c in job["clips"]):
        st = whisper.hl_state
        why = (f"is still downloading ({st['percent']}%)" if st.get("doing") else
               f"couldn't be used: {st['error']}" if st.get("error") else "isn't downloaded yet")
        notes.append(f"Hinglish captions: the Hinglish model {why}, so these Hindi captions were spelled out by rules. "
                     "Once it's ready, re-make a clip (or clip again) for proper Hinglish.")
        whisper.fetch_hinglish_soon()
    elif any(c.get("caption_by") == "hinglish-model" for c in job["clips"]):
        notes.append("Hinglish captions were written by the Hinglish model.")
    if exact:           # keep our own words for the clips in the saved transcript, so editing a clip later uses them
        for a_s, b_s, tw in exact.values():
            words = [w for w in words if not a_s <= w["s"] < b_s] + tw
        words.sort(key=lambda w: w["s"])
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    # Clips still waiting (ones given up on above) are cancelled, not left to start after the audio they need is
    # freed: a long stream's audio is over a gigabyte of memory.
    renders.shutdown(wait=False, cancel_futures=True)
    ahead.shutdown(wait=False, cancel_futures=True)
    fetch.shutdown(wait=False, cancel_futures=True)
    del audio
    clips = job["clips"]
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:],
                 "note": next((x for x in notes if x.startswith("Clipped exactly the part you chose")), "") if o.get("exact")
                 else "Nothing stood out to the AI, so the liveliest parts were clipped." if job.get("safety_net") and model
                 else "Your AI key wasn't answering, so the free AI on this computer picked the clips." if str(model or "").startswith("ollama:")
                 else "The AI was unavailable, so clips were picked from the sound and the words." if clips and not model else ""}
    stage = "Done" if clips else "Done, but nothing clip-worthy was found"
    _set(job, status="done", stage=stage, progress=1.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{len(clips)} clip{'s' if len(clips) != 1 else ''} ready" if clips else "Nothing clip-worthy found",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    creators.log(job["creator"], f"Video finished with {len(clips)} clips in {round(time.time() - t0)}s: {job['title']}")


def _worker(quick_only=False):
    while True:
        kind, a, cid = _q.get(quick_only)
        if kind == "fn":
            try:
                a()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            continue
        job = _jobs.get(a)
        if not job:
            if kind == "job":
                _q.done()
            continue
        if kind == "job" and job.get("stop") and job.get("status") != "running":
            _q.done()                        # stopped while it waited its turn
            continue
        token = ctx._uid.set(job["user"])  # everything this job does runs as its owner
        if kind == "job":
            _making[job["id"]] = threading.get_ident()
        try:
            if kind == "job":
                if job.get("stop"):
                    raise Stopped()
                _run_job(job)
            else:
                clip = next(c for c in job["clips"] if c["id"] == cid)
                words = []
                wf = Path(job["dir"]) / "words.json"
                if clip.get("captions", True) and wf.is_file():   # no transcript kept: the clip is made without captions
                    try:
                        words = json.loads(wf.read_text(encoding="utf-8"))
                    except ValueError:
                        words = []
                if clip.get("layout") in render.FACECAM_LAYOUTS and not clip.get("camera"):
                    _use_camera(job, clip)    # re-made with a facecam layout: how the camera shows in this clip
                render_one(job, clip, words)
        except Stopped:
            _stopped(job)
        except Exception as e:  # noqa: BLE001
            if kind == "job" and job.get("stop"):
                _stopped(job)                # stopped mid-step: what that step said on its way out isn't an error
                continue
            (Path(job["dir"]) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            # where it happened, for a bug report: the last steps as file:line function, no folders or names
            frames = traceback.extract_tb(e.__traceback__)[-5:]
            job["error_where"] = " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))[:400]
            if kind == "job":
                for c in job["clips"]:
                    if c.get("status") in ("queued", "rendering"):
                        c.update(status="error", error="The video stopped before this clip was made.")
                _set(job, status="error", error=safe.friendly(e), stage="Stopped")
                creators.log(job["creator"], f"Video failed: {safe.friendly(e)[:200]}")
            else:                            # one clip made again: it says what went wrong instead of waiting forever
                for c in job["clips"]:
                    if c["id"] == cid and c.get("status") in ("queued", "rendering"):
                        c.update(status="error", error=safe.friendly(e, 300))
                save(job)
        finally:
            ctx._uid.reset(token)
            if kind == "job":
                _making.pop(job["id"], None)
                _q.done()


def _pick_up_after_restart():
    """Videos and single clips that were being made when the server stopped: queued again, oldest first."""
    waiting = sorted((j for j in all_jobs() if j.get("status") == "queued" and j.get("resume")), key=lambda j: j["created"])
    for j in waiting:
        _q.put(("job", j["id"], None), user=j["user"])
    for j in all_jobs():
        for cid in j.pop("remake", []):
            _q.put(("clip", j["id"], cid))
        if j.get("status") == "done":
            save(j)


def start():
    load.start()
    load_all()
    _pick_up_after_restart()
    for _ in range(max(1, config.JOB_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_worker, args=(True,), daemon=True).start()   # re-making a single clip never waits behind whole videos
    threading.Thread(target=_live_worker, daemon=True).start()
