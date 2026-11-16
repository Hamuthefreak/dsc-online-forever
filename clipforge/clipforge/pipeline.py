"""The pipeline: one call per VOD, every stage recorded in stages.json."""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import asr, captions, chat, highlights, ingest, package
from . import qc as qc_mod
from . import render, review
from .config import Settings
from .highlights import Candidate
from .util import (
    audio_energy_series,
    free_mb,
    human_duration,
    log,
    measure_loudness,
    now_stamp,
    probe_summary,
    read_json,
    slugify,
    write_json,
)


@dataclass
class RunOptions:
    urls: list[str] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    run_id: str | None = None
    live: bool = False
    source: Path | None = None
    chat_json: Path | None = None
    transcript_json: Path | None = None
    keep_source: bool = False


def _slug_for(url: str, local: Path | None) -> str:
    if local:
        return slugify(local.stem)
    cleaned = url.split("?")[0].rstrip("/")
    return slugify(cleaned.rsplit("/", 1)[-1] or "stream")


def _make_run_dir(cfg: Settings, run_id: str | None, label: str) -> tuple[str, Path]:
    out_root = Path(cfg.out_dir).expanduser().resolve()
    base = run_id or f"{now_stamp()}-{label}"
    run_dir = out_root / base
    run_dir.mkdir(parents=True, exist_ok=True)
    return base, run_dir


def _stage(stages: list, name: str, status: str, detail: str = "", **extra) -> None:
    stages.append({"stage": name, "status": status, "detail": detail, "at": now_stamp(), **extra})
    log(f"  {name:<11} {status}{(' — ' + detail) if detail else ''}")


def run_pipeline(opts: RunOptions) -> dict:
    """Run every stage for every target. Returns one summary dict per target."""
    cfg = opts.settings
    if opts.source:
        local = Path(opts.source).expanduser().resolve()
        if not local.is_file():
            raise RuntimeError(f"--source not found: {local}")
        targets: list[tuple[str, Path | None]] = [(str(local), local)]
    else:
        if not opts.urls:
            raise RuntimeError("nothing to do: pass one or more URLs, or --source <file>")
        targets = [(url, None) for url in opts.urls]

    runs = []
    free = free_mb(cfg.out_dir)
    if free < cfg.min_free_mb:
        raise RuntimeError(
            f"only {free:.0f} MB free on {Path(cfg.out_dir).expanduser().resolve()} "
            f"(CLIPFORGE_MIN_FREE_MB={cfg.min_free_mb}). Free space, lower the guard, "
            f"run `clipforge clean --all`, or point CLIPFORGE_OUT at another drive."
        )
    for index, (url, local) in enumerate(targets, start=1):
        label = _slug_for(url, local)
        run_id = opts.run_id
        if run_id and len(targets) > 1:
            run_id = f"{run_id}-{index:02d}"
        elif run_id is None:
            run_id = None
        runs.append(_run_one(url, local, opts, index, len(targets), run_id, label))
    return {"runs": runs, "count": len(runs)}


def _prepare_source(url: str, local: Path | None, run_dir: Path, opts: RunOptions, index: int):
    """Return (info, material_path, material_offset, material_duration)."""
    cfg = opts.settings
    if local:
        duration = probe_summary(local)["duration"]
        return (
            {"title": local.stem, "duration": duration, "webpage_url": str(local),
             "uploader": None, "is_live": False, "id": local.stem},
            local,
            0.0,
            duration,
        )

    if opts.live:
        dest = run_dir / "source" / f"live_{index:02d}.mp4"
        path = ingest.capture_live(url, dest, cfg)
        info = ingest.video_info(url, cfg)
        duration = probe_summary(path)["duration"]
        info["duration"] = duration
        return info, path, 0.0, duration

    info = ingest.video_info(url, cfg)
    return info, None, 0.0, float(info.get("duration") or 0.0)


