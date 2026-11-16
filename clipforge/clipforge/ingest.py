"""Ingest: yt-dlp wrappers for VOD sections, audio-only probing and live capture."""
from __future__ import annotations

import json
from pathlib import Path

from .util import log, run


def _base_args(cfg) -> list:
    args = ["--no-warnings", "--no-playlist", "--ignore-config", "--no-progress"]
    if cfg.cookies_file:
        args += ["--cookies", cfg.cookies_file]
    if cfg.ytdlp_extra:
        args += cfg.ytdlp_extra.split()
    return args


def _format(cfg, audio_only: bool = False) -> str:
    h = int(cfg.max_source_height)
    if audio_only:
        return "bestaudio[ext=m4a]/bestaudio"
    return (
        f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={h}]+ba/b[height<={h}]/b"
    )


def video_info(url: str, cfg) -> dict:
    proc = run(["yt-dlp", *_base_args(cfg), "-J", url], check=False, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp could not read {url}: {(proc.stderr or '')[-800:]}")
    data = json.loads(proc.stdout)
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "duration": float(data.get("duration") or 0.0),
        "is_live": bool(data.get("is_live")),
        "was_live": bool(data.get("was_live")),
        "uploader": data.get("uploader") or data.get("channel"),
        "extractor": data.get("extractor_key"),
        "webpage_url": data.get("webpage_url") or url,
    }


def download_section(url: str, dest: Path, start: float, end: float, cfg) -> Path:
    """Download only [start,end] at up to max_source_height. Returns the file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "yt-dlp",
        *_base_args(cfg),
        "-f", _format(cfg),
        "--merge-output-format", "mp4",
        "--download-sections", f"*{int(max(0, start))}-{int(max(start + 1, end))}",
        "-o", str(dest.with_suffix(".%(ext)s")),
        url,
    ]
    proc = run(args, check=False, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"section download failed: {(proc.stderr or '')[-1200:]}")
    if not dest.is_file():
        candidates = sorted(dest.parent.glob(dest.stem + ".*"))
        if not candidates:
            raise RuntimeError(f"section download produced no file for {dest}")
        return candidates[0]
    return dest


def download_audio(url: str, dest: Path, cfg) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            "yt-dlp",
            *_base_args(cfg),
            "-f", _format(cfg, audio_only=True),
            "-o", str(dest.with_suffix(".%(ext)s")),
            url,
        ],
        check=False,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"audio download failed: {(proc.stderr or '')[-1200:]}")
    if not dest.is_file():
        candidates = sorted(dest.parent.glob(dest.stem + ".*"))
        if not candidates:
            raise RuntimeError(f"audio download produced no file for {dest}")
        return candidates[0]
    return dest


def capture_live(url: str, dest: Path, cfg) -> Path:
    """Grab the first `live_minutes` of a live stream (needs a DVR window)."""
    minutes = max(1.0, cfg.live_minutes)
    log(f"live capture: up to {minutes:.0f} min from {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "yt-dlp",
        *_base_args(cfg),
        "--live-from-start",
        "-f", _format(cfg),
        "--merge-output-format", "mp4",
        "--download-sections", f"*0-{int(minutes * 60)}",
        "-o", str(dest.with_suffix(".%(ext)s")),
        url,
    ]
    proc = run(args, check=False, timeout=int(minutes * 60 * 3 + 600))
    if not dest.is_file():
        candidates = sorted(dest.parent.glob(dest.stem + ".*"))
        if candidates:
            return candidates[0]
        raise RuntimeError(f"live capture failed: {(proc.stderr or '')[-1200:]}")
    return dest
