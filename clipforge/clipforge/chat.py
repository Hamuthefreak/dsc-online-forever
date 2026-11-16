"""Chat velocity capture: the highlight signal that actually works on streams."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .util import debug, log, write_json


def fetch_chat(
    url: str,
    *,
    live: bool = False,
    max_seconds: float | None = None,
    timeout: float | None = 900.0,
) -> dict | None:
    """Download chat replay/live chat and return a per-bucket series.

    Returns None when the platform or library cannot supply chat, which makes the
    pipeline fall back to the audio-energy signal.
    """
    try:
        from chat_downloader import ChatDownloader  # type: ignore
    except Exception:
        log("chat-downloader not installed -> falling back to audio energy")
        return None

    kwargs: dict[str, Any] = {"message_groups": ["messages"]}
    if timeout:
        kwargs["timeout"] = timeout
    if max_seconds:
        kwargs["end_time"] = max_seconds

    counts: dict[int, int] = {}
    emotes: dict[int, int] = {}
    total = 0
    try:
        for message in ChatDownloader().get_chat(url, **kwargs):
            t = message.get("time_in_seconds")
            if t is None:
                t = message.get("timestamp")
            if t is None:
                continue
            bucket = int(float(t))
            counts[bucket] = counts.get(bucket, 0) + 1
            emote_hits = 0
            for emote in message.get("emotes") or []:
                emote_hits += int(emote.get("count") or 1)
            if emote_hits == 0:
                body = message.get("message") or ""
                emote_hits = sum(1 for token in body.split() if token.startswith(":") and token.endswith(":"))
            emotes[bucket] = emotes.get(bucket, 0) + emote_hits
            total += 1
            if total % 5000 == 0:
                debug(f"chat messages: {total}")
    except Exception as exc:  # network, geo-block, unsupported platform
        log(f"chat download failed ({type(exc).__name__}: {exc}) -> falling back to audio energy")
        return None

    if not counts:
        log("no chat messages recovered -> falling back to audio energy")
        return None

    duration = max(counts) + 1
    return {
        "source": "chat",
        "bucket": 1.0,
        "duration": float(duration),
        "messages": total,
        "counts": [counts.get(i, 0) for i in range(duration)],
        "emotes": [emotes.get(i, 0) for i in range(duration)],
    }


def series_from_points(points: list[dict], bucket: float = 1.0) -> dict | None:
    """Convert an RMS-level curve (audio fallback) into the same series shape."""
    if not points:
        return None
    usable = [p for p in points if math.isfinite(p.get("db", float("-inf")))]
    if not usable:
        return None
    duration = int(max(p["t"] for p in usable)) + 1
    values = [0.0] * duration
    seen = [0] * duration
    for p in usable:
        idx = int(p["t"])
        if 0 <= idx < duration:
            values[idx] += p["db"]
            seen[idx] += 1
    floor = min(values)
    counts = []
    for i in range(duration):
        avg = values[i] / seen[i] if seen[i] else floor
        counts.append(max(0.0, avg - floor))  # 0 dB floor -> positive energy
    return {
        "source": "audio",
        "bucket": bucket,
        "duration": float(duration),
        "messages": 0,
        "counts": counts,
        "emotes": [0.0] * duration,
    }


def save_series(series: dict, path: Path) -> Path:
    write_json(path, series)
    return path
