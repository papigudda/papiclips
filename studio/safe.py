"""Small helpers for what is safe to show a user."""
import re

_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s'\"]*[\\/][^\s'\"]+")


def friendly(e, limit=600) -> str:
    """An error message for the user: trimmed, with any server file paths removed. A missing ffmpeg, which Windows
    reports only as "the system cannot find the file specified", is said in plain words with what to do."""
    from . import tools
    text = str(e)
    if isinstance(e, MemoryError) or re.search(r"allocate memory|bad_alloc|out of memory|MemoryError|Unable to allocate", text, re.I):
        return ("Your computer ran out of memory while working on this video. Close other programs (browser tabs, "
                "games), then try again. PapiClips needs about 4 GB of free memory; a long stream needs more.")
    if re.search(r"WinError 4551|Application Control policy", text, re.I):
        return ("Windows blocked one of PapiClips's files (Smart App Control, which stops apps that aren't signed "
                "yet). Open Windows Security, App & browser control, Smart App Control settings, and turn it off; or add "
                "PapiClips's folder to the exclusions of your antivirus. Then restart PapiClips and try again.")
    if re.search(r"WinError (5|32)\b|Access is denied|being used by another process", text, re.I):
        return ("Another program was holding one of PapiClips's files (usually an antivirus scanning it). Try again; "
                "if it keeps happening, add PapiClips's folders to your antivirus's exclusions: the app's own "
                "folder and the folder your videos and clips are kept in (Settings, Storage).")
    # Only YouTube's own answer counts, not a note one of the other ways of asking left behind it ("(tried 5 ways) (...)").
    main = text.split(" (tried ", 1)[0]
    if re.search(r"This live event will begin|Premieres in|is_upcoming", main, re.I):
        return "This stream or premiere hasn't started yet. Clip it once it's live (Clip a live stream), or after it's over."
    if re.search(r"live event has ended|live stream recording is not available|post.live", main, re.I):
        return POST_LIVE
    if re.search(r"timed out|Remote end closed connection|Connection reset|forcibly closed|IncompleteRead|"
                 r"Connection aborted|Connection broken|getaddrinfo failed|Temporary failure in name resolution", main, re.I):
        # the site's own words kept after it, short, so a bug report still says what really happened
        said = _PATH.sub("[file]", re.sub(r"\x1b\[[0-9;]*m", "", main)).strip().splitlines()
        said = re.sub(r"^(ERROR:\s*)?(\[download\]\s*)?(Got error:\s*)?", "", said[-1] if said else "")[:140]
        return NET_DROP + (f" (Details: {said})" if said else "")
    return tools.explain(e) or _PATH.sub("[file]", text[-limit:])


POST_LIVE = ("YouTube hasn't made this stream's replay available yet, so it can't be downloaded. After a long stream YouTube "
             "can take many hours to finish the replay, and for a stream of 12 hours or more it may keep only part of it, "
             "or none. What works now: upload the video file instead (the raw stream or clips from Drive), or clip it "
             "live next time with Clip a live stream.")

NET_DROP = ("The connection kept dropping while this video was downloading, even after many tries. Check your internet "
            "(a VPN or a busy Wi-Fi often causes it), then try again. For a very long stream, use Exact moment so only "
            "the part you want is downloaded, or upload the video file instead.")


_LINK_WHY = (
    (r"private video|video is private", "This video is private, so it can't be opened. Ask for a public link."),
    (r"members.only|join this channel|available to this channel's members", "This video is for the channel's members only, so it can't be downloaded."),
    (r"confirm your age|age.restricted|inappropriate for some users", "YouTube only shows this video to signed-in adults, so it can't be downloaded."),
    (r"not (made this video )?available in your country|geo.?restrict|blocked it in your country", "This video isn't available in your country."),
    (r"live event will begin|premieres in|is_upcoming|this live event", "This stream or premiere hasn't started yet. Clip it once it's live, or after."),
    (r"has been removed|account .*terminated|no longer available|video unavailable|this video is unavailable", "YouTube says this video is unavailable: it was removed, made private, or blocked."),
    (r"http error 429|too many requests", "YouTube asked PapiClips to slow down. Wait a few minutes, then try again."),
    (r"unsupported url|is not a valid url|no video formats found", "That isn't a link to a single video. Open the video itself and copy its link."),
    (r"requested format is not available|only images are available", "YouTube didn't offer this video's picture and sound right now. Try again in a few minutes."),
    (r"timed out|connection reset|remote end closed|getaddrinfo failed|name resolution|network is unreachable", "The internet connection dropped while opening the link. Check your internet, then try again."),
)


def link_reason(e) -> str:
    """Why a video link couldn't be opened, in plain words (the video site's own reason, not a guess)."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    for pat, why in _LINK_WHY:
        if re.search(pat, text, re.I):
            return why
    short = _PATH.sub("[file]", re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*[\w-]*:\s*)?", "", text.strip().splitlines()[-1] if text.strip() else ""))[:180]
    return "Couldn't open that video" + (f": {short}" if short else ". Check the link is public and complete.")
