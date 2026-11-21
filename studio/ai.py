"""Talking to the AI service. Supports single or multi-model cascades with automatic failover."""
import base64
import json
import os
import re
import threading

import httpx

from . import config

PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemma-4-26b-a4b-it"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "openai/gpt-oss-120b"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}

_lock = threading.Lock()
_saved = {"at": None, "data": {}}


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


import time

_KEY_STATE = {}
_key_lock = threading.Lock()


def parse_keys(val) -> list[str]:
    """Extract list of clean non-empty keys from string, list, or dict."""
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        raw = val
    elif isinstance(val, str):
        raw = re.split(r"[\r\n,;]+", val)
    else:
        raw = [str(val)]
    out = []
    seen = set()
    for k in raw:
        s = str(k).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get_keys_for(p: str) -> list[str]:
    """Returns all configured API keys for provider p in priority order."""
    p = ALIASES.get(p.lower(), p.lower())
    d = _from_dashboard()
    cur_p = current()["provider"]
    keys = []

    # Priority 1: Current provider's explicitly set key (from dashboard or config.ai_key)
    if p == cur_p:
        if d.get("provider") == p and d.get("key"):
            keys.extend(parse_keys(d["key"]))
        elif config.ai_key():
            keys.extend(parse_keys(config.ai_key()))

    # Priority 2: Dashboard saved keys map
    keys_map = d.get("keys", {}) if isinstance(d.get("keys"), dict) else {}
    if p in keys_map and keys_map[p]:
        keys.extend(parse_keys(keys_map[p]))

    # Priority 3: Provider-specific environment variable keys
    is_test_cleared = (p == cur_p and "PEAKCUT_AI_KEY" in os.environ and not os.environ["PEAKCUT_AI_KEY"])
    if not is_test_cleared:
        env_keys = (config.env(f"PEAKCUT_{p.upper()}_KEYS") or config.env(f"PEAKCUT_{p.upper()}_KEY") or
                    config.env(f"{p.upper()}_API_KEYS") or config.env(f"{p.upper()}_API_KEY"))
        if env_keys:
            keys.extend(parse_keys(env_keys))

    if p == cur_p and config.ai_key():
        keys.extend(parse_keys(config.ai_key()))

    dedup = []
    seen_keys = set()
    for k in keys:
        if k and k not in seen_keys:
            seen_keys.add(k)
            dedup.append(k)
    return dedup


def get_active_key(p: str) -> str:
    """Returns the currently active key for provider p, skipping rate-limited ones."""
    p = ALIASES.get(p.lower(), p.lower())
    keys = get_keys_for(p)
    if not keys:
        return ""
    now = time.time()
    with _key_lock:
        state = _KEY_STATE.setdefault(p, {"idx": 0, "cooldowns": {}})
        cooldowns = state["cooldowns"]
        available = [k for k in keys if cooldowns.get(k, 0) <= now]
        if available:
            idx = state["idx"] % len(available)
            return available[idx]
        return min(keys, key=lambda k: cooldowns.get(k, 0))


def rotate_key(p: str, failed_key: str = "", reason: str = "", cooldown_secs: float = 180.0):
    """Place failed key in cooldown and advance to next available key."""
    p = ALIASES.get(p.lower(), p.lower())
    now = time.time()
    with _key_lock:
        state = _KEY_STATE.setdefault(p, {"idx": 0, "cooldowns": {}})
        if failed_key:
            state["cooldowns"][failed_key] = now + cooldown_secs
        state["idx"] += 1
    print(f"[PapiClips AI] Provider '{p}' key rotated. Reason: {reason[:120] if reason else 'limit reached'}")


def mark_key_success(p: str, key_val: str):
    p = ALIASES.get(p.lower(), p.lower())
    with _key_lock:
        state = _KEY_STATE.setdefault(p, {"idx": 0, "cooldowns": {}})
        if key_val in state["cooldowns"]:
            del state["cooldowns"][key_val]


def is_rate_limit(e: Exception | str) -> bool:
    s = str(e).lower()
    return any(w in s for w in ["429", "rate limit", "ratelimit", "quota", "too many requests", "resource_exhausted", "credit", "exhausted", "503", "demand", "overload", "busy", "timeout", "timed out", "temporarily unavailable"])


def get_key_for(p: str) -> str:
    return get_active_key(p)


