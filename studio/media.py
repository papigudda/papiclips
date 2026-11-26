"""Getting video in: link download, probing, audio extraction, loudness."""
import contextlib
import contextvars
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config
from . import tag as _tag

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- when YouTube refuses this server
# YouTube sometimes refuses a server's downloads altogether ("sign in to confirm you're not a bot"). It is a
# refusal of the server, not of one video, so once it happens YouTube links are turned away at once for a few
# minutes, with a clear message, instead of each one waiting to fail. Then the next link simply tries again.
YT_PAUSE = 600
_yt_refused_until = 0.0
YT_BLOCKED = ("YouTube isn't letting PapiClips download videos right now. Upload the video file instead, or use a "
              "Twitch or Kick link. YouTube links will be tried again in a few minutes.")
_BOT_CHECK = re.compile(r"not a bot|confirm you.?re not|sign in to confirm(?! your age)", re.I)


class YouTubeBlocked(RuntimeError):
    pass


def is_youtube(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith((".youtube.com", ".youtu.be"))


def youtube_paused() -> bool:
    return time.time() < _yt_refused_until


def check_youtube(url: str):
    """Raise YouTubeBlocked straight away if YouTube is refusing this server right now."""
    if is_youtube(url) and youtube_paused():
        raise YouTubeBlocked(YT_BLOCKED)


_defer = contextvars.ContextVar("yt_defer", default=False)   # set while the other ways of asking are still to try


def note_refusal(url: str, err) -> bool:
    """If this error is YouTube refusing the server, pause YouTube links for a while. True if it was.
    While other ways of asking YouTube are still to be tried (_defer), it says so without pausing anything."""
    global _yt_refused_until
    if is_youtube(url) and _BOT_CHECK.search(str(err)):
        if not _defer.get():
            _yt_refused_until = time.time() + YT_PAUSE
        return True
    return False


def _pause_youtube():
    global _yt_refused_until
    _yt_refused_until = time.time() + YT_PAUSE


@contextlib.contextmanager
def _youtube_guard(url):
    check_youtube(url)
    try:
        yield
    except YouTubeBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        if note_refusal(url, e):
            raise YouTubeBlocked(YT_BLOCKED) from None
        raise


def site_allowed(url: str) -> bool:
    """Links are fetched by the server, so only known video sites are accepted (never arbitrary addresses)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in config.ALLOWED_SITES)


def remove(path) -> bool:
    """Delete a working file, tried again a few times if an antivirus or Windows Search is holding it (WinError 32 /
    5 on Windows). True if it's gone. A leftover working file never stops a video: it's cleared with the job."""
    for wait in (0.1, 0.4, 1.0, 0):
        try:
            Path(path).unlink(missing_ok=True)
            return True
        except PermissionError:
            if wait:
                time.sleep(wait)
        except OSError:
            return False
    return False


def run(cmd, cwd=None, timeout=3600):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(str(cmd[0])).name} took too long and was stopped.")
    if p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed: {p.stderr[-1500:]}")
    return p.stdout


class _Quiet:
    """Says nothing, but keeps the last few problems, so a failed download can say why."""
    def __init__(self):
        self.problems = []

    def debug(self, m): pass
    def info(self, m): pass

    def warning(self, m):
        self._keep(m)

    def error(self, m):
        self._keep(m)

    def _keep(self, m):
        m = re.sub(r"\x1b\[[0-9;]*m", "", str(m)).strip()      # no terminal colour codes in messages people read
        if m:
            self.problems = (self.problems + [m[:300]])[-4:]


_js = {}


def _js_runtimes() -> dict:
    """YouTube's pages can only be read with a JavaScript runtime. Without one, yt-dlp gets links that YouTube
    refuses part-way, and fetching one stretch of a long video fails ("ffmpeg exited with code ..."). Deno comes
    with PapiClips (the `deno` package in requirements.txt); one already on the computer is used first."""
    if "v" not in _js:
        path = shutil.which("deno")
        if not path:
            try:
                from deno import find_deno_bin
                path = find_deno_bin()
            except Exception:  # noqa: BLE001 - not installed: yt-dlp does what it can without one
                path = None
        _js["v"] = {"deno": {"path": str(path)}} if path else {}
    return _js["v"]


def cookie_opts() -> dict:
    """The YouTube cookies an admin saved (Settings, Connected apps), if any. yt-dlp only sends them to YouTube."""
    return {"cookiefile": str(config.YT_COOKIES)} if config.YT_COOKIES.is_file() else {}


def _ydl(opts):
    import yt_dlp
    # a channel or playlist link is answered as "a list" straight away, never walked video by video
    base = {"noplaylist": True, "quiet": True, "no_warnings": True, "logger": _Quiet(),
            "extract_flat": "in_playlist", "playlistend": 1, "socket_timeout": 30, "concurrent_fragment_downloads": 4,
            # slow home internet: more tries before giving up on a download or one of its pieces
            "retries": 15, "fragment_retries": 15, "extractor_retries": 3,
            # an antivirus or Windows Search holding a file a moment (WinError 32 / 5): tried again, not failed
            "file_access_retries": 10,
            # ffmpeg fetching a part of a video: a connection that stops sending is dropped after 30 s and tried again
            "external_downloader_args": {"ffmpeg_i": ["-rw_timeout", "30000000"]}}
    if _js_runtimes():
        base["js_runtimes"] = _js_runtimes()
    base.update(cookie_opts())
    return yt_dlp.YoutubeDL({**base, **opts})


def _video_format(height):
    return f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b"


def _biggest(job_dir: Path, stem: str) -> Path:
    files = sorted((p for p in job_dir.glob(stem + ".*") if p.suffix not in (".part", ".ytdl")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was produced.")
    return files[0]


# what no other way of asking can change: the video itself is private, removed, members-only, not out yet...
_UNFIXABLE = re.compile(r"private video|members.only|join this channel|has been removed|account .*terminated|confirm your age|"
                        r"not available in your country|live event will begin|premieres in|unsupported url|is not a valid url", re.I)


def info(url: str) -> dict:
    """Title, channel and length, without downloading anything. If YouTube asks one of its apps to prove it isn't
    a bot, the next is asked; YouTube is only taken as refusing when all of them were refused."""
    clients = YT_CLIENTS if is_youtube(url) else [None]
    token = _defer.set(True)
    refused, other = 0, None
    try:
        for n, client in enumerate(clients):
            try:
                with _youtube_guard(url), _ydl(_with_client({}, client)) as ydl:
                    i = ydl.extract_info(url, download=False)
                break
            except YouTubeBlocked:
                refused += 1
                if n == len(clients) - 1:
                    if refused == len(clients):
                        _pause_youtube()
                        raise
                    raise other                     # some were refused, others failed: the other reason is the real one
            except Exception as e:  # noqa: BLE001
                # "The page needs to be reloaded", a format list YouTube cut short...: one of YouTube's apps failing
                # isn't the video failing, so the next is asked. Only when every one failed is it said.
                if not _UNFIXABLE.search(str(e)) and n < len(clients) - 1:
                    other = e
                    continue
                raise
    finally:
        _defer.reset(token)
    return {"title": i.get("title") or "", "uploader": i.get("uploader") or i.get("channel") or "",
            "duration": float(i.get("duration") or 0), "is_live": bool(i.get("is_live")),
            "thumbnail": i.get("thumbnail") or "", "kind": i.get("_type") or "video",
            "channel_url": i.get("channel_url") or i.get("uploader_url") or "",
            # "post_live": a stream that ended and whose replay YouTube hasn't finished making
            "live_status": i.get("live_status") or "",
            # the channel's handle and platform, for the creator tag on the clips
            "handle": _tag.handle_from_info(i, url), "platform": _tag.platform_of(url)}


def youtube_words(url: str):
    """YouTube's own transcript of a video, word by word: ([{'w', 's', 'e'}, ...], language), or None if the video
    has none. Only the transcript in the language that was spoken is used, never a translation."""
    if not is_youtube(url):
        return None
    import json
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        auto = i.get("automatic_captions") or {}
        # the track in the language the video says it's in first ("hi-orig", "hi", then "en-IN" read as "en"),
        # then any original; never one of YouTube's translations
        lang = str(i.get("language") or "").strip().lower()
        base = lang.split("-")[0] if lang else ""
        key = next((c for c in (f"{lang}-orig", lang, f"{base}-orig", base) if c and c in auto), None)
        if not key:
            key = next((k for k in auto if k.endswith("-orig")), None)
        track = next((f for f in auto.get(key) or [] if f.get("ext") == "json3" and f.get("url")), None) if key else None
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    return _caption_words(json.loads(raw)), key.replace("-orig", "")


def youtube_english_words(url: str):
    """YouTube's English captions of a video spoken in another language: ones a person wrote if there are any,
    otherwise YouTube's own translation of its transcript. Word list like youtube_words, or None."""
    if not is_youtube(url):
        return None
    import json
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        tracks = []
        for group in (i.get("subtitles") or {}, i.get("automatic_captions") or {}):
            for k in ("en", "en-US", "en-GB"):
                tracks.append(next((f for f in group.get(k) or [] if f.get("ext") == "json3" and f.get("url")), None))
        track = next((t for t in tracks if t), None)
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    words = _caption_words(json.loads(raw))
    return words or None


def _caption_words(data: dict) -> list:
    out = []
    for ev in data.get("events") or []:
        segs = [x for x in ev.get("segs") or [] if (x.get("utf8") or "").strip()]
        if not segs:
            continue
        t0, dur = float(ev.get("tStartMs") or 0), float(ev.get("dDurationMs") or 0)
        timed = len(segs) > 1 or "tOffsetMs" in segs[0]
        for x in segs:
            parts = x["utf8"].split()
            for n, w in enumerate(parts):
                if re.fullmatch(r"[\[(].*[\])]", w) or w in ("[", "]", "__"):
                    continue                      # [Music], [Applause], and words YouTube blanked out
                # a line with no word timings: its words are spread evenly over the time the line was on screen
                at = t0 + float(x.get("tOffsetMs") or 0) if timed else t0 + dur * n / max(1, len(parts))
                out.append({"w": w, "s": round(at / 1000, 2)})
    out.sort(key=lambda w: w["s"])
    for n, w in enumerate(out):
        nxt = out[n + 1]["s"] if n + 1 < len(out) else w["s"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, w["s"] + 0.6)), 2)
    return out


