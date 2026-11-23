"""The creator tag: the streamer's channel on the clip, as a platform logo and their handle ("▶ /Mizoplays").

Clippers are asked to credit the streamer, and a clip that shows whose stream it is gets trusted (and found) more.
The handle comes by itself from the video's channel (YouTube's @handle, the Twitch or Kick channel name); an editor
can type another, and pick which logos go with it: YouTube, Instagram, Kick, Twitch, or several.

It is drawn here as a picture (Pillow) and laid over the clip by ffmpeg. The logos are drawn from simple shapes, the
way each platform's mark looks, so no image files are needed. Four looks:

    clean   white letters with a black edge and a soft shadow, the logo beside them
    pill    in a white rounded box with a black edge
    dark    in a see-through dark rounded box
    brand   in a box in the platform's own colour
"""
from __future__ import annotations

import re
from pathlib import Path

FONTS = Path(__file__).parent / "fonts"

PLATFORMS = {"youtube": "YouTube", "instagram": "Instagram", "kick": "Kick", "twitch": "Twitch"}
STYLES = {"clean": "Clean", "pill": "White pill", "dark": "Dark pill", "brand": "Brand colour"}
POSITIONS = {"bottom_left": "Bottom left", "bottom_center": "Bottom centre", "top_left": "Top left"}
BRAND = {"youtube": (255, 0, 51), "instagram": (214, 41, 118), "kick": (83, 252, 24), "twitch": (145, 70, 255)}
FONT = {"clean": "LuckiestGuy-Regular.ttf", "pill": "LuckiestGuy-Regular.ttf", "dark": "Poppins-ExtraBold.ttf",
        "brand": "Poppins-ExtraBold.ttf"}
H = 84                     # how tall the tag is in the 1080x1920 clip
SS = 3                     # drawn this many times bigger, then made small: smooth edges


def clean_handle(v) -> str:
    """A channel handle as people write it: letters, digits, dots, underscores, dashes. No @ or / in front (added
    when it's drawn), nothing that isn't part of a handle, at most 30 characters."""
    s = str(v or "").strip()
    s = re.sub(r"^(https?://)?(www\.)?[\w.-]+\.(com|tv|gg)/", "", s, flags=re.I)        # a pasted channel link
    s = s.lstrip("@/ ").split("/")[0].split("?")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "", s)[:30]


def clean_platforms(v) -> list:
    return [p for p in dict.fromkeys(v if isinstance(v, (list, tuple)) else []) if p in PLATFORMS][:4]


def platform_of(url: str) -> str:
    """Which platform a video link is on, or ""."""
    host = re.sub(r"^https?://", "", str(url or "").lower()).split("/")[0]
    for key, names in (("youtube", ("youtube.com", "youtu.be")), ("twitch", ("twitch.tv",)), ("kick", ("kick.com",)),
                       ("instagram", ("instagram.com",))):
        if any(host == n or host.endswith("." + n) for n in names):
            return key
    return ""


def handle_from_info(i: dict, url: str = "") -> str:
    """The channel's handle from what yt-dlp says about a video: YouTube's @handle, else the channel's short name."""
    for k in ("uploader_id", "channel_id" if platform_of(url) == "kick" else "", "channel", "uploader"):
        v = str(i.get(k) or "") if k else ""
        if not v or (k == "uploader_id" and platform_of(url) == "youtube" and not v.startswith("@")):
            continue                                     # YouTube's old ids (UC...) aren't anyone's handle
        if k in ("channel", "uploader") and " " in v.strip() and platform_of(url) == "youtube":
            v = v.replace(" ", "")                       # a display name with spaces: the closest to a handle
        h = clean_handle(v)
        if h and not re.fullmatch(r"\d{5,}", h):         # a number is an id, not a handle
            return h
    return ""


def spec(job: dict, clip: dict):
    """What tag this clip gets, or None: {"text", "platforms", "style", "pos"}."""
    if not clip.get("tag"):
        return None
    handle = clean_handle(clip.get("tag_handle")) or clean_handle(job.get("handle")) or clean_handle(
        str(job.get("uploader") or "").replace(" ", ""))
    if not handle:
        return None
    plats = clean_platforms(clip.get("tag_platforms")) or ([job["platform"]] if job.get("platform") in PLATFORMS else [])
    prefix = "@" if plats == ["instagram"] or not plats else "/"
    return {"text": prefix + handle, "platforms": plats, "style": clip.get("tag_style") if clip.get("tag_style") in STYLES else "clean",
            "pos": clip.get("tag_pos") if clip.get("tag_pos") in POSITIONS else "bottom_left"}


# ---------------------------------------------------------------- drawing

