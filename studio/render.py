"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, roman
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
    # the word being said sits on a bright yellow box, in dark letters
    "box": {"label": "Box", "font": "Poppins ExtraBold", "size": 116, "base": "FFFFFF", "active": "14100E",
            "strong": "FFFFFF", "outline": 6, "pop": 100, "upper": True, "per": 3, "box": "00E5FF"},
    # one big word at a time, the way gaming and podcast clips often do it
    "one": {"label": "One word", "font": "Anton", "size": 200, "base": "FFFFFF", "active": "32F0B4",
            "strong": "00D7FF", "outline": 9, "pop": 112, "upper": True, "per": 1},
    # white letters with a purple glow-like outline, the word being said in lime
    "neon": {"label": "Neon", "font": "Archivo Black", "size": 118, "base": "FFFFFF", "active": "5AFAD4",
             "strong": "5AFAD4", "outline": 8, "pop": 110, "upper": True, "per": 3, "outline_colour": "FF3CC8"},
    # comic-book letters, two words at a time, the word being said in yellow
    "comic": {"label": "Comic", "font": "Bangers", "size": 156, "base": "FFFFFF", "active": "00E1FF",
              "strong": "00E1FF", "outline": 9, "pop": 116, "upper": True, "per": 2},
    # round, chunky letters with a thick outline, the word being said in lime
    "pop": {"label": "Pop", "font": "Luckiest Guy", "size": 124, "base": "FFFFFF", "active": "32F4C6",
            "strong": "32F4C6", "outline": 9, "pop": 114, "upper": True, "per": 2},
    # tall, narrow letters, the word being said in orange-red
    "tall": {"label": "Tall", "font": "Bebas Neue", "size": 176, "base": "FFFFFF", "active": "3C5AFF",
             "strong": "00D7FF", "outline": 7, "pop": 110, "upper": True, "per": 3},
    # each word turns yellow as it is said, and stays yellow: the line fills up like karaoke
    "karaoke": {"label": "Karaoke", "font": "Poppins ExtraBold", "size": 104, "base": "FFFFFF", "active": "00DCFF",
                "strong": "00DCFF", "outline": 6, "pop": 104, "upper": False, "per": 4, "keep": True},
    # calm, for podcasts and talking: grey words turn white as they are said, on a soft dark band
    "podcast": {"label": "Podcast", "font": "Poppins ExtraBold", "size": 86, "base": "B9B9B9", "active": "FFFFFF",
                "strong": "FFFFFF", "outline": 0, "pop": 100, "upper": False, "per": 4, "keep": True, "band": "000000"},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf",
              "Bangers": "Bangers-Regular.ttf", "Luckiest Guy": "LuckiestGuy-Regular.ttf", "Bebas Neue": "BebasNeue-Regular.ttf",
              "Mukta ExtraBold": "Mukta-ExtraBold.ttf"}
DEVA_FONT = "Mukta ExtraBold"      # Hindi in its own script (Devanagari): every style draws it in this font
MARK = {"font": "Poppins ExtraBold", "size": 46}      # the clipper's own @handle, small and see-through
CAP_MAX_W = 960   # captions never run wider than this, whatever is said


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    src = Path(__file__).resolve().parent / "fonts"
    for f in src.glob("*.ttf"):
        if not (config.FONTS_DIR / f.name).exists():
            shutil.copyfile(f, config.FONTS_DIR / f.name)


LAYOUTS = {"blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)",
           "gameplay": "Gameplay fills the screen, facecam in the corner",
           "split": "Facecam on top, gameplay below"}
FACECAM_LAYOUTS = ("gameplay", "split")     # the layouts that need the webcam to be found first
SPLIT_TOP = 720        # how tall the webcam part of the split layout is; the game takes the 1200 below it
# How the split layout can share the frame between the webcam (top) and the game (below), as the webcam's height.
SPLITS = {"balanced": 720, "half": 960, "small": 608, "big": 1312}      # small: webcam band 16:9; big: game band 16:9


