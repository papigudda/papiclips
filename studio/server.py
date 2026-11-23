"""ClipKontrast web server. Run with:  python -m studio.server

Public pages:  /  /login  /signup          The app:  /app  (signed-in users only)
Everything under /api, /media and /oauth needs a session, except /api/auth/* and /api/public."""
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import sqlalchemy as sa
import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import admin as admins
from . import account, load
from . import (accounts, ai, alerts, auth, brain, config, creators, ctx, db, earn, jobs, live, media, posting,
               profiles, render, safe, transcribe, usage, whisper)

from . import tools as _tools  # noqa: E402
from . import tag as tagmod  # noqa: E402


def _quiet(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - said again, in plain words, when a video needs it
        print(f"  {e}", flush=True)


app = FastAPI(title="PapiClips", docs_url=None, redoc_url=None, openapi_url=None)
WEB = config.ROOT / "web"
OPEN = ("/api/auth/", "/pub/", "/download/", "/api/public", "/api/earn/public/", "/earn/cover/", "/auth/google/", "/auth/verify", "/brand/", "/assets/", "/healthz", "/favicon.ico", "/robots.txt", "/sitemap.xml")
# On an Earn-only website these are the only addresses people other than admins may change things through.
EARN_ONLY_WRITES = ("/api/auth/", "/api/earn/", "/api/account/delete", "/api/notifications/", "/api/feedback", "/api/public/bug-report")
PAGES = {"/": "landing.html", "/login": "auth.html", "/signup": "auth.html", "/reset": "reset.html",
         "/privacy": "legal.html", "/terms": "legal.html", "/acceptable-use": "legal.html", "/earn": "earn.html",
         "/clip-maker": "landing.html", "/download": "download.html", "/report": "report.html"}
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".flv"}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
       "frame-src https://player.twitch.tv https://www.youtube-nocookie.com https://player.kick.com https://drive.google.com; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://accounts.google.com")


BOOT = f"{int(time.time()) % 100000:x}"     # part of every sync token, so pages reload their data after a restart


@app.on_event("startup")
async def _more_threads():
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = int(config.env("CLIPKONTRAST_REQUEST_THREADS", "64"))
    # Windows: a browser tab closed mid-request makes asyncio print a long "WinError 10054" traceback into the log.
    # Nothing is wrong when it does; it's left out so the log (and bug reports) show what matters.
    import asyncio
    loop = asyncio.get_running_loop()

    def _quiet_resets(lp, c):
        if isinstance(c.get("exception"), ConnectionResetError):
            return
        lp.default_exception_handler(c)
    loop.set_exception_handler(_quiet_resets)


@app.on_event("startup")
def _startup():
    db.init()
    for f in (config.TMP_DIR / "uploads").glob("*"):      # uploads cut short by a restart
        try:
            f.unlink()
        except OSError:
            pass
    render.install_fonts()
    if _tools.missing():
        print("\n  " + _tools.HELP + "\n", flush=True)
    if not transcribe.REMOTE and config.env("CLIPKONTRAST_FETCH_MODEL", "1") != "0":     # the speech model: fetched now, in the background, if it was never downloaded
        from . import fetch_model
        threading.Thread(target=lambda: _quiet(fetch_model.ensure), daemon=True).start()
    jobs.start()
    live.start()
    posting.start()
    earn.start()                     # keeps YouTube view counts of campaign clips fresh
    from . import updates
    updates.start()                  # looks for a newer ClipKontrast every minute, in the background


# ================================================================ the gate: who is asking, and are they allowed
class Gate:
    """Runs before every request: finds the signed-in user, blocks what needs sign-in, blocks cross-site form
    posts, caps upload size and adds security headers."""

    def __init__(self, app):
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        req = Request(scope)
        path, method = scope["path"], scope["method"]

        async def deny(status, msg, redirect=None):
            no_keep = {"Cache-Control": "private, no-store"}      # never kept by a browser or CDN
            resp = RedirectResponse(redirect, 303, headers=no_keep) if redirect else JSONResponse({"detail": msg}, status, headers=no_keep)
            await resp(scope, receive, send)

        if method not in ("GET", "HEAD", "OPTIONS"):
            if not _from_here(req):
                return await deny(403, "Cross-site requests are not allowed.")
            # Only the upload may be big. Everything else is a small form, and is cut off at 256 KB whatever its
            # headers claim, so nobody can fill the server's memory by posting junk (signed in or not).
            piece = method == "PUT" and path.startswith("/api/upload/")
            cap = ((min(config.MAX_UPLOAD_MB, config.UPLOAD_PART_MB) if piece else config.MAX_UPLOAD_MB) + 8) * 1024 * 1024 \
                if path == "/api/upload" or piece else 256 * 1024
            if re.fullmatch(r"/api/((admin/)?earn/submissions/\d+/proof|admin/earn/campaigns/[\w\-]+/cover)", path):
                cap = 9 * 1024 * 1024          # a screenshot or a campaign cover
            if path in ("/api/public/bug-report", "/api/bug-report"):
                cap = 7 * 1024 * 1024          # a bug report, with its screenshot
            try:
                if int(req.headers.get("content-length") or 0) > cap:
                    return await deny(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB." if path == "/api/upload" else "That request is too large.")
            except ValueError:
                return await deny(400, "Bad request.")
            seen, inner_receive = 0, receive

            async def receive():
                nonlocal seen
                m = await inner_receive()
                if m["type"] == "http.request":
                    seen += len(m.get("body", b""))
                    if seen > cap:
                        raise HTTPException(413, "That request is too large.")
                return m

        is_open = path in PAGES or any(path.startswith(p) for p in OPEN)
        admin_area = path == "/admin" or path.startswith(("/admin/", "/api/admin/"))
        if config.ACCOUNTS:
            cookie = req.cookies.get(auth.COOKIE, "")
            user = auth._sess.get(auth._h(cookie)) if cookie and not admin_area else None      # remembered: no database, no thread hop
            user = user[1] if user and time.time() - user[0] < auth.SESS_FOR else (
                await run_in_threadpool(auth.user_for, cookie, admin_area) if cookie else None)
        else:
            # No sign-in: whoever opens the page is the owner. Because nothing else stands in the way, the page
            # must have been asked for by its real address (a number, "localhost", or the address in settings),
            # so another website cannot reach it through a made-up name that points at this machine.
            if not _own_address(req.headers.get("host", "")):
                return await deny(403, "Open ClipKontrast by its own address.")
            if path == "/" or path == "/reset" or path.startswith("/auth/"):
                return await deny(303, "", redirect="/app")
            if path.startswith("/api/auth/") and path != "/api/auth/me":
                return await deny(404, "Not Found")
            user = auth._local or await run_in_threadpool(auth.local_user)
        if admin_area:
            # The admin area does not exist for anyone else: same "not found" whether signed out or a normal user.
            if not user or not user.is_admin:
                return await deny(404, "Not Found")
            if method not in ("GET", "HEAD"):
                # changes must come from our own pages: the browser has to say so, and it has to match
                if not _from_here(req, must_say=True):
                    return await deny(403, "Cross-site requests are not allowed.")
        if not user and not is_open:
            if path == "/app" or path.startswith("/app/"):
                return await deny(401, "", redirect="/login")
            return await deny(401, "Please sign in.")
        if user and path in ("/login", "/signup"):
            return await deny(303, "", redirect="/app")
        # This lists what is allowed rather than what is refused, so a new clip-studio route stays closed here by default
        # (creators, settings and alerts would otherwise make this computer fetch channels or send messages for anyone).
        if (config.EARN_ONLY and user and not user.is_admin and method not in ("GET", "HEAD")
                and path.startswith("/api/") and not path.startswith(EARN_ONLY_WRITES)):
            return await deny(403, "Clip-making runs on your own computer here. Download the free ClipKontrast clip studio from the Earn page, make your clips, then send the links back to earn.")
        scope.setdefault("state", {})["user"] = user
        if user and path in ("/api/upload", "/api/upload/start") and method == "POST":
            # decided before a single byte of the file is accepted: verified, has a free slot, has disk space
            def allowed():
                with ctx.as_user(user.id):
                    _need_verified(); _job_slots(); _quota()                     # noqa: E702
            try:
                await run_in_threadpool(allowed)
            except HTTPException as e:
                return await deny(e.status_code, e.detail)
        if user and path.startswith("/api/") and not auth.seen_recently(user.id):
            await run_in_threadpool(auth.touch, user.id)

        unchanged = False

        async def send_with_headers(message):
            nonlocal unchanged
            if unchanged:                       # the browser's copy is current: send no body at all
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    await send({"type": "http.response.body", "body": b""})
                return
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                if method == "GET" and message.get("status") == 200:
                    tag = next((v.decode("latin-1") for k, v in h if k.lower() == b"etag"), "")
                    have = req.headers.get("if-none-match", "")
                    if tag and have and tag.removeprefix("W/") in [x.strip().removeprefix("W/") for x in have.split(",")]:
                        unchanged = True
                        message["status"] = 304
                        h[:] = [(k, v) for k, v in h if k.lower() not in (b"content-length", b"content-type", b"content-encoding")]
                h += [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                      (b"referrer-policy", b"strict-origin-when-cross-origin"), (b"content-security-policy", CSP.encode()),
                      (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
                if config.HTTPS:
                    h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if path.startswith("/api/"):
                    h.append((b"cache-control", b"no-store"))
                elif message.get("status", 200) >= 300 and not any(k.lower() == b"cache-control" for k, _ in h):
                    # redirects and errors are never kept by a CDN: a "not found" for a clip that is still being
                    # made, or a sign-in redirect, must not be shown to anyone later
                    h.append((b"cache-control", b"private, no-store"))
                if path.startswith(("/app", "/admin", "/api/", "/auth/", "/reset", "/media/")):
                    h.append((b"x-robots-tag", b"noindex, nofollow"))
            await send(message)

        token = ctx._uid.set(user.id if user else None)
        try:
            if user and path == "/api/sync" and method == "GET":
                # By far the most common request. When nothing of this user's changed it is answered right here:
                # no worker thread, no database, no disk. Anything else falls through to the normal route.
                quick = _sync_unchanged(req.query_params.get("v", ""))
                if quick is not None:
                    return await JSONResponse(quick)(scope, receive, send_with_headers)
            await self.inner(scope, receive, send_with_headers)
        finally:
            ctx._uid.reset(token)
            if method not in ("GET", "HEAD"):
                _EARN_PUBLIC.clear()        # something may have changed: the next visitor sees the Earn page fresh


app.add_middleware(Gate)


class Compress:
    """gzip for text responses. Videos and images are already compressed and need byte ranges, so they pass through."""
    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware
        self.plain, self.gzip = app, GZipMiddleware(app, minimum_size=800, compresslevel=6)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        media = path.startswith(("/media/", "/api/upload")) or path.endswith((".mp4", ".jpg", ".png", ".webp", ".ico"))
        await (self.plain if scope["type"] != "http" or media else self.gzip)(scope, receive, send)


app.add_middleware(Compress)


# Cloudflare's published address ranges (cloudflare.com/ips). CLIPKONTRAST_CDN_RANGES replaces them if they ever change.
_CDN_RANGES = None


def _from_cdn(addr: str) -> bool:
    global _CDN_RANGES
    import ipaddress
    if _CDN_RANGES is None:
        ranges = config.env("CLIPKONTRAST_CDN_RANGES") or (
            "173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 "
            "190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 "
            "104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 "
            "2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32")
        _CDN_RANGES = [ipaddress.ip_network(r) for r in ranges.replace(",", " ").split()]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in n for n in _CDN_RANGES)


def _ip(request: Request) -> str:
    """The visitor's address, used for rate limits. The connection's own address, or the one the proxy in front
    reports (CLIPKONTRAST_TRUST_PROXY). If that connection comes from the CDN, the CDN's header names the real visitor
    (CLIPKONTRAST_CLIENT_IP_HEADER). The header is believed only when the CDN really sent the request, so nobody can
    pick their own address by sending it themselves.

    The proxy adds the address it saw at the END of X-Forwarded-For. Everything before that was written by the
    visitor, so only the last entry is believed."""
    fwd = request.headers.get("x-forwarded-for", "")
    peer = (fwd.split(",")[-1].strip() if fwd and (config.env("PAPICLIPS_TRUST_PROXY") or config.env("CLIPKONTRAST_TRUST_PROXY")) else "") or (request.client.host if request.client else "")
    named = (config.env("PAPICLIPS_CLIENT_IP_HEADER") or config.env("CLIPKONTRAST_CLIENT_IP_HEADER"))
    # Behind a Cloudflare Tunnel, every request arrives from cloudflared on this same machine, so the header is
    # believed when the connection comes from this machine itself and CLIPKONTRAST_TUNNEL=1 says a tunnel is in use.
    local = config.env("CLIPKONTRAST_TUNNEL") == "1" and peer in ("127.0.0.1", "::1")
    if named and request.headers.get(named) and (local or _from_cdn(peer)):
        return request.headers[named].split(",")[0].strip()[:64]
    return peer


def _from_here(req, must_say=False) -> bool:
    """Did this change come from ClipKontrast's own page, and not from another website?

    The browser's own answer is trusted first: it marks every request "same-origin" or not, and a web page cannot
    fake that mark. Comparing the page's address with the address the request arrived at is only the fallback,
    because that comparison goes wrong when something in between rewrites either of them (a proxy, an antivirus
    web shield, a browser extension), which made every save fail with "Cross-site requests are not allowed"."""
    if (req.headers.get("sec-fetch-site") or "").lower() == "same-origin":
        return True
    origin = (req.headers.get("origin") or "").rstrip("/").lower()
    if not origin:
        return not must_say
    return urlparse(origin).netloc == (req.headers.get("host") or "").lower() or origin == config.PUBLIC_URL.lower()


def _own_address(host: str) -> bool:
    """Is this the address ClipKontrast is really reached by? A number (127.0.0.1, 192.168.1.20), "localhost", or the
    host in CLIPKONTRAST_PUBLIC_URL."""
    import ipaddress
    name = host.strip().lower()
    name = name[1:name.index("]")] if name.startswith("[") and "]" in name else name.rsplit(":", 1)[0] if ":" in name else name
    if name in ("localhost", urlparse(config.PUBLIC_URL).hostname or "") or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _me(request: Request):
    return request.state.user


def _admin(request: Request):
    """The signed-in admin, read from the database for this very request. Anyone else gets "not found"."""
    u = getattr(request.state, "user", None)
    if not u or not u.is_admin or u.disabled:
        raise HTTPException(404)
    return u


def _need_verified():
    if not auth.is_verified(ctx.uid()):
        raise HTTPException(403, "Confirm your email first. We sent you a link when you signed up; you can send a new one from the banner at the top.")


# ================================================================ pages
def _page(name, shared=False):
    """A page. The browser keeps its copy and asks each time whether it has changed; when it has not, the answer
    is a few bytes ("not modified") instead of the whole page again. Signed-in pages are never kept by anything
    in between (a proxy or CDN), only by the person's own browser."""
    return FileResponse(WEB / name, headers={"Cache-Control": ("public" if shared else "private") + ", no-cache"})


_LANDING: dict = {}


@app.get("/")
def home():
    # The Earn-only website opens on Earn (campaigns, sign-up, the download); the clip maker's page is at /clip-maker
    if config.EARN_ONLY:
        return _page("earn.html", shared=True)
    return landing()


@app.get("/clip-maker")
def landing():
    f = WEB / "landing.html"
    m = f.stat().st_mtime
    if _LANDING.get("m") != m:
        html = f.read_text(encoding="utf-8").replace("{{SITE}}", config.PUBLIC_URL).replace("{{DOWNLOAD}}", config.DOWNLOAD_URL)
        _LANDING.update(m=m, html=html, etag='"' + hashlib.md5(html.encode()).hexdigest() + '"')
    # fresh for five minutes; after that a CDN or browser may show its copy while it quietly fetches the new one
    return HTMLResponse(_LANDING["html"], headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=86400",
                                                   "ETag": _LANDING["etag"]})


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nDisallow: /app\nDisallow: /api/\nDisallow: /auth/\nDisallow: /reset\nDisallow: /media/\n"
                    f"\nSitemap: {config.PUBLIC_URL}/sitemap.xml\n", media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sitemap.xml")
def sitemap():
    day = time.strftime("%Y-%m-%d")
    pages = [("/", "1.0", "weekly"), ("/earn", "0.9", "daily"), ("/clip-maker", "0.8", "monthly"), ("/signup", "0.8", "monthly"), ("/login", "0.4", "monthly"), ("/terms", "0.3", "yearly"),
             ("/privacy", "0.3", "yearly"), ("/acceptable-use", "0.2", "yearly")]
    body = "".join(f"<url><loc>{config.PUBLIC_URL}{u}</loc><lastmod>{day}</lastmod><changefreq>{c}</changefreq>"
                   f"<priority>{p}</priority></url>" for u, p, c in pages)
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + body + "</urlset>", media_type="application/xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/login")
@app.get("/signup")
def auth_page():
    return _page("auth.html", shared=True)


