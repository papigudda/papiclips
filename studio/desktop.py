"""ClipKontrast as a desktop app on Windows: its own window, and no black console window behind it.

The desktop and Start menu shortcuts run `ClipKontrast.exe -m studio.desktop` (a copy of pythonw, which has no
console, with ClipKontrast's name and icon). This starts the ClipKontrast server hidden, with its output in
data/logs/clipkontrast.log, and shows ClipKontrast in a window of its own, made with pywebview on Microsoft's WebView2
(the web engine Windows apps like Teams are built on; it comes with Windows 11 and with Edge on Windows 10). Its title
bar, taskbar button and icon are ClipKontrast's. Closing the window stops ClipKontrast.

Without WebView2, Edge's or Chrome's app mode is used instead (a window with no address bar), and without either,
the normal browser.

It also does what the console launcher (ClipKontrast.cmd, kept for troubleshooting) did: swaps in an update that was
downloaded (app.new) and starts again, or runs the downloaded installer. See studio/selfupdate.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[2]           # <install>\app\studio\desktop.py
APP = INSTALL / "app"
DATA = INSTALL / "data"
PORT = int(os.environ.get("CLIPKONTRAST_PORT") or 8765)
URL = f"http://127.0.0.1:{PORT}/app"
LOG = DATA / "logs" / "clipkontrast.log"
RESTART_CODE, INSTALLER_CODE = 75, 76                     # the same as studio/selfupdate.py
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env() -> dict:
    env = dict(os.environ)
    env.update({"CLIPKONTRAST_INSTALLED": "1", "CLIPKONTRAST_INSTALL_DIR": str(INSTALL),
                "CLIPKONTRAST_DEFAULT_DATA": str(DATA), "CLIPKONTRAST_STORAGE_FILE": str(INSTALL / "storage.json"),
                "CLIPKONTRAST_AUTO_RELOAD": "0", "CLIPKONTRAST_OPEN_BROWSER": "0", "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1", "PYTHONWARNINGS": "ignore::DeprecationWarning"})
    env["PATH"] = os.pathsep.join([str(INSTALL / "ffmpeg" / "bin"), str(INSTALL / "python"),
                                   str(INSTALL / "python" / "Scripts"), env.get("PATH", "")])
    return env


def _message(text: str, error: bool = False):
    """A plain Windows message box (there is no console to print to)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "ClipKontrast", 0x10 if error else 0x40)
    else:
        print(text)


def _tail(lines: int = 12) -> str:
    try:
        return "\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def _swap():
    """An update downloaded while ClipKontrast ran: put the new code in place (the old one kept aside)."""
    new, old = INSTALL / "app.new", INSTALL / "app.old"
    if (new / "studio" / "server.py").is_file():
        try:
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            APP.rename(old)
            new.rename(APP)
        except OSError:
            pass
    if not (APP / "studio" / "server.py").is_file() and (old / "studio" / "server.py").is_file():
        shutil.rmtree(APP, ignore_errors=True)     # the swap couldn't finish: the old code goes back
        old.rename(APP)


def _browser():
    """Microsoft Edge or Google Chrome, which can show a page as an app window."""
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for r in roots:
            if r and Path(r, rel).is_file():
                return str(Path(r, rel))
    return shutil.which("msedge") or shutil.which("chrome")


def _open_window(url: str):
    """ClipKontrast's own window. Returns its process (to know when it is closed), or None if none could be opened."""
    fake = os.environ.get("CLIPKONTRAST_WINDOW_CMD")         # the build's test stands in a process for the window
    if fake:
        return subprocess.Popen(fake, creationflags=NO_WINDOW)
    exe = _browser()
    if not exe:
        return None
    profile = DATA / "window"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                             "--no-default-browser-check", "--window-size=1380,880", "--hide-crash-restore-bubble",
                             "--disable-features=Translate"])


def _note(text: str):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} desktop: {text}\n")
    except OSError:
        pass


def _webview2() -> bool:
    """Is Microsoft's WebView2 there (and .NET 4.6.2, which pywebview needs)? Without it pywebview would fall back
    to Internet Explorer's engine, which can't show ClipKontrast, so the app window is used instead."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as k:
            if winreg.QueryValueEx(k, "Release")[0] < 394802:
                return False
        key = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"            # the WebView2 Runtime
        for hive, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{key}"),
                           (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{key}"),
                           (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{key}")):
            try:
                with winreg.OpenKey(hive, path) as k:
                    v = str(winreg.QueryValueEx(k, "pv")[0])
                if v and v != "0.0.0.0" and int(v.split(".")[0]) >= 86:
                    return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return False
    return False


def _native_ok() -> bool:
    if os.environ.get("CLIPKONTRAST_WINDOW_CMD") or os.environ.get("CLIPKONTRAST_WINDOW") == "app":
        return False
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return _webview2()


LOADING = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#1E1612;color:#F4EEEA;font:16px 'Segoe UI',system-ui,sans-serif}
body{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:18px}
b{font-size:28px;letter-spacing:-.5px}b i{font-style:normal;background:#C6F432;color:#1A2100;padding:0 6px;border-radius:6px}
p{margin:0;color:#A99A91;max-width:440px;text-align:center;line-height:1.5}
.dot{width:10px;height:10px;border-radius:50%;background:#C6F432;animation:p 1s ease-in-out infinite alternate}
@keyframes p{from{opacity:.25}}</style></head><body><b>Clip<i>Kontrast</i></b><div class="dot"></div>
<p>MESSAGE</p></body></html>"""


def _page(message: str) -> str:
    import html
    return LOADING.replace("MESSAGE", html.escape(message).replace("\n", "<br>"))


