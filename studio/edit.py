"""The clip editor.

What the editor needs to show a clip live, without waiting for a render: a light preview copy of the clip with some
room either side of it, a strip of thumbnails, its words, how loud it is moment by moment and a "hype" curve (the
loudness, and the stream's chat where it was read). And what an edit can hold, checked before it is saved, and how it
is turned into the ffmpeg graph that makes the clip:

  cuts       parts taken out (on the timeline, or by deleting words in the transcript)
  fx         transitions on a cut: flash, whoosh, zoom punch, wipe, glitch, dip to black, spin, blur, shake
  sfx        sound effects, made here from scratch (no one else's recordings, so nothing to license)
  punch      punch-ins: the picture pushes in for a moment
  look       a colour look for the whole clip
  hook_fx    how the big text appears, and hook_y / hook_dur: where it sits and how long it stays
  cap_y      where the captions sit

Every time in an edit is in the clip's own source timeline (the same seconds as the clip's start and end), so
trimming the clip never moves an effect off the moment it was put on.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import threading
from pathlib import Path

from . import config
from .media import NO_WINDOW

TRANSITIONS = {"flash": "Flash", "whoosh": "Whoosh", "zoom": "Zoom punch", "wipe": "Wipe", "glitch": "Glitch",
               "fade": "Dip to black", "spin": "Spin", "blur": "Blur", "shake": "Shake"}
# the sound a transition brings with it, when its sound is on
TRANSITION_SOUND = {"flash": "boom", "whoosh": "whoosh", "zoom": "swoosh", "wipe": "swoosh", "glitch": "glitch",
                    "fade": "", "spin": "whoosh", "blur": "swoosh", "shake": "hit"}
# name, the sound drawn from nothing (ffmpeg's own generators), and its finishing (level, filters)
SOUNDS = {
    "whoosh": ("Whoosh", "anoisesrc=c=pink:d=0.75:a=0.9",
               "highpass=f=250,lowpass=f=3500,afade=t=in:d=0.45:curve=exp,afade=t=out:st=0.45:d=0.3,volume=3.2"),
    "swoosh": ("Swoosh", "anoisesrc=c=white:d=0.4:a=0.7",
               "highpass=f=1600,afade=t=in:d=0.18,afade=t=out:st=0.18:d=0.22:curve=exp,volume=1.1"),
    "boom": ("Boom", "aevalsrc='0.95*sin(2*PI*(38+70*exp(-7*t))*t)*exp(-2.6*t)+0.25*(random(0)-0.5)*exp(-18*t)':d=1.6:s=48000",
             "lowpass=f=900,volume=1.2"),
    "hit": ("Hit", "aevalsrc='0.9*sin(2*PI*(60+120*exp(-25*t))*t)*exp(-6*t)+0.5*(random(0)-0.5)*exp(-30*t)':d=0.7:s=48000",
            "volume=1.0"),
    "bass": ("Bass drop", "aevalsrc='0.95*sin(2*PI*(110*exp(-1.2*t))*t)*min(1\\,t*20)*exp(-0.9*t)':d=2.2:s=48000",
             "lowpass=f=300,volume=1.1"),
    "riser": ("Riser", "aevalsrc='0.35*sin(2*PI*(180*t+260*t*t))*(t/1.8)+0.15*(random(0)-0.5)*(t/1.8)':d=1.8:s=48000",
              "highpass=f=120,afade=t=out:st=1.65:d=0.15,volume=3.2"),
    "ding": ("Ding", "aevalsrc='0.5*sin(2*PI*1318*t)*exp(-3.5*t)+0.22*sin(2*PI*2637*t)*exp(-5*t)+0.1*sin(2*PI*3955*t)*exp(-7*t)':d=1.4:s=48000",
             "volume=1.6"),
    "pop": ("Pop", "aevalsrc='0.9*sin(2*PI*(900-4800*t)*t)*exp(-40*t)':d=0.18:s=48000", "volume=1.1"),
    "glitch": ("Glitch", "aevalsrc='0.45*sgn(sin(2*PI*(90+400*random(1))*t))*gt(random(0)\\,0.35)':d=0.35:s=48000",
               "lowpass=f=6000,afade=t=out:st=0.25:d=0.1,volume=2.6"),
    "shutter": ("Camera", "aevalsrc='0.7*(random(0)-0.5)*(exp(-90*t)+0.8*exp(-90*abs(t-0.09))*gt(t\\,0.08))':d=0.25:s=48000",
                "highpass=f=800,volume=2.4"),
}
# colour looks: the ffmpeg filters, and the browser's CSS filter that previews the same look
LOOKS = {
    "natural": ("Natural", "", "none"),
    "punchy": ("Punchy", "eq=contrast=1.10:saturation=1.45", "contrast(1.1) saturate(1.45)"),
    "vivid": ("Vivid", "eq=saturation=1.8:contrast=1.05,unsharp=5:5:0.6", "saturate(1.8) contrast(1.05)"),
    "warm": ("Warm", "colorbalance=rs=.08:gs=.02:bs=-.07:rm=.05:bm=-.05,eq=saturation=1.15", "sepia(.25) saturate(1.3)"),
    "cool": ("Cool", "colorbalance=rs=-.06:bs=.08:bm=.05,eq=saturation=1.05", "hue-rotate(-12deg) saturate(1.05) brightness(1.02)"),
    "cinema": ("Cinema", "eq=contrast=1.18:saturation=.85:gamma=.95,colorbalance=bs=.06:rh=.05", "contrast(1.18) saturate(.85) brightness(.96)"),
    "bw": ("Black & white", "hue=s=0,eq=contrast=1.25", "grayscale(1) contrast(1.25)"),
}
HOOK_FX = {"box": "Box", "pop": "Pop", "lime": "Lime", "outline": "Outline", "glow": "Glow", "type": "Typewriter"}

MAX_CUTS, MAX_FX, MAX_SFX, MAX_PUNCH, MAX_MEDIA = 60, 30, 40, 20, 12
LIB_ID = re.compile(r"^[0-9a-f]{12}$")             # an item of the person's own library (studio/library.py)
LIB = re.compile(r"^lib:[0-9a-f]{12}$")
PAD = 12.0                 # seconds of the video either side of the clip in its preview copy (room to trim out)
SFX_DIR = config.DATA_DIR / "sfx"


class EditError(ValueError):
    pass


# ---------------------------------------------------------------- checking an edit before it is saved
def _num(v, lo, hi, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise EditError(f"{what} isn't a number.")
    if not math.isfinite(f):
        raise EditError(f"{what} isn't a number.")
    return round(min(hi, max(lo, f)), 3)


def clean(v, start: float, end: float, lo: float | None = None, hi: float | None = None) -> dict:
    """Only what an edit can hold, every time inside the part of the video the clip can reach (lo..hi, the clip
    with room either side) and every value inside its range. Raises EditError on anything that isn't an edit."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise EditError("That edit isn't valid.")
    lo = start - PAD if lo is None else lo
    hi = end + PAD if hi is None else hi
    lo = max(0.0, lo)
    t = lambda x, what="A time": _num(x, lo, hi, what)          # noqa: E731
    out: dict = {}
    cuts = []
    for c in (v.get("cuts") or [])[:MAX_CUTS]:
        if not isinstance(c, (list, tuple)) or len(c) != 2:
            raise EditError("A cut isn't valid.")
        a, b = sorted((t(c[0]), t(c[1])))
        a, b = max(a, start), min(b, end)
        if b - a >= 0.05:
            cuts.append([a, b])
    cuts.sort()
    merged: list = []
    for a, b in cuts:
        if merged and a <= merged[-1][1] + 0.02:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if merged:
        if (end - start) - sum(b - a for a, b in merged) < 1.5:
            raise EditError("That cuts out almost all of the clip. Keep at least a second and a half.")
        out["cuts"] = merged
    fx = []
    for f in (v.get("fx") or [])[:MAX_FX]:
        if not isinstance(f, dict) or f.get("type") not in TRANSITIONS:
            raise EditError("A transition isn't valid.")
        fx.append({"t": t(f.get("t")), "type": f["type"], "d": _num(f.get("d", 0.4), 0.1, 1.5, "A transition's length"),
                   "sound": bool(f.get("sound", True))})
    if fx:
        out["fx"] = sorted(fx, key=lambda x: x["t"])
    sfx = []
    for s in (v.get("sfx") or [])[:MAX_SFX]:
        # one of PapiClips's sounds, or one from the person's own library ("lib:" and its id)
        if not isinstance(s, dict) or not (s.get("kind") in SOUNDS or LIB.match(str(s.get("kind", "")))):
            raise EditError("A sound effect isn't valid.")
        sfx.append({"t": t(s.get("t")), "kind": s["kind"], "vol": _num(s.get("vol", 0.8), 0.0, 1.5, "A sound's volume")})
    if sfx:
        out["sfx"] = sorted(sfx, key=lambda x: x["t"])
    punch = []
    for p in (v.get("punch") or [])[:MAX_PUNCH]:
        if not isinstance(p, dict):
            raise EditError("A punch-in isn't valid.")
        punch.append({"t": t(p.get("t")), "z": _num(p.get("z", 1.2), 1.05, 1.8, "A punch-in's zoom"),
                      "d": _num(p.get("d", 1.2), 0.3, 6.0, "A punch-in's length")})
    if punch:
        out["punch"] = sorted(punch, key=lambda x: x["t"])
    look = v.get("look") or "natural"
    if look not in LOOKS:
        raise EditError("That look isn't one of PapiClips's.")
    if look != "natural":
        out["look"] = look
    hfx = v.get("hook_fx") or "box"
    if hfx not in HOOK_FX:
        raise EditError("That text effect isn't one of PapiClips's.")
    if hfx != "box":
        out["hook_fx"] = hfx
    for k, (a, b) in {"hook_y": (120, 1750), "cap_y": (300, 1750)}.items():
        if v.get(k) not in (None, ""):
            out[k] = int(_num(v[k], a, b, "A place on the screen"))
    if v.get("voice") not in (None, ""):          # the stream's own sound: 0 (off) to 1.5 (louder)
        vo = round(_num(v["voice"], 0, 1.5, "The stream's volume"), 2)
        if vo != 1:
            out["voice"] = vo
    if v.get("hook_dur") not in (None, "", 0):
        out["hook_dur"] = _num(v["hook_dur"], 0.5, 180, "How long the big text stays")
    for k in ("hook_font", "cap_font"):            # a font from the person's library, by its id
        if v.get(k):
            if not LIB_ID.match(str(v[k])):
                raise EditError("That font isn't in your library.")
            out[k] = str(v[k])
    media = []
    for m in (v.get("media") or [])[:MAX_MEDIA]:   # pictures, GIFs and videos from the library, laid over the clip
        if not isinstance(m, dict) or not LIB_ID.match(str(m.get("id", ""))):
            raise EditError("A picture or video on the clip isn't valid.")
        media.append({"id": str(m["id"]), "t": t(m.get("t")), "d": _num(m.get("d", 3), 0.2, 120, "How long a picture stays"),
                      "x": _num(m.get("x", 0.5), -0.5, 1.5, "A place on the screen"), "y": _num(m.get("y", 0.5), -0.5, 1.5, "A place on the screen"),
                      "w": _num(m.get("w", 0.6), 0.05, 2.0, "A size"), "vol": _num(m.get("vol", 1.0), 0.0, 1.5, "A volume"),
                      "from": _num(m.get("from", 0), 0, 600, "Where a video starts")})
    if media:
        out["media"] = sorted(media, key=lambda x: x["t"])
    return out