def _signal(url: str, local: Path | None, run_dir: Path, opts: RunOptions, info: dict):
    """Chat replay if available, else an audio-energy curve."""
    cfg = opts.settings
    if opts.chat_json:
        data = read_json(opts.chat_json)
        if data:
            return data, f"chat json ({Path(opts.chat_json).name})"
        log(f"chat json unreadable: {opts.chat_json}")

    if not local:
        series = chat.fetch_chat(url, live=opts.live, max_seconds=info.get("duration") or None)
        if series:
            chat.save_series(series, run_dir / "chat_series.json")
            return series, "chat replay"
        log("  no chat replay available -> audio-energy fallback")

    audio: Path | None = None
    try:
        if local:
            audio = local
        else:
            audio = ingest.download_audio(url, run_dir / "source" / "audio", cfg)
        series = chat.series_from_points(audio_energy_series(audio))
        if series:
            chat.save_series(series, run_dir / "audio_series.json")
        return series, "audio energy"
    except Exception as exc:
        log(f"  audio fallback failed: {type(exc).__name__}: {exc}")
        return None, "none"
    finally:
        if audio and audio != local and not opts.keep_source:
            audio.unlink(missing_ok=True)


def _snap_window(words: list[dict], rel_start: float, rel_end: float, cfg: Settings) -> tuple[float, float]:
    """Move clip edges onto speech boundaries so nothing is cut mid-sentence."""
    start, end = rel_start, rel_end
    if words:
        before = [w for w in words if w["start"] <= rel_start + 1.2]
        if before:
            candidate = before[-1]["start"] - 0.15
            if abs(candidate - rel_start) <= 1.6:
                start = max(0.0, candidate)
        after = [w for w in words if w["end"] >= rel_end - 1.2]
        if after:
            candidate = after[0]["end"] + 0.25
            if abs(candidate - rel_end) <= 1.8:
                end = max(start + 1.0, candidate)
    if end - start > cfg.clip_max_s:
        end = start + cfg.clip_max_s
    if end - start < cfg.clip_min_s:
        end = start + cfg.clip_min_s
    return round(start, 3), round(end, 3)


def _material_for(url: str, cand: Candidate, run_dir: Path, local: Path | None, cfg: Settings):
    """Return (path, offset) where offset is the source time of the file's t=0."""
    if local:
        return local, 0.0
    pad = 2.0
    start = max(0.0, cand.start - pad)
    end = cand.end + pad
    dest = run_dir / "source" / f"part_{cand.index:02d}.mp4"
    if not dest.is_file():
        ingest.download_section(url, dest, start, end, cfg)
    if not dest.is_file():
        found = sorted(dest.parent.glob(dest.stem + ".*"))
        if not found:
            raise RuntimeError(f"could not obtain material for {dest}")
        dest = found[0]
    return dest, start