_PREVIEWS: dict = {}


def preview(url: str) -> dict:
    """What the user will get from a link, checked before any work starts. The thumbnail comes back inline
    (the page may only load images from this site). Cached, so retyping a link costs nothing."""
    url = url.strip()
    if url in _PREVIEWS:
        return _PREVIEWS[url]
    i = info(url)
    if i["kind"] != "video" or not i["title"]:
        raise ValueError("That link is a channel or playlist, not a single video.")
    thumb = ""
    t = i.get("thumbnail") or ""
    if t.startswith("https://"):
        import base64
        import urllib.request
        try:
            req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                data = r.read(600_000 + 1)
            if ctype in ("image/jpeg", "image/png", "image/webp") and len(data) <= 600_000:
                thumb = f"data:{ctype};base64," + base64.b64encode(data).decode()
        except Exception:
            thumb = ""
    out = {"title": i["title"][:200], "uploader": i["uploader"][:120], "duration": i["duration"],
           "is_live": i["is_live"], "thumb": thumb, "post_live": i.get("live_status") == "post_live",
           "handle": i.get("handle", ""), "platform": i.get("platform", "")}
    if len(_PREVIEWS) > 300:
        _PREVIEWS.pop(next(iter(_PREVIEWS)))
    _PREVIEWS[url] = out
    return out


