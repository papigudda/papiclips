"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, roman
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hormozi": {"label": "Hormozi", "font": "Poppins ExtraBold", "size": 138, "base": "FFFFFF", "active": "00FF52",
                "strong": "00FF52", "outline": 8, "pop": 120, "upper": True, "per": 2},
    "beast": {"label": "MrBeast", "font": "Anton", "size": 156, "base": "FFFFFF", "active": "00F5FF",
              "strong": "FFD700", "outline": 8, "pop": 122, "upper": True, "per": 2},
    "neon_cyber": {"label": "Cyberpunk", "font": "Archivo Black", "size": 120, "base": "FFF000", "active": "7F00FF",
                   "strong": "7F00FF", "outline": 8, "pop": 115, "upper": True, "per": 3, "outline_colour": "1A000D"},
    "fire": {"label": "Fire", "font": "Bangers", "size": 158, "base": "FFFFFF", "active": "303BFF",
             "strong": "00A2FF", "outline": 9, "pop": 118, "upper": True, "per": 2},
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

EMOJI_KEYWORDS = {
    "car": "🏎️", "cars": "🏎️", "drive": "🏎️", "driving": "🏎️", "speed": "🏎️",
    "money": "💸", "cash": "💸", "rich": "💸", "dollar": "💵", "paid": "💰",
    "police": "🚨", "cop": "🚨", "cops": "🚨", "busted": "🚨", "jail": "🔒",
    "fire": "🔥", "crazy": "🔥", "insane": "🔥", "lit": "🔥", "hype": "🔥",
    "dead": "💀", "die": "💀", "died": "💀", "kill": "💀", "killed": "💀", "rip": "💀",
    "bro": "🗣️", "wait": "✋", "stop": "🛑", "no": "❌", "yes": "✅",
    "gun": "🔫", "shoot": "🔫", "fight": "🥊", "hit": "💥", "boom": "💥",
    "run": "🏃", "running": "🏃", "fast": "⚡", "win": "👑", "goat": "🐐",
    "laugh": "🤣", "lol": "🤣", "lmao": "🤣", "funny": "🤣", "roast": "💀",
    "shock": "😱", "omg": "😱", "what": "❓", "why": "❓",
}

def get_word_emoji(word: str) -> str:
    k = re.sub(r"[^a-zA-Z]", "", str(word or "")).lower()
    return EMOJI_KEYWORDS.get(k, "")
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf",
              "Bangers": "Bangers-Regular.ttf", "Luckiest Guy": "LuckiestGuy-Regular.ttf", "Bebas Neue": "BebasNeue-Regular.ttf",
              "Mukta ExtraBold": "Mukta-ExtraBold.ttf"}
CUSTOM_FONTS: dict = {}         # a family name -> its file, for fonts from a person's library (studio/library.py)
DEVA_FONT = "Mukta ExtraBold"      # Hindi in its own script (Devanagari): every style draws it in this font
MARK = {"font": "Poppins ExtraBold", "size": 46}      # the clipper's own @handle, small and see-through
CAP_MAX_W = 960   # captions never run wider than this, whatever is said


def font_names(path) -> dict:
    """A font file's own names, by name id (1 family, 2 style, 4 full name, 16 typographic family), as Windows reads
    them (English), else as a Mac does. {} when the file can't be read."""
    import struct
    try:
        d = Path(path).read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        off = tables[b"name"]
        _, count, so = struct.unpack(">HHH", d[off: off + 6])
        win, mac = {}, {}
        for i in range(count):
            pid, _eid, lid, nid, ln, o = struct.unpack(">HHHHHH", d[off + 6 + 12 * i: off + 18 + 12 * i])
            raw = d[off + so + o: off + so + o + ln]
            if pid == 3 and lid == 0x409:
                win[nid] = raw.decode("utf-16-be", "replace").strip()
            elif pid == 1 and lid == 0:
                mac.setdefault(nid, raw.decode("mac_roman", "replace").strip())
        return {**mac, **win}
    except Exception:  # noqa: BLE001 - not a font we can read
        return {}


def ass_family(path, fallback="") -> str:
    """The name to ask the caption renderer for, so it finds exactly this file."""
    n = font_names(path)
    return (n.get(4) or n.get(1) or fallback or "").strip()[:60]


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        own = CUSTOM_FONTS.get(name)
        path = Path(own) if own else Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
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
        own = CUSTOM_FONTS.get(name)
        if name not in cls._cache or (own and cls._cache[name].path != str(own)):
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