def _run_one(url: str, local: Path | None, opts: RunOptions, index: int, total: int,
             run_id: str | None, label: str) -> dict:
    cfg = opts.settings
    started = time.time()
    stages: list[dict] = []
    run_id, run_dir = _make_run_dir(cfg, run_id, label)
    log(f"run {run_id} -> {run_dir}")

    info, _, _, _ = _prepare_source(url, local, run_dir, opts, index)
    _stage(stages, "ingest", "ok",
           f"{info.get('title') or label} ({human_duration(info.get('duration') or 0)})")

    series, signal_name = _signal(url, local, run_dir, opts, info)
    candidates: list[Candidate] = []
    if series:
        candidates = highlights.enforce_spacing(
            highlights.detect_candidates(series, cfg), cfg.min_gap_s, cfg.max_clips
        )
    _stage(stages, "highlights", "ok" if candidates else "failed",
           f"{len(candidates)} candidate(s) from {signal_name}")
    if not candidates:
        write_json(run_dir / "summary.json", {
            "run_id": run_id, "url": url, "source": info, "stages": stages, "clips": [],
            "error": "no highlight candidates",
        })
        return {"run_id": run_id, "dir": str(run_dir), "clips": [], "stages": stages,
                "error": "no highlight candidates", "signal": signal_name}

    if len(candidates) < cfg.min_clips:
        log(f"  only {len(candidates)} candidate(s) found (minimum was {cfg.min_clips})")

    global_transcript = read_json(opts.transcript_json) if opts.transcript_json else None
    clips: list[dict] = []
    for cand in candidates[: cfg.max_clips]:
        try:
            clips.append(_render_one(url, cand, run_dir, info, opts, local, global_transcript))
        except Exception as exc:
            log(f"  clip {cand.index} failed: {type(exc).__name__}: {exc}")
            clips.append({"id": f"clip_{cand.index:02d}", "start": cand.start, "end": cand.end,
                          "error": f"{type(exc).__name__}: {exc}"})

    ok_clips = [c for c in clips if not c.get("error")]
    passed = [c for c in ok_clips if c.get("qc_passed")]
    _stage(stages, "render", "ok" if ok_clips else "failed", f"{len(ok_clips)} clip(s)")
    _stage(stages, "qc", "ok" if ok_clips and len(passed) == len(ok_clips) else ("warn" if ok_clips else "failed"),
           f"{len(passed)}/{len(ok_clips)} passed")

    if not opts.keep_source:
        shutil.rmtree(run_dir / "source", ignore_errors=True)

    review.register(run_dir, ok_clips)
    _stage(stages, "review", "pending", f"{len(ok_clips)} clip(s) awaiting approval")

    summary = {
        "run_id": run_id,
        "url": url,
        "source": info,
        "signal": signal_name,
        "settings": cfg.as_dict(),
        "stages": stages,
        "clips": clips,
        "failed": [c for c in clips if c.get("error")],
        "elapsed_s": round(time.time() - started, 1),
    }
    write_json(run_dir / "summary.json", summary)
    return {"run_id": run_id, "dir": str(run_dir), "clips": ok_clips,
            "failed": summary["failed"], "stages": stages, "signal": signal_name,
            "elapsed_s": summary["elapsed_s"]}