_FRAMES: dict = {}


def _first_frame(video: Path) -> bytes:
    """The first picture of a video file on this computer, 960 wide, as a JPEG."""
    try:
        return subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-vf", "scale=960:-2",
                               "-pix_fmt", "yuvj420p", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                              capture_output=True, timeout=60, creationflags=NO_WINDOW).stdout or b""
    except subprocess.TimeoutExpired:
        return b""


def _live_frame(url: str) -> bytes:
    """The picture a live stream is showing now: its streaming (HLS) address read straight by ffmpeg, asked as each of
    YouTube's apps in turn until one answers."""
    for client in (YT_CLIENTS if is_youtube(url) else [None]):
        try:
            with _youtube_guard(url), _ydl(_with_client({"format": "b[protocol^=m3u8]/bv*[protocol^=m3u8]/b"}, client)) as ydl:
                i = ydl.extract_info(url, download=False)
            fmt = (i.get("requested_formats") or [i])[0]
            src = fmt.get("url") or i.get("url")
            if not (isinstance(src, str) and src.startswith("https://")):
                continue
            jpeg = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-frames:v", "1", "-vf", "scale=960:-2",
                                   "-pix_fmt", "yuvj420p", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                                  capture_output=True, timeout=45, creationflags=NO_WINDOW).stdout or b""
            if len(jpeg) > 1000:
                return jpeg
        except YouTubeBlocked:
            raise
        except Exception:  # noqa: BLE001 - the next way of asking
            continue
    return b""