# ---------------------------------------------------------------- turning an edit into the clip
def keep_from_cuts(cuts, start, end) -> list:
    """The parts of the clip kept after the edit's cuts, in seconds from the clip's start ([] when nothing is cut)."""
    keep, at = [], 0.0
    for a, b in cuts or []:
        a, b = max(0.0, a - start), min(end - start, b - start)
        if b <= a:
            continue
        if a - at > 0.05:
            keep.append((round(at, 3), round(a, 3)))
        at = max(at, b)
    if not keep and at == 0.0:
        return []
    if (end - start) - at > 0.05:
        keep.append((round(at, 3), round(end - start, 3)))
    return keep


def intersect(a: list, b: list) -> list:
    """Where two lists of kept parts overlap (an empty list means the whole clip is kept)."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi - lo > 0.05:
            out.append((round(lo, 3), round(hi, 3)))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def to_out(t_abs: float, start: float, keep: list, length: float):
    """A moment in the source, in the finished clip's time (None when it was cut out or falls outside the clip). A
    moment on a cut (a transition) lands on the join."""
    t = t_abs - start
    if not keep:
        return t if 0 <= t <= length else None
    out = 0.0
    for a, b in keep:
        if t < a - 0.05:
            return out if out > 0 else None       # in a cut: the join it leads to
        if t <= b + 0.05:
            return out + min(max(t - a, 0.0), b - a)
        out += b - a
    return None


def _bump(t, h):
    """1 at moment t, falling to 0 h seconds either side: for brightness, zoom and shake."""
    return f"max(0\\,1-abs(T-{t:.3f})/{h:.3f})"


def post_graph(ed: dict, start: float, keep: list, length: float, w: int, h: int, fps: int, src: str = "[v0]",
               dst: str = "[vp]") -> str:
    """The ffmpeg graph that adds an edit's look, punch-ins and transitions to the laid-out picture (src -> dst), in
    the finished clip's time. "" when the edit adds nothing to the picture."""
    ed = ed or {}
    look = LOOKS.get(ed.get("look") or "natural", LOOKS["natural"])[1]
    fx = []
    for f in ed.get("fx") or []:
        at = to_out(f["t"], start, keep, length)
        if at is not None and 0.05 < at < length - 0.05:
            fx.append(dict(f, at=at, h=max(0.05, f["d"] / 2)))
    punch = []
    for p in ed.get("punch") or []:
        at = to_out(p["t"], start, keep, length)
        if at is not None and at < length - 0.2:
            punch.append(dict(p, at=at))
    if not (look or fx or punch):
        return ""
    chain = []
    if look:
        chain.append(look)
    # one zoompan for every push in: punch-ins (quick in, held, eased out) and the zoom, whoosh and spin transitions
    zs = [f"{p['z'] - 1:.3f}*clip((it-{p['at'] - 0.12:.3f})/0.15\\,0\\,1)*clip(({p['at'] + p['d']:.3f}-it)/0.3\\,0\\,1)"
          for p in punch]
    push = {"zoom": 0.45, "whoosh": 0.10, "spin": 0.30, "shake": 0.06}
    zs += ["%.2f*%s" % (push[f["type"]], _bump(f["at"], f["h"]).replace("T", "it")) for f in fx if f["type"] in push]
    if zs:
        chain.append(f"zoompan=z='1+{'+'.join(zs)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps}")
    spins = [f for f in fx if f["type"] == "spin"]
    if spins:
        a = "+".join(f"0.35*sin(PI*clip((t-{f['at'] - f['h']:.3f})/{2 * f['h']:.3f}\\,0\\,1))*sin(2*PI*clip((t-{f['at'] - f['h']:.3f})/{2 * f['h']:.3f}\\,0\\,1))"
                     for f in spins)
        chain.append(f"rotate=a='{a}':c=black")
    shakes = [f for f in fx if f["type"] == "shake"]
    if shakes:
        s = "+".join(_bump(f["at"], f["h"] * 1.6).replace("T", "t") for f in shakes)
        mx, my = int(w * 0.03), int(h * 0.03)
        chain.append(f"scale={w + 2 * mx}:{h + 2 * my},crop={w}:{h}:x='{mx}+{mx * 0.8:.0f}*sin(t*73)*({s})':y='{my}+{my * 0.8:.0f}*cos(t*61)*({s})'")
    for f in fx:
        win = f"between(t,{f['at'] - f['h']:.3f},{f['at'] + f['h']:.3f})"
        if f["type"] == "blur":
            chain.append(f"boxblur=26:2:enable='{win}'")
        elif f["type"] == "whoosh":
            chain.append(f"avgblur=sizeX=70:sizeY=1:enable='{win}'")
        elif f["type"] == "zoom":
            chain.append(f"boxblur=8:1:enable='between(t,{f['at'] - f['h'] / 2:.3f},{f['at'] + f['h'] / 2:.3f})'")
        elif f["type"] == "glitch":
            chain.append(f"rgbashift=rh=18:bh=-18:gv=6:enable='{win}',noise=alls=40:allf=t:enable='{win}'")
    light = "+".join(f"0.85*{_bump(f['at'], f['h']).replace('T', 't')}" for f in fx if f["type"] == "flash")
    dark = "+".join(_bump(f["at"], f["h"]).replace("T", "t") for f in fx if f["type"] == "fade")
    if light or dark:
        chain.append(f"eq=brightness='{(light or '0')}-({dark or '0'})':eval=frame")
    graph = f"{src}{','.join(chain) if chain else 'null'}"
    wipes = [f for f in fx if f["type"] == "wipe"]
    if not wipes:
        return graph + dst
    parts, cur = [graph + "[pw0]"], "[pw0]"
    for i, f in enumerate(wipes):
        a, d = f["at"] - f["h"], 2 * f["h"]
        nxt = dst if i == len(wipes) - 1 else f"[pw{i + 1}]"
        parts.append(f"color=c=0xC6F432:s={w}x{h}:r={fps}:d={length + 1:.2f}[wc{i}]")
        parts.append(f"{cur}[wc{i}]overlay=x='-w+(t-{a:.3f})/{d:.3f}*(W+w)':y=0:enable='between(t,{a:.3f},{a + d:.3f})':eval=frame:shortest=1{nxt}")
        cur = nxt
    return ";".join(parts)