@app.get("/reset")
def reset_page():
    return _page("reset.html", shared=True)


LEGAL = {
    "privacy": ("Privacy Policy", "How ClipKontrast collects, uses and protects your data, including Google user data, cookies and your privacy rights."),
    "terms": ("Terms of Service", "The terms for using ClipKontrast, the AI clipping tool for streams and videos: accounts, availability and liability."),
    "acceptable-use": ("Acceptable Use Policy", "What you may and may not do with ClipKontrast, the AI stream clipping tool."),
}


@app.get("/privacy")
@app.get("/terms")
@app.get("/acceptable-use")
def legal_page(request: Request):
    """Public, so Google's app review (and anyone) can read them. Business name, law and contact come from settings."""
    import html as _h
    page = request.url.path.strip("/")
    title, desc = LEGAL[page]
    contact = config.CONTACT_EMAIL
    who = (f'<a href="mailto:{_h.escape(contact)}">{_h.escape(contact)}</a>' if contact
           else "the contact details on our website")
    name = _h.escape(config.LEGAL_NAME or "ClipKontrast")
    law = (f"the laws of {_h.escape(config.LEGAL_COUNTRY)}" if config.LEGAL_COUNTRY
           else "the laws of the country where ClipKontrast's operator is established")
    out = (_LEGAL_HTML or (WEB / "legal.html").read_text(encoding="utf-8"))
    for k, v in {"{{CONTACT}}": who, "{{PAGE}}": page, "{{PAGE_PATH}}": page, "{{SITE}}": _h.escape(config.PUBLIC_URL),
                 "{{NAME}}": name, "{{LAW}}": law, "{{TITLE}}": title, "{{DESC}}": desc}.items():
        out = out.replace(k, v)
    return HTMLResponse(out, headers={"Cache-Control": "public, max-age=3600"})


_LEGAL_HTML = ""


@app.get("/app")
@app.get("/app/{rest:path}")
def app_page(request: Request, rest: str = ""):
    """Every app address (/app/videos, /app/creators/<id>/setup ...) is the same page; it shows the right view.
    On an Earn-only website the clip studio is the free download, so everyone but admins goes to Earn."""
    u = getattr(request.state, "user", None)
    if config.EARN_ONLY and not (u and u.is_admin):
        return RedirectResponse("/earn#me", 303)
    return _page("index.html")


@app.get("/admin/earn")
@app.get("/admin/earn/{rest:path}")
def admin_earn_page(request: Request, rest: str = ""):
    _admin(request)
    return _page("earn-admin.html")


@app.get("/report")
def report_page():
    return _page("report.html", shared=True)


@app.get("/download")
def download_page():
    return _page("download.html", shared=True)


@app.get("/download/{system}")
def download_app(system: str):
    """The app for Windows, Mac or Linux: the browser is sent straight to the file of the newest version. Only these
    three names are known, so this can't be used to send anyone anywhere else."""
    name = config.APP_FILES.get(system)
    if not name:
        raise HTTPException(404, "Not found.")
    return RedirectResponse(config.RELEASES_URL + name, 302, headers={"Cache-Control": "no-store"})


_APP_VERSION: dict = {"at": 0.0, "v": None}


@app.get("/api/public/app-version")
def app_version():
    """The newest app's version and the SHA-256 of the Windows installer, for the download page. Read from the
    published latest.json at most once an hour, never per visitor."""
    if time.time() - _APP_VERSION["at"] > 3600:
        _APP_VERSION["at"] = time.time()
        try:
            import httpx
            r = httpx.get(config.RELEASES_URL + "latest.json", timeout=8, follow_redirects=True)
            m = json.loads(r.content.decode("utf-8-sig")) if r.status_code == 200 else {}
            v, sha = str(m.get("version", ""))[:40], str(m.get("installer_sha256", ""))
            if re.fullmatch(r"[\d.]+", v) and re.fullmatch(r"[0-9a-f]{64}", sha):
                _APP_VERSION["v"] = {"version": v, "windows_sha256": sha}
        except Exception:  # noqa: BLE001 - the page simply leaves the version out
            pass
    return _APP_VERSION["v"] or {}


@app.get("/earn")
def earn_page():
    # The downloaded app has no shared database, so its Earn button goes to the public Earn website.
    if not config.ACCOUNTS and config.env("CLIPKONTRAST_LOCAL_EARN") != "1":
        return RedirectResponse(config.EARN_URL + "/earn", 303)
    return _page("earn.html", shared=True)


@app.get("/admin")
@app.get("/admin/{rest:path}")
def admin_page(request: Request, rest: str = ""):
    _admin(request)
    return _page("admin.html")


@app.get("/healthz")
def healthz():
    with db.engine.connect() as c:
        c.execute(sa.text("select 1"))
    return {"ok": True}


@app.get("/brand/{name}")
def brand(name: str):
    if not re.fullmatch(r"[\w\-]+\.(svg|png|ico|webp|jpg|mp4)", name) or not (WEB / "brand" / name).is_file():
        raise HTTPException(404)
    return FileResponse(WEB / "brand" / name, headers={"Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"})


@app.get("/brand/styles/{name}")
def style_preview(name: str):
    """The moving preview of each caption style (made by packaging/make_style_previews.py)."""
    if not re.fullmatch(r"[a-z]+\.(webm|jpg)", name) or not (WEB / "brand" / "styles" / name).is_file():
        raise HTTPException(404)
    return FileResponse(WEB / "brand" / "styles" / name, media_type="video/webm" if name.endswith(".webm") else "image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"})


_TAG_PREVIEWS: dict = {}