def frame(url: str, at: float = 0.35, t: float = None) -> dict:
    """One picture from a video link, to set the webcam and the game on before any clip is made.
    {"image": data URL, "w", "h" (the picture's size), "uploader", "at", "live", "thumb": True if it is only the
    video's thumbnail}

    A video: a three-second stretch from `at` (0-1) into it is downloaded the same way clip pieces are (each of
    YouTube's apps in turn, the kinds of file that work), and its first picture is taken: nothing else of the video is
    fetched. A live stream: the picture it shows now. If neither can be had, the video's thumbnail, said so, rather
    than nothing: for a stream it is usually a picture of the stream."""
    import base64
    import tempfile
    url = url.strip()
    at = min(0.9, max(0.05, float(at)))
    key = (url, round(at, 2)) if t is None else (url, "t", int(t))
    if key in _FRAMES:
        return _FRAMES[key]
    p = _PREVIEWS.get(url) or preview(url)               # title, length, live or not (already looked up, usually)
    dur, live = float(p.get("duration") or 0), bool(p.get("is_live"))
    jpeg = b""
    if live:
        jpeg = _live_frame(url)
    else:
        t = (max(0.0, min(dur - 3.0, float(t))) if dur > 3 else 0.0) if t is not None else \
            (max(1.0, min(dur - 5.0, dur * at)) if dur > 10 else 0.0)
        work = Path(tempfile.mkdtemp(prefix="ckf"))
        try:
            got = download_section(url, work, "frame", t, t + 3.0, height=720)
            jpeg = _first_frame(got)
        except YouTubeBlocked:
            raise
        except Exception as e:  # noqa: BLE001 - the thumbnail instead
            print(f"  Frame of {url[:80]}: {_plain(e)[-200:]}", flush=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    thumb = False
    if len(jpeg) > 1000:
        image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    elif p.get("thumb"):
        image, thumb = p["thumb"], True
    else:
        raise ValueError("Couldn't get a picture from this video. Check the link plays on YouTube, or set the webcam "
                         "on a clip once it's made (Fix facecam).")
    w = h = 0
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(base64.b64decode(image.split(",", 1)[1]))) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        pass
    out = {"image": image, "w": w, "h": h, "uploader": (p.get("uploader") or "")[:120], "at": round(at, 2),
           "live": live, "thumb": thumb}
    if not live and not thumb:                 # a live picture changes, and a thumbnail is only a stand-in
        if len(_FRAMES) > 40:
            _FRAMES.pop(next(iter(_FRAMES)))
        _FRAMES[key] = out
    return out


CHAT_BUDGET = int(config.env("PAPICLIPS_CHAT_SECONDS") or 300)    # the longest the chat replay is fetched for


class _ChatStop(Exception):
    pass


class ChatReplay:
    """The chat of a past YouTube live stream (its "chat replay"), fetched in the background while the video is worked
    on: what viewers wrote, and when. Viewers going wild in chat is the clearest sign of a moment worth clipping.

    Fetched by yt-dlp in this process (the same settings as the downloads), for at most `budget` seconds: the chat of
    a very long stream takes longer than that to fetch, and what has come by then is used. messages() can be read at
    any time; it gives what has arrived so far. Nothing here can stop a video being clipped: no chat (not a YouTube
    stream, chat turned off, YouTube refusing) is simply no chat."""

    def __init__(self, url: str, out_dir: Path, budget: int = CHAT_BUDGET):
        self.url, self.dir, self.budget = url, Path(out_dir) / "chat", budget
        self.t0, self.done, self.stop_flag = time.time(), False, False
        self.thread = None
        if not (is_youtube(url) and budget > 0):
            self.done = True
            return
        import threading
        self.dir.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._fetch, daemon=True, name="chat-replay")
        self.thread.start()

    def _hook(self, _d):
        if self.stop_flag or time.time() - self.t0 > self.budget:
            raise _ChatStop("enough chat")

    def _fetch(self):
        opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
                "ignore_no_formats_error": True, "outtmpl": str(self.dir / "c.%(ext)s"), "progress_hooks": [self._hook]}
        try:
            with _ydl(opts) as ydl:
                ydl.download([self.url])
        except Exception:  # noqa: BLE001 - stopped on time, no chat replay, or refused: what came is used
            pass
        finally:
            self.done = True

    def messages(self, wait: float = 0.0) -> list:
        """[(seconds into the video, who, what they wrote)] so far, waiting up to `wait` seconds for the rest."""
        end = time.time() + max(0.0, wait)
        while not self.done and time.time() < end and time.time() - self.t0 <= self.budget:
            time.sleep(0.5)
        out = []
        for f in sorted(self.dir.glob("c.live_chat.json*")) if self.dir.is_dir() else []:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        got = _chat_line(line)
                        if got:
                            out.append(got)
            except OSError:
                continue
        out.sort(key=lambda m: m[0])
        return out

    def stop(self):
        self.stop_flag = True
        shutil.rmtree(self.dir, ignore_errors=True)