def split_top(clip: dict) -> int:
    """How tall the webcam band is for this clip (the split chosen, or the usual one)."""
    v = clip.get("split")
    return SPLITS.get(v, SPLIT_TOP) if isinstance(v, str) else SPLIT_TOP


def _box(v) -> list:
    """A crop box [x, y, w, h] in the video's pixels, or None if it isn't one."""
    if isinstance(v, (list, tuple)) and len(v) == 4 and all(isinstance(n, (int, float)) for n in v) and v[2] > 8 and v[3] > 8:
        return [int(n) for n in v]
    return None


def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def geometry(src_w, src_h, layout, top_h=SPLIT_TOP):
    """Where the video sits in the 1080x1920 frame, and where hook and captions go."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout == "blur":
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2
    if layout == "split":
        # the heading sits on the line between face and game, the captions low on the game
        return layout, fg_h, top_h, min(1640, top_h + int((H - top_h) * 0.63))
    if layout in ("crop", "gameplay"):
        return layout, fg_h, 300, 1380
    return layout, fg_h, max(230, top - 190), min(1640, top + fg_h + 180)


def _rounded(x0, y0, x1, y1, r):
    """A rounded rectangle as an ASS drawing."""
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    return (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
            f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
            f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
            f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")


def _hook_events(hook, dur, y):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    f, size = _Font.get(HOOK["font"]), HOOK["size"]
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > HOOK["max_w"] and len(words) > 1:      # two lines, split where they come out most even
        cut = min(range(1, len(words)), key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    x0, y0 = (W - bw) / 2, y - bh / 2
    box = _rounded(x0, y0, x0 + bw, y0 + bh, min(HOOK["radius"], bh / 2))
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def _mark_y(cap_y):
    """Where the @handle goes: just under the captions, or just above them when that would be too low (the bottom
    of the screen is covered by the app's buttons and the post's text)."""
    return cap_y + 128 if cap_y + 128 <= 1660 else cap_y - 140


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, watermark="") -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00{st.get('outline_colour', '000000')},&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: Mark,{MARK['font']},{MARK['size']},&H60FFFFFF,&H60FFFFFF,&H90000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = _hook_events(_clean(hook), dur, hook_y)
    mark = _clean(watermark)[:40]
    if mark:
        ev.append(f"Dialogue: 5,{_ts(0)},{_ts(dur)},Mark,,0,0,0,,{{\\an5\\pos({W // 2},{_mark_y(cap_y)})\\q2}}{mark}")
    font_main = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        deva = any(roman.DEVANAGARI.search(t) for t in texts)
        f = _Font.get(DEVA_FONT) if deva else font_main
        fn = f"\\fn{DEVA_FONT}" if deva else ""          # Hindi script: the style's look, in a font that has it
        if not deva and not all(roman.drawable(t) for t in texts):
            # A script the caption font cannot draw (Japanese, Thai, Chinese...): its words cannot be measured, so
            # they are not placed one by one. The line is drawn whole and centred, in a font the system has for it.
            line = " ".join(texts)
            fs = max(44, min(int(st["size"] * 0.7), int(1500 / max(1, len(line)) * 1.6)))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,{{\\an5\\pos({W // 2},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = int(st["size"] * 0.86) if deva else st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > CAP_MAX_W:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(36, int(size * CAP_MAX_W / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (W - total) / 2
        p = st["pop"]
        if st.get("band"):                                     # a soft dark band behind the whole line
            bw, bh = total + 0.7 * size, f.em(size) * 1.32
            path = _rounded((W - bw) / 2, cap_y - bh / 2, (W + bw) / 2, cap_y + bh / 2, min(28, bh / 3))
            ev.append(f"Dialogue: 0,{_ts(g_start)},{_ts(g_end)},Cap,,0,0,0,,{{\\an7\\pos(0,0)\\c&H{st['band']}&\\1a&H5A&\\bord0\\p1}}{path}{{\\p0}}")
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}{fn}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:                                                   # waiting to be said
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02 and st.get("box"):                                       # being said: on a bright box, in dark letters
                bw, bh = wd + 0.26 * size, f.em(size) * 1.08
                bx, by = x - gap - wd / 2, cap_y          # x has already moved past this word
                path = _rounded(bx - bw / 2, by - bh / 2, bx + bw / 2, by + bh / 2, min(22, bh / 3))
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{\\an7\\pos(0,0)\\c&H{st['box']}&\\bord0\\p1}}{path}{{\\p0}}")
                ev.append(f"Dialogue: 3,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\bord0}}{t}")
            elif b - a >= 0.02:                                                       # being said: colour and a quick pop, in front
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:                                                     # said
                done = st["active"] if st.get("keep") and not st.get("box") else base   # karaoke: it stays lit
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{done}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


PIP_W = 380            # how wide the facecam is in the 1080-wide frame
PIP_MARGIN = 26


ZOOM = 0.12           # how far a punch-in goes (12% closer)
ZOOM_MAX = 4          # at most this many in one clip


_LOUD: dict = {}


def clip_loudness(src, start, end):
    """How loud the clip is every 50 ms (a quarter-second average), read once per clip and kept for the next caller."""
    import numpy as np
    src = Path(src).resolve()
    key = (str(src), round(start, 2), round(end, 2))
    if key in _LOUD:
        return _LOUD[key]
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src), "-vn",
                            "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=90,
                           creationflags=NO_WINDOW)
        a = np.frombuffer(p.stdout[: len(p.stdout) // 2 * 2], np.int16).astype(np.float32)
    except (OSError, subprocess.TimeoutExpired):
        a = np.zeros(0, np.float32)
    n = len(a) // 400
    rms = np.convolve(np.sqrt((a[: n * 400].reshape(n, 400) ** 2).mean(axis=1)), np.ones(5) / 5, mode="same") if n else np.zeros(0)
    if len(_LOUD) > 64:
        _LOUD.clear()
    _LOUD[key] = rms
    return rms


def zoom_moments(src, start, end) -> list:
    """The loudest moments of a clip (a shout, a laugh, the big reaction), in seconds from its start: where the
    picture punches in. At most one every few seconds, none in the first moment or the last."""
    import numpy as np
    dur = end - start
    if dur < 6:
        return []
    step = 400                                          # 50 ms at 8 kHz
    rms = clip_loudness(src, start, end)
    if len(rms) < 40:
        return []
    floor = float(np.median(rms))
    if floor <= 1:
        return []                                       # silence: nothing to react to
    thr = max(floor * 1.8, float(np.percentile(rms, 90)))
    want = min(ZOOM_MAX, max(1, int(dur // 12)))
    picked = []
    for i in np.argsort(rms)[::-1]:
        t = i * step / 8000
        if rms[i] < thr or len(picked) >= want:
            break
        if t < 0.8 or t > dur - 2.0 or any(abs(t - q) < 6 for q in picked):
            continue
        if rms[i] < rms[max(0, i - 10): i + 11].max():   # not the top of its own half second
            continue
        picked.append(round(float(t), 2))
    return sorted(picked)


FILLER = re.compile(r"^(u+m+|u+h+|u+h+m+|h+m+|e+r+m+|e+r+|a+h+|m+h*m+)[.,!?…]*$", re.I)
SILENCE_MIN = 0.9          # a quiet stretch at least this long is shortened...
SILENCE_KEEP = 0.25        # ...to this
CUT_MIN = 0.25             # cuts shorter than this aren't worth a join
FADE = 0.03                # every join fades the sound out and in, so no click is heard


def tighten_plan(src, start, end, words) -> list:
    """The parts of a clip to keep, in seconds from its start: everything except stretches that are really quiet
    (by the sound, so a game's action with no talking is kept) and filler words (um, uh, hmm). [] when there is
    nothing worth cutting, or too little would be left."""
    import numpy as np
    dur = end - start
    rms = clip_loudness(src, start, end)
    cuts = []
    if len(rms) >= 20:
        quiet_at = max(1.0, 0.30 * float(np.median(rms)))
        quiet = rms < quiet_at
        i = 0
        while i < len(quiet):
            if quiet[i]:
                j = i
                while j < len(quiet) and quiet[j]:
                    j += 1
                a, b = i * 0.05, j * 0.05
                if b - a >= SILENCE_MIN and a > 0.3 and b < dur - 0.3:
                    cuts.append((a + SILENCE_KEEP / 2, b - SILENCE_KEEP / 2))
                i = j
            else:
                i += 1
    for w in words:
        s, e = w["s"] - start, w["e"] - start
        if 0.3 < s < dur - 0.3 and e - s < 1.2 and FILLER.match(re.sub(r"[^\w.,!?…]", "", w["w"])):
            cuts.append((s, e))
    cuts = sorted((max(0.0, a), min(dur, b)) for a, b in cuts if b - a >= CUT_MIN)
    merged = []
    for a, b in cuts:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    merged = merged[:25]
    if not merged:
        return []
    keep, at = [], 0.0
    for a, b in merged:
        if a - at > 0.05:
            keep.append((round(at, 3), round(a, 3)))
        at = b
    if dur - at > 0.05:
        keep.append((round(at, 3), round(dur, 3)))
    if sum(b - a for a, b in keep) < 0.6 * dur:
        return []                                       # that much quiet: better left as it is
    return keep


def _remap(t, keep):
    """A time in the clip as it was, in the clip as it is after the cuts (None when it was cut out)."""
    out = 0.0
    for a, b in keep:
        if t < a:
            return None if out else out
        if t <= b:
            return out + (t - a)
        out += b - a
    return None


def tightened_words(words, start, keep):
    """The words moved to where they are said after the cuts; words that were cut out are dropped."""
    out = []
    for w in words:
        mid = _remap((w["s"] + w["e"]) / 2 - start, keep)
        if mid is None:
            continue                                    # said inside a cut (a filler word): gone with it
        s, e = _remap(w["s"] - start, keep), _remap(w["e"] - start, keep)
        s = s if s is not None else mid
        e = e if e is not None and e > s else s + 0.2
        out.append(dict(w, s=round(start + s, 3), e=round(start + e, 3)))
    return out


def _tighten_filter(keep, layout_fc):
    """Cut the clip down to `keep` (video and sound together, the sound faded at every join), then the layout."""
    k = len(keep)
    vs = "".join(f"[v{i}]" for i in range(k))
    av = "".join(f"[a{i}]" for i in range(k))
    parts = [f"[0:v]split={k}{vs}" if k > 1 else "[0:v]null[v0]", f"[0:a:0]asplit={k}{av}" if k > 1 else "[0:a:0]anull[a0]"]
    for i, (a, b) in enumerate(keep):
        d = b - a
        parts.append(f"[v{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[w{i}]")
        parts.append(f"[a{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d={FADE},afade=t=out:st={max(0.0, d - FADE):.3f}:d={FADE}[s{i}]")
    pairs = "".join(f"[w{i}][s{i}]" for i in range(k))
    parts.append(f"{pairs}concat=n={k}:v=1:a=1[cv][ca]")
    return ";".join(parts) + ";" + layout_fc.replace("[0:v]", "[cv]")


def zoom_expr(moments) -> str:
    """zoompan's zoom for these moments: a quick push in, held, then eased back out."""
    if not moments:
        return ""
    parts = [f"{ZOOM}*clip((it-{t - 0.15:.2f})/0.18\\,0\\,1)*clip(({t + 1.6:.2f}-it)/0.4\\,0\\,1)" for t in moments]
    return "1+" + "+".join(parts)


def _zp(z, w, h):
    """Punch in on the middle of a picture that is w x h, following zoom_expr."""
    if not z:
        return ""
    return f",zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={config.OUT_FPS}"


def _filter(layout, fg_h, facecam=None, z="", game=None, top_h=SPLIT_TOP):
    # The part of the picture that is the game, when it was set by hand: cut out first, then placed as usual.
    g = _box(game)
    gcrop = f"crop={g[2]}:{g[3]}:{g[0]}:{g[1]}," if g else ""
    if layout == "split" and facecam:
        # The webcam is cut out and fills the top of the frame; the game, cropped around its middle, fills the rest.
        # The webcam is cut to the shape of its band first, so the face is never stretched.
        fx, fy, fw, fh = (int(v) for v in facecam)
        if fw > 8 and fh > 8:
            bottom = H - top_h
            return (f"[0:v]fps={config.OUT_FPS},split=2[g][c];"
                    f"[c]crop={fw}:{fh}:{fx}:{fy},scale={W}:{top_h}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{top_h}{_zp(z, W, top_h)},setsar=1[top];"
                    f"[g]{gcrop}scale={W}:{bottom}:force_original_aspect_ratio=increase,crop={W}:{bottom},setsar=1[bot];"
                    f"[top][bot]vstack=inputs=2[v0];")
    if layout == "gameplay" and facecam:
        # The game fills the screen, and the webcam sits in the top corner of it, so a viewer sees
        # the play and the face at the same time.
        fx, fy, fw, fh = (int(v) for v in facecam)
        if fw > 8 and fh > 8:
            pip_h = max(2, int(round(PIP_W * fh / fw)))
            return (f"[0:v]fps={config.OUT_FPS},split=2[base][fc];"
                    f"[base]{gcrop}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{_zp(z, W, H)},setsar=1[b];"
                    f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={PIP_W}:{pip_h},setsar=1[p];"
                    f"[b][p]overlay={W - PIP_W - PIP_MARGIN}:{PIP_MARGIN}[v0];")
    if layout == "crop" or (layout in FACECAM_LAYOUTS and not facecam):
        return f"[0:v]fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{_zp(z, W, H)},setsar=1[v0];"
    return (f"[0:v]fps={config.OUT_FPS},split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:(H-h)/2{_zp(z, W, H)},setsar=1[v0];")


def _hook_y(layout, hook_y, facecam=None):
    """Where the heading goes. In the gameplay layout the webcam sits in the top corner, so a heading
    across the top would run under it: it goes just below the picture instead."""
    if layout == "gameplay" and facecam and len(facecam) == 4 and facecam[2] > 8:
        return PIP_MARGIN + int(round(PIP_W * facecam[3] / facecam[2])) + 32
    return hook_y


# Graphics-card encoders, fastest first, each tried once: one that fails (no such card, an old driver) is not tried
# again until PapiClips restarts, and the processor (libx264) always remains. NVIDIA, AMD and Intel on Windows and
# Linux; Apple's on a Mac. Each is set to a quality close to libx264's crf 21.
HW_ENCODERS = [
    ("nvenc", ["yuv420p", "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]),
    ("amf", ["yuv420p", "-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"]),
    ("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]),
    ("videotoolbox", ["yuv420p", "-c:v", "h264_videotoolbox", "-q:v", "62"]),
]
_hw_ok: dict = {}            # encoder -> True (works) / False (failed once)
RENDER_SECONDS = 600      # one clip's video encode; far beyond normal, so a frozen encoder is stopped, not waited on


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = clip["start"], clip["end"]
    face = clip.get("facecam")
    want = clip.get("layout", "blur")
    if want in FACECAM_LAYOUTS and clip.get("camera") == "full":
        want = "crop"         # the camera fills the picture ("just chatting"): the clip is zoomed onto the streamer
    elif want in FACECAM_LAYOUTS and not (face and len(face) == 4):
        # No webcam was found: the whole picture is shown, over a blurred fill. Cropping it to fill the screen
        # would cut off most of the game, which is where the action is.
        want = "blur"
    top_h = split_top(clip)
    game = _box(clip.get("game"))
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, want, top_h)
    hook_y = _hook_y(layout, hook_y, face)
    keep = tighten_plan(src, start, end, words) if clip.get("tighten") else []
    shown = tightened_words(words, start, keep) if keep else words
    length = sum(b - a for a, b in keep) if keep else end - start
    ass = build_ass(shown if clip.get("captions", True) else [], start, start + length, clip.get("hook", ""), clip.get("style", "bounce"),
                    clip.get("highlight_words", []), hook_y, cap_y, watermark=clip.get("watermark", ""))
    moments = zoom_moments(src, start, end) if clip.get("zoom") else []
    if keep:
        moments = [m for m in (_remap(t, keep) for t in moments) if m is not None and 0.8 < m < length - 1.5]
    z = zoom_expr(moments)
    # The clip is made in a short work folder and moved into place afterwards. The caption step of ffmpeg cannot
    # open files whose full path is longer than 260 characters on Windows, which a data folder deep inside other
    # folders easily reaches. Inside the work folder every name is short and relative (a drive colon would also
    # break the filter syntax).
    work = Path(tempfile.mkdtemp(prefix="pk"))
    tag = None
    t = clip.get("tag")
    if isinstance(t, dict) and t.get("text"):
        # the streamer's handle: drawn as a picture, placed clear of the captions, the @handle and the heading
        from . import tag as tagmod
        try:
            im = tagmod.image(t["text"], t.get("platforms") or [], t.get("style", "clean"))
            if im.width > W - 80:
                im = im.resize((W - 80, max(1, int(im.height * (W - 80) / im.width))))
            im.save(work / "t.png")
            tag = tagmod.place(im.size, t.get("pos", "bottom_left"), cap_y, hook_y, bool(clip.get("watermark")))
        except Exception as e:  # noqa: BLE001 - the clip is still made, without the tag
            print(f"  Creator tag not drawn: {e}", flush=True)
            tag = None
    try:
        try:
            out = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, z=z, keep=keep,
                             game=game, top_h=top_h, tag=tag)
        except RuntimeError:
            if not z and not keep:
                raise
            # a zoom or cuts this ffmpeg can't make: the clip is made without them, never not at all
            ass = build_ass(words if clip.get("captions", True) else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                            clip.get("highlight_words", []), hook_y, cap_y, watermark=clip.get("watermark", ""))
            out = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, game=game, top_h=top_h, tag=tag)
            keep = []
        out["tightened"] = round((end - start) - sum(b - a for a, b in keep), 1) if keep else 0
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, face=None, z="", keep=None,
               game=None, top_h=SPLIT_TOP, tag=None) -> dict:
    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve()
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir(exist_ok=True)
    for f in config.FONTS_DIR.glob("*.ttf"):
        shutil.copyfile(f, work / "f" / f.name)
    fc = _filter(layout, fg_h, face, z, game, top_h) + f"[v0]ass=filename={name}.ass:fontsdir=f[v]"   # option names said: newer ffmpeg (Mac) refuses the short form
    extra = []
    if tag:             # the creator tag (t.png, written by render_clip) laid over everything
        fc = fc[:-3] + f"[va];[va][1:v]overlay={int(tag[0])}:{int(tag[1])}[v]"
        extra = ["-i", "t.png"]
    if keep:            # silences and filler words cut out first; the sound evened out in the same graph
        fc = _tighten_filter(keep, fc) + ";[ca]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        sound = ["-map", "[a]"]
    else:
        sound = ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    base = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src), *extra,
            "-filter_complex", fc, "-map", "[v]", *sound,
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", "-pix_fmt"]
    working = [e for e in HW_ENCODERS if _hw_ok.get(e[0]) is True]
    encoders = working[:1] or [e for e in HW_ENCODERS if _hw_ok.get(e[0]) is None]
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "veryfast"]))
    err = None
    for label, enc in encoders:
        try:
            p = subprocess.run(base + enc + [f"{name}.mp4"], cwd=work, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                               timeout=RENDER_SECONDS if label == "x264" else 180)
        except subprocess.TimeoutExpired:
            if label == "x264":
                raise RuntimeError("Making the clip took too long and was stopped.")
            _hw_ok[label] = False          # a graphics-card encoder that hangs is not used again
            err = f"{label} took too long"
            continue
        if p.returncode == 0:
            if label != "x264":
                _hw_ok[label] = True
            break
        err = p.stderr[-1200:]
        if label != "x264":
            _hw_ok[label] = False
    else:
        raise RuntimeError(f"Render failed: {err}")
    length = sum(b - a for a, b in keep) if keep else end - start
    cover_t = min(1.2, length / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        shutil.move(str(work / f"{name}.{ext}"), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
