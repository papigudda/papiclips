"""Speech to text for Hindi and other languages, with Whisper (through faster-whisper), on this computer.

The main speech model (Parakeet, studio/transcribe.py) only understands English. A Hindi stream fed to it comes out
as English-sounding nonsense, so its captions were useless unless YouTube had its own Hindi transcript. Whisper
understands Hindi, Urdu, Bengali, Tamil and the other Indian languages, tells which language is spoken, and gives
the time of every word, which the captions need.

It is installed the first time it is needed, not with the rest of ClipKontrast: it is a large download (the library
about 200 MB, the model about 500 MB), and a person who only clips English streams never needs it. After that it
works offline like the rest.

Captions in Hindi come out of Whisper in Devanagari; studio/roman.py and the AI turn them into Hinglish, as before.

Hinglish captions: a Whisper trained on Indian Hindi to write it in Roman letters ("bhai ye kya ho gaya"), Oriserve's
Whisper-Hindi2Hinglish-Apex (Apache-2.0), converted for faster-whisper by .github/workflows/hinglish-model.yml and
downloaded once (about 800 MB) from this project's releases, checked against its SHA256SUMS. Once it is here, each
Hindi clip's captions are written by it straight from the clip's sound: no Devanagari to spell out, no AI needed, so
they come out right even when the AI key is out of credit. Until it has downloaded (in the background, the first time
a Hindi video is clipped), captions are made the old way.
"""
from __future__ import annotations

import importlib
import re
import os
import subprocess
import sys
import threading
import time

import numpy as np

from . import config

# The spoken languages a person can pick. "auto" lets Whisper tell (or YouTube, when it knows).
LANGUAGES = {"auto": "Auto-detect", "en": "English", "hi": "Hindi / Hinglish", "ur": "Urdu", "bn": "Bengali",
             "mr": "Marathi", "gu": "Gujarati", "pa": "Punjabi", "ta": "Tamil", "te": "Telugu", "kn": "Kannada",
             "ml": "Malayalam"}
# What the captions are written in.
CAPTION_LANGS = {"auto": "As spoken (Hinglish)", "deva": "Hindi script (हिंदी)", "en": "Always English"}

PACKAGE = "faster-whisper==1.2.1"
# "small" is quick on an ordinary computer and good at Hindi; "large-v3-turbo" is better and slower.
MODEL = (config.env("CLIPKONTRAST_WHISPER_MODEL") or "small").strip()
# The quick model that listens to a long video only to find its best moments, several times faster than MODEL on an
# ordinary computer. The captions of the chosen clips are then written by MODEL, so they are as good as before.
SCAN_MODEL = (config.env("CLIPKONTRAST_WHISPER_SCAN_MODEL") or "base").strip()
ENABLED = config.env("CLIPKONTRAST_WHISPER", "1") != "0"
BEAM = max(1, int(config.env("CLIPKONTRAST_WHISPER_BEAM") or "1"))     # 5 is a little more accurate, about twice as slow
THREADS = int(config.env("CLIPKONTRAST_WHISPER_THREADS") or max(2, (os.cpu_count() or 4) - 2))
MODEL_DIR = config.MODELS_DIR / "whisper"
SR = 16000

# The Hinglish caption model (see above). The files, the release they come from, and where they are kept.
HL_TAG = (config.env("PAPICLIPS_HINGLISH_TAG") or "hinglish-model-v1").strip()
HL_URL = (config.env("PAPICLIPS_HINGLISH_URL") or "https://huggingface.co/Systran/faster-whisper-small/resolve/main/").strip().rstrip("/") + "/"
HL_FILES = ("config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json", "model.bin")
HL_DIR = config.MODELS_DIR / "whisper" / HL_TAG
HL_ON = ENABLED and config.env("CLIPKONTRAST_HINGLISH", "1") != "0"
HL_NEED_FREE = 2 * 1024 ** 3                 # the download (about 0.8 GB) with room to spare
hl_state = {"doing": False, "percent": 0, "error": "", "at": 0.0}
_hl_lock = threading.Lock()

_lock = threading.Lock()          # one install and one model load at a time
_run_lock = threading.Lock()      # one transcription at a time: a second copy of the model would double the memory
_models: dict = {}


def installed() -> bool:
    try:
        importlib.import_module("faster_whisper")
        return True
    except Exception:  # noqa: BLE001 - not installed, or broken: either way it is not usable
        return False