def _chat_line(line: str):
    """One line of yt-dlp's live_chat.json: (seconds, who, text), or None for anything that isn't a viewer's message
    (or isn't shaped the way it should be: YouTube sends many kinds of item, and one odd line is skipped, never fatal)."""
    try:
        return _chat_line_inner(line)
    except Exception:  # noqa: BLE001
        return None


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _chat_line_inner(line: str):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    act = _d(d.get("replayChatItemAction"))
    try:
        t = float(act.get("videoOffsetTimeMsec") or d.get("videoOffsetTimeMsec") or -1) / 1000.0
    except (TypeError, ValueError):
        return None
    if t < 0:
        return None
    actions = act.get("actions")
    for a in actions if isinstance(actions, list) else []:
        item = _d(_d(_d(a).get("addChatItemAction")).get("item"))
        r = _d(item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer"))
        if not r:
            continue
        parts = []
        msg = r.get("message")
        runs = _d(msg).get("runs") if isinstance(msg, dict) else None
        if isinstance(msg, str):
            parts.append(msg)
        elif not isinstance(runs, list) and _d(msg).get("simpleText"):
            parts.append(str(_d(msg)["simpleText"]))
        for run in runs if isinstance(runs, list) else []:
            run = _d(run)
            if "text" in run:
                parts.append(str(run["text"]))
            elif "emoji" in run:
                e = _d(run["emoji"])
                shortcuts = e.get("shortcuts") if isinstance(e.get("shortcuts"), list) else [""]
                val = e.get("emojiId") if len(str(e.get("emojiId") or "")) <= 2 else (shortcuts or [""])[0]
                if val:
                    parts.append(str(val))
        name = r.get("authorName")
        who = str(_d(name).get("simpleText") if isinstance(name, dict) else name or "")[:40]
        text = " ".join(parts).strip()[:200]
        if "liveChatPaidMessageRenderer" in item:
            text = ("$ " + text).strip()                 # a Super Chat: someone paid to say it
        if text:
            return (round(t, 2), who, text)
    return None


def save_thumb(job_dir: Path, url: str = "", video: Path = None) -> bool:
    """A small picture for the video list: the link's own thumbnail, or a frame from an uploaded file."""
    import base64
    out = Path(job_dir) / "clips" / "thumb.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if url:
            p = _PREVIEWS.get(url.strip()) or preview(url)
            if not p.get("thumb"):
                return False
            out.write_bytes(base64.b64decode(p["thumb"].split(",", 1)[1]))
            return True
        if video:
            d = probe(video).get("duration") or 0
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(3.0, d / 3):.2f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)])
            return out.is_file()
    except Exception:
        return False
    return False


# YouTube answers differently depending on which of its apps asks ("clients"). Some give links that come back
# empty or are refused part-way for some videos ("The downloaded file is empty"), and which ones changes from
# month to month. So a download that fails is tried again as the other apps, and the one that worked is used first
# for that video's next downloads (every clip of a long stream).
YT_CLIENTS = [None, ["android_vr"], ["tv"], ["web_safari"], ["mweb"]]     # None: yt-dlp's own choice
_client_ok: dict = {}


def _plain(e) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()


def _with_client(opts: dict, client) -> dict:
    return {**opts, "extractor_args": {"youtube": {"player_client": client}}} if client else dict(opts)