LIME, INK, WHITE = "32F4C6", "14100E", "FFFFFF"     # ASS colours (&HBBGGRR)


def _hook_events(hook, dur, y, fx="box", until=0.0, font=None):
    """The heading: by default dark text in a white rounded box that fits it, on one line or two balanced ones.
    fx changes how it looks and comes in (pop, lime, outline, glow, typewriter); until, when set, is how many
    seconds it stays (it fades out then)."""
    f, size = _Font.get(font or HOOK["font"]), HOOK["size"]
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
    end = min(dur, until) if until and until > 0 else dur
    span = f"{_ts(0)},{_ts(end)}"
    fade = "\\fad(160,220)" if end < dur else "\\fad(160,0)"
    ev = []
    boxed = fx in ("box", "pop", "lime", "type")
    if boxed:
        colour = LIME if fx == "lime" else HOOK["box"]
        if fx == "pop":
            # the box and its text grow in from small with a little overshoot, together, from the middle
            box = _rounded(0, 0, bw, bh, min(HOOK["radius"], bh / 2))
            ev.append(f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{y:.0f}){fade}\\c&H{colour}&\\fscx30\\fscy30"
                      f"\\t(0,170,\\fscx112\\fscy112)\\t(170,300,\\fscx100\\fscy100)\\p1}}{box}{{\\p0}}")
        else:
            x0, y0 = (W - bw) / 2, y - bh / 2
            box = _rounded(x0, y0, x0 + bw, y0 + bh, min(HOOK["radius"], bh / 2))
            ev.append(f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0){fade}\\c&H{colour}&\\p1}}{box}{{\\p0}}")
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        pos = f"\\an5\\pos({W // 2},{cy:.0f}){fade}\\fs{size}"
        if fx == "pop":
            # the line is scaled around the box's middle, so the text stays in its place inside it
            ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{{pos}\\org({W // 2},{y:.0f})\\fscx30\\fscy30"
                      f"\\t(0,170,\\fscx112\\fscy112)\\t(170,300,\\fscx100\\fscy100)}}{line}")
        elif fx == "type":
            # letter by letter, the way a typewriter writes it (a whole heading in about 0.6 s)
            n = max(1, len("".join(lines)))
            step = max(18, min(45, int(600 / n)))
            before = sum(len(x) for x in lines[:i])
            chars = "".join(f"{{\\alpha&HFF&\\t({(before + k) * step},{(before + k) * step + 1},\\alpha&H00&)}}{ch}"
                            for k, ch in enumerate(line))
            ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{{pos}}}{chars}")
        elif fx == "outline":
            ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{{pos}\\c&H{WHITE}&\\3c&H000000&\\bord9\\shad4\\4c&H000000&}}{line}")
        elif fx == "glow":
            ev.append(f"Dialogue: 3,{span},Hook,,0,0,0,,{{{pos}\\c&H{LIME}&\\3c&H{LIME}&\\bord16\\blur14\\1a&H40&}}{line}")
            ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{{pos}\\c&H{WHITE}&\\3c&H14100E&\\bord3}}{line}")
        else:
            ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{{pos}}}{line}")
    return ev


def _mark_y(cap_y):
    """Where the @handle goes: just under the captions, or just above them when that would be too low (the bottom
    of the screen is covered by the app's buttons and the post's text)."""
    return cap_y + 128 if cap_y + 128 <= 1660 else cap_y - 140