def _starting() -> tuple:
    """What the server is busy with while it starts, read from the end of its log: (words for the window, whether it
    is moving the folder). Moving videos to another drive can take many minutes, and must not be given up on."""
    lines = [x.strip() for x in _tail(40).splitlines() if x.strip()]
    start = max((i for i, x in enumerate(lines) if x.startswith("---- ClipKontrast starting")), default=-1)
    lines = lines[start + 1:]
    moving = next((x for x in lines if x.startswith("Moving ClipKontrast's folder to ")), "")
    if moving and not any(x == "Moved." for x in lines):
        where = moving[len("Moving ClipKontrast's folder to "):].split(" (")[0]
        step = next((x for x in reversed(lines) if x.startswith("Moved ") and " of " in x), "")
        return (f"Moving your ClipKontrast folder to {where}…\nYour videos and clips are being copied to the new place "
                f"you chose in Settings. This is done once and can take several minutes. Please don't close this window."
                + (f"\n\n{step.split(':')[0]}" if step else ""), True)
    return "Starting ClipKontrast…\nThis can take up to a minute.", False


def _wait_for_server(server, show=None) -> None:
    """Wait until the server answers (or stops). While it moves the folder there is no time limit."""
    began, said = time.time(), ""
    while not _up() and server.poll() is None:
        text, moving = _starting()
        if not moving and time.time() - began > 600:
            return
        if show and time.time() - began > 3 and text != said:
            show(text)
            said = text
        time.sleep(0.5)


def _run_native():
    """ClipKontrast in its own pywebview window; the server is looked after on a second thread."""
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True                 # the Download buttons save clips as usual
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Instagram, YouTube, the Earn site: the normal browser
    ico = INSTALL / "clipkontrast.ico"
    window = webview.create_window("ClipKontrast", html=_page("Starting ClipKontrast…"), width=1380, height=880,
                                   min_size=(960, 640), background_color="#1E1612", text_select=True)
    state = {"server": None, "closing": False}

    def supervise():
        first = True
        while not state["closing"]:
            _swap()
            server = state["server"] = _start_server()
            _wait_for_server(server, lambda text: window.load_html(_page(text)))
            if server.poll() is not None and server.returncode not in (RESTART_CODE, INSTALLER_CODE):
                window.load_html(_page("ClipKontrast couldn't start.\n\n" + _tail(8)))
                return
            if first and server.poll() is None:
                window.load_url(URL)
                first = False
            server.wait()
            if state["closing"]:
                return
            if server.returncode == RESTART_CODE:          # updated: started again with the new code
                continue
            if server.returncode == INSTALLER_CODE:
                subprocess.Popen([str(INSTALL / "update" / "ClipKontrast-Setup.exe"), "/SILENT", "/SUPPRESSMSGBOXES",
                                  "/NORESTART"])
                window.destroy()
                return
            window.load_html(_page("ClipKontrast stopped unexpectedly. Close this window and open it again.\n\n"
                                   + _tail(8)))
            return

    _note("window: native")
    webview.start(supervise, private_mode=False, storage_path=str(DATA / "window"),
                  icon=str(ico) if ico.is_file() else None)
    state["closing"] = True                                      # the window was closed: ClipKontrast closes with it
    _stop(state["server"])
    os._exit(0)


def _start_server():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.is_file() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG.with_suffix(".old.log"))
    out = open(LOG, "a", encoding="utf-8", errors="replace")
    out.write(f"\n---- ClipKontrast starting {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    out.flush()
    py = INSTALL / "python" / "python.exe"
    return subprocess.Popen([str(py if py.is_file() else sys.executable), "-m", "studio.server"], cwd=str(INSTALL),
                            env=_env(), stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)


def _stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    if sys.platform == "win32":
        try:                       # the taskbar shows ClipKontrast's own button and icon, not Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipKontrast.App")
        except Exception:  # noqa: BLE001
            pass
    if _up():                      # already running (the icon was clicked again): just open its window
        if _native_ok():
            import webview
            webview.create_window("ClipKontrast", URL, width=1380, height=880, min_size=(960, 640))
            ico = INSTALL / "clipkontrast.ico"
            webview.start(private_mode=False, storage_path=str(DATA / "window"), icon=str(ico) if ico.is_file() else None)
        elif not _open_window(URL):
            webbrowser.open(URL)
        return
    if _native_ok():
        try:
            _run_native()
            return
        except Exception as e:  # noqa: BLE001 - the app window below instead
            _note(f"native window failed ({e}), using the app window")
    _note("window: app" if _browser() or os.environ.get("CLIPKONTRAST_WINDOW_CMD") else "window: browser")
    window = None
    while True:
        _swap()
        server = _start_server()
        _wait_for_server(server)
        if server.poll() is not None and server.returncode not in (RESTART_CODE, INSTALLER_CODE):
            _message("ClipKontrast couldn't start.\n\n" + _tail(), error=True)
            return
        if window is None and server.poll() is None:
            window = _open_window(URL)
            if window is None:     # no Edge or Chrome: the usual browser, and this box to close ClipKontrast
                webbrowser.open(URL)
                _message("ClipKontrast is open in your browser.\n\nPress OK when you're done to close ClipKontrast.")
                _stop(server)
                return
        while server.poll() is None:
            if window and window.poll() is not None:   # the window was closed: ClipKontrast closes with it
                _stop(server)
                return
            time.sleep(0.5)
        code = server.returncode
        if code == RESTART_CODE:   # updated: the new code is swapped in, and the open window reconnects by itself
            continue
        if code == INSTALLER_CODE:
            setup = INSTALL / "update" / "ClipKontrast-Setup.exe"
            _stop(window)
            subprocess.Popen([str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
            return
        _stop(window)
        _message("ClipKontrast stopped unexpectedly.\n\n" + _tail(), error=True)
        return


if __name__ == "__main__":
    main()