def _each_client(url: str, out_dir: Path, stem: str, fetch):
    """fetch(client, logger) for each way of asking YouTube until one gives a real file; the file's path."""
    clients = YT_CLIENTS if is_youtube(url) else [None]
    if url in _client_ok:                                    # the one that worked for this video, first
        clients = [_client_ok[url]] + [c for c in clients if c != _client_ok[url]]
    why, tried, refused = [], 0, 0
    token = _defer.set(True)          # one app refused (a bot check) isn't YouTube refusing: the others are asked
    try:
        return _each_try(url, out_dir, stem, fetch, clients, why, tried, refused)
    finally:
        _defer.reset(token)


def _each_try(url, out_dir, stem, fetch, clients, why, tried, refused):
    last = None
    for client in clients:
        for p in out_dir.glob(stem + ".*"):
            remove(p)                                        # nothing half-written from the try before
        log = _Quiet()
        tried += 1
        try:
            fetch(client, log)
            got = _biggest(out_dir, stem)
            if got.stat().st_size < 1024:
                raise RuntimeError("The downloaded file is empty")
            if is_youtube(url):
                _client_ok[url] = client
                if len(_client_ok) > 200:
                    _client_ok.pop(next(iter(_client_ok)))
            return got
        except YouTubeBlocked as e:
            if youtube_paused():
                raise                                        # refused before: not asked again for a while
            refused += 1
            last, why = e, log.problems
        except Exception as e:  # noqa: BLE001 - asked again as another of YouTube's apps
            last, why = e, log.problems
    if refused and refused == tried:
        _pause_youtube()                                     # every one of YouTube's apps was refused
        raise YouTubeBlocked(YT_BLOCKED)
    extra = [w for w in why if w not in str(last)][-2:]
    more = f" (tried {tried} ways)" if tried > 1 else ""
    raise RuntimeError(_plain(last) + more + (" (" + " | ".join(extra) + ")" if extra else ""))


def download_audio(url: str, job_dir: Path, on_progress=None) -> Path:
    """Audio only: a few seconds even for long videos, and all the AI needs to pick clips."""
    last = {"at": 0.0}

    def hook(d):
        if not on_progress or d.get("status") != "downloading":
            return
        now = time.time()
        if now - last["at"] < 1.0:
            return                               # a long stream sends thousands of these: once a second is plenty
        last["at"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        got = d.get("downloaded_bytes") or 0
        # A long stream often has no size known up front (it comes in pieces): how many pieces are done says it
        # instead, so the bar moves and nobody thinks it's stuck at 3%.
        frac = got / total if total else (d.get("fragment_index") or 0) / d["fragment_count"] if d.get("fragment_count") else None
        note = f"{got / 1e6:.0f} MB" if got >= 1e6 else ""
        try:
            on_progress(min(1.0, frac) if frac is not None else None, note)
        except TypeError:
            if frac is not None:
                on_progress(min(1.0, frac))

    def fetch(client, log):
        opts = {"format": "ba[ext=m4a]/ba/worst", "outtmpl": str(job_dir / "audio_src.%(ext)s"),
                "progress_hooks": [hook], "logger": log}
        with _youtube_guard(url), _ydl(_with_client(opts, client)) as ydl:
            ydl.download([url])
    return _each_client(url, job_dir, "audio_src", fetch)


def download(url: str, job_dir: Path, height=720) -> Path:
    """The whole video. Used for shorter videos, where this is quicker than fetching pieces."""
    def fetch(client, log):
        opts = {"format": _video_format(height), "merge_output_format": "mp4",
                "outtmpl": str(job_dir / "source.%(ext)s"), "logger": log}
        with _youtube_guard(url), _ydl(_with_client(opts, client)) as ydl:
            ydl.download([url])
    return _each_client(url, job_dir, "source", fetch)


def download_section(url: str, out_dir: Path, stem: str, start: float, end: float, height=720, exact=False) -> Path:
    """Just one stretch of the video. The cut lands on a keyframe, so find_offset() pins the true start; with
    exact=True the cut is made at exactly `start` instead (slower), for when there is nothing to match against."""
    from yt_dlp.utils import download_range_func
    out_dir.mkdir(parents=True, exist_ok=True)
    # For each way of asking YouTube: the usual video and sound files first; if fetching a stretch of those fails,
    # the streaming (HLS) version, which is cut differently; then the single small file YouTube keeps for old players.
    formats = (_video_format(height),
               f"b[height<={height}][protocol^=m3u8]/bv*[height<={height}][protocol^=m3u8]+ba/b[protocol^=m3u8]",
               "18/b[ext=mp4][vcodec!=none][acodec!=none]")

    def fetch(client, log):
        err = None
        for fmt in formats:
            for p in out_dir.glob(stem + ".*"):
                p.unlink(missing_ok=True)
            opts = {"format": fmt, "merge_output_format": "mp4", "outtmpl": str(out_dir / (stem + ".%(ext)s")),
                    "download_ranges": download_range_func(None, [(max(0.0, start), end)]), "logger": log}
            if exact:
                opts["force_keyframes_at_cuts"] = True
            try:
                with _youtube_guard(url), _ydl(_with_client(opts, client)) as ydl:
                    ydl.download([url])
                if _biggest(out_dir, stem).stat().st_size >= 1024:
                    return
                err = RuntimeError("The downloaded file is empty")
            except YouTubeBlocked:
                raise
            except Exception as e:  # noqa: BLE001 - the next kind of file
                err = e
        raise err
    return _each_client(url, out_dir, stem, fetch)


def find_offset(full: np.ndarray, part: np.ndarray, expected: float, search=20.0) -> float:
    """Where in the full audio (seconds) a downloaded piece really begins, by matching the sound itself."""
    n = min(len(part), 30 * 16000)
    a0 = max(0, int((expected - search) * 16000))
    hay = full[a0: int((expected + search) * 16000) + n].astype(np.float32)
    needle = part[:n].astype(np.float32)
    if n < 16000 or len(hay) < n or float(np.abs(needle).mean()) < 20:
        return expected  # too short or silent to match: trust the requested time
    size = 1 << (len(hay) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay, size) * np.conj(np.fft.rfft(needle, size)), size)[: len(hay) - n + 1]
    return (a0 + int(np.argmax(corr))) / 16000


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    if not any(s["codec_type"] == "audio" for s in data["streams"]):
        raise RuntimeError("This file has no audio track, so there is nothing to transcribe.")
    w, h = int(v["width"]), int(v["height"])
    if w * h > 3840 * 2160 or w < 16 or h < 16:
        raise RuntimeError("This video's resolution isn't supported (4K is the largest).")
    rot = str((v.get("side_data_list") or [{}])[0].get("rotation", "0"))
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    return {"width": w, "height": h, "duration": float(data["format"]["duration"])}