def install(note=None) -> bool:
    """Install faster-whisper into this ClipKontrast's own Python, once. Returns True when it can be used."""
    if not ENABLED:
        return False
    with _lock:
        if installed():
            return True
        if note:
            note("Installing Hindi and multi-language support (once, about 200 MB)")
        try:
            p = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", PACKAGE],
                               capture_output=True, text=True, timeout=1800)
        except Exception as e:  # noqa: BLE001
            print(f"  Couldn't install Whisper: {e}", flush=True)
            return False
        if p.returncode != 0:
            print(f"  Couldn't install Whisper: {(p.stderr or p.stdout)[-600:]}", flush=True)
            return False
        importlib.invalidate_caches()
        return installed()


def ready(note=None) -> bool:
    """Installed (installing it now if needed)."""
    return ENABLED and (installed() or install(note))


def _get(note=None, name=None):
    """A model, loaded once. The first time, it is downloaded into ClipKontrast's models folder."""
    name = name or MODEL
    with _lock:
        if name not in _models:
            from faster_whisper import WhisperModel
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            if note and not any(MODEL_DIR.glob(f"*{name}*")):
                note("Downloading the quick Hindi speech model (once, about 150 MB)" if name != MODEL else
                     "Downloading the Hindi speech model (once, about 500 MB)")
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=THREADS,
                                         download_root=str(MODEL_DIR))
        return _models[name]


def _float(audio: np.ndarray) -> np.ndarray:
    return audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio.astype(np.float32)


def _loudest(audio: np.ndarray, seconds: int = 30) -> np.ndarray:
    """The liveliest stretch of the audio: someone talking, not the silent start of a stream."""
    n = len(audio)
    size = seconds * SR
    if n <= size:
        return audio
    step = SR * 5
    best, at = -1.0, 0
    for a in range(0, n - size, step):
        x = audio[a:a + size:160].astype(np.float32)
        v = float((x * x).mean())
        if v > best:
            best, at = v, a
    return audio[at:at + size]


INDIC_SCRIPT = re.compile(r"[\u0900-\u097F\u0600-\u06FF]")     # Devanagari and Urdu letters


def _lively(audio: np.ndarray, seconds: int = 30, n: int = 3) -> list:
    """Up to n of the liveliest stretches, from different parts of the audio (start, middle, end)."""
    size = seconds * SR
    if len(audio) <= size * n:
        return [_loudest(audio, seconds)]
    third = len(audio) // n
    return [_loudest(audio[i * third:(i + 1) * third], seconds) for i in range(n)]


def decide(samples) -> tuple:
    """Which language, from Whisper's look at a few stretches: [(how likely each language is, the words it wrote)].

    A stretch with no real speech in it (game sound, music) says nothing: Whisper names some language for it anyway,
    often Urdu, and that once turned whole English streams into "Hindi". So only stretches where words were heard
    vote, and what was written decides: Whisper writes Hindi in Devanagari (or Urdu script) and English in English
    letters. With no speech anywhere, the likelihoods alone decide, and only a clear Hindi answer counts."""
    def words(t):
        return re.findall(r"\w+", t or "")
    speech = [(p, t) for p, t in samples if len(words(t)) >= 3]
    if not speech:
        total: dict = {}
        for p, _t in samples:
            for k, v in (p or {}).items():
                total[k] = total.get(k, 0.0) + float(v or 0) / max(1, len(samples))
        if not total:
            return "en", 0.0
        hindi = total.get("hi", 0.0) + total.get("ur", 0.0)
        top = max(total, key=total.get)
        return ("hi", hindi) if hindi >= 0.5 else ("en" if top in ("hi", "ur") else top, total[top])
    votes: dict = {}
    for p, t in speech:
        letters = [c for c in t if c.isalpha()]
        indic = sum(1 for c in letters if INDIC_SCRIPT.match(c))
        p = p or {}
        top = max(p, key=p.get) if p else "en"
        if letters and indic >= 0.3 * len(letters):
            v = "hi"                                       # written in Hindi or Urdu letters: Hindi was spoken
        elif top in ("hi", "ur"):
            v = "hi" if p.get("hi", 0) + p.get("ur", 0) >= 0.6 else "en"
        else:
            v = top
        votes[v] = votes.get(v, 0) + 1
    lang = max(votes, key=lambda k: (votes[k], k == "en"))   # a tie goes to English: its captions are the safer guess
    return lang, votes[lang] / len(speech)


