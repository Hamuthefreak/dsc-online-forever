"""Highlight detection: rolling z-score over chat velocity, then candidate windows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class Candidate:
    index: int
    start: float
    end: float
    peak_t: float
    score: float
    signal: str
    breakdown: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "peak_t": round(self.peak_t, 2),
            "score": round(self.score, 3),
            "signal": self.signal,
            "breakdown": {k: round(v, 3) for k, v in self.breakdown.items()},
        }


def moving_average(values: Sequence[float], window: int) -> list[float]:
    if window <= 1 or not values:
        return list(values)
    out: list[float] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= window:
            total -= values[i - window]
        out.append(total / min(i + 1, window))
    return out


def rolling_z(values: Sequence[float], window: int = 120) -> list[float]:
    """z-score of each bucket against a trailing window (chat bursts stand out)."""
    out: list[float] = []
    for i, value in enumerate(values):
        lo = max(0, i - window)
        chunk = values[lo : i + 1]
        n = len(chunk)
        if n < 5:
            out.append(0.0)
            continue
        mean = sum(chunk) / n
        var = sum((c - mean) ** 2 for c in chunk) / n
        std = var**0.5
        out.append(0.0 if std < 1e-6 else (value - mean) / std)
    return out


def pick_peaks(scores: Sequence[float], threshold: float, min_gap: int) -> list[int]:
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    chosen: list[int] = []
    for idx in order:
        if scores[idx] < threshold:
            break
        if all(abs(idx - other) >= min_gap for other in chosen):
            chosen.append(idx)
    return sorted(chosen)


def _clamp_window(start: float, end: float, duration: float, cfg) -> tuple[float, float]:
    end = min(end, duration)
    start = max(0.0, start)
    if end - start > cfg.clip_max_s:
        end = start + cfg.clip_max_s
    if end - start < cfg.clip_min_s:
        end = min(duration, start + cfg.clip_min_s)
        start = max(0.0, end - cfg.clip_min_s)
    return round(start, 2), round(end, 2)


def detect_candidates(series: dict, cfg) -> list[Candidate]:
    """Turn a chat/audio series into ranked, non-overlapping candidate windows."""
    bucket = float(series.get("bucket") or 1.0)
    counts = [float(c) for c in series.get("counts") or []]
    emotes = [float(c) for c in series.get("emotes") or []]
    if not counts:
        return []

    duration = float(series.get("duration") or len(counts) * bucket)
    smooth = moving_average(counts, max(1, int(round(5 / bucket))))
    z = rolling_z(smooth, window=max(10, int(round(120 / bucket))))
    emote_rate = [0.0] * len(counts)
    for i, c in enumerate(counts):
        emote_rate[i] = (emotes[i] / c) if i < len(emotes) and c > 0 else 0.0

    combined = []
    for i, value in enumerate(z):
        combined.append(value + min(2.5, emote_rate[i]))

    min_gap = max(1, int(round(cfg.min_gap_s / bucket)))
    peaks = pick_peaks(combined, threshold=cfg.peak_z, min_gap=min_gap)

    candidates: list[Candidate] = []
    for peak in peaks:
        peak_t = peak * bucket
        start, end = _clamp_window(peak_t - cfg.pre_roll, peak_t + cfg.post_roll, duration, cfg)
        candidates.append(
            Candidate(
                index=0,
                start=start,
                end=end,
                peak_t=round(peak_t, 2),
                score=round(combined[peak], 3),
                signal=str(series.get("source") or "chat"),
                breakdown={
                    "chat_z": round(z[peak], 3),
                    "messages_at_peak": round(counts[peak], 2),
                    "emote_rate": round(emote_rate[peak], 3),
                },
            )
        )

    if not candidates:
        return []

    candidates.sort(key=lambda c: c.score, reverse=True)
    candidates = candidates[: max(1, cfg.max_clips * 2)]
    candidates.sort(key=lambda c: c.start)
    for i, cand in enumerate(candidates, start=1):
        cand.index = i
    return candidates


def enforce_spacing(candidates: list[Candidate], min_gap: float, limit: int) -> list[Candidate]:
    """Keep the strongest candidates that do not overlap or sit too close together."""
    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    kept: list[Candidate] = []
    for cand in ordered:
        if all(abs(cand.peak_t - other.peak_t) >= min_gap for other in kept):
            kept.append(cand)
        if len(kept) >= limit:
            break
    kept.sort(key=lambda c: c.start)
    for i, cand in enumerate(kept, start=1):
        cand.index = i
    return kept