def _logo(name: str, s: int):
    """One platform's mark, s pixels tall (YouTube's is wider), on a clear background."""
    from PIL import Image, ImageDraw
    if name == "youtube":
        w = int(s * 1.42)
        im = Image.new("RGBA", (w, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w - 1, s - 1), radius=int(s * 0.28), fill=BRAND["youtube"])
        cx, cy, r = w / 2 + s * 0.04, s / 2, s * 0.24
        d.polygon([(cx - r * 0.85, cy - r), (cx - r * 0.85, cy + r), (cx + r * 1.05, cy)], fill="white")
        return im
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if name == "instagram":
        # the gradient square: warm yellow at the bottom left, through orange and pink, to purple at the top right
        grad = Image.new("RGBA", (s, s))
        stops = [(254, 218, 117), (250, 126, 30), (214, 41, 118), (150, 47, 191), (79, 91, 213)]
        px = grad.load()
        for y in range(s):
            for x in range(s):
                t = min(0.999, max(0.0, (x + (s - y)) / (2 * s)))
                i, f = int(t * (len(stops) - 1)), t * (len(stops) - 1) % 1
                a, b = stops[i], stops[i + 1]
                px[x, y] = tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3)) + (255,)
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.28), fill=255)
        im.paste(grad, (0, 0), mask)
        lw = max(2, int(s * 0.075))
        d.rounded_rectangle((s * 0.2, s * 0.2, s * 0.8, s * 0.8), radius=int(s * 0.17), outline="white", width=lw)
        d.ellipse((s * 0.355, s * 0.355, s * 0.645, s * 0.645), outline="white", width=lw)
        d.ellipse((s * 0.645, s * 0.27, s * 0.725, s * 0.35), fill="white")
        return im
    if name == "kick":
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.18), fill=BRAND["kick"])
        u = s / 10                                     # Kick's K is drawn in blocks, like pixels
        blocks = [(2, 2, 4, 8), (4, 4, 5, 6), (5, 3, 6, 4.5), (5, 5.5, 6, 7), (6, 2, 8, 3.5), (6, 6.5, 8, 8)]
        for x0, y0, x1, y1 in blocks:
            d.rectangle((x0 * u, y0 * u, x1 * u, y1 * u), fill=(0, 0, 0))
        return im
    if name == "twitch":
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.2), fill=BRAND["twitch"])
        u = s / 10
        d.polygon([(2.4 * u, 1.8 * u), (8 * u, 1.8 * u), (8 * u, 5.8 * u), (6.4 * u, 7.3 * u), (5 * u, 7.3 * u),
                   (3.8 * u, 8.4 * u), (3.8 * u, 7.3 * u), (2.4 * u, 7.3 * u)], fill="white")
        d.rectangle((4.4 * u, 3.3 * u, 5.1 * u, 5.3 * u), fill=BRAND["twitch"])
        d.rectangle((6.1 * u, 3.3 * u, 6.8 * u, 5.3 * u), fill=BRAND["twitch"])
        return im
    return im


def image(text: str, platforms: list, style: str = "clean"):
    """The tag as a picture (RGBA), H pixels tall."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    style = style if style in STYLES else "clean"
    s = H * SS
    font = ImageFont.truetype(str(FONTS / FONT[style]), int(s * (0.66 if style in ("clean", "pill") else 0.5)))
    pad = int(s * 0.22) if style != "clean" else int(s * 0.08)
    logo_h = int(s * (0.78 if style == "clean" else 0.62))
    logos = [_logo(p, logo_h) for p in platforms]
    gap = int(s * 0.14)
    stroke = int(s * 0.07) if style == "clean" else 0
    box = font.getbbox(text, stroke_width=stroke)
    tw, th = box[2] - box[0], box[3] - box[1]
    width = pad * 2 + sum(lg.width for lg in logos) + gap * len(logos) + tw + stroke
    im = Image.new("RGBA", (width + s // 4, s + s // 4), (0, 0, 0, 0))      # room for the shadow
    d = ImageDraw.Draw(im)
    ink = (255, 255, 255)
    if style == "pill":
        d.rounded_rectangle((0, 0, width, s), radius=s // 2, fill="white", outline=(0, 0, 0), width=int(s * 0.06))
        ink = (17, 17, 17)
    elif style == "dark":
        d.rounded_rectangle((0, 0, width, s), radius=s // 2, fill=(0, 0, 0, 170))
    elif style == "brand":
        colour = BRAND.get(platforms[0] if platforms else "", (30, 30, 30))
        d.rounded_rectangle((0, 0, width, s), radius=s // 2, fill=colour)
        ink = (0, 0, 0) if platforms and platforms[0] == "kick" else (255, 255, 255)
    x = pad
    for lg in logos:
        if style == "brand":                           # on its own colour the logo gets a white back to stand out
            back = Image.new("RGBA", (lg.width + 8 * SS, lg.height + 8 * SS), (0, 0, 0, 0))
            ImageDraw.Draw(back).rounded_rectangle((0, 0, back.width - 1, back.height - 1), radius=int(back.height * 0.3), fill="white")
            im.alpha_composite(back, (x - 4 * SS, (s - back.height) // 2))
        im.alpha_composite(lg, (x, (s - lg.height) // 2))
        x += lg.width + gap
    ty = (s - th) // 2 - box[1]
    if style == "clean":                               # a soft shadow under white letters, as on the clips people know
        sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((x + SS * 3, ty + SS * 4), text, font=font, fill=(0, 0, 0, 170), stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
        im = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(SS * 3)), im)
        d = ImageDraw.Draw(im)
    d.text((x, ty), text, font=font, fill=ink, stroke_width=stroke, stroke_fill=(0, 0, 0))
    bbox = im.getbbox() or (0, 0, 1, 1)
    im = im.crop((0, 0, bbox[2], bbox[3]))
    return im.resize((max(1, im.width // SS), max(1, im.height // SS)), Image.LANCZOS)


def place(size: tuple, pos: str, cap_y: int, hook_y: int, has_mark: bool, w: int = 1080, h: int = 1920) -> tuple:
    """Where the tag's top left corner goes in the clip: clear of the captions, the @handle watermark and the
    heading, and of the edges the apps cover."""
    tw, th = size
    tw = min(tw, w - 80)
    if pos == "top_left":
        cy = hook_y - 140 if hook_y < 520 else 220
        return 40, max(110, int(cy - th / 2))
    cy = max(1660, cap_y + 130)
    if has_mark:
        cy = max(cy, min(1800, cap_y + 128 + 80) if cap_y + 128 <= 1660 else cy)
    cy = min(1790, cy)
    x = 40 if pos == "bottom_left" else (w - tw) // 2
    return x, int(cy - th / 2)