def sounds_for(ed: dict, start: float, keep: list, length: float, lib: dict | None = None) -> list:
    """(sound file, when in the finished clip, volume) for every sound effect of the edit, and the sounds its
    transitions bring. Sounds that can't be made (or were deleted from the library) are left out."""
    out = []
    items = [(s["t"], s["kind"], s["vol"]) for s in (ed or {}).get("sfx") or []]
    items += [(f["t"], TRANSITION_SOUND.get(f["type"], ""), 0.8) for f in (ed or {}).get("fx") or [] if f.get("sound")]
    for t, kind, vol in items:
        if not kind or vol <= 0:
            continue
        at = to_out(t, start, keep, length)
        if at is None or at >= length - 0.05:
            continue
        if kind in ("whoosh", "swoosh", "riser"):         # these build up to the moment: they start before it
            at = max(0.0, at - {"whoosh": 0.45, "swoosh": 0.18, "riser": 1.6}[kind])
        if kind.startswith("lib:"):
            it = (lib or {}).get(kind[4:])
            f = Path(it["path"]) if it and it.get("kind") == "sound" else None
        else:
            f = sound_file(kind)
        if f:
            out.append((f, round(at, 3), vol))
    return out[:MAX_SFX]


def overlays_for(ed: dict, start: float, keep: list, length: float, lib: dict | None = None) -> list:
    """The pictures, GIFs and videos laid over the clip, ready for the render: their file, kind, when they show (in
    the finished clip's time) and for how long, their middle (x, y as parts of the frame), width (part of the frame),
    whether their sound is heard, and its volume. Ones deleted from the library are left out."""
    out = []
    for m in (ed or {}).get("media") or []:
        it = (lib or {}).get(m["id"])
        if not it or it.get("kind") != "media":
            continue
        at = to_out(m["t"], start, keep, length)
        if at is None or at >= length - 0.05:
            continue
        d = min(m["d"], length - at)
        if it.get("type") in ("video", "gif") and it.get("dur"):
            d = min(d, max(0.2, it["dur"] - m.get("from", 0)))
        out.append({"path": it["path"], "type": it.get("type", "image"), "at": round(at, 3), "d": round(d, 3), "x": m["x"], "y": m["y"],
                    "w": m["w"], "vol": m["vol"], "from": m.get("from", 0), "audio": bool(it.get("audio")) and m["vol"] > 0})
    return out[:MAX_MEDIA]