def current() -> dict:
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    p = ALIASES.get(p, p)
    ms = list(d["models"]) if d.get("provider") and d.get("models") else list(config.AI_MODELS)
    return {"provider": p,
            "models": ms,
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": str(d["key"]) if d.get("provider") and "key" in d else config.ai_key()}


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return current()["key"]


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def resolve_model(model_spec: str):
    spec = str(model_spec or "").strip()
    if ":" in spec:
        p, m = spec.split(":", 1)
        p = ALIASES.get(p.lower(), p.lower())
    else:
        m = spec
        if m.startswith(("openai/", "qwen/", "allam-")):
            p = "groq"
        elif m.startswith(("gemini", "gemma")):
            p = "gemini"
        elif m.startswith("anthropic/") or m.startswith("claude"):
            p = "anthropic"
        elif m.startswith("google/") or (current()["provider"] == "openrouter" and "/" in m):
            p = "openrouter"
        else:
            p = provider()
    k = get_key_for(p)
    base = PROVIDERS.get(p, ("", ""))[1]
    if p == provider() and current()["base_url"]:
        base = current()["base_url"]
    how = PROVIDERS.get(p, ("", "", "openai"))[2]
    return p, m, base, k, how


def ready() -> bool:
    c = current()
    if not c["models"]:
        return False
    for m in c["models"]:
        p, _, base, k, _ = resolve_model(m)
        if base and (k or p in NO_KEY):
            return True
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def all_keys_detailed() -> list[dict]:
    """Returns a list of all configured API keys across providers, with masked representation and active status."""
    d = _from_dashboard()
    cur_p = current()["provider"]
    keys_map = d.get("keys", {}) if isinstance(d.get("keys"), dict) else {}
    out = []
    seen = set()
    for p, v in keys_map.items():
        p_clean = ALIASES.get(p.lower(), p.lower())
        for raw_k in parse_keys(v):
            if (p_clean, raw_k) in seen:
                continue
            seen.add((p_clean, raw_k))
            masked = raw_k[:8] + "..." + raw_k[-4:] if len(raw_k) > 14 else (raw_k[:4] + "..." if len(raw_k) > 6 else "***")
            out.append({
                "provider": p_clean,
                "label": PROVIDERS.get(p_clean, (p_clean.title(),))[0],
                "key_masked": masked,
                "key_full": raw_k,
                "is_active": raw_k == get_active_key(p_clean),
                "is_current_provider": p_clean == cur_p,
            })
    return out


def test_single_key(provider_id: str, key_val: str, address: str = "") -> dict:
    """Verifies an individual API key by querying models."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    k = str(key_val or "").strip()
    if p not in PROVIDERS:
        raise ValueError("Invalid AI provider.")
    if not k and p not in NO_KEY:
        raise ValueError("API key cannot be empty.")
    mods = list_models(p, address=address, with_key=k)
    return {"ok": True, "provider": p, "models_count": len(mods), "models_sample": mods[:5]}


def add_key_to_pool(provider_id: str, key_val: str, address: str = "", test_first: bool = True) -> dict:
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    k = str(key_val or "").strip()
    if test_first:
        test_single_key(p, k, address)
    d = _from_dashboard()
    km = dict(d.get("keys", {}) if isinstance(d.get("keys"), dict) else {})
    existing = parse_keys(km.get(p, ""))
    if k not in existing:
        existing.append(k)
    km[p] = ",".join(existing)

    cur = current()
    models_to_use = list(cur["models"])
    prov_example = PROVIDERS.get(p, ("", "", "", "", ""))[4]
    if prov_example and not any(p in m for m in models_to_use):
        models_to_use = [f"{p}:{prov_example}"] + models_to_use

    save(cur["provider"] or p, models_to_use, cur["base_url"] or address, keys_map=km)
    return {"ok": True, "provider": p, "total_keys": len(existing)}


def remove_key_from_pool(provider_id: str, key_val: str) -> dict:
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    k = str(key_val or "").strip()
    d = _from_dashboard()
    km = dict(d.get("keys", {}) if isinstance(d.get("keys"), dict) else {})
    existing = parse_keys(km.get(p, ""))
    updated = [x for x in existing if x != k]
    km[p] = ",".join(updated)
    cur = current()
    save(cur["provider"], cur["models"], cur["base_url"], keys_map=km)
    return {"ok": True, "provider": p, "total_keys": len(updated)}


def analyze_model_capabilities(model_spec: str) -> dict:
    """Evaluates a model's architectural characteristics and determines its score (0-100)
    for Scout (Speed/Detection), Director (Creativity/Virality), and Captioner (Language/Precision)."""
    spec = str(model_spec or "").lower().strip()
    scout_score = 50
    director_score = 50
    caption_score = 50
    tags = []

    # Provider characteristics
    if "ollama" in spec or "localhost" in spec or "127.0.0.1" in spec:
        tags.append("Local / Private (Ollama)")
        scout_score += 20  # local speed, zero API cost
        director_score += 10
    if "groq" in spec or "cerebras" in spec:
        tags.append("Ultra-Low Latency (Groq)")
        scout_score += 35  # hardware LPU speed
    if "gemini" in spec:
        tags.append("Multimodal & Creative (Gemini)")
        director_score += 30
        caption_score += 25
    if "anthropic" in spec or "claude" in spec:
        tags.append("Deep Reasoning (Claude)")
        director_score += 35
        caption_score += 20
    if "openai" in spec or "gpt-4" in spec:
        tags.append("High Precision (OpenAI)")
        director_score += 25
        caption_score += 25
    if "deepseek" in spec:
        tags.append("Reasoning & Logic")
        director_score += 30

    # Model size & family characteristics
    if any(k in spec for k in ["flash", "mini", "8b", "7b", "3b", "1b", "turbo", "fast", "tiny"]):
        scout_score += 20
        director_score -= 5
    if any(k in spec for k in ["70b", "120b", "pro", "large", "sonnet", "opus", "plus", "r1"]):
        director_score += 25
        scout_score -= 10
    if any(k in spec for k in ["qwen", "gemini", "gemma", "gpt"]):
        caption_score += 15

    return {
        "model": model_spec,
        "scout": min(100, max(10, scout_score)),
        "director": min(100, max(10, director_score)),
        "captioner": min(100, max(10, caption_score)),
        "tags": tags
    }


def auto_assigned_team() -> dict:
    """Dynamically analyzes the active models in the pool and assigns the optimal model
    for each role based on architecture & real capability benchmarks."""
    ms = models()
    if not ms:
        return {
            "scout": {"model": "None configured", "reason": "Add models to pool", "tags": []},
            "director": {"model": "None configured", "reason": "Add models to pool", "tags": []},
            "captioner": {"model": "None configured", "reason": "Add models to pool", "tags": []}
        }
    scored = [analyze_model_capabilities(m) for m in ms]
    best_scout = max(scored, key=lambda x: x["scout"])
    best_director = max(scored, key=lambda x: x["director"])
    best_captioner = max(scored, key=lambda x: x["captioner"])

    return {
        "scout": {"model": best_scout["model"], "score": best_scout["scout"], "tags": best_scout["tags"]},
        "director": {"model": best_director["model"], "score": best_director["director"], "tags": best_director["tags"]},
        "captioner": {"model": best_captioner["model"], "score": best_captioner["captioner"], "tags": best_captioner["tags"]},
    }


def public() -> dict:
    c = current()
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(c["key"]),
            "key_hint": c["key"][-4:] if len(c["key"]) >= 12 else "", "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "all_keys": all_keys_detailed(),
            "team": auto_assigned_team(),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None, keys_map=None) -> dict:
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:10]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current()
    if new_key is None and old["provider"] == p:
        k = old["key"]
    elif new_key is not None:
        k = str(new_key).strip()[:2000]
    else:
        d = _from_dashboard()
        km = d.get("keys", {}) if isinstance(d.get("keys"), dict) else {}
        k = str(km.get(p) or "").strip()
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service.")
    d = _from_dashboard()
    old_keys = d.get("keys", {}) if isinstance(d.get("keys"), dict) else {}
    if keys_map is None:
        keys_map = old_keys
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        save_data = {"provider": p, "models": ms, "base_url": address, "key": k}
        if isinstance(keys_map, dict):
            save_data["keys"] = keys_map
        tmp.write_text(json.dumps(save_data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k_candidates = parse_keys(with_key) if with_key else []
    k = k_candidates[0] if k_candidates else (get_key_for(p) or (parse_keys(c.get("key"))[0] if c.get("key") and c["provider"] == p else ""))
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = []
        for m in data.get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            mid = m.get("name", "").split("/")[-1]
            low_id = mid.lower()
            if mid and low_id != "aqa" and not any(t in low_id for t in ("-tts", "-image", "imagen-", "veo-", "lyria-", "embedding")):
                found.append(mid)
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    last_err = None
    for m in ms:
        try:
            out, sent, wrote = call(m, 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}}, timeout=20)
            clear_problem()
            return {"ok": True, "model": m, "tokens": sent + wrote, "answer": str(out)[:80]}
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise ValueError("No models available.")


class AIError(RuntimeError):
    """A failed AI call. `wait` is how many seconds the service asked us to wait before trying again, when it
    said so (a Retry-After header, or Gemini's retryDelay)."""
    def __init__(self, message, wait=None, daily=False):
        super().__init__(message)
        self.wait = wait
        self.daily = daily        # the limit is for the whole day, so waiting a minute won't help


problem = {"text": "", "at": 0.0}


def explain(e) -> str:
    text, low = str(e), str(e).lower()
    name = (models() or ["?"])[0]
    m = re.match(r"HTTP (\d{3})\b", text)
    status = m.group(1) if m else ""
    if status in ("401", "403") or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label()} refused the key. Paste a working key under Settings, AI."
    if "no longer available" in low or "deprecated" in low or "retired" in low or "discontinued" in low:
        return f"{label()} has retired \"{name}\" (or it isn't available to new keys). Open Settings, AI, press Find models and pick a newer model."
    # Only a 404 that is about a model says the model is unknown. A 400 like "API key not found" or a stray
    # "404" inside some other message must not be reported as an unknown model.
    if (status == "404" and "model" in low) or (not status and ("404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low)) \
            or (status and "is not found for api version" in low):
        return f"{label()} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{label()} says the key has hit its limit or has no credit left. Check the account with {label()}, or try again later."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low or "nodename" in low or "servname" in low or "nameresolutionerror" in low or "name resolution" in low:
        return f"PapiClips couldn't reach {label()}. Check the internet connection" + (" and that Ollama is running." if provider() == "ollama" else ".")
    return ""


def note_problem(e):
    import time
    why = explain(e)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def asked_wait(r) -> float | None:
    """How long the service asked us to wait, in seconds, or None when it didn't say."""
    try:
        v = (r.headers.get("retry-after") or "").strip()
        if v:
            return max(0.0, float(v))
    except (ValueError, AttributeError):
        pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', getattr(r, "text", "") or "")
    return float(m.group(1)) if m else None


def _fail(r, model, k=None):
    if r is None:
        raise AIError(f"No response from {model}")
    text = r.text[:300].replace((k or key()) or "\0", "<key>")
    limited = r.status_code in (429, 503)
    raise AIError(f"HTTP {r.status_code} {text}", wait=asked_wait(r) if limited else None,
                  daily=limited and bool(re.search(r"PerDay|per day|daily", r.text or "", re.I)))
def _json_schema(s):
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _json_chunks(text: str):
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(text[start:i + 1])
                start = None
            elif depth < 0:
                depth = 0
    return out


def _parse(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    out = None
    try:
        out = json.loads(text)
    except ValueError:
        for chunk in _json_chunks(text):
            try:
                out = json.loads(chunk)
                break
            except ValueError:
                continue
        if out is None:
            m = re.search(r"[\[{].*[\]}]", text, re.S)
            if not m:
                raise AIError("The AI did not answer with JSON.")
            try:
                out = json.loads(m.group(0))
            except ValueError:
                raise AIError("The AI did not answer with JSON.")
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


def call(model: str, prompt: str, schema: dict, timeout: float | None = None):
    p, actual_model, base, _, how = resolve_model(model)
    keys = get_keys_for(p)
    if not keys and p not in NO_KEY:
        raise AIError(f"Missing API key for provider {p} ({actual_model})")
    if p in NO_KEY:
        keys = [""]

    active = get_active_key(p)
    ordered_keys = [active] + [k for k in keys if k != active] if active else keys

    last_err = None
    for k in ordered_keys:
        try:
            if how == "gemini":
                res = _gemini_call(actual_model, prompt, schema, k, base, timeout)
            elif how == "anthropic":
                res = _anthropic_call(actual_model, prompt, schema, k, base, timeout)
            else:
                res = _openai_call(actual_model, prompt, schema, k, base, timeout)
            if k:
                mark_key_success(p, k)
            clear_problem()
            return res
        except Exception as e:
            last_err = e
            if is_rate_limit(e):
                rotate_key(p, failed_key=k, reason=str(e), cooldown_secs=180.0)
                continue
            raise e

    if last_err:
        raise last_err
    raise AIError(f"No usable API keys available for provider {p}")


def _gem(model: str) -> str:
    """Gemini names models 'models/gemini-2.5-flash'; the address already ends in /models, so only the bare id is used."""
    return model.removeprefix("models/")


def _gemini_call(model, prompt, schema, api_key, base, timeout=None):
    model = _gem(model)
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{base}/{model}:generateContent", json=body, headers={"x-goog-api-key": api_key}, timeout=timeout or 120)
    used_plain = False
    if r.status_code in (400, 422) and "api key" not in r.text.lower() and "api_key" not in r.text.lower():
        plain = {"contents": [{"role": "user", "parts": [{"text": _wrapped(prompt, schema)}]}]}
        r = httpx.post(f"{base}/{model}:generateContent", json=plain, headers={"x-goog-api-key": api_key}, timeout=timeout or 120)
        used_plain = True
    if r.status_code != 200:
        _fail(r, model, api_key)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    parsed = _parse(text) if used_plain else json.loads(text)
    return parsed, sent, max(0, int(u.get("totalTokenCount") or 0) - sent)


def _openai_call(model, prompt, schema, api_key, base, timeout=None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}], "stream": False}
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{base}/chat/completions", json={**body, **extra}, headers=headers, timeout=timeout or 180)
        if r.status_code not in (400, 422):
            break
    if not r or r.status_code != 200:
        _fail(r, model, api_key)
    data = _openai_body(r)
    text = (data["choices"][0]["message"].get("content") or "")
    u = data.get("usage") or {}
    sent = int(u.get("prompt_tokens") or 0)
    wrote = int(u.get("completion_tokens") or 0)
    return _parse(text), sent, wrote


def _anthropic_call(model, prompt, schema, api_key, base, timeout=None):
    body = {"model": model, "max_tokens": 4000,
            "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base}/messages", json=body, timeout=timeout or 180,
                   headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model, api_key)
    data = r.json()
    text = "".join(p.get("text", "") for p in data.get("content") or [] if p.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)


def _gemini(model, prompt, schema, timeout=None):
    return _gemini_call(model, prompt, schema, key(), base_url(), timeout)


def _openai(model, prompt, schema, timeout=None):
    return _openai_call(model, prompt, schema, key(), base_url(), timeout)


def _anthropic(model, prompt, schema, timeout=None):
    return _anthropic_call(model, prompt, schema, key(), base_url(), timeout)


def see(prompt: str, image: bytes, timeout: float | None = None) -> tuple[str, int, int]:
    if not ready() or not image or not models():
        return "", 0, 0
    b64 = base64.b64encode(image).decode()
    for model in models():
        p, actual_model, base, k, how = resolve_model(model)
        try:
            if how == "gemini":
                body = {"contents": [{"role": "user", "parts": [
                    {"text": prompt},
                    {"inline_data": {"mimeType": "image/jpeg", "data": b64}}]}]}
                r = httpx.post(f"{base}/{actual_model}:generateContent", json=body,
                               headers={"x-goog-api-key": k}, timeout=timeout or 60)
                if r.status_code != 200:
                    continue
                data = r.json()
                text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                u = data.get("usageMetadata", {})
                sent = int(u.get("promptTokenCount") or 0)
                return text, sent, max(0, int(u.get("totalTokenCount") or 0) - sent)
            else:
                headers = {"Authorization": f"Bearer {k}"} if k else {}
                body = {"model": actual_model, "stream": False, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
                r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=timeout or 60)
                if r.status_code != 200:
                    continue
                data = _openai_body(r)
                text = (data["choices"][0]["message"].get("content") or "")
                u = data.get("usage") or {}
                return text, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        except Exception:
            continue
    return "", 0, 0


def _openai_body(r):
    try:
        data = r.json()
    except ValueError:
        data = _from_events(r.text)
    for ch in data.get("choices") or []:
        msg = ch.get("message") or {}
        if (msg.get("content") or "").strip():
            continue
        for c in msg.get("tool_calls") or []:
            args = (c.get("function") or {}).get("arguments")
            if args:
                msg["content"] = args
                break
    return data


def _from_events(text: str) -> dict:
    parts, usage, calls = [], {}, {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except ValueError:
            continue
        usage = d.get("usage") or usage
        for ch in d.get("choices") or []:
            for piece in ((ch.get("message") or {}), (ch.get("delta") or {})):
                if piece.get("content"):
                    parts.append(piece["content"])
                for call_ in piece.get("tool_calls") or []:
                    i = call_.get("index", 0)
                    calls[i] = calls.get(i, "") + ((call_.get("function") or {}).get("arguments") or "")
    message = {"content": "".join(parts)}
    if not message["content"] and calls:
        message["content"] = "".join(calls.values())
    return {"choices": [{"message": message}], "usage": usage}
