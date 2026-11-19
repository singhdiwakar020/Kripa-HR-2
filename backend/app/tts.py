"""ElevenLabs voice for the phone calls.

Every line the caller says becomes an mp3 that Plivo plays. Lines are cached on disk by their text, and the fixed ones
(greeting, questions, closing) are made BEFORE the phone rings (prewarm), so they play with no delay. Only replies to a
candidate's own questions are made during the call. If ElevenLabs is slow, down or not configured, the caller keeps
using the Plivo (Polly) voice, so a call never hangs because of the voice.

Settings in backend/.env: ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID. Optional: ELEVENLABS_MODEL (default eleven_flash_v2_5),
TTS_PROVIDER=polly (turn ElevenLabs off), TTS_LIVE_TIMEOUT (seconds to wait for a live line, default 4).
"""
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

CACHE_DIR = Path(__file__).resolve().parent.parent / "audio_cache"
KEEP_DAYS = 14
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_down_until = 0.0  # after a failure, use the Plivo voice for a short while instead of waiting on a broken service
_pruned = False
_pool = ThreadPoolExecutor(max_workers=6)  # lines needed during a call
_prewarm_pool = ThreadPoolExecutor(max_workers=2)  # lines made ahead of time, so they never queue in front of a live one


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def enabled() -> bool:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    return (_env("TTS_PROVIDER", "elevenlabs").lower() == "elevenlabs" and bool(_env("ELEVENLABS_API_KEY"))
            and bool(_env("ELEVENLABS_VOICE_ID")) and _env("PUBLIC_BASE_URL").startswith("https://"))


def _key(text: str) -> str:
    # the secret salt keeps file names unguessable: the address is public so Plivo can fetch it
    salt = _env("PLIVO_AUTH_TOKEN") + _env("ELEVENLABS_VOICE_ID") + _env("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    return hashlib.sha256((salt + "\n" + text).encode("utf-8")).hexdigest()[:40]


def path_for(name: str) -> Path:
    return CACHE_DIR / name


def url_for(name: str) -> str:
    return f"{_env('PUBLIC_BASE_URL').rstrip('/')}/api/plivo/audio/{name}"


def _fetch(text: str) -> bytes:
    """One call to ElevenLabs. Returns mp3 bytes or raises."""
    import httpx
    voice = _env("ELEVENLABS_VOICE_ID")
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        params={"output_format": "mp3_22050_32"},  # small files are enough for a phone line and download faster
        headers={"xi-api-key": _env("ELEVENLABS_API_KEY"), "Content-Type": "application/json"},
        json={"text": text, "model_id": _env("ELEVENLABS_MODEL", "eleven_flash_v2_5")},
        timeout=float(_env("TTS_FETCH_TIMEOUT", "8")))
    if r.status_code != 200 or len(r.content) < 500:
        raise RuntimeError(f"ElevenLabs answered {r.status_code}: {r.text[:200]}")
    return r.content


def _prune():
    global _pruned
    if _pruned:
        return
    _pruned = True
    try:
        cutoff = time.time() - KEEP_DAYS * 86400
        for f in CACHE_DIR.glob("*.mp3"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def ensure(text: str) -> str | None:
    """Make sure the audio for this text exists on disk. Returns its file name, or None if it could not be made."""
    global _down_until
    text = (text or "").strip()
    if not text or not enabled():
        return None
    name = _key(text) + ".mp3"
    f = path_for(name)
    if f.exists():
        return name
    if time.time() < _down_until:
        return None
    with _locks_guard:
        lock = _locks.setdefault(name, threading.Lock())
    with lock:  # a second request for the same line waits for the first instead of paying twice
        if f.exists():
            return name
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            _prune()
            data = _fetch(text)
            tmp = f.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(f)
            return name
        except Exception as e:
            plan = "402" in str(e)  # the plan or voice is not allowed: retrying every minute would only slow things down
            print("ElevenLabs failed, using the Plivo voice for a while:", type(e).__name__, str(e)[:200])
            _down_until = time.time() + (600 if plan else 60)
            return None


def urls_for(lines: list[str]) -> list[str | None]:
    """Audio addresses for the lines (None where the Plivo voice must be used). Missing lines are made in parallel,
    and a line that takes longer than TTS_LIVE_TIMEOUT is spoken by the Plivo voice so the caller is never kept waiting."""
    if not enabled():
        return [None] * len(lines)
    futures = [_pool.submit(ensure, t) for t in lines]
    deadline = time.time() + float(_env("TTS_LIVE_TIMEOUT", "4"))
    out: list[str | None] = []
    for fu in futures:
        try:
            name = fu.result(timeout=max(0.1, deadline - time.time()))
        except Exception:
            name = None
        out.append(url_for(name) if name else None)
    return out


def prewarm(lines: list[str]):
    """Make the audio for lines the call will need, in the background, while the phone is still ringing."""
    if not enabled():
        return
    todo = [t for t in dict.fromkeys(lines) if t and not path_for(_key(t) + ".mp3").exists()]
    for t in todo:
        _prewarm_pool.submit(ensure, t)