_sfx_lock = threading.Lock()


def sound_file(kind: str) -> Path | None:
    """The sound effect's file, made the first time it is asked for."""
    if kind not in SOUNDS:
        return None
    f = SFX_DIR / f"{kind}.wav"
    if f.is_file() and f.stat().st_size > 1000:
        return f
    with _sfx_lock:
        if f.is_file() and f.stat().st_size > 1000:
            return f
        SFX_DIR.mkdir(parents=True, exist_ok=True)
        _, src, fin = SOUNDS[kind]
        tmp = f.with_suffix(".part.wav")
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-af", fin + ",alimiter=limit=0.89",
                                "-ac", "2", "-ar", "48000", str(tmp)], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0 or not tmp.is_file():
            print(f"  Sound effect {kind} not made: {r.stderr.decode(errors='replace')[-200:]}", flush=True)
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(f)
        return f


# ---------------------------------------------------------------- what the editor shows: the preview copy
_prep_locks: dict = {}


def _lock_for(key) -> threading.Lock:
    with _sfx_lock:
        return _prep_locks.setdefault(key, threading.Lock())


def prepare(job: dict, clip: dict, src: Path, meta: dict, off: float, words: list) -> dict:
    """The editor's preview copy of a clip (made once, then kept until the clip's video changes): a light mp4 of the
    clip with PAD seconds either side, a thumbnail strip, its words, its loudness and its hype curve. Times in the
    returned data are in the clip's source timeline; `p0` is where the preview copy starts in it."""
    d = Path(job["dir"]) / "edit" / clip["id"]
    d.mkdir(parents=True, exist_ok=True)
    dur = float(meta.get("duration") or 0) or (clip["end"] - off + PAD)
    a = max(0.0, clip["start"] - off - PAD)
    b = min(dur, clip["end"] - off + PAD)
    st = src.stat()
    key = [str(src), st.st_size, int(st.st_mtime), round(a, 2), round(b, 2)]
    info = d / "info.json"
    with _lock_for(str(d)):
        try:
            got = json.loads(info.read_text(encoding="utf-8"))
            if got.get("key") == key and (d / "preview.mp4").is_file():
                return got
        except (OSError, ValueError):
            pass
        sw, sh = int(meta.get("width") or 0), int(meta.get("height") or 0)
        if sw and sh:
            big = 960
            if sw >= sh:
                pw, ph = min(big, sw), int(round(min(big, sw) * sh / sw / 2)) * 2
            else:
                ph, pw = min(big, sh), int(round(min(big, sh) * sw / sh / 2)) * 2
            scale = f"scale={pw}:{ph}"
        else:
            scale = "scale='min(960,iw)':-2"
        # keyframes every half second, so scrubbing and clicking anywhere lands at once
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(src),
                            "-vf", f"{scale},fps=30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-g", "15",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart",
                            str(d / "preview.part.mp4")], capture_output=True, timeout=600, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("The preview of this clip couldn't be made: " + r.stderr.decode(errors="replace")[-300:])
        (d / "preview.part.mp4").replace(d / "preview.mp4")
        length = b - a
        step = 1.0 if length <= 120 else length / 120          # at most 120 thumbnails
        n = max(1, int(math.ceil(length / step)))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(d / "preview.mp4"), "-vf",
                        f"fps=1/{step:.3f},scale=-2:72,tile={n}x1", "-frames:v", "1", "-q:v", "5", str(d / "strip.jpg")],
                       capture_output=True, timeout=180, creationflags=NO_WINDOW)
        peaks, hype = _sound_curves(d / "preview.mp4", length, job, a)
        out = {"key": key, "p0": round(a + off, 3), "p1": round(b + off, 3), "step": step, "thumbs": n,
               "peaks": peaks, "hype": hype, "w": sw, "h": sh}
        info.write_text(json.dumps(out), encoding="utf-8")
        return out