def _render_one(url: str, cand: Candidate, run_dir: Path, info: dict, opts: RunOptions,
                local: Path | None, global_transcript: dict | None) -> dict:
    cfg = opts.settings
    clip_id = f"clip_{cand.index:02d}"
    clip_dir = run_dir / "clips" / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)

    material, offset = _material_for(url, cand, run_dir, local, cfg)
    material_duration = probe_summary(material)["duration"]

    # Window inside the material file (t=0 of `material` is source time `offset`).
    rel_start = max(0.0, cand.start - offset)
    rel_end = min(material_duration, cand.end - offset)

    # Transcript, in material time.
    words: list[dict] = []
    segments: list[dict] = []
    language = None
    if global_transcript:
        sliced = asr.words_from_global(global_transcript, offset, offset + material_duration)
        words, segments = sliced["words"], sliced["segments"]
        language = sliced.get("language")
    elif cfg.captions:
        try:
            transcript = asr.transcribe(material, cfg)
            language = transcript.get("language")
            words = [w for w in transcript["words"] if w["end"] > rel_start - 2 and w["start"] < rel_end + 2]
            segments = [s for s in transcript["segments"] if s["end"] > rel_start and s["start"] < rel_end]
        except Exception as exc:
            log(f"  {clip_id}: transcription unavailable ({type(exc).__name__}: {exc}); no captions")

    start_rel, end_rel = _snap_window(words, rel_start, rel_end, cfg)
    duration = round(end_rel - start_rel, 3)

    clip_words = [
        {"word": w["word"],
         "start": round(max(0.0, w["start"] - start_rel), 3),
         "end": round(max(0.0, min(duration, w["end"] - start_rel)), 3)}
        for w in words
        if w["end"] > start_rel and w["start"] < end_rel
    ]
    clip_segments = [
        {"start": round(max(0.0, s["start"] - start_rel), 3),
         "end": round(max(0.0, min(duration, s["end"] - start_rel)), 3),
         "text": s["text"]}
        for s in segments
        if s["end"] > start_rel and s["start"] < end_rel
    ]

    # Make the caption font portable: libass loads it from the run's font dir.
    fonts_dir = None
    if cfg.caption_font_file:
        font_src = Path(cfg.caption_font_file).expanduser()
        if font_src.is_file():
            font_dir = run_dir / "clips" / "_fonts"
            font_dir.mkdir(parents=True, exist_ok=True)
            if not (font_dir / font_src.name).is_file():
                shutil.copy2(font_src, font_dir / font_src.name)
            fonts_dir = "../_fonts"

    caption_files: list[Path] = []
    ass_name = None
    if cfg.captions and clip_words:
        ass_path = clip_dir / "captions.ass"
        ass_path.write_text(captions.build_ass(clip_words, cfg, clip_duration=duration), encoding="utf-8")
        caption_files.append(ass_path)
        ass_name = ass_path.name
    if clip_segments:
        srt_path = clip_dir / "captions.srt"
        srt_path.write_text(captions.build_srt(clip_segments), encoding="utf-8")
        caption_files.append(srt_path)

    # Render (times are material-relative) and self-correct loudness once.
    video = clip_dir / "clip.mp4"
    render.render_clip(material, video, start_rel, duration, cfg, ass_name=ass_name, fonts_dir=fonts_dir)
    stats = measure_loudness(video, cfg.loudness_target, cfg.loudness_tp)
    if stats and abs(stats["input_i"] - cfg.loudness_target) > 1.5:
        log(f"  {clip_id}: re-normalising {stats['input_i']:.1f} -> {cfg.loudness_target} LUFS")
        render.render_clip(material, video, start_rel, duration, cfg, ass_name=ass_name,
                           fonts_dir=fonts_dir, loudnorm_stats=stats)

    result = qc_mod.run_qc(video, caption_files, cfg,
                           expected_min=cfg.clip_min_s, expected_max=cfg.clip_max_s)

    meta = package.build_metadata(
        {"words": clip_words, "segments": clip_segments, "language": language},
        cfg,
        context={"source_title": info.get("title"), "uploader": info.get("uploader"),
                 "start": round(cand.start, 1), "end": round(cand.end, 1), "signal": cand.signal},
        fallback_title=(clip_segments[0]["text"] if clip_segments else ""),
    )
    meta.update({"clip_id": clip_id, "start": round(cand.start, 2), "end": round(cand.end, 2),
                 "duration": duration, "language": language})

    files = [video, *caption_files]
    if cfg.thumbnails:
        peak_in_clip = max(0.4, (cand.peak_t - offset) - start_rel)
        try:
            thumb, thumb_info = package.make_thumbnail(
                video, clip_dir / "thumb.jpg",
                meta.get("hook") or meta.get("title") or clip_id, cfg, at=peak_in_clip,
            )
            meta["thumbnail"] = thumb_info
            files.append(thumb)
        except Exception as exc:
            log(f"  {clip_id}: thumbnail failed ({type(exc).__name__}: {exc})")
    write_json(clip_dir / "metadata.json", meta)

    package.write_manifest(clip_dir, source={"url": url, "title": info.get("title")},
                           candidate=cand, qc=result, files=files)

    return {
        "id": clip_id, "dir": str(clip_dir), "video": str(video),
        "start": round(cand.start, 2), "end": round(cand.end, 2), "duration": duration,
        "score": cand.score, "signal": cand.signal,
        "qc_passed": result["passed"], "qc_failed": result["failed"],
        "qc_warnings": result["warnings"], "lufs": result.get("lufs"),
        "title": meta.get("title"), "hook": meta.get("hook"),
        "has_captions": bool(caption_files),
    }