def _probe(m, audio: np.ndarray):
    """Whisper's look at one stretch: (how likely each language is, the first words it wrote)."""
    with _run_lock:
        segs, info = m.transcribe(_float(audio), language=None, beam_size=1, vad_filter=True, without_timestamps=True,
                                  condition_on_previous_text=False, max_new_tokens=40)
        text = " ".join((getattr(s, "text", "") or "") for s in segs)
    probs = dict(getattr(info, "all_language_probs", None) or [(info.language or "en", info.language_probability or 1.0)])
    return probs, text


def detect(audio: np.ndarray, note=None):
    """(language code, how sure from 0 to 1) of the speech in this audio, or None if Whisper can't be used.
    Three lively half minutes from across it are listened to (see decide())."""
    if not ready(note):
        return None
    m = _get(note)
    return decide([_probe(m, part) for part in _lively(audio)])


def clip_language(audio: np.ndarray):
    """The language spoken in one clip's sound ("hi", "en", ...), or None when Whisper isn't installed. Asked of each
    clip of a Hindi video before its captions are written, so an English clip never goes to the Hindi model."""
    if not (ENABLED and installed()):
        return None
    try:
        m = _get()
        mid = len(audio) // 2
        part = audio[max(0, mid - 15 * SR): mid + 15 * SR]
        return decide([_probe(m, part)])[0]
    except Exception:  # noqa: BLE001 - can't tell: the video's language is used
        return None


# ---------------------------------------------------------------- Hinglish captions

def hinglish_ready() -> bool:
    """Is the whole Hinglish model here (every file, checked when it was downloaded)?"""
    return HL_ON and (HL_DIR / ".ok").is_file() and all((HL_DIR / f).is_file() for f in HL_FILES)