def extract_audio(src: Path, wav: Path):
    # first_pts=0 pads any gap before the audio starts, so second 0 of the wav is second 0 of the file
    # -t: never more audio than the longest video allowed, whatever the file claims about itself
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", str(int(config.MAX_VIDEO_HOURS * 3600) + 60),
         "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(wav)])


def load_audio(wav: Path) -> np.ndarray:
    data, sr = sf.read(str(wav), dtype="int16")
    assert sr == 16000
    return data


def voice_per_second(audio: np.ndarray) -> np.ndarray:
    """For each second, how much of the sound is in the 2-4 kHz band: a raised voice (shouting, screaming, yelling)
    puts its energy there, while game music and explosions mostly sit lower. 0 to 1."""
    n = len(audio) // 16000
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    out = np.empty(n, dtype=np.float32)
    step = 120                                           # two minutes at a time, so memory stays small
    for i in range(0, n, step):
        j = min(n, i + step)
        x = audio[i * 16000: j * 16000].reshape(j - i, 16000).astype(np.float32) / 32768.0
        p = np.abs(np.fft.rfft(x, axis=1)) ** 2           # one bin per Hz
        total = p[:, 100:8000].sum(axis=1) + 1e-9
        out[i:j] = p[:, 2000:4000].sum(axis=1) / total
    return out


def loudness_per_second(audio: np.ndarray) -> np.ndarray:
    """RMS level in dB for each second of audio."""
    n = len(audio) // 16000
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    # ten minutes at a time: a long video's audio converted all at once needs more than a gigabyte of memory
    out = np.empty(n, dtype=np.float32)
    step = 600
    for i in range(0, n, step):
        j = min(n, i + step)
        x = audio[i * 16000: j * 16000].reshape(j - i, 16000).astype(np.float32) / 32768.0
        out[i:j] = np.sqrt((x * x).mean(axis=1) + 1e-10)
    return (20 * np.log10(out)).astype(np.float32)
