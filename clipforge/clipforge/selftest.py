"""Self-test: synthesise media, run the whole pipeline, assert the output is real."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .config import Settings
from .pipeline import RunOptions, run_pipeline
from .util import ffmpeg, log, probe_summary, write_json


def _make_video(path: Path, seconds: float = 100.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(
        [
            "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=320:sample_rate=48000",
            "-t", f"{seconds}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            path.name,
        ],
        cwd=path.parent,
        timeout=900,
    )
    return path


def _chat_series(seconds: int = 100, peaks=(20, 55, 85)) -> dict:
    counts = [3] * seconds
    emotes = [0] * seconds
    for peak in peaks:
        for offset, bump in ((-2, 18), (-1, 34), (0, 62), (1, 28), (2, 12)):
            idx = peak + offset
            if 0 <= idx < seconds:
                counts[idx] += bump
                emotes[idx] += bump * 2
    return {
        "source": "chat",
        "bucket": 1.0,
        "duration": float(seconds),
        "messages": sum(counts),
        "counts": counts,
        "emotes": emotes,
    }


def _transcript(seconds: int = 100) -> dict:
    """Speech clusters with gaps, so boundary snapping has something to snap to."""
    words: list[dict] = []
    segments: list[dict] = []
    vocabulary = ["look", "at", "this", "insane", "moment", "right", "here", "no", "way", "that", "just", "happened"]
    t = 6.0
    while t < seconds - 8:
        seg_words = []
        for i in range(6):
            word = vocabulary[(len(words) + i) % len(vocabulary)]
            start = round(t + i * 0.42, 2)
            end = round(start + 0.36, 2)
            words.append({"word": word, "start": start, "end": end})
            seg_words.append(word)
        segments.append({"start": words[-6]["start"], "end": words[-1]["end"], "text": " ".join(seg_words)})
        t += 11.0
    return {"language": "en", "words": words, "segments": segments}


def run_selftest(keep: bool = False) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="clipforge-selftest-"))
    log(f"selftest workspace: {tmp}")
    video = _make_video(tmp / "synthetic.mp4")
    chat_path = write_json(tmp / "chat.json", _chat_series())
    transcript_path = write_json(tmp / "transcript.json", _transcript())

    cfg = Settings()
    cfg.out_dir = tmp / "runs"
    cfg.min_clips = 2
    cfg.max_clips = 3
    cfg.clip_min_s = 8.0
    cfg.clip_max_s = 20.0
    cfg.min_gap_s = 20.0
    cfg.captions = True
    cfg.thumbnails = True
    cfg.reframe = "fit"

    result = run_pipeline(RunOptions(
        settings=cfg,
        source=video,
        chat_json=chat_path,
        transcript_json=transcript_path,
        run_id="selftest",
    ))

    run = result["runs"][0]
    clips = run["clips"]
    failures: list[str] = []
    if not clips:
        failures.append(f"no clips produced: {run.get('error')}")
    for clip in clips:
        info = probe_summary(Path(clip["video"]))
        if (info["width"], info["height"]) != (1080, 1920):
            failures.append(f"{clip['id']} wrong size {info['width']}x{info['height']}")
        if not (Path(clip["dir"]) / "captions.ass").is_file():
            failures.append(f"{clip['id']} missing burned-caption file")
        if not (Path(clip["dir"]) / "thumb.jpg").is_file():
            failures.append(f"{clip['id']} missing thumbnail")
        if not (Path(clip["dir"]) / "manifest.json").is_file():
            failures.append(f"{clip['id']} missing manifest")
        if not clip.get("qc_passed"):
            failures.append(f"{clip['id']} failed QC: {clip.get('qc_failed')}")
        print(f"  {clip['id']}: {info['width']}x{info['height']} {info['duration']:.1f}s "
              f"{clip.get('lufs')} LUFS qc={'ok' if clip.get('qc_passed') else 'FAIL'}")

    ok = not failures
    print("\nSELFTEST " + ("PASS" if ok else "FAIL"))
    for failure in failures:
        print(f"  - {failure}")
    if keep or not ok:
        print(f"  artifacts kept in {tmp}")
    else:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return ok