def _sha256(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_hinglish() -> bool:
    """Download the Hinglish model, once, checking every file against the release's SHA256SUMS. True when it's here.
    A cut-off download carries on from where it stopped the next time."""
    if hinglish_ready():
        return True
    if not HL_ON or not _hl_lock.acquire(blocking=False):
        return False                                   # off, or another download is already at it
    from . import fetch_model
    try:
        hl_state.update(doing=True, percent=0, error="", at=time.time())
        import shutil
        part = HL_DIR.with_name(HL_DIR.name + ".part")
        part.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(part).free < HL_NEED_FREE:
            raise RuntimeError("The drive is almost full. Free up about 2 GB for the Hinglish captions model.")
        sums_file = part / "SHA256SUMS"
        sums_file.unlink(missing_ok=True)
        fetch_model._download(HL_URL + "SHA256SUMS", sums_file, (0, 1), hl_state)
        sums = {}
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            bits = line.split()
            if len(bits) == 2 and len(bits[0]) == 64:
                sums[bits[1].lstrip("*")] = bits[0].lower()
        if set(HL_FILES) - set(sums):
            raise RuntimeError("The model's file list is incomplete.")
        shares = {"config.json": (1, 2), "tokenizer.json": (2, 3), "vocabulary.json": (3, 4),
                  "preprocessor_config.json": (4, 5), "model.bin": (5, 99)}
        for name in HL_FILES:
            dest = part / name
            if dest.is_file() and _sha256(dest) == sums[name]:
                continue                               # already here from an earlier try
            fetch_model._download(HL_URL + name, dest, shares[name], hl_state)
            if _sha256(dest) != sums[name]:
                dest.unlink(missing_ok=True)           # damaged: downloaded again next time, from the start
                raise RuntimeError(f"The download of {name} was damaged. It will be downloaded again.")
        (part / ".ok").write_text(HL_TAG, encoding="utf-8")
        shutil.rmtree(HL_DIR, ignore_errors=True)
        part.rename(HL_DIR)
        hl_state.update(percent=100)
        print("  Hinglish captions model ready.", flush=True)
        return True
    except Exception as e:  # noqa: BLE001 - captions are made the old way meanwhile; it is tried again later
        hl_state["error"] = fetch_model._why(e) if not isinstance(e, RuntimeError) else str(e)
        print(f"  Hinglish captions model didn't download: {hl_state['error']}", flush=True)
        return False
    finally:
        hl_state["doing"] = False
        _hl_lock.release()


def fetch_hinglish_soon():
    """Start downloading the Hinglish model in the background, unless it is here, already coming, or failed a moment
    ago (then it waits ten minutes before trying again, so a missing file isn't asked for in a loop)."""
    if hinglish_ready() or hl_state["doing"] or not HL_ON or not installed():
        return
    if hl_state["error"] and time.time() - hl_state["at"] < 600:
        return
    hl_state["at"] = time.time()
    threading.Thread(target=fetch_hinglish, daemon=True, name="hinglish-model").start()


def hinglish(audio: np.ndarray) -> list:
    """[{'w', 's', 'e'}, ...]: the Hindi (and English) said in this audio, in Roman letters, timed to the word.
    Raises if the model isn't here or can't run."""
    if not hinglish_ready() or not ready():
        raise RuntimeError("The Hinglish captions model isn't downloaded yet.")
    with _lock:
        if "hinglish" not in _models:
            from faster_whisper import WhisperModel
            try:
                _models["hinglish"] = WhisperModel(str(HL_DIR), device="cpu", compute_type="int8", cpu_threads=THREADS)
            except Exception as e:  # noqa: BLE001 - said in the bug report; captions are made the other way
                hl_state["error"] = f"It downloaded but won't run on this computer: {str(e)[-200:]}"
                raise
        m = _models["hinglish"]
    words = []
    with _run_lock:
        # The model was taught to write Hinglish under the "English" language tag. Whisper can get stuck repeating a
        # word ("nan nan nan nan"): quiet stretches are skipped (vad, hallucination_silence_threshold), it retries
        # once at most, and drop_loops() takes out any repeat that still gets through.
        segments, _info = m.transcribe(_float(audio), language="en", task="transcribe", beam_size=BEAM,
                                       word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
                                       temperature=(0.0, 0.4), hallucination_silence_threshold=1.0)
        for seg in segments:
            for w in seg.words or []:
                text = (w.word or "").strip()
                if text:
                    s0 = round(float(w.start), 2)
                    words.append({"w": text, "s": s0, "e": round(max(float(w.end), s0 + 0.05), 2)})
    return drop_loops(words)


def _key(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def drop_loops(words: list, most: int = 3) -> list:
    """Takes out what a speech model writes when it gets stuck: the same word, or the same two or three words, over
    and over. Someone really saying "bhaago bhaago bhaago" keeps them; a fourth and later in a row go."""
    out = list(words)
    for n in (1, 2, 3):                                   # one word repeated, then pairs, then three-word phrases
        keep, i = [], 0
        while i < len(out):
            gram = [_key(w["w"]) for w in out[i:i + n]]
            j, reps = i + n, 1
            while len(gram) == n and all(gram) and [_key(w["w"]) for w in out[j:j + n]] == gram:
                j, reps = j + n, reps + 1
            limit = most if n == 1 else 2
            if reps > limit:
                keep.extend(out[i:i + n * limit])
                i = j
            else:
                keep.append(out[i])
                i += 1
        out = keep
    return out


def quick_model() -> bool:
    """Is there a quicker model than the caption one to find the moments with?"""
    return SCAN_MODEL and SCAN_MODEL != MODEL


def transcribe(audio: np.ndarray, lang: str = None, on_progress=None, note=None, quick=False) -> list:
    """[{'w', 's', 'e'}, ...] for the whole audio, like transcribe.transcribe. Raises if Whisper can't be used.

    quick: for finding the moments of a long video only. The quick model, with the time of each line instead of each
    word (the words of a line are spread over it), which is much faster. Captions never use this."""
    if not ready(note):
        raise RuntimeError("Hindi speech support couldn't be installed. Check the internet connection and try again.")
    m = None
    if quick and quick_model():
        try:
            m = _get(note, SCAN_MODEL)
        except Exception as e:  # noqa: BLE001 - it couldn't be downloaded: the caption model does it, more slowly
            print(f"  Quick speech model unavailable ({str(e)[-120:]}), using the full one", flush=True)
            m = None
    m = m or _get(note)
    a = _float(audio)
    dur = max(len(a) / SR, 0.01)
    words = []
    with _run_lock:
        segments, _info = m.transcribe(a, language=lang or None, task="transcribe", beam_size=BEAM,
                                       word_timestamps=not quick, vad_filter=True, condition_on_previous_text=False)
        for seg in segments:
            if quick:
                parts = (seg.text or "").split()
                s0, e0 = float(seg.start), max(float(seg.end), float(seg.start) + 0.1)
                step = (e0 - s0) / max(1, len(parts))
                for i, text in enumerate(parts):
                    s = round(s0 + i * step, 2)
                    words.append({"w": text, "s": s, "e": round(s + max(0.05, step * 0.9), 2)})
            else:
                for w in seg.words or []:
                    text = (w.word or "").strip()
                    if text:
                        s = round(float(w.start), 2)
                        words.append({"w": text, "s": s, "e": round(max(float(w.end), s + 0.05), 2)})
            if on_progress:
                on_progress(min(1.0, float(seg.end) / dur))
    return words


def is_english(lang: str) -> bool:
    return (lang or "").lower().startswith("en")
