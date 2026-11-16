"""Quality control: the gate that stops a broken clip reaching the review queue."""
from __future__ import annotations

from pathlib import Path

from .util import black_segments, ebur128_integrated, probe_summary

REQUIRED_W, REQUIRED_H = 1080, 1920


def _check(name: str, ok: bool, detail, *, severity: str = "fail") -> dict:
    status = "pass" if ok else ("warn" if severity == "warn" else "fail")
    return {"check": name, "status": status, "detail": detail}


def run_qc(
    clip: Path,
    caption_files: list[Path],
    cfg,
    *,
    expected_min: float,
    expected_max: float,
) -> dict:
    checks: list[dict] = []
    info = probe_summary(clip)

    checks.append(
        _check(
            "resolution",
            info["width"] == REQUIRED_W and info["height"] == REQUIRED_H,
            f"{info['width']}x{info['height']}",
        )
    )
    checks.append(
        _check(
            "duration",
            expected_min - 1.0 <= info["duration"] <= expected_max + 1.0,
            f"{info['duration']:.2f}s (allowed {expected_min:.0f}-{expected_max:.0f}s)",
        )
    )
    checks.append(_check("has_audio", info["has_audio"], info["acodec"] or "none"))
    checks.append(_check("fps", 25 <= info["fps"] <= 61, info["fps"]))
    checks.append(
        _check(
            "filesize",
            info["size_bytes"] > 200_000,
            f"{info['size_bytes'] / 1024 / 1024:.2f} MiB",
            severity="warn",
        )
    )

    lufs = ebur128_integrated(clip)
    if lufs is None:
        checks.append(_check("loudness", False, "could not measure", severity="warn"))
    else:
        delta = abs(lufs - cfg.loudness_target)
        checks.append(
            _check("loudness", delta <= 2.0, f"{lufs:.1f} LUFS (target {cfg.loudness_target:.1f})")
        )

    blacks = black_segments(clip, min_duration=0.5)
    total_black = sum(e - s for s, e in blacks)
    checks.append(
        _check("black_frames", total_black <= 1.0, f"{total_black:.2f}s in {len(blacks)} segment(s)")
    )

    if cfg.captions:
        have_caption = any(p.is_file() and p.stat().st_size > 20 for p in caption_files)
        checks.append(_check("captions", have_caption, [p.name for p in caption_files]))

    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    return {
        "passed": not failed,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "warnings": [c["check"] for c in warned],
        "probe": info,
        "lufs": lufs,
    }