@app.get("/api/tag-preview")
def tag_preview(request: Request, text: str = "", platforms: str = "", style: str = "clean"):
    """The creator tag exactly as it will be drawn on a clip, as a PNG, for the style picker."""
    try:
        auth.throttle(f"tagprev:{_me(request).id}", 240, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many at once. Wait a moment.")
    handle = tagmod.clean_handle(text) or "streamer"
    plats = tagmod.clean_platforms([p for p in platforms.split(",") if p])
    style = style if style in tagmod.STYLES else "clean"
    key = (handle, tuple(plats), style)
    png = _TAG_PREVIEWS.get(key)
    if png is None:
        import io
        buf = io.BytesIO()
        tagmod.image(("@" if plats == ["instagram"] or not plats else "/") + handle, plats, style).save(buf, "PNG")
        png = buf.getvalue()
        if len(_TAG_PREVIEWS) > 300:
            _TAG_PREVIEWS.pop(next(iter(_TAG_PREVIEWS)))
        _TAG_PREVIEWS[key] = png
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/assets/{name}")
def asset(name: str):
    if not re.fullmatch(r"[\w\-]+\.js", name) or not (WEB / name).is_file():
        raise HTTPException(404)
    return FileResponse(WEB / name, media_type="text/javascript", headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"})


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/brand/favicon.svg", 308)


# ================================================================ sign up, sign in, sign out
@app.get("/api/public")
def public_config():
    from . import mailer
    return {"google": auth.google_ready(), "mail": mailer.ready(), "verify_email": config.VERIFY_EMAIL,
            "earn_only": config.EARN_ONLY, "download_url": config.DOWNLOAD_URL, "guide_url": config.GUIDE_URL}


def _signed_in(request: Request, user, first: bool, redirect=None):
    token = auth.start_session(user.id, _ip(request), request.headers.get("user-agent", ""))
    resp = RedirectResponse(redirect, 303) if redirect else JSONResponse({"user": auth.public(user)})
    resp.set_cookie(**auth.cookie_args(token))
    return resp


@app.post("/api/auth/signup")
def signup(request: Request, body: dict = Body(...)):
    try:
        # per address, generous: mobile networks in India put thousands of people behind one address (CGNAT)
        auth.throttle("signup:" + _ip(request), 60, 3600)
        user, first = auth.signup(str(body.get("email", "")), str(body.get("password", "")), str(body.get("name", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    if not auth.is_verified(user.id):
        auth.send_verify(user)
    return _signed_in(request, user, first)


@app.post("/api/auth/resend-verification")
def resend_verification(request: Request):
    u = _me(request)
    if not u:
        raise HTTPException(401, "Please sign in.")
    if auth.is_verified(u.id):
        return {"verified": True}
    try:
        auth.throttle("verify:" + u.id, 3, 900)
    except auth.AuthError:
        raise HTTPException(429, "We've sent a few already. Check your spam folder, or try again in 15 minutes.")
    from . import mailer
    return {"sent": auth.send_verify(u), "mail_ready": mailer.ready()}


@app.post("/api/auth/verify-code")
def verify_code(request: Request, body: dict = Body(...)):
    u = _me(request)
    if not u:
        raise HTTPException(401, "Please sign in.")
    if auth.is_verified(u.id):
        return {"verified": True}
    try:
        auth.verify_code(u.id, str(body.get("code", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    ctx.bump(u.id)
    return {"verified": True}


@app.get("/auth/verify")
def verify_email(request: Request, t: str = ""):
    try:
        auth.verify_link(t)
    except auth.AuthError as e:
        return RedirectResponse("/app?verify_error=" + quote(str(e)), status_code=303)
    return RedirectResponse("/app?verified=1", status_code=303)


@app.post("/api/auth/forgot")
def forgot_password(request: Request, body: dict = Body(...)):
    email = str(body.get("email", "")).strip().lower()
    try:
        auth.throttle("forgot-ip:" + _ip(request), 40, 3600)
        auth.throttle("forgot:" + email, 3, 3600)
    except auth.AuthError as e:
        raise HTTPException(429, str(e))
    auth.forgot(email)
    return {"ok": True}   # the same answer whether or not the account exists


@app.post("/api/auth/reset")
def reset_password(request: Request, body: dict = Body(...)):
    try:
        auth.throttle("reset-ip:" + _ip(request), 60, 3600)
        user = auth.reset_password(str(body.get("token", "")), str(body.get("password", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return _signed_in(request, user, False)


@app.post("/api/auth/login")
def login(request: Request, body: dict = Body(...)):
    email = str(body.get("email", ""))
    try:
        auth.throttle("login-ip:" + _ip(request), 150, 600)   # shared mobile addresses; each account has its own limit below
        auth.throttle(f"login:{email.strip().lower()[:254]}:{_ip(request)}", 8, 600)
        auth.throttle("login:" + email.strip().lower()[:254], 60, 600)
        user = auth.login(email, str(body.get("password", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return _signed_in(request, user, False)


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me(request: Request):
    return {"user": auth.public(_me(request)) if _me(request) else None}


@app.post("/api/auth/password")
def change_password(request: Request, body: dict = Body(...)):
    if not _me(request):
        raise HTTPException(401, "Please sign in.")
    try:
        auth.throttle("pw:" + _me(request).id, 6, 600)
        auth.change_password(_me(request).id, str(body.get("current", "")), str(body.get("new", "")),
                             keep_token=request.cookies.get(auth.COOKIE, ""))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/auth/google/start")
def google_start(next: str = "/app"):
    if not auth.google_ready():
        raise HTTPException(404)
    url, state = auth.google_url(next)
    resp = RedirectResponse(url)
    # the sign-in can only be finished by the browser that started it
    resp.set_cookie("g_state", state, max_age=900, httponly=True, samesite="lax", secure=config.HTTPS, path="/auth/google")
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or not state or not hmac.compare_digest(request.cookies.get("g_state", ""), state):
        return RedirectResponse("/login?error=google", 303)
    try:
        auth.throttle("google:" + _ip(request), 150, 600)
        user, first, nxt = auth.google_finish(code, state)
    except auth.AuthError:
        return RedirectResponse("/login?error=google", 303)
    return _signed_in(request, user, first, redirect=nxt)


# ================================================================ app status
@app.get("/api/status")
def status(request: Request):
    from . import notices
    from . import updates
    me_ = _me(request)
    return {"user": auth.public(me_), "update": updates.latest() if me_.is_admin else None, "accounts": config.ACCOUNTS, "disk_full": load.disk_full(), "no_ffmpeg": bool(_tools.missing()), "links": {"repo": config.REPO_URL, "follow": config.FOLLOW_URL, "earn": ("/earn" if config.ACCOUNTS else config.EARN_URL + "/earn")}, "ai": usage.brief(), "unread": notices.unread(),
            "tour": not accounts.settings()["tour_done"],
            "earn_url": "/earn" if config.ACCOUNTS else config.EARN_URL + "/earn", "earn_only": config.EARN_ONLY,
            "youtube": accounts.youtube_ready(), "connect": {p: accounts.can_connect(p) for p in accounts.PLATFORM_NAMES}, "max_upload_mb": config.MAX_UPLOAD_MB, "upload_part_mb": config.UPLOAD_PART_MB, "max_batch": config.MAX_ACTIVE_JOBS_PER_USER,
            "styles": {k: v["label"] for k, v in render.STYLES.items()}, "layouts": render.LAYOUTS,
            "tag": {"styles": tagmod.STYLES, "platforms": tagmod.PLATFORMS, "positions": tagmod.POSITIONS},
            "languages": whisper.LANGUAGES, "caption_langs": whisper.CAPTION_LANGS}


# ================================================================ videos
def _job_or_404(jid: str):
    job = jobs.get(jid) if re.fullmatch(r"[\w\-]{8,60}", jid) else None
    if not job:
        raise HTTPException(404, "No such video")
    return job


def _job_slots():
    from . import tools
    if tools.missing():
        raise HTTPException(503, tools.HELP)
    if jobs.busy_count() >= config.MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"You already have {config.MAX_ACTIVE_JOBS_PER_USER} videos in progress. Wait for one to finish.")


_create_lock = threading.Lock()      # "do I have a free slot?" and "take it" happen together, so bursts cannot slip past the limit
_lookups = threading.BoundedSemaphore(8)   # link and channel previews running at once, whole server
_disk: dict = {}


def _quota():
    """Each account has a fixed amount of disk space. Measured at most once a minute."""
    if load.disk_full():
        from . import storage
        raise HTTPException(507, storage.full_message())
    uid = ctx.uid()
    got = _disk.get(uid)
    if not got or time.time() - got[0] > 60:
        total = 0
        for root, _dirs, files in os.walk(config.user_dir(uid)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        got = _disk[uid] = (time.time(), total)
    if got[1] > config.MAX_USER_GB * 1e9:
        raise HTTPException(507, "Your storage is full. Delete some older videos to make room.")


def _throttle(key, limit, window, msg="Too many requests at once. Wait a moment."):
    try:
        auth.throttle(key, limit, window)
    except auth.AuthError:
        raise HTTPException(429, msg)


def _lookup(fn, *a):
    """Run a preview lookup, unless the server is already doing as many as it should."""
    if not _lookups.acquire(blocking=False):
        raise HTTPException(429, "Lots of people are looking things up right now. Try again in a few seconds.")
    try:
        return fn(*a)
    finally:
        _lookups.release()


def _creator_rows():
    out = []
    for c in creators.all_creators():
        if profiles.needs_refresh(c):
            profiles.refresh(c["id"])
        s = live.active_for(c["id"])
        out.append({**c, "live_job": s.job["id"] if s else None, "log": creators.read_log(c["id"], 30)})
    return out


def _sync_token() -> str:
    return f"{BOOT}.{ctx.version()}.{jobs.queue_epoch() if jobs.busy_count() else 0}"


def _sync_unchanged(v: str):
    """{"v": token} if the page is up to date and this user's creators are already in memory, else None."""
    mine = creators._mem.get(ctx.uid())
    if mine is None or not v or v != _sync_token():
        return None
    for c in list(mine.values()):                      # still keep live status fresh while the page is open
        if profiles.needs_refresh(c):
            profiles.refresh(c["id"])
    return {"v": v}


@app.get("/api/sync")
def sync(v: str = ""):
    """What the page asks every couple of seconds. It sends the token it got last time; when nothing of this
    user's has changed, the answer is just that token: no files read, nothing built."""
    token = _sync_token()
    if v == token:
        return _sync_unchanged(v) or {"v": token}
    from . import notices
    return {"v": token, "jobs": [jobs.public(j) for j in jobs.list_jobs()], "creators": _creator_rows(),
            "accounts": accounts.list_accounts(), "ai": usage.brief(), "unread": notices.unread()}


@app.get("/api/jobs")
def list_jobs():
    return [jobs.public(j) for j in jobs.list_jobs()]


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return jobs.public(_job_or_404(jid))


@app.get("/api/preview")
def link_preview(request: Request, url: str = ""):
    url = url.strip()[:500]
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        auth.throttle(f"preview:{_me(request).id}", 12, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many links at once. Wait a moment.")
    try:
        return _lookup(media.preview, url)
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - the site's own reason, in plain words
        raise HTTPException(422, safe.link_reason(e))


@app.get("/api/frame")
def link_frame(request: Request, url: str = "", at: float = 0.35, creator: str = "", t: float = -1):
    """A frame of a video link, to set the webcam and the game on before the clips are made, with where they were
    set for this streamer last time (if they were)."""
    url = url.strip()[:500]
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        auth.throttle(f"frame:{_me(request).id}", 30, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many pictures at once. Wait a moment.")
    try:
        f = _lookup(media.frame, url, at, t if 0 <= t < 48 * 3600 else None)
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Couldn't get a picture from this video right now. Try again in a moment.")
    cid = creator if creator and creators.get(creator) else None
    info = jobs._remembered_info({"creator": cid, "uploader": f.get("uploader")})
    last = {k: info.get(k) for k in ("box", "game", "split") if info.get(k)} if info.get("manual") else {}
    return {**f, "last": last}


@app.post("/api/jobs")
def create_from_link(body: dict = Body(...)):
    _need_verified()
    _quota()
    _throttle("newjob:" + ctx.uid(), 30, 600)
    url = str(body.get("url", "")).strip()[:500]
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        media.check_youtube(url)          # YouTube is refusing us right now: say so, rather than queue a job that fails
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    creator = str(body.get("creator") or "") or None
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    try:
        with _create_lock:
            _job_slots()
            job = jobs.create({"type": "url", "url": url, "name": url}, body.get("options") or {}, creator=creator)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) else "Those options aren't valid.")
    seen = media._PREVIEWS.get(url)  # already looked up while the user was typing: show it straight away
    if seen:
        job.update(title=seen["title"], uploader=seen["uploader"], duration=seen["duration"],
                   handle=seen.get("handle", ""), platform=seen.get("platform", ""))
        if media.save_thumb(jobs.job_dir(job["id"]), url=url):
            job["thumb"] = True
        jobs.save(job)
    return jobs.public(job)


@app.post("/api/upload")
def create_from_file(file: UploadFile = File(...), options: str = Form("{}"), creator: str = Form("")):
    _need_verified()
    _job_slots()
    name = re.sub(r"[^\w.\- ]", "_", Path(file.filename or "video.mp4").name)[:120]
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "Upload a video file (mp4, mov, mkv or webm).")
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    try:
        opts = json.loads(options)
        opts = opts if isinstance(opts, dict) else {}
    except ValueError:
        opts = {}
    updir = config.TMP_DIR / "uploads"
    updir.mkdir(exist_ok=True)
    tmp = updir / f"{os.urandom(8).hex()}{ext}"
    limit, size = config.MAX_UPLOAD_MB * 1024 * 1024, 0
    try:
        with tmp.open("wb") as f:
            while chunk := file.file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB.")
                f.write(chunk)
        try:
            with _create_lock:
                _job_slots()
                job = jobs.create({"type": "file", "path": str(tmp), "name": name}, opts, enqueue=False, creator=creator or None)
        except (ValueError, TypeError):
            raise HTTPException(400, "Those options aren't valid.")
        _disk.pop(ctx.uid(), None)
        dest = jobs.job_dir(job["id"]) / ("source" + ext)
        shutil.move(str(tmp), dest)
    finally:
        tmp.unlink(missing_ok=True)
    job["source"]["path"] = str(dest)
    jobs.enqueue(job)
    return jobs.public(job)


# ---------------------------------------------------------------- big files, sent in pieces
# The page cuts a large file into pieces, sends each one, and asks for them to be joined. Each piece is small enough
# for anything standing in front of the app. Nothing is trusted from the page: the sizes are fixed when the upload
# starts, every piece must be exactly its size, and only the account that started an upload can add to it.
_UP_ID = re.compile(r"[0-9a-f]{24}")
_up_lock = threading.Lock()


def _up_root() -> Path:
    d = config.TMP_DIR / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _up_sweep():
    """Forget uploads that were started and never finished (after six hours)."""
    for d in _up_root().glob("up-*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _up_get(upid: str) -> dict:
    """The upload, only if it belongs to whoever is asking."""
    if not _UP_ID.fullmatch(upid or ""):
        raise HTTPException(404, "No such upload")
    try:
        meta = json.loads((_up_root() / f"up-{upid}" / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "This upload has expired. Start it again.")
    if meta.get("user") != ctx.uid():
        raise HTTPException(404, "No such upload")
    return meta


@app.post("/api/upload/start")
def upload_start(body: dict = Body(...)):
    _need_verified()
    _job_slots()
    _quota()
    _throttle("upstart:" + ctx.uid(), 30, 600)
    name = re.sub(r"[^\w.\- ]", "_", Path(str(body.get("name") or "video.mp4")).name)[:120]
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "Upload a video file (mp4, mov, mkv or webm).")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise HTTPException(400, "Bad file size.")
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB.")
    if _disk.get(ctx.uid(), (0, 0))[1] + size > config.MAX_USER_GB * 1e9:
        raise HTTPException(507, "There isn't room for this file in your storage. Delete some older videos to make room.")
    creator = str(body.get("creator") or "")
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    opts = body.get("options") if isinstance(body.get("options"), dict) else {}
    try:
        jobs.clean_options(opts)                 # refused now rather than after the whole file has been sent
    except (ValueError, TypeError):
        raise HTTPException(400, "Those options aren't valid.")
    _up_sweep()
    mine = 0
    for d in _up_root().glob("up-*"):
        try:
            mine += json.loads((d / "meta.json").read_text(encoding="utf-8")).get("user") == ctx.uid()
        except (OSError, ValueError):
            pass
    if mine >= config.MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"You already have {mine} uploads in progress. Let one finish first.")
    part = config.UPLOAD_PART_MB * 1024 * 1024
    upid = os.urandom(12).hex()
    d = _up_root() / f"up-{upid}"
    d.mkdir()
    meta = {"user": ctx.uid(), "name": name, "ext": ext, "size": size, "part": part, "parts": -(-size // part),
            "options": opts, "creator": creator, "t": time.time()}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"id": upid, "part": part, "parts": meta["parts"]}


@app.put("/api/upload/{upid}/{n}")
async def upload_part(request: Request, upid: str, n: int):
    meta = await run_in_threadpool(_up_get, upid)
    if not 0 <= n < meta["parts"]:
        raise HTTPException(400, "No such piece.")
    want = meta["part"] if n < meta["parts"] - 1 else meta["size"] - meta["part"] * (meta["parts"] - 1)
    d = _up_root() / f"up-{upid}"
    tmp = d / f"{n:05d}.{os.urandom(4).hex()}.tmp"
    got = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > want:
                    raise HTTPException(400, "That piece is the wrong size.")
                await run_in_threadpool(f.write, chunk)
        if got != want:
            raise HTTPException(400, "That piece is the wrong size.")
        tmp.replace(d / f"{n:05d}.part")           # sending a piece again simply replaces it
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "n": n}


@app.post("/api/upload/{upid}/finish")
def upload_finish(upid: str):
    meta = _up_get(upid)
    _need_verified()
    d = _up_root() / f"up-{upid}"
    with _up_lock:                                    # two "finish" calls for one upload make one video, not two
        if not (d / "meta.json").is_file():
            raise HTTPException(404, "This upload has expired. Start it again.")
        parts = [d / f"{i:05d}.part" for i in range(meta["parts"])]
        missing = [i for i, p in enumerate(parts) if not p.is_file()]
        if missing:
            raise HTTPException(409, f"Piece {missing[0] + 1} of {meta['parts']} has not arrived yet.")
        if sum(p.stat().st_size for p in parts) != meta["size"]:
            raise HTTPException(400, "The pieces do not add up to the file.")
        try:
            with _create_lock:
                _job_slots()
                job = jobs.create({"type": "file", "path": "", "name": meta["name"]}, meta["options"], enqueue=False,
                                  creator=meta["creator"] or None)
        except (ValueError, TypeError):
            raise HTTPException(400, "Those options aren't valid.")
        dest = jobs.job_dir(job["id"]) / ("source" + meta["ext"])
        try:
            with dest.open("wb") as out:
                for p in parts:
                    with p.open("rb") as src:
                        shutil.copyfileobj(src, out, 4 * 1024 * 1024)
        except OSError:
            jobs.delete(job["id"])
            raise HTTPException(507, "There wasn't room to save this file. Delete some older videos and try again.")
        shutil.rmtree(d, ignore_errors=True)
    _disk.pop(ctx.uid(), None)
    job["source"]["path"] = str(dest)
    jobs.enqueue(job)
    return jobs.public(job)


@app.delete("/api/upload/{upid}")
def upload_cancel(upid: str):
    _up_get(upid)
    shutil.rmtree(_up_root() / f"up-{upid}", ignore_errors=True)
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{cid}/render")
def rerender(jid: str, cid: str, body: dict = Body(...)):
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    if job["status"] in ("queued", "running") or clip.get("status") in ("queued", "rendering"):
        raise HTTPException(409, "This clip is still being made. Try again when it has finished.")
    _throttle("render:" + ctx.uid(), 20, 600, "That's a lot of re-makes in a short time. Wait a few minutes.")
    try:
        jobs.request_render(jid, cid, body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return jobs.public(job)


_PULL = {"status": "", "percent": 0, "error": "", "busy": False}
PULL_MODEL = "llama3.2:3b"


@app.get("/api/local-ai")
def local_ai(request: Request):
    """The free AI on this computer (Ollama): is it running, which models it has, and a model download's progress."""
    _me(request)
    names = brain.local_models(fresh=True)
    return {"running": bool(names) or _ollama_up(), "models": names, "model": brain._LOCAL["model"], "pull": dict(_PULL),
            "suggested": PULL_MODEL}


def _ollama_up() -> bool:
    import httpx
    try:
        return httpx.get(brain.OLLAMA + "/api/version", timeout=1.5).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@app.post("/api/local-ai/pull")
def local_ai_pull(request: Request):
    """Download the free model into Ollama (about 2 GB, once), in the background; its progress shows in Settings."""
    me_ = _me(request)
    if config.ACCOUNTS and not me_.is_admin:
        raise HTTPException(403, "Only the owner can set this up.")
    if _PULL["busy"]:
        return dict(_PULL)
    if not _ollama_up():
        raise HTTPException(400, "Ollama isn't running. Install it from ollama.com, then try again.")

    def run():
        import httpx
        _PULL.update(busy=True, status="Starting", percent=0, error="")
        try:
            with httpx.stream("POST", brain.OLLAMA + "/api/pull", json={"model": PULL_MODEL, "stream": True}, timeout=None) as r:
                for line in r.iter_lines():
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    if d.get("total"):
                        _PULL["percent"] = int(100 * (d.get("completed") or 0) / d["total"])
                    _PULL["status"] = str(d.get("status") or "")[:80]
            _PULL.update(status="Ready", percent=100)
            brain.local_models(fresh=True)
        except Exception as e:  # noqa: BLE001
            _PULL.update(error=str(e)[:200], status="")
        finally:
            _PULL["busy"] = False
    threading.Thread(target=run, daemon=True).start()
    return dict(_PULL, busy=True)


@app.get("/api/jobs/{jid}/clips/{cid}/frame")
def clip_frame(jid: str, cid: str):
    """A frame of the video a clip is cut from, to draw the webcam's place on."""
    job = _job_or_404(jid)
    if not any(c["id"] == cid for c in job["clips"]):
        raise HTTPException(404, "No such clip")
    try:
        jpeg, frac = jobs.facecam_frame(jid, cid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    import base64
    return {"image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(), "box": frac}


@app.post("/api/jobs/{jid}/facecam")
def set_job_facecam(jid: str, body: dict = Body(...)):
    """The webcam's place, drawn by hand: for one clip or all of this video's clips, and remembered for the streamer."""
    job = _job_or_404(jid)
    if job["status"] in ("queued", "running", "live"):
        raise HTTPException(409, "Wait until the clips are made, then set the webcam.")
    _throttle("camset:" + ctx.uid(), 30, 600, "That's a lot of changes in a short time. Wait a few minutes.")
    cid = body.get("clip") or None
    if cid is not None and not any(c["id"] == cid for c in job["clips"]):
        raise HTTPException(404, "No such clip")
    box = body.get("box")
    try:
        n = jobs.set_facecam(jid, None if box in (None, "none") else box, cid)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"remade": n, "job": jobs.public(job)}


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: str):
    """Stop a video being made. The clips already made stay."""
    job = _job_or_404(jid)
    if job.get("status") == "live" or jid in live.SESSIONS:
        raise HTTPException(409, "This is a live recording: use Stop recording.")
    try:
        return jobs.public(jobs.stop(jid))
    except ValueError as e:
        raise HTTPException(409, str(e))




def _clip_or_404(jid: str, cid: str):
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    return job, clip


@app.put("/api/jobs/{jid}/clips/{cid}/text")
def clip_text(jid: str, cid: str, body: dict = Body(...)):
    """Save a clip's title, description and hashtags. The video is not made again."""
    job, clip = _clip_or_404(jid, cid)
    _throttle("cliptext:" + ctx.uid(), 120, 600)
    try:
        jobs.save_post_text(job, clip, body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return jobs.public(job)


@app.post("/api/jobs/{jid}/clips/{cid}/write")
def clip_write(jid: str, cid: str):
    """Ask the AI for a new title, description and hashtags for one clip. Returned to the page, not saved."""
    job, clip = _clip_or_404(jid, cid)
    _throttle("clipwrite:" + ctx.uid(), 20, 600, "That's a lot of rewrites in a short time. Wait a few minutes.")
    usage.set_context(job.get("creator"), job.get("title") or "")
    try:
        return brain.post_text(jobs.spoken_in(job, clip), job.get("title") or "", job.get("uploader") or "",
                               game=job.get("game") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))

@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] in ("queued", "running", "live") or jid in live.SESSIONS:
        raise HTTPException(409, "This is still being made. Press Stop on it first, or wait for it to finish.")
    jobs.delete(jid)
    return {"ok": True}


def _pairs(v, limit=300) -> list:
    """[[video id, clip id], ...] from the page, checked."""
    out = []
    for x in (v if isinstance(v, list) else [])[:limit]:
        if isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(y, str) and re.fullmatch(r"[\w\-]{1,60}", y) for y in x):
            out.append((x[0], x[1]))
    return out


@app.delete("/api/jobs/{jid}/clips/{cid}")
def delete_clip(jid: str, cid: str):
    _job_or_404(jid)
    try:
        if not account.delete_clip(jid, cid):
            raise HTTPException(404, "No such clip")
    except account.Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/bulk/delete")
def bulk_delete(body: dict = Body(...)):
    _throttle("bulkdel:" + ctx.uid(), 30, 600)
    vids = [x for x in (body.get("videos") if isinstance(body.get("videos"), list) else [])[:300]
            if isinstance(x, str) and re.fullmatch(r"[\w\-]{8,60}", x)]
    return account.delete_many(vids, _pairs(body.get("clips")))


@app.get("/api/clips.zip")
def clips_zip(ids: str = ""):
    """Many clips as one download. `ids` is video.clip pairs separated by commas."""
    _throttle("zip:" + ctx.uid(), 12, 600, "Give it a moment: you just downloaded several groups of clips.")
    pairs = _pairs([p.split(".", 1) for p in ids.split(",") if "." in p][:300])
    try:
        f = account.zip_clips(pairs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from starlette.background import BackgroundTask
    return FileResponse(f, filename=f"clipkontrast-clips-{len(pairs)}.zip", media_type="application/zip",
                        headers={"Cache-Control": "private, no-store"}, background=BackgroundTask(lambda: f.unlink(missing_ok=True)))


@app.delete("/api/creators/{cid}")
def delete_creator(cid: str):
    _throttle("cdel:" + ctx.uid(), 30, 600)
    try:
        r = account.delete_creator(cid)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    if not r.get("ok"):
        raise HTTPException(404, "No such creator")
    return r


@app.post("/api/account/delete")
def delete_my_account(request: Request, body: dict = Body(...)):
    """Deletes the signed-in account and everything in it. The page asks for the email to be typed, so it cannot
    happen by a stray click or a request the person did not mean to make."""
    me_ = _me(request)
    if str(body.get("confirm") or "").strip().lower() != me_.email.lower():
        raise HTTPException(400, "Type your email address exactly to confirm.")
    try:
        account.delete_account(me_.id)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.cookie_args("x")["key"], path="/")
    return resp


@app.get("/media/{jid}/{name}")
def media_file(jid: str, name: str, download: int = 0):
    if not re.fullmatch(r"[\w\-]+\.(mp4|jpg)", name):
        raise HTTPException(404)
    f = Path(_job_or_404(jid)["dir"]) / "clips" / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, filename=name if download else None, headers={"Cache-Control": "private, max-age=300"})


# ================================================================ creators and live recording
def _channel_ok(body: dict):
    links = [str(body.get("channel") or "").strip()]
    chs = body.get("channels")
    if chs is not None and not isinstance(chs, dict):
        raise HTTPException(400, "Bad channel list.")
    for plat, url in (chs or {}).items():
        url = str(url or "").strip()
        if url and creators.platform_key(url) != plat:
            raise HTTPException(400, f"That link isn't a {plat.title()} channel.")
        links.append(url)
    for ch in links:
        if ch and not media.site_allowed(live.normalize(ch)):
            raise HTTPException(400, "Channel links must be from YouTube, Twitch or Kick.")


@app.get("/api/admin/email-preview")
def email_preview(request: Request, kind: str = "verify"):
    """What account emails look like, for admins."""
    from . import mailer
    _admin(request)
    if kind == "reset":
        page = mailer.preview("Reset your password", "Someone asked to reset the password for this ClipKontrast account. If that was you, choose a new one below.",
                              "Choose a new password", config.PUBLIC_URL + "/reset?t=example",
                              "The link works for one hour. If you didn't ask for this, ignore this email and your password stays the same.")
    else:
        page = mailer.preview()
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.post("/api/admin/test-email")
def test_email(request: Request):
    """Send the confirm-email template to the admin, to check the email service works."""
    from . import mailer
    me_ = _admin(request)
    if not mailer.ready():
        raise HTTPException(400, "No email service is set up yet. Add the CLIPKONTRAST_SMTP settings to .env first.")
    mailer.send(me_.email, "ClipKontrast test email", "Your emails are working",
                "This is a test from your ClipKontrast server. Account emails like this one will now reach people.",
                "Open ClipKontrast", config.PUBLIC_URL + "/app", "You can ignore this message.")
    return {"sent_to": me_.email}


@app.post("/api/feedback")
def send_feedback(request: Request, body: dict = Body(...)):
    from . import feedback
    u = _me(request)
    try:
        auth.throttle("feedback:" + u.id, 10, 3600)
        return feedback.submit(u.id, str(body.get("kind", "")), str(body.get("message", "")),
                               str(body.get("page", "")), str(body.get("context", "")))
    except auth.AuthError:
        raise HTTPException(429, "You've sent a lot of messages in the last hour. Try again a bit later.")
    except feedback.Refused as e:
        raise HTTPException(400, str(e))


@app.post("/api/public/bug-report")
async def public_bug_report(request: Request, message: str = Form(""), steps: str = Form(""), contact: str = Form(""),
                            app_info: str = Form("", alias="app"), diag: str = Form(""), context: str = Form(""),
                            source: str = Form("website"), screenshot: UploadFile | None = File(None)):
    """A bug report: from the clip maker on anyone's computer (sent on by its own server, so no account is needed),
    or from the website's report page. Limited per address, and in all, so it can't be used to flood the admins."""
    from . import feedback
    ip = _ip(request)
    try:
        auth.throttle("bug-ip:" + ip, 6, 3600)
        auth.throttle("bug-ip-day:" + ip, 25, 86400)
        auth.throttle("bug-all", 500, 86400)
    except auth.AuthError:
        raise HTTPException(429, "Lots of reports were sent from here recently. Try again in a while.")
    data = await screenshot.read(6 * 1024 * 1024 + 1) if screenshot is not None else b""
    me_ = getattr(request.state, "user", None)
    try:
        return await run_in_threadpool(feedback.submit_report, message, steps, contact, app_info, diag, context,
                                       "app" if source == "app" else "website", data, me_.id if me_ else None)
    except feedback.Refused as e:
        raise HTTPException(400, str(e))


@app.get("/api/bug-report/details")
def bug_report_details(request: Request):
    """What a report from this clip maker would carry, shown to the person before they send it."""
    from . import diagnose
    return {"app": diagnose.app_line(), "details": diagnose.details() if _may_see_diag(_me(request)) else
            "(Technical details are only sent from the ClipKontrast app on your own computer.)"}


def _may_see_diag(user) -> bool:
    """The log and the recent errors are about everyone on this server: on the app on someone's own computer that is
    just them, on a shared server only its admins may see or send them."""
    return not config.ACCOUNTS or bool(user and user.is_admin)


@app.post("/api/bug-report")
async def bug_report(request: Request, message: str = Form(""), steps: str = Form(""), contact: str = Form(""),
                     details: str = Form("1"), context: str = Form(""), screenshot: UploadFile | None = File(None)):
    """The clip maker's "Report a problem": the person's words, and if they agree the technical details, sent on to the
    ClipKontrast website (this computer runs its own server, so the report goes from here, not from the page)."""
    user = _me(request)
    import httpx
    from . import diagnose
    data = await screenshot.read(6 * 1024 * 1024 + 1) if screenshot is not None else b""
    form = {"message": message, "steps": steps, "contact": contact, "context": context[:1000], "source": "app",
            "app": diagnose.app_line(), "diag": diagnose.details() if details == "1" and _may_see_diag(user) else ""}
    files = {"screenshot": ("screenshot.jpg", data, "image/jpeg")} if data else None

    def send():
        return httpx.post(config.EARN_URL + "/api/public/bug-report", data=form, files=files, timeout=45,
                          headers={"User-Agent": "ClipKontrast-bug-report"})
    try:
        r = await run_in_threadpool(send)
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Couldn't reach the ClipKontrast website. Check the internet connection and try again.")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        raise HTTPException(r.status_code if r.status_code in (400, 413, 429) else 502,
                            str(body.get("detail") or "The report couldn't be sent. Try again in a minute."))
    return {"id": body.get("id")}


@app.get("/api/admin/feedback/{fid}/shot")
def admin_feedback_shot(request: Request, fid: int):
    _admin(request)
    from . import feedback
    p = feedback.shot_path(fid)
    if not p:
        raise HTTPException(404, "Not found.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.get("/api/admin/feedback")
def admin_feedback(request: Request, status: str = ""):
    from . import feedback
    _admin(request)
    return feedback.listing(status)


@app.get("/api/admin/feedback.csv")
def admin_feedback_csv(request: Request, status: str = ""):
    """Every bug report as a CSV file (the ones with this status, or all), with the technical details."""
    from . import feedback
    _admin(request)
    name = f"clipkontrast-bug-reports-{status if status in feedback.STATUSES else 'all'}-{time.strftime('%Y-%m-%d')}.csv"
    return Response(feedback.csv_text(status), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"})


@app.post("/api/admin/feedback/{fid}")
def admin_feedback_status(request: Request, fid: int, body: dict = Body(...)):
    from . import feedback
    _admin(request)
    try:
        feedback.set_status(fid, str(body.get("status", "")))
    except feedback.Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/notifications")
def notifications_list():
    from . import notices
    return notices.mine()


@app.post("/api/notifications/read")
def notifications_read():
    from . import notices
    notices.read_all()
    return {"ok": True}


@app.get("/api/me/photo")
def my_photo(request: Request):
    u = _me(request)
    got = auth.photo(u.id) if u else None
    if not got:
        raise HTTPException(404)
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/channel-preview")
def channel_preview(request: Request, url: str = ""):
    url = url.strip()[:300]
    if not url or not media.site_allowed(live.normalize(url)):
        raise HTTPException(400, "Paste a YouTube, Twitch or Kick channel link.")
    try:
        auth.throttle(f"chanprev:{_me(request).id}", 12, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many lookups at once. Wait a moment.")
    try:
        return _lookup(profiles.preview, url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Couldn't open that channel. Check the link.")


@app.get("/api/creators/{cid}/last-stream")
def creator_last_stream(request: Request, cid: str):
    """The creator's newest stream that has ended, for the card shown on their page when they aren't live."""
    c = creators.get(cid)
    if not c:
        raise HTTPException(404, "No such creator")
    try:
        auth.throttle(f"laststream:{_me(request).id}", 20, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many at once. Wait a moment.")
    urls = [c["channel"]] + [u for u in (c.get("channels") or {}).values() if u != c["channel"]]
    for url in [u for u in urls if u][:3]:
        got = _lookup(profiles.last_stream, url)
        if got:
            return got
    return {}


@app.get("/api/creators/{cid}/avatar")
def creator_avatar(cid: str, p: str = ""):
    c = creators.get(cid)
    if not c:
        raise HTTPException(404)
    key = p if p in creators.PLATFORMS else creators.platform_key(c["channel"])
    prof = (c.get("profiles") or {}).get(key) or {}
    f = creators.root(cid) / f"avatar-{key}.img"
    if not f.is_file() or prof.get("pic") not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(404)
    return FileResponse(f, media_type=prof["pic"], headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/creators")
def list_creators():
    return _creator_rows()


@app.post("/api/creators")
def new_creator(body: dict = Body(...)):
    _channel_ok(body)
    if len(creators.all_creators()) >= config.MAX_CREATORS:
        raise HTTPException(400, f"This server allows {config.MAX_CREATORS} creators per account.")
    try:
        c = creators.create(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if c.get("channels"):
        profiles.refresh(c["id"], force=True)
    return c


@app.put("/api/creators/{cid}")
def edit_creator(cid: str, body: dict = Body(...)):
    _throttle("cedit:" + ctx.uid(), 60, 600)
    _channel_ok(body)
    try:
        c = creators.update(cid, body)
    except KeyError:
        raise HTTPException(404, "No such creator")
    except ValueError as e:
        raise HTTPException(400, str(e))
    s = live.active_for(cid)
    if s:
        s.settings = c  # new settings apply to the running recording straight away
    if "channels" in body or "channel" in body:
        profiles.refresh(cid, force=True)
    if body.get("auto_watch") and c.get("auto_watch") and not s:
        live.check_now(cid)    # if they are live right now, recording starts within seconds
    return c


@app.post("/api/creators/{cid}/live")
def go_live(cid: str, body: dict = Body(default={})):
    _need_verified()
    try:
        c = creators.get(cid)
    except ValueError:
        c = None
    if not c:
        raise HTTPException(404, "No such creator")
    # the stream the user is watching, else where they are live right now, else the channel chosen in Setup
    on_air = [p["channel"] for k, p in (c.get("profiles") or {}).items() if p.get("live") and c["channels"].get(k) == p.get("channel")]
    asked = str((body or {}).get("channel") or "")
    url = asked if asked in c["channels"].values() else c["channel"] if c["channel"] in on_air or not on_air else on_air[0]
    try:
        job = live.start_session(c, url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if c.get("hold"):
        creators.update(c["id"], {"hold": False})
    return jobs.public(job)


def _channel_key(url: str) -> str:
    """One spelling for a channel link, to tell whether two links are the same channel."""
    u = re.sub(r"^https?://(www\.|m\.)?", "", (url or "").strip().lower()).rstrip("/")
    return re.sub(r"/(live|streams|videos|featured)$", "", u)


_LIVE_VIDEO = re.compile(r"(youtube\.com/(watch\?|live/)|youtu\.be/)", re.I)


@app.post("/api/live/quick")
def quick_live(body: dict = Body(...)):
    """Live clipping in one step, from the home screen: paste a channel (or a stream that is live right now) and
    press Start. The creator's workspace is made by itself (or the one already there is used), auto-record is
    turned on, and the recording starts at once if they are live, or by itself when they next go live."""
    _need_verified()
    _throttle("quicklive:" + ctx.uid(), 20, 600, "That's a lot of channels in a short time. Wait a few minutes.")
    raw = str(body.get("url", "")).strip()[:300]
    if raw and not re.match(r"^https?://", raw):
        raw = "https://" + raw
    if not raw or not media.site_allowed(raw):
        raise HTTPException(400, "Paste a YouTube, Twitch or Kick channel link, like youtube.com/@name or twitch.tv/name.")
    look = jobs.clean_options({k: body[k] for k in ("style", "layout", "captions", "zoom", "watermark", "lang", "caption_lang") if k in body})
    channel, stream, name = raw, None, ""
    if _LIVE_VIDEO.search(raw):              # one video: it has to be live right now, and its channel is the creator
        try:
            i = media.info(raw)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, safe.link_reason(e))
        if not i["is_live"]:
            raise HTTPException(400, "That video isn't live right now. To make clips of it, use Clip a video. To clip "
                                     "their next stream, paste their channel link here instead.")
        stream, channel, name = raw, i.get("channel_url") or "", i.get("uploader") or ""
    key = _channel_key(channel) if channel else ""
    c = next((x for x in creators.all_creators() if key and any(_channel_key(u) == key for u in
                                                                   [x.get("channel", "")] + list((x.get("channels") or {}).values()))), None)
    made = False
    if c:
        c = creators.update(c["id"], {"watch_once": True, "hold": False, **look})
    else:
        if len(creators.all_creators()) >= config.MAX_CREATORS:
            raise HTTPException(400, f"This server allows {config.MAX_CREATORS} creators per account. Delete one first.")
        if not name and channel:
            try:
                name = (profiles.preview(channel) or {}).get("name") or ""
            except Exception:  # noqa: BLE001 - named from the link instead
                name = ""
        if not name:
            m = re.search(r"(?:@|twitch\.tv/|kick\.com/|channel/|c/)([^/?#]+)", channel or raw, re.I)
            name = (m.group(1) if m else "Live stream")[:60]
        try:
            c = creators.create({"name": name[:60], "channel": channel or stream, "watch_once": True, **look})
        except ValueError as e:
            raise HTTPException(400, str(e))
        made = True
        if c.get("channels"):
            profiles.refresh(c["id"], force=True)
    job, note = None, ""
    running = live.active_for(c["id"])
    if running:
        job = running.job
    elif stream:
        try:
            job = live.start_session(c, stream)
        except ValueError as e:
            note = str(e)
    else:
        live.check_now(c["id"])               # live right now: recording starts within seconds; if not, when they go live
    return {"creator": c, "made": made, "job": jobs.public(job) if job else None, "note": note}


@app.post("/api/live/{jid}/stop")
def stop_live(jid: str):
    s = live.mine(jid)
    if not s:
        raise HTTPException(404, "This recording has already ended")
    s.stop()
    cid = s.job.get("creator")
    if cid and creators.get(cid):
        # Stopped by hand: not recorded again by itself while this stream goes on (only once they've gone offline
        # and come back), however often the app checks or is opened again.
        creators.update(cid, {"hold": True, "watch_once": False})
    return {"ok": True}


@app.post("/api/live/{jid}/clip")
def clip_now(jid: str, body: dict = Body(...)):
    s = live.mine(jid)
    if not s or s.ended:
        raise HTTPException(404, "This recording has already ended")
    _throttle("clipnow:" + ctx.uid(), 6, 60, "Give it a moment: your last clips are still being made.")
    if len(s.manual) >= 3:
        raise HTTPException(429, "Give it a moment: your last clips are still being made.")
    try:
        s.clip_last(max(5, min(180, float(body.get("seconds", 30)))))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ================================================================ AI usage
@app.get("/api/usage")
def ai_usage():
    return usage.summary({c["id"]: c["name"] for c in creators.all_creators()})


# ---------------------------------------------------------------- the AI key and model, set from the dashboard
# Only the owner (or an admin, when accounts are on) reaches these. The key is never sent back to the page.
def _ai_error(e) -> str:
    return safe.friendly(e, 300).replace(ai.key() or "\0", "<key>")


@app.get("/api/ai")
def ai_get(request: Request):
    _admin(request)
    return ai.public()


@app.put("/api/ai")
def ai_put(request: Request, body: dict = Body(...)):
    _admin(request)
    k = body.get("key")
    try:
        out = ai.save(body.get("provider"), body.get("models"), body.get("base_url") or "",
                      None if k is None or (isinstance(k, str) and not k.strip() and not body.get("remove_key")) else str(k))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    usage.forget_all()
    ctx.bump()
    return out


@app.post("/api/ai/models")
def ai_models(request: Request, body: dict = Body(default={})):
    _admin(request)
    _throttle("aimodels:" + ctx.uid(), 30, 600)
    try:
        return {"models": ai.list_models(body.get("provider"), body.get("base_url"), body.get("key"))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        reason = ai.explain(e)
        raise HTTPException(502, f"The service did not give its list of models. {reason}" if reason else
                            "The service did not give its list of models. Check the key, or type the model yourself. " + _ai_error(e)[:160])


@app.post("/api/ai/test")
def ai_test(request: Request):
    _admin(request)
    _throttle("aitest:" + ctx.uid(), 20, 600)
    try:
        return ai.test()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, ai.explain(e) or "The AI did not answer: " + _ai_error(e))


# ================================================================ posting, accounts, settings

@app.post("/api/ai/key/test")
def ai_key_test(request: Request, body: dict = Body(...)):
    _admin(request)
    _throttle("aikeytest:" + ctx.uid(), 20, 600)
    p = body.get("provider")
    k = ai.clean_key(body.get("key"))
    try:
        return ai.test_single_key(p, k, body.get("base_url") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, ai.explain(e, provider_id=p) or str(e))


@app.post("/api/ai/key/add")
def ai_key_add(request: Request, body: dict = Body(...)):
    _admin(request)
    _throttle("aikeyadd:" + ctx.uid(), 30, 600)
    p = body.get("provider")
    k = ai.clean_key(body.get("key"))
    try:
        out = ai.add_key_to_pool(p, k, body.get("base_url") or "", test_first=bool(body.get("test_first", True)))
        usage.forget_all()
        ctx.bump()
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, ai.explain(e, provider_id=p) or str(e))


@app.post("/api/ai/key/remove")
def ai_key_remove(request: Request, body: dict = Body(...)):
    _admin(request)
    _throttle("aikeyrem:" + ctx.uid(), 30, 600)
    p = body.get("provider")
    k = ai.clean_key(body.get("key"))
    try:
        out = ai.remove_key_from_pool(p, k)
        usage.forget_all()
        ctx.bump()
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, ai.explain(e, provider_id=p) or str(e))


@app.get("/api/overview")
def overview():
    return posting.overview()


@app.get("/api/settings")
def get_settings():
    return accounts.public_settings()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    try:
        accounts.save_settings(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return accounts.public_settings()


@app.get("/api/accounts")
def list_accounts():
    return accounts.list_accounts()


@app.delete("/api/accounts/{aid}")
def remove_account(aid: str):
    accounts.remove(aid)
    return {"ok": True}


# ---------------------------------------------------------------- connecting YouTube, Instagram and TikTok
# Connect sends the browser to the platform's own sign-in page; the platform sends it back here, and the person lands
# on Settings, Connected apps, with the account added (or a message saying what went wrong).
def _back(**q):
    from urllib.parse import urlencode as _ue
    return RedirectResponse("/app/settings/apps?" + _ue(q), 303)


@app.get("/oauth/{platform}/start")
def connect_start(platform: str):
    if platform not in accounts.PLATFORM_NAMES:
        raise HTTPException(404)
    try:
        return RedirectResponse(accounts.connect_url(platform), 303)
    except ValueError as e:
        return _back(connect_error=str(e))


@app.get("/oauth/{platform}/callback")
def connect_callback(platform: str, code: str = "", state: str = "", error: str = "", error_description: str = "",
                     error_reason: str = ""):
    if platform not in accounts.PLATFORM_NAMES:
        raise HTTPException(404)
    name = accounts.PLATFORM_NAMES[platform]
    if error or not code:
        why = "you cancelled" if error in ("access_denied",) or error_reason == "user_denied" else (error_description or error or "no sign-in code came back")
        return _back(connect_error=f"{name} was not connected: {why[:160]}.")
    try:
        _throttle("connect:" + ctx.uid(), 30, 600)
        acc = accounts.connect_finish(platform, code, state)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return _back(connect_error=f"{name} was not connected: {safe.friendly(e, 240)}")
    return _back(connected=platform, name=acc["name"])


@app.get("/pub/{token}.mp4")
def public_clip(token: str):
    """A clip for a platform to fetch while posting it: one clip, one hour, signed (see accounts.clip_link)."""
    got = accounts.read_clip_link(token)
    if not got:
        raise HTTPException(404)
    uid, job_id, name = got
    with ctx.as_user(uid):
        job = jobs.get(job_id) if re.fullmatch(r"[\w\-]{8,60}", job_id) else None
        f = Path(job["dir"]) / "clips" / name if job else None
    if not f or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- where ClipKontrast keeps everything (Settings, Storage)
def _on_this_computer(request: Request) -> bool:
    """Is the person sitting at the computer ClipKontrast runs on? Only then can a folder window be opened for them."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost") or (config.env("PAPICLIPS_TRUST_PROXY") or config.env("CLIPKONTRAST_TRUST_PROXY")):
        return False
    # A tunnel (Cloudflare) running on this same computer also connects from 127.0.0.1, so a request from anywhere on
    # the internet would look local. The tunnel always adds its own headers, which a browser here never sends.
    named = ((config.env("PAPICLIPS_CLIENT_IP_HEADER") or config.env("CLIPKONTRAST_CLIENT_IP_HEADER")) or "").strip().lower()
    proxied = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded") + ((named,) if named else ())
    return not any(request.headers.get(h) for h in proxied)


@app.get("/api/storage")
def storage_get(request: Request):
    from . import storage
    _admin(request)
    return storage.info(_on_this_computer(request))


@app.post("/api/storage/open")
def storage_open(request: Request):
    from . import storage
    _admin(request)
    if not _on_this_computer(request):
        raise HTTPException(400, "The folder can only be opened on the computer ClipKontrast runs on.")
    try:
        storage.open_folder()
    except OSError as e:
        raise HTTPException(500, "Couldn't open the folder: " + safe.friendly(e, 200))
    return {"ok": True}


@app.post("/api/storage/pick")
def storage_pick(request: Request):
    from . import storage
    _admin(request)
    if not _on_this_computer(request):
        raise HTTPException(400, "Type the folder's path instead: a folder window can only be shown on the computer ClipKontrast runs on.")
    return {"path": storage.pick_folder()}


@app.put("/api/storage")
def storage_put(request: Request, body: dict = Body(...)):
    from . import storage
    _admin(request)
    try:
        if body.get("cancel"):
            storage.cancel_change()
            return storage.info(_on_this_computer(request))
        storage.change(str(body.get("path") or ""), bool(body.get("move", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return storage.info(_on_this_computer(request))


@app.get("/api/apps")
def apps_get(request: Request):
    _admin(request)
    return accounts.apps_public()


@app.put("/api/apps")
def apps_put(request: Request, body: dict = Body(...)):
    _admin(request)
    try:
        return accounts.save_app(str(body.get("platform", "")), body.get("client_id"), body.get("client_secret"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/posts")
def add_posts(body: dict = Body(...)):
    job = jobs.get(str(body.get("job")))
    clip = next((c for c in job["clips"] if c["id"] == body.get("clip")), None) if job else None
    if not clip or clip.get("status") != "done":
        raise HTTPException(404, "That clip is not ready")
    made = posting.enqueue(job, clip, [str(a) for a in body.get("accounts", [])][:20], now=bool(body.get("now")))
    if not made:
        raise HTTPException(400, "Nothing was queued. Pick at least one account this clip has not already gone to.")
    return {"queued": len(made)}


@app.post("/api/posts/{pid}/{action}")
def post_action(pid: str, action: str):
    try:
        {"cancel": posting.cancel, "retry": posting.retry}[action](pid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/alerts/test")
def test_alert():
    if not alerts.configured():
        raise HTTPException(400, "Save a Discord webhook or Telegram details first.")
    auth_problem = None
    try:
        auth.throttle("alert:" + ctx.uid(), 10, 600)
    except auth.AuthError as e:
        auth_problem = str(e)
    if auth_problem:
        raise HTTPException(429, auth_problem)
    problems = alerts.send("ClipKontrast: test message. Alerts are working.")
    if problems:
        raise HTTPException(400, "; ".join(problems))
    return {"ok": True}


# ================================================================ admin (only accounts marked as admin in the database)
# The gate above already answers "not found" to everyone else; each route checks again so neither can be forgotten.
def _uid_ok(uid: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{6,40}", uid):
        raise HTTPException(404)
    return uid


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    _admin(request)
    return admins.overview()


@app.get("/api/admin/users/{uid}")
def admin_user(request: Request, uid: str):
    _admin(request)
    try:
        return admins.user_detail(_uid_ok(uid))
    except admins.Refused as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/admin/users/{uid}/disabled")
def admin_disable(request: Request, uid: str, body: dict = Body(...)):
    me_ = _admin(request)
    try:
        return admins.set_disabled(me_, _uid_ok(uid), body.get("disabled"))
    except admins.Refused as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/admin/users/{uid}/delete")
def admin_delete_user(request: Request, uid: str, body: dict = Body(...)):
    me_ = _admin(request)
    target = auth.get_user(_uid_ok(uid))
    if not target:
        raise HTTPException(404, "No such account")
    if target.id == me_.id:
        raise HTTPException(400, "You can't delete your own account from here.")
    if str(body.get("confirm") or "").strip().lower() != target.email.lower():
        raise HTTPException(400, "Type the account's email address exactly to confirm.")
    try:
        account.delete_account(target.id)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    with db.engine.begin() as c:
        admins._log(c, me_.id, "delete", target.id, "account deleted")
    return {"ok": True}


def _open_browser_when_ready():
    """Open the dashboard in the default browser once the server answers (used by the start scripts)."""
    import threading, time, urllib.request, webbrowser
    url = f"http://127.0.0.1:{config.PORT}/app"

    def wait_then_open():
        for _ in range(600):                 # up to ten minutes: the first start downloads the speech model
            try:
                urllib.request.urlopen(url, timeout=2)
                webbrowser.open(url)
                return
            except Exception:
                time.sleep(1)
    threading.Thread(target=wait_then_open, daemon=True).start()


# ================================================================ Earn: paid clipping campaigns
def _earn(fn, *a, **k):
    """Runs an Earn action; problems the person can fix come back as a plain message."""
    try:
        out = fn(*a, **k)
    except earn.EarnError as e:
        raise HTTPException(400, str(e))
    if fn in (earn.save_campaign, earn.delete_campaign, earn.set_order, earn.save_cover):
        _APP_FEED.clear()                    # what the app shows follows the admin's change at once
    return out


def _uid_or_none(request: Request) -> str:
    u = getattr(request.state, "user", None)
    return u.id if u else ""


@app.get("/earn/cover/{cid}.jpg")
def earn_cover(cid: str):
    f = earn.COVERS / f"{cid}.jpg"
    if not re.fullmatch(r"[\w\-]{3,30}", cid) or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


_EARN_PUBLIC: dict = {}      # the Earn page as a visitor sees it, kept for a few seconds so a rush of visitors is cheap


@app.get("/api/earn/public/overview")
def earn_overview(request: Request):
    me_ = getattr(request.state, "user", None)
    if not me_:
        # everyone who isn't signed in sees the same page: worked out at most every 10 seconds, not once per visitor
        hit = _EARN_PUBLIC.get("v")
        if hit and time.time() - hit[0] < 10:
            return hit[1]
    out = {"overview": earn.overview(), "campaigns": earn.campaigns(uid=_uid_or_none(request)), "leaderboard": earn.leaderboard(),
           "user": auth.public(me_) if me_ else None, "accounts": config.ACCOUNTS,
           "earn_only": config.EARN_ONLY, "download_url": config.DOWNLOAD_URL, "guide_url": config.GUIDE_URL}
    if not me_:
        _EARN_PUBLIC["v"] = (time.time(), out)
    return out


_MOMENTS_CACHE: dict = {}


@app.get("/api/earn/public/moments")
def earn_open_moments():
    """The best moments of the campaigns taking clips, for the ClipKontrast app's home screen. The same for everyone,
    so it is worked out at most once a minute."""
    hit = _MOMENTS_CACHE.get("v")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = {"campaigns": earn.open_moments(), "earn_url": config.EARN_URL}
    _MOMENTS_CACHE["v"] = (time.time(), out)
    return out


_APP_FEED: dict = {}


@app.get("/api/earn/public/app")
def earn_app_feed():
    """The campaigns as the ClipKontrast app shows them (home screen, Paid campaigns page). Kept a minute."""
    hit = _APP_FEED.get("v")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = {**earn.app_feed(), "earn_url": config.EARN_URL}
    _APP_FEED["v"] = (time.time(), out)
    return out


@app.post("/api/admin/earn/moments-preview")
def earn_moments_preview(request: Request, body: dict = Body(...)):
    """How the admin's pasted timestamps are read, while typing: which stream each group opens."""
    _admin(request)
    groups = earn.parse_moments(str(body.get("moments", ""))[:12000], str(body.get("sources", ""))[:4000])
    return {"groups": [{"title": g["title"], "url": g["url"], "count": len(g["moments"])} for g in groups],
            "count": sum(len(g["moments"]) for g in groups)}


_APP_MOMENTS: dict = {}


@app.get("/api/campaign-moments")
def campaign_moments(request: Request):
    """The clip maker's view of the Earn campaigns: their best moments, read from the ClipKontrast website (kept five
    minutes), so they can be clipped from the home screen. Nothing when the website can't be reached."""
    _me(request)
    hit = _APP_MOMENTS.get("v")
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    import httpx
    try:
        r = httpx.get(config.EARN_URL + "/api/earn/public/moments", timeout=8, headers={"User-Agent": "ClipKontrast"})
        data = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001 - offline: no campaigns shown, nothing else changes
        data = {}
    camps = []
    for c in (data.get("campaigns") or [])[:10] if isinstance(data, dict) else []:
        try:
            groups = [{"title": str(g["title"])[:120], "url": str(g["url"])[:300],
                       "moments": [{"t": int(m["t"]), "end": int(m["end"]) if m.get("end") else None, "label": str(m.get("label") or "")[:140]}
                                   for m in g["moments"][:200]]}
                      for g in c["moments"][:40] if re.match(r"https://", str(g.get("url", "")))]
            camps.append({"slug": re.sub(r"[^\w-]", "", str(c["slug"]))[:80], "title": str(c["title"])[:120],
                          "creator_name": str(c.get("creator_name") or "")[:120], "notice": str(c.get("notice") or "")[:1000],
                          "rate_per_1k": float(c.get("rate_per_1k") or 0), "moments": groups})
        except (KeyError, TypeError, ValueError):
            continue                       # one odd entry is skipped, never the page
    out = {"campaigns": camps, "earn_url": config.EARN_URL}
    _APP_MOMENTS["v"] = (time.time(), out)
    return out


# ---------------------------------------------------------------- the campaigns in the app: home screen, Paid campaigns
_APP_CAMPS: dict = {}
_COVER_PATH = re.compile(r"/earn/cover/[\w\-]{3,30}\.jpg(?:\?v=\d{1,12})?")
_SLUG = re.compile(r"[^\w-]")


def _s(v, n):
    return str(v if v is not None else "")[:n]


def _f(v):
    try:
        x = float(v or 0)
        return x if x == x and abs(x) < 1e12 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _when(v):
    x = _f(v)
    return x if x > 0 else None


def _cover(v) -> str:
    """A campaign's picture, through this app (the page may only show pictures from here): only the website's own."""
    v = str(v or "")
    return "/api/campaign-cover?p=" + quote(v, safe="") if _COVER_PATH.fullmatch(v) else ""


def _moment(m) -> dict:
    t = int(_f(m.get("t")))
    end = int(_f(m.get("end"))) if m.get("end") else None
    return {"t": t, "end": end if end and end > t else None, "label": _s(m.get("label"), 140)}


def _app_stream(x) -> dict:
    url = str(x.get("url") or "")
    if not media.site_allowed(url) and not re.match(r"https://", url):
        raise ValueError
    kind = x.get("kind") if x.get("kind") in ("youtube", "kick", "twitch", "instagram", "drive", "other") else "other"
    return {"name": _s(x.get("name") or x.get("title"), 120) or "Stream", "url": url[:300], "kind": kind,
            "latest": bool(x.get("latest")), "clip": media.site_allowed(url),
            "moments": [_moment(m) for m in (x.get("moments") or [])[:300] if isinstance(m, dict)]}


def _app_live(c) -> dict:
    st = c.get("state") if c.get("state") in ("open", "starts", "soon") else "open"
    streams = []
    for x in (c.get("streams") or [])[:30]:
        try:
            streams.append(_app_stream(x))
        except (ValueError, TypeError, AttributeError):
            continue
    plats = [p for p in (c.get("platforms") or []) if p in ("youtube", "instagram", "x", "tiktok")]
    return {"slug": _SLUG.sub("", _s(c["slug"], 80)), "title": _s(c["title"], 120), "creator_name": _s(c.get("creator_name"), 120),
            "tagline": _s(c.get("tagline"), 200), "brief": _s(c.get("brief"), 1500), "notice": _s(c.get("notice"), 1000),
            "rules": [_s(r, 300) for r in (c.get("rules") or [])[:8]], "platforms": plats,
            "required_tag": _s(c.get("required_tag"), 80), "category": _s(c.get("category"), 40),
            "rate_per_1k": _f(c.get("rate_per_1k")), "min_views": int(_f(c.get("min_views"))), "max_per_clip": _f(c.get("max_per_clip")),
            "budget": _f(c.get("budget")), "remaining": _f(c.get("remaining")), "used_pct": min(100.0, _f(c.get("used_pct"))),
            "state": st, "starts_at": _when(c.get("starts_at")), "ends_at": _when(c.get("ends_at")), "cover": _cover(c.get("cover")),
            "accent": c["accent"] if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c.get("accent") or "")) else "#C6F432",
            "clippers": int(_f(c.get("clippers"))), "clips": int(_f(c.get("clips"))), "views": int(_f(c.get("views"))),
            "hold_days": int(_f(c.get("hold_days"))), "streams": streams}


def _app_ended(c) -> dict:
    return {"slug": _SLUG.sub("", _s(c["slug"], 80)), "title": _s(c["title"], 120), "creator_name": _s(c.get("creator_name"), 120),
            "category": _s(c.get("category"), 40), "cover": _cover(c.get("cover")),
            "accent": c["accent"] if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c.get("accent") or "")) else "#C6F432",
            "ends_at": _when(c.get("ends_at")), "budget": _f(c.get("budget")), "views": int(_f(c.get("views"))),
            "clips": int(_f(c.get("clips"))), "clippers": int(_f(c.get("clippers"))), "top_views": int(_f(c.get("top_views")))}


def _feed_from_old_site(get) -> dict:
    """A website not updated yet has no /api/earn/public/app: the same is put together from its Earn page and its
    best moments (the streams without best moments can't be known that way)."""
    ov = get("/api/earn/public/overview") or {}
    moments = {c.get("slug"): c for c in ((get("/api/earn/public/moments") or {}).get("campaigns") or []) if isinstance(c, dict)}
    live, ended = [], []
    for c in ov.get("campaigns") or []:
        if not isinstance(c, dict):
            continue
        if c.get("state") in ("open", "starts", "soon"):
            m = moments.get(c.get("slug")) or {}
            live.append({**c, "streams": [{"name": g.get("title"), "url": g.get("url"), "kind": "youtube" if media.is_youtube(str(g.get("url"))) else "other",
                                           "moments": g.get("moments")} for g in (m.get("moments") or []) if isinstance(g, dict)]})
        elif c.get("state") == "ended":
            sc = c.get("showcase") or {}
            ended.append({**c, "views": sc.get("views"), "clips": sc.get("clips"), "clippers": sc.get("clippers"), "top_views": sc.get("top_views")})
    o = ov.get("overview") or {}
    return {"live": live, "ended": ended, "totals": {"paid_out": o.get("paid_out"), "clippers": o.get("clippers"),
                                                    "ended_budget": sum(_f(x.get("budget")) for x in ended), "ended_count": len(ended)}}


@app.get("/api/campaigns")
def app_campaigns(request: Request, fresh: int = 0):
    """The paid campaigns, read from the ClipKontrast website (kept five minutes): what's taking clips now, with every
    stream it can be clipped from, and what ended. Everything from the site is checked and cleaned here; nothing is
    shown when the website can't be reached, and nothing else changes."""
    _me(request)
    hit = _APP_CAMPS.get("v")
    if hit and time.time() - hit[0] < (60 if fresh else 300):
        return hit[1]
    import httpx

    def get(path):
        try:
            r = httpx.get(config.EARN_URL + path, timeout=8, headers={"User-Agent": "ClipKontrast"})
            d = r.json() if r.status_code == 200 else None
            return d if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001 - offline, or not JSON
            return None

    data = get("/api/earn/public/app")
    if data is None or not isinstance(data.get("live"), list):
        data = _feed_from_old_site(get)
    live, ended = [], []
    for c in (data.get("live") or [])[:6]:
        try:
            live.append(_app_live(c))
        except (KeyError, TypeError, AttributeError):
            continue                       # one odd entry is skipped, never the page
    for c in (data.get("ended") or [])[:24]:
        try:
            ended.append(_app_ended(c))
        except (KeyError, TypeError, AttributeError):
            continue
    t = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    out = {"live": live, "ended": ended, "earn_url": config.EARN_URL,
           "totals": {"paid_out": _f(t.get("paid_out")), "clippers": int(_f(t.get("clippers"))),
                      "ended_budget": _f(t.get("ended_budget")) or sum(x["budget"] for x in ended), "ended_count": len(ended)},
           "reached": bool(live or ended or t)}
    _APP_CAMPS["v"] = (time.time(), out)
    return out


_IMG_CACHE: dict = {}


def _img_cached(key: str, fetch):
    """A picture fetched once and kept: in memory (the last 200) and in the working folder (a day)."""
    hit = _IMG_CACHE.get(key)
    if hit:
        return hit
    d = config.TMP_DIR / "pictures"
    f = d / (hashlib.sha1(key.encode()).hexdigest() + ".img")
    data = b""
    try:
        if f.is_file() and time.time() - f.stat().st_mtime < 86400:
            data = f.read_bytes()
    except OSError:
        data = b""
    if not data:
        data = fetch() or b""
        if not data:
            return None
        try:
            d.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        except OSError:
            pass
    kind = _img_kind(data)
    if not kind:
        return None
    if len(_IMG_CACHE) > 200:
        _IMG_CACHE.pop(next(iter(_IMG_CACHE)))
    _IMG_CACHE[key] = (data, kind)
    return _IMG_CACHE[key]


def _img_kind(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _get_img(url: str) -> bytes:
    import httpx
    try:
        r = httpx.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 ClipKontrast"}, follow_redirects=False)
        return r.content if r.status_code == 200 and len(r.content) <= 3_000_000 and _img_kind(r.content) else b""
    except Exception:  # noqa: BLE001
        return b""


def _picture(hit):
    if not hit:
        raise HTTPException(404)
    return Response(hit[0], media_type=hit[1], headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/campaign-cover")
def campaign_cover(request: Request, p: str = ""):
    """A campaign's picture from the ClipKontrast website: only its own campaign pictures, never another address."""
    _me(request)
    if not _COVER_PATH.fullmatch(p or ""):
        raise HTTPException(404)
    return _picture(_img_cached("cover:" + p, lambda: _get_img(config.EARN_URL + p)))


def _yt_id(url: str) -> str:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        m = re.match(r"^/([\w-]{11})", u.path)
    else:
        m = re.match(r"^/(?:live|shorts|embed)/([\w-]{11})", u.path) or re.search(r"(?:^|&)v=([\w-]{11})", u.query)
    return m.group(1) if m else ""


def link_thumb_bytes(url: str) -> bytes:
    """The picture of a video link: a YouTube video's from its id (no asking YouTube about the video), any other
    site's (Kick, Twitch) the one its page names."""
    if media.is_youtube(url):
        vid = _yt_id(url)
        if not vid:
            return b""
        return _get_img(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg") or _get_img(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
    try:
        t = media.preview(url).get("thumb") or ""
    except Exception:  # noqa: BLE001 - no picture: the page shows the site's own badge instead
        return b""
    import base64
    try:
        return base64.b64decode(t.split(",", 1)[1]) if t.startswith("data:image/") else b""
    except (ValueError, IndexError):
        return b""


@app.get("/api/link-thumb")
def link_thumb(request: Request, url: str = ""):
    """The picture of a stream or video link (the campaigns' streams, the admin's stream boxes). Only links to the
    video sites ClipKontrast clips from. On the website, only the admin may ask (it isn't a picture fetcher for all)."""
    me_ = _me(request)
    if config.ACCOUNTS and not me_.is_admin:
        raise HTTPException(404)
    url = (url or "").strip()[:500]
    if not media.site_allowed(url):
        raise HTTPException(404)
    key = "thumb:" + url
    if key not in _IMG_CACHE:
        try:
            auth.throttle(f"thumb:{me_.id}", 60, 60)
        except auth.AuthError:
            raise HTTPException(429, "Too many pictures at once.")
    return _picture(_img_cached(key, lambda: link_thumb_bytes(url)))


@app.get("/api/earn/public/campaigns/{key}")
def earn_campaign(request: Request, key: str):
    return _earn(earn.campaign, key, uid=_uid_or_none(request))


@app.get("/api/earn/me")
def earn_me(request: Request):
    return earn.mine(_me(request).id)


@app.put("/api/earn/profile")
def earn_profile(request: Request, body: dict = Body(...)):
    return _earn(earn.save_profile, _me(request).id, body)


@app.post("/api/earn/campaigns/{key}/join")
def earn_join(request: Request, key: str):
    _need_verified()
    return _earn(earn.join, _me(request).id, key)


@app.post("/api/earn/submissions")
def earn_submit(request: Request, body: dict = Body(...)):
    _need_verified()
    try:
        auth.throttle("earn-submit:" + _me(request).id, 40, 3600)
    except auth.AuthError:
        raise HTTPException(429, "That's a lot of clips in one hour. Wait a bit, then send the rest.")
    return _earn(earn.submit, _me(request).id, str(body.get("campaign", "")), str(body.get("url", "")), str(body.get("note", "")))


@app.delete("/api/earn/submissions/{sid}")
def earn_withdraw(request: Request, sid: int):
    _earn(earn.withdraw, _me(request).id, sid)
    return {"ok": True}


@app.post("/api/earn/submissions/{sid}/proof")
async def earn_proof(request: Request, sid: int, file: UploadFile = File(...)):
    data = await file.read(9 * 1024 * 1024)
    uid = _me(request).id
    return await run_in_threadpool(_earn, earn.save_proof, uid, sid, data)


@app.get("/api/earn/proof/{sid}")
def earn_proof_file(request: Request, sid: int):
    u = _me(request)
    p = _earn(earn.proof_path, sid, u.id, bool(u.is_admin))
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/admin/earn/summary")
def earn_admin_summary(request: Request):
    _admin(request)
    return earn.admin_summary()


@app.get("/api/admin/earn/campaigns")
def earn_admin_campaigns(request: Request):
    _admin(request)
    return earn.campaigns(admin=True)


@app.post("/api/admin/earn/campaigns")
def earn_admin_create(request: Request, body: dict = Body(...)):
    _admin(request)
    return _earn(earn.save_campaign, body)


@app.post("/api/admin/earn/campaigns/order")
def earn_admin_order(request: Request, body: dict = Body(...)):
    _admin(request)
    ids = [str(x)[:40] for x in (body.get("ids") or [])][:500]
    return {"campaigns": _earn(earn.set_order, ids)}


@app.put("/api/admin/earn/campaigns/{cid}")
def earn_admin_update(request: Request, cid: str, body: dict = Body(...)):
    _admin(request)
    return _earn(earn.save_campaign, body, cid)


@app.delete("/api/admin/earn/campaigns/{cid}")
def earn_admin_delete(request: Request, cid: str):
    _admin(request)
    _earn(earn.delete_campaign, cid)
    return {"ok": True}


@app.post("/api/admin/earn/campaigns/{cid}/cover")
async def earn_admin_cover(request: Request, cid: str, file: UploadFile = File(...)):
    _admin(request)
    data = await file.read(9 * 1024 * 1024)
    await run_in_threadpool(_earn, earn.save_cover, cid, data)
    return _earn(earn.campaign, cid, admin=True)


@app.post("/api/update/check")
def update_check(request: Request):
    """The "Check for updates" button: asks right away instead of waiting for the next look."""
    u = getattr(request.state, "user", None)
    if not u or not u.is_admin:
        raise HTTPException(404, "Not found.")
    from . import updates
    return updates.check_now()


@app.post("/api/update/apply")
def update_apply(request: Request):
    u = getattr(request.state, "user", None)
    if not u or not u.is_admin:
        raise HTTPException(404, "Not found.")
    from . import selfupdate
    try:
        return selfupdate.apply()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/update/progress")
def update_progress(request: Request):
    u = getattr(request.state, "user", None)
    if not u or not u.is_admin:
        raise HTTPException(404, "Not found.")
    from . import selfupdate
    return {**selfupdate.progress(), "version": config.APP_VERSION}


@app.get("/api/admin/earn/submissions")
def earn_admin_queue(request: Request, campaign: str = "", status: str = "pending", q: str = ""):
    _admin(request)
    return earn.review_queue(campaign, status, q)


@app.get("/api/admin/earn/submissions.csv")
def earn_admin_queue_csv(request: Request, campaign: str = "", status: str = "", q: str = ""):
    _admin(request)
    return Response(earn.submissions_csv(campaign, status, q), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="clipkontrast-clips-{time.strftime("%Y-%m-%d")}.csv"'})


@app.post("/api/admin/earn/submissions/bulk")
def earn_admin_bulk(request: Request, body: dict = Body(...)):
    _admin(request)
    done, problems = 0, []
    for sid in [int(x) for x in (body.get("ids") or [])][:500]:
        try:
            earn.decide(sid, str(body.get("action", "")), str(body.get("note", "")))
            done += 1
        except earn.EarnError as e:
            problems.append(f"#{sid}: {e}")
    return {"done": done, "problems": problems[:20]}


@app.post("/api/admin/earn/submissions/{sid}")
def earn_admin_decide(request: Request, sid: int, body: dict = Body(...)):
    _admin(request)
    return _earn(earn.decide, sid, str(body.get("action", "")), str(body.get("note", "")), body.get("views"))


@app.post("/api/admin/earn/submissions/{sid}/views")
def earn_admin_views(request: Request, sid: int, body: dict = Body(...)):
    _admin(request)
    return _earn(earn.set_views, sid, body.get("views"))


@app.post("/api/admin/earn/submissions/{sid}/proof")
async def earn_admin_proof(request: Request, sid: int, file: UploadFile = File(...)):
    u = _admin(request)
    data = await file.read(9 * 1024 * 1024)
    return await run_in_threadpool(_earn, earn.save_proof, u.id, sid, data, True)


@app.post("/api/admin/earn/refresh")
def earn_admin_refresh(request: Request, body: dict = Body(default={})):
    _admin(request)
    if not earn.youtube_key():
        raise HTTPException(400, "Add a YouTube API key in Earn settings first.")
    return {"updated": _earn(earn.refresh_views, [int(x) for x in (body.get("ids") or [])] or None)}


@app.get("/api/admin/earn/clippers")
def earn_admin_clippers(request: Request):
    _admin(request)
    return {"clippers": earn.clippers_admin()}


@app.get("/api/admin/earn/owed")
def earn_admin_owed(request: Request):
    _admin(request)
    return {"owed": earn.owed(), "payouts": earn.payouts()}


@app.get("/api/admin/earn/owed.csv")
def earn_admin_owed_csv(request: Request):
    _admin(request)
    return Response(earn.owed_csv(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="clipkontrast-payouts-{time.strftime("%Y-%m-%d")}.csv"'})


@app.post("/api/admin/earn/payouts")
def earn_admin_pay(request: Request, body: dict = Body(...)):
    u = _admin(request)
    return _earn(earn.mark_paid, str(body.get("user_id", "")), str(body.get("ref", "")), u.id)


@app.get("/api/admin/earn/settings")
def earn_admin_settings(request: Request):
    _admin(request)
    return earn.public_settings()


@app.put("/api/admin/earn/settings")
def earn_admin_save_settings(request: Request, body: dict = Body(...)):
    _admin(request)
    return _earn(earn.save_settings, str(body.get("youtube_key", "")))




def _speech_lang(lang: str) -> str:
    from . import fetch_model
    if not fetch_model.known(lang):
        raise HTTPException(400, "PapiClips has no speech model for that language.")
    return lang


@app.get("/api/speech")
def speech_list(request: Request):
    _admin(request)
    from . import fetch_model
    return {"models": [{"lang": k, "label": m[0], "size_mb": m[3], "credit": m[4], "state": fetch_model.status(k),
                        "percent": fetch_model.state[k]["percent"], "error": fetch_model.state[k]["error"],
                        "default": k == fetch_model.DEFAULT} for k, m in fetch_model.MODELS.items()]}


@app.post("/api/speech/{lang}")
def speech_download(request: Request, lang: str):
    _admin(request)
    from . import fetch_model
    fetch_model.start(_speech_lang(lang))
    ctx.bump()
    return speech_list(request)


@app.delete("/api/speech/{lang}")
def speech_remove(request: Request, lang: str):
    _admin(request)
    from . import fetch_model
    try:
        fetch_model.remove(_speech_lang(lang))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    ctx.bump()
    return speech_list(request)


@app.post("/api/accounts/google/setup")
def google_setup_easy(body: dict = Body(...)):
    raw = body.get("json") or body
    cid = ""
    secret = ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            pass
    if isinstance(raw, dict):
        web = raw.get("web") or raw.get("installed") or raw
        cid = web.get("client_id") or raw.get("client_id") or ""
        secret = web.get("client_secret") or raw.get("client_secret") or ""
    cid = str(cid or body.get("client_id") or "").strip()
    secret = str(secret or body.get("client_secret") or "").strip()
    if not cid or not secret:
        raise HTTPException(400, "Could not find client_id and client_secret in the provided data.")
    
    accounts.save_app("youtube", cid, secret)
    env_file = config.DATA_DIR.parent / ".env"
    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            if "GOOGLE_CLIENT_ID=" in content:
                content = re.sub(r"GOOGLE_CLIENT_ID=.*", f"GOOGLE_CLIENT_ID={cid}", content)
            else:
                content += f"\nGOOGLE_CLIENT_ID={cid}"
            if "GOOGLE_CLIENT_SECRET=" in content:
                content = re.sub(r"GOOGLE_CLIENT_SECRET=.*", f"GOOGLE_CLIENT_SECRET={secret}", content)
            else:
                content += f"\nGOOGLE_CLIENT_SECRET={secret}"
            env_file.write_text(content, encoding="utf-8")
        except Exception:
            pass
    
    return {"ok": True, "connect_url": "/oauth/youtube/start"}


@app.get("/api/youtube-cookies")
def yt_cookies_get(request: Request):
    _admin(request)
    f = config.YT_COOKIES
    return {"saved": f.is_file(), "updated": int(f.stat().st_mtime) if f.is_file() else 0}


@app.post("/api/youtube-cookies")
async def yt_cookies_upload(request: Request, file: UploadFile = File(None)):
    _admin(request)
    if not file or not file.filename:
        raise HTTPException(400, "Choose your cookies.txt file first.")
    body = await file.read()
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(413, "Cookies file is too large.")
    lines = body.decode("utf-8", errors="replace").splitlines()
    valid = any(not l.startswith("#") and "	youtube.com	" in l.lower() for l in lines)
    if not valid:
        raise HTTPException(400, "That file doesn't look like exported YouTube cookies (Netscape format).")
    config.YT_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    config.YT_COOKIES.write_bytes(body)
    return {"ok": True}


@app.delete("/api/youtube-cookies")
def yt_cookies_delete(request: Request):
    _admin(request)
    if config.YT_COOKIES.is_file():
        config.YT_COOKIES.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/accounts/youtube/status")
def youtube_status():
    yt_accs = [a for a in accounts.list_accounts() if a["platform"] == "youtube"]
    can_conn = accounts.can_connect("youtube")
    return {
        "connected": len(yt_accs) > 0,
        "accounts": yt_accs,
        "can_connect": can_conn
    }


@app.post("/api/posts/bulk")
def add_bulk_posts(body: dict = Body(...)):
    account_id = str(body.get("account_id") or "").strip()
    acc = accounts.get(account_id) if account_id else None
    if not acc:
        yt_accs = [a for a in accounts.list_accounts() if a["platform"] == "youtube"]
        if not yt_accs:
            raise HTTPException(400, "No connected YouTube account found. Connect an account first.")
        acc = yt_accs[0]
        account_id = acc["id"]

    clips = body.get("clips") or []
    if not clips:
        raise HTTPException(400, "No clips provided.")

    scheduled_start = float(body.get("scheduled_start") or time.time())
    interval_hours = float(body.get("interval_hours") or 24.0)

    created_posts = []
    for i, c in enumerate(clips):
        post_time = scheduled_start + (i * interval_hours * 3600)
        p = posting.create_post(
            account_id=account_id,
            video_path=c.get("video_path") or c.get("path") or "",
            title=c.get("title") or "PapiClips Short",
            description=c.get("description") or "",
            tags=c.get("hashtags") or [],
            scheduled_for=post_time,
            clip_id=c.get("id") or ""
        )
        created_posts.append(p)

    return {"ok": True, "created": len(created_posts), "posts": created_posts}


def main():
    print(f"\n  PapiClips is running:  {config.PUBLIC_URL}\n")
    if not config.ACCOUNTS and (config.HTTPS or config.HOST not in ("127.0.0.1", "localhost", "::1")):
        print("  Sign-in is off, so anyone who can reach this address can use PapiClips and its AI key.\n"
              "  On a server other people can reach, set CLIPKONTRAST_ACCOUNTS=1 in .env.\n", flush=True)
    if (config.env("PAPICLIPS_OPEN_BROWSER") or config.env("CLIPKONTRAST_OPEN_BROWSER")) == "1":
        _open_browser_when_ready()
    if (config.env("PAPICLIPS_AUTO_RELOAD") or config.env("CLIPKONTRAST_AUTO_RELOAD")) == "1":
        # restart by itself whenever the server code changes (pages in web/ never need a restart)
        uvicorn.run("studio.server:app", host=config.HOST, port=config.PORT, log_level="warning", backlog=4096, reload=True,
                    reload_dirs=[str(config.ROOT / "studio")], reload_includes=["*.py"],
                    proxy_headers=bool((config.env("PAPICLIPS_TRUST_PROXY") or config.env("CLIPKONTRAST_TRUST_PROXY"))), forwarded_allow_ips="*")
        return
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning", backlog=4096,
                proxy_headers=bool((config.env("PAPICLIPS_TRUST_PROXY") or config.env("CLIPKONTRAST_TRUST_PROXY"))), forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