def parse_ass_phrases(ass_path: Path) -> list:
    """Parses .ass subtitle file and returns grouped timed phrases and words for live editor preview."""
    if not ass_path.is_file():
        return []
    lines = ass_path.read_text(encoding="utf-8").splitlines()
    def to_sec(t):
        p = t.split(":")
        return round(int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2]), 2)

    active_words = []
    for l in lines:
        if l.startswith("Dialogue:") and ",Cap," in l:
            p = l.split(",", 9)
            if len(p) == 10:
                layer_part = p[0].split(":")[1].strip()
                try:
                    layer = int(layer_part)
                except ValueError:
                    layer = 0
                if layer in (2, 3):
                    s = to_sec(p[1])
                    e = to_sec(p[2])
                    txt = re.sub(r"\{[^}]*\}", "", p[9]).strip()
                    if txt:
                        pos_m = re.search(r"\\pos\((\d+)", p[9])
                        x_pos = int(pos_m.group(1)) if pos_m else 0
                        active_words.append({"s": s, "e": e, "w": txt, "x": x_pos})

    dedup = []
    for w in active_words:
        if not dedup or dedup[-1]["w"] != w["w"] or abs(dedup[-1]["s"] - w["s"]) > 0.05:
            dedup.append(w)

    dedup.sort(key=lambda item: (item["s"], item["x"]))

    phrases = []
    cur_group = []
    for w in dedup:
        if not cur_group:
            cur_group.append(w)
        else:
            prev = cur_group[-1]
            if w["s"] <= prev["e"] + 0.45 and len(cur_group) < 3:
                cur_group.append(w)
            else:
                phrases.append({
                    "start": cur_group[0]["s"],
                    "end": cur_group[-1]["e"],
                    "words": [{"w": x["w"], "s": x["s"], "e": x["e"], "emoji": get_word_emoji(x["w"])} for x in cur_group]
                })
                cur_group = [w]
    if cur_group:
        phrases.append({
            "start": cur_group[0]["s"],
            "end": cur_group[-1]["e"],
            "words": [{"w": x["w"], "s": x["s"], "e": x["e"], "emoji": get_word_emoji(x["w"])} for x in cur_group]
        })
    return phrases


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, watermark="", hook_fx="box", hook_dur=0.0,
              hook_font=None, cap_font=None) -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    if cap_font:                      # the person's own font for the captions, in the style's colours and sizes
        st = dict(st, font=cap_font)
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
Style: Hook,{hook_font or HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: Mark,{MARK['font']},{MARK['size']},&H60FFFFFF,&H60FFFFFF,&H90000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = _hook_events(_clean(hook), dur, hook_y, hook_fx, hook_dur, hook_font)
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
RENDER_SECONDS = 3600     # one clip's encode, at the very most (a slow computer making a 1080p clip is waited for)
RENDER_STALL = 180        # ...but an encoder that stops making progress for this long is frozen: stopped


class _Done:
    def __init__(self, code, err):
        self.returncode, self.stderr = code, err


def _encode(cmd, cwd, stall=RENDER_STALL, cap=RENDER_SECONDS, progress=None):
    """Run ffmpeg, reading its progress: stopped only when it stops moving for `stall` seconds (or after `cap`), not
    after a fixed time, so a slow computer finishes its clips and a frozen encoder doesn't hold one for ever."""
    import threading as _th
    p = subprocess.Popen(cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    seen = {"at": time.time(), "t": 0.0}
    err = []

    def read_out():
        for line in p.stdout:
            seen["at"] = time.time()          # every progress line is ffmpeg still moving
            if line.startswith(("out_time_us=", "out_time_ms=")):     # both are in millionths of a second
                try:
                    seen["t"] = max(seen["t"], int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass

    def read_err():
        for line in p.stderr:
            err.append(line)
            del err[:-60]

    ts = [_th.Thread(target=read_out, daemon=True), _th.Thread(target=read_err, daemon=True)]
    for t in ts:
        t.start()
    began, told = time.time(), -1.0
    while p.poll() is None:
        if progress and seen["t"] != told:     # how far it is, told here (not from the reading thread)
            told = seen["t"]
            try:
                progress(told)
            except Exception:  # noqa: BLE001 - showing progress never stops a clip
                pass
        now = time.time()
        if now - seen["at"] > stall or now - began > cap:
            p.kill()
            p.wait()
            raise subprocess.TimeoutExpired(cmd, stall)
        time.sleep(0.5)
    for t in ts:
        t.join(2)
    return _Done(p.returncode, "".join(err))


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int, progress=None) -> dict:
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
    if clip.get("hook_y") not in (None, ""):
        try:
            hook_y = int(round(H * float(clip["hook_y"]) / 100))
        except (ValueError, TypeError):
            pass
    if clip.get("caption_y") not in (None, ""):
        try:
            cap_y = int(round(H * float(clip["caption_y"]) / 100))
        except (ValueError, TypeError):
            pass
    from . import edit as editmod
    ed = clip.get("edit") or {}                     # what was done in the editor: cuts, effects, sounds, text
    hook_y, cap_y = ed.get("hook_y") or hook_y, ed.get("cap_y") or cap_y
    hook_fx, hook_dur = ed.get("hook_fx", "box"), ed.get("hook_dur", 0)
    lib = clip.get("lib") or {}                     # the person's own fonts, sounds and media this edit uses
    fonts, hook_font, cap_font = [], None, None
    for key in ("hook_font", "cap_font"):
        it = lib.get(ed.get(key) or "")
        if it and it.get("kind") == "font" and it.get("path"):
            fam = ass_family(it["path"], it.get("family", ""))      # read from the file: right for fonts added before too
            if not fam:
                continue
            CUSTOM_FONTS[fam] = it["path"]
            fonts.append(it["path"])
            if key == "hook_font":
                hook_font = fam
            else:
                cap_font = fam
    keep = tighten_plan(src, start, end, words) if clip.get("tighten") else []
    if ed.get("cuts"):                              # the parts cut out in the editor, together with the silences cut
        keep = editmod.intersect(keep, editmod.keep_from_cuts(ed["cuts"], start, end)) or [(0.0, round(end - start, 3))]
        if keep == [(0.0, round(end - start, 3))]:
            keep = []
    shown = tightened_words(words, start, keep) if keep else words
    length = sum(b - a for a, b in keep) if keep else end - start
    ass = build_ass(shown if clip.get("captions", True) else [], start, start + length, clip.get("hook", ""), clip.get("style", "bounce"),
                    clip.get("highlight_words", []), hook_y, cap_y, watermark=clip.get("watermark", ""),
                    hook_fx=hook_fx, hook_dur=hook_dur, hook_font=hook_font, cap_font=cap_font)
    moments = zoom_moments(src, start, end) if clip.get("zoom") else []
    if keep:
        moments = [m for m in (_remap(t, keep) for t in moments) if m is not None and 0.8 < m < length - 1.5]
    z = zoom_expr(moments)
    post = editmod.post_graph(ed, start, keep, length, W, H, config.OUT_FPS)
    sfx = editmod.sounds_for(ed, start, keep, length, lib)
    over = editmod.overlays_for(ed, start, keep, length, lib)
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
            if clip.get("tag_x") not in (None, "") and clip.get("tag_y") not in (None, ""):
                try:
                    tag = (int(round(W * float(clip["tag_x"]) / 100 - im.width / 2)),
                           int(round(H * float(clip["tag_y"]) / 100 - im.height / 2)))
                except (ValueError, TypeError):
                    pass
        except Exception as e:  # noqa: BLE001 - the clip is still made, without the tag
            print(f"  Creator tag not drawn: {e}", flush=True)
            tag = None
    meta = {"title": clip.get("title") or "", "description": clip.get("description") or "",
            "comment": clip.get("description") or ""}
    try:
        out = None
        if post or sfx or over:
            try:
                out = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, z=z, keep=keep,
                                 game=game, top_h=top_h, tag=tag, post=post, sfx=sfx, over=over, fonts=fonts, progress=progress, voice=ed.get("voice"), meta=meta)
            except RuntimeError as e:
                # effects this ffmpeg can't make: the clip is still made, with the edit's cuts and text, without them
                print(f"  Effects not added to {name}: {str(e)[-300:]}", flush=True)
                out = None
        try:
            if out is None:
                out = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, z=z, keep=keep,
                                 game=game, top_h=top_h, tag=tag, fonts=fonts, progress=progress, voice=ed.get("voice"), meta=meta)
                if post or sfx or over:
                    out["fx_note"] = "The effects couldn't be added on this computer, so the clip was made without them."
        except RuntimeError as e:
            if not z and not keep:
                raise
            print(f"  Cuts and zoom not made in {name}: {str(e)[-300:]}", flush=True)
            # a zoom or cuts this ffmpeg can't make: the clip is made without them, never not at all
            ass = build_ass(words if clip.get("captions", True) else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                            clip.get("highlight_words", []), hook_y, cap_y, watermark=clip.get("watermark", ""),
                            hook_fx=hook_fx, hook_dur=hook_dur, hook_font=hook_font, cap_font=cap_font)
            out = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, game=game, top_h=top_h, tag=tag,
                             fonts=fonts, progress=progress, voice=ed.get("voice"), meta=meta)
            if ed.get("cuts") or post or sfx or over:      # say so: the edit didn't make it into the clip
                out["fx_note"] = "Your cuts and effects couldn't be made on this computer, so the clip was made without them."
            keep = []
        out["tightened"] = round((end - start) - sum(b - a for a, b in keep), 1) if keep else 0
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, face=None, z="", keep=None,
               game=None, top_h=SPLIT_TOP, tag=None, post="", sfx=None, over=None, fonts=None, progress=None, voice=None, meta=None) -> dict:
    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve()
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir(exist_ok=True)
    for f in config.FONTS_DIR.glob("*.ttf"):
        shutil.copyfile(f, work / "f" / f.name)
    for f in fonts or []:                # the person's own fonts, beside PapiClips's
        try:
            shutil.copyfile(f, work / "f" / Path(f).name)
        except OSError:
            pass
    from . import tools as _tools

    # Audio enhancement: vocal isolation, speech EQ, dynamic compression and broadcast loudness
    voice_af = "highpass=f=80,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=4:attack=5:release=50,loudnorm=I=-14:TP=-1.5:LRA=11"

    # Step 1: Render clean 1080x1920 video with layout, post FX, overlays, cuts, and mixed/enhanced audio (clean.mp4)
    pic = _filter(layout, fg_h, face, z, game, top_h)
    if post:             # the editor's look, punch-ins and transitions, on the laid-out picture, under the captions
        pic += post + ";"
    lab = "[vp]" if post else "[v0]"
    extra = []
    first_over = 1
    if over:             # pictures, GIFs and videos from the library, over the picture and under the text
        for i, o in enumerate(over):
            ow = max(2, int(round(W * o["w"] / 2)) * 2)
            if o["type"] == "image":
                extra += ["-loop", "1", "-t", f"{end - start + 1:.2f}", "-i", o["path"]]
                fr = f"[{first_over + i}:v]scale={ow}:-2,format=rgba[ov{i}]"
            else:
                extra += ["-ss", f"{o['from']:.2f}", "-i", o["path"]]
                fr = f"[{first_over + i}:v]setpts=PTS-STARTPTS+{o['at']:.3f}/TB,fps={config.OUT_FPS},scale={ow}:-2,format=rgba[ov{i}]"
            cx, cy = o["x"] * W, o["y"] * H
            nxt = f"[vo{i}]"
            pic += (f"{fr};{lab}[ov{i}]overlay=x='{cx:.0f}-w/2':y='{cy:.0f}-h/2':enable='between(t,{o['at']:.3f},{o['at'] + o['d']:.3f})'"
                    f":eof_action=pass{nxt};")
            lab = nxt
    fc_clean = pic + f"{lab}null[v]"
    if keep:
        fc_clean = _tighten_filter(keep, fc_clean)

    heard = [(i, o) for i, o in enumerate(over or []) if o.get("audio")]
    vol = f",volume={voice:.2f}" if voice is not None and abs(voice - 1) > 0.001 else ""     # the stream's own sound, as set in the editor
    if sfx or heard:    # sound effects, and the sound of videos laid over the clip, each at its moment, over the voice
        first = 1 + len(over or [])
        for f, _at, _vol in sfx or []:
            extra += ["-i", str(f)]
        mix = [f"{'[ca]' if keep else '[0:a:0]'}{voice_af}{vol}[an]"]
        for i, (_f, at, s_vol) in enumerate(sfx or []):
            ms = int(at * 1000)
            mix.append(f"[{first + i}:a]aresample=48000,adelay={ms}|{ms},volume={s_vol:.2f}[x{i}]")
        n = len(sfx or [])
        for j, (i, o) in enumerate(heard):
            ms = int(o["at"] * 1000)
            mix.append(f"[{1 + i}:a]atrim=0:{o['d']:.3f},asetpts=PTS-STARTPTS,aresample=48000,adelay={ms}|{ms},volume={o['vol']:.2f}[x{n + j}]")
        n += len(heard)
        mix.append("[an]" + "".join(f"[x{i}]" for i in range(n)) + f"amix=inputs={n + 1}:duration=first:normalize=0,alimiter=limit=0.95[a]")
        fc_clean += ";" + ";".join(mix)
        sound = ["-map", "[a]"]
    elif keep:
        fc_clean += f";[ca]{voice_af}{vol}[a]"
        sound = ["-map", "[a]"]
    else:
        sound = ["-map", "0:a:0", "-af", f"{voice_af}{vol}"]

    meta_flags = [arg for k, v in (meta or {}).items() if v for arg in ("-metadata", f"{k}={v}")]
    base_clean = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src), *extra,
                  "-filter_complex", fc_clean, "-map", "[v]", *sound,
                  "-c:a", "aac", "-b:a", "160k", "-ar", "48000", *meta_flags, "-movflags", "+faststart", "-pix_fmt"]

    working = [e for e in HW_ENCODERS if _hw_ok.get(e[0]) is True]
    encoders = working[:1] or [e for e in HW_ENCODERS if _hw_ok.get(e[0]) is None]
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "veryfast"]))

    clean_file = work / f"{name}.clean.mp4"
    final_file = work / f"{name}.mp4"
    total = max(0.5, sum(b - a for a, b in keep) if keep else end - start)

    # Execute Step 1: Render clean clip
    err = None
    for label, enc in encoders:
        try:
            cmd_clean = base_clean + enc + [str(clean_file)]
            p = _encode(cmd_clean, work, stall=RENDER_STALL if label == "x264" else 90,
                        progress=(lambda t: progress(min(0.75, 0.75 * t / total))) if progress else None)
        except subprocess.TimeoutExpired:
            if label == "x264":
                raise RuntimeError("Making the clean clip took too long and was stopped.")
            _hw_ok[label] = False
            err = f"{label} took too long"
            continue
        if p.returncode == 0 and clean_file.is_file() and clean_file.stat().st_size > 100000:
            if label != "x264":
                _hw_ok[label] = True
            break
        err = p.stderr[-1200:]
        if label != "x264":
            _hw_ok[label] = False
    else:
        raise RuntimeError(f"Clean render failed: {err}")

    # Step 2: Burn subtitles and creator tag onto clean clip to create final clip (name.mp4)
    # Uses -c:a copy so audio is 100% identical and encoding is fast
    tag_png = work / "t.png"
    can_cap = _tools.can_draw_captions()
    has_tag = bool(tag and tag_png.is_file())
    if has_tag:
        cap_f = f"ass=filename={name}.ass:fontsdir=f" if can_cap else "null"
        filter_burn = f"[0:v]{cap_f}[va];[va][1:v]overlay={int(tag[0])}:{int(tag[1])}[v]"
        base_burn = ["ffmpeg", "-y", "-v", "error", "-i", str(clean_file), "-i", "t.png",
                     "-filter_complex", filter_burn, "-map", "[v]", "-map", "0:a",
                     "-c:a", "copy", *meta_flags, "-movflags", "+faststart", "-pix_fmt"]
    elif can_cap:
        base_burn = ["ffmpeg", "-y", "-v", "error", "-i", str(clean_file),
                     "-vf", f"ass=filename={name}.ass:fontsdir=f", "-map", "0:v", "-map", "0:a",
                     "-c:a", "copy", *meta_flags, "-movflags", "+faststart", "-pix_fmt"]
    else:
        base_burn = None

    if base_burn is None:
        shutil.copyfile(clean_file, final_file)
    else:
        for label, enc in encoders:
            try:
                cmd_burn = base_burn + enc + [str(final_file)]
                p = _encode(cmd_burn, work, stall=RENDER_STALL if label == "x264" else 90,
                            progress=(lambda t: progress(min(0.99, 0.75 + 0.24 * t / total))) if progress else None)
            except subprocess.TimeoutExpired:
                if label == "x264":
                    raise RuntimeError("Burning subtitles took too long and was stopped.")
                continue
            if p.returncode == 0 and final_file.is_file() and final_file.stat().st_size > 100000:
                break
            err = p.stderr[-1200:]
        else:
            raise RuntimeError(f"Subtitle burn failed: {err}")

    length = sum(b - a for a, b in keep) if keep else end - start
    cover_t = min(1.2, length / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "clean.mp4", "jpg", "ass"):
        f = work / f"{name}.{ext}"
        if f.is_file():
            shutil.move(str(f), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