def _sound_curves(f: Path, length: float, job: dict, a: float):
    """How loud the preview is every 0.05 s (0..1, for the waveform) and a hype curve every 0.25 s (0..1): the
    loudness, smoothed, lifted where the stream's chat went off."""
    import numpy as np
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, timeout=120, creationflags=NO_WINDOW)
        x = np.frombuffer(p.stdout[: len(p.stdout) // 2 * 2], np.int16).astype(np.float32)
    except (OSError, subprocess.TimeoutExpired):
        x = np.zeros(0, np.float32)
    n = len(x) // 400
    if not n:
        return [], []
    rms = np.sqrt((x[: n * 400].reshape(n, 400) ** 2).mean(axis=1))
    top = float(np.percentile(rms, 99)) or 1.0
    peaks = np.clip(rms / top, 0, 1)
    db = 20 * np.log10(rms + 1)
    k = 5                                                    # 0.25 s
    m = len(db) // k
    loud = db[: m * k].reshape(m, k).mean(axis=1) if m else np.zeros(0)
    if len(loud) > 4:
        loud = np.convolve(loud, np.ones(4) / 4, mode="same")
        lo, hi = np.percentile(loud, 20), np.percentile(loud, 98)
        loud = np.clip((loud - lo) / max(1e-6, hi - lo), 0, 1)
    chat = _chat_curve(job, a, len(loud))
    hype = np.clip(loud * 0.7 + chat * 0.45, 0, 1) if chat is not None else loud
    return [round(float(v), 3) for v in peaks], [round(float(v), 3) for v in hype]


def _chat_curve(job, a, m):
    """The stream's chat activity (0..1) every 0.25 s from second a, when the chat replay was read and kept."""
    import numpy as np
    f = Path(job["dir"]) / "chat.json"
    if not f.is_file() or not m:
        return None
    try:
        z = np.asarray(json.loads(f.read_text(encoding="utf-8")), dtype=np.float32)
    except (OSError, ValueError):
        return None
    if not len(z):
        return None
    idx = (a + np.arange(m) * 0.25).astype(int)
    v = np.where(idx < len(z), z[np.minimum(idx, len(z) - 1)], 0)
    return np.clip(v / 3.0, 0, 1)


def options() -> dict:
    """The editor's choices, for the app."""
    return {"transitions": TRANSITIONS, "transition_sound": TRANSITION_SOUND,
            "sounds": {k: v[0] for k, v in SOUNDS.items()},
            "looks": {k: {"label": v[0], "css": v[2]} for k, v in LOOKS.items()}, "hook_fx": HOOK_FX}
