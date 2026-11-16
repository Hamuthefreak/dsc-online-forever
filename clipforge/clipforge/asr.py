"""Word-level transcription via faster-whisper (lazy import, models cached)."""
from __future__ import annotations

import tempfile
import wave
from pathlib import Path

from .util import debug, ffmpeg, log

_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
SAMPLE_RATE = 16000


def _device(cfg) -> str:
    if cfg.asr_device != "auto":
        return cfg.asr_device
    try:
        import ctranslate2  # ships with faster-whisper

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def _compute(device: str, cfg) -> str:
    if cfg.asr_compute != "auto":
        return cfg.asr_compute
    return "float16" if device == "cuda" else "int8"


def _model(cfg):
    from faster_whisper import WhisperModel

    device = _device(cfg)
    compute = _compute(device, cfg)
    key = (cfg.asr_model, device, compute)
    if key not in _MODEL_CACHE:
        log(f"loading whisper model={cfg.asr_model} device={device} compute={compute}")
        _MODEL_CACHE[key] = WhisperModel(cfg.asr_model, device=device, compute_type=compute)
    return _MODEL_CACHE[key]


def _load_audio(path: Path) -> "object":
    """Decode to 16 kHz mono float32 ourselves.

    faster-whisper's own decoder uses PyAV's `metadata_errors` argument, which
    PyAV 19 removed. Decoding with ffmpeg and passing an array sidesteps that
    version trap entirely (and reuses the same ffmpeg the pipeline already needs).
    """
    import numpy as np

    path = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix="clipforge-asr-") as tmp:
        wav_path = Path(tmp) / "audio.wav"
        ffmpeg(
            ["-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-f", "wav", wav_path.name],
            cwd=Path(tmp),
        )
        with wave.open(str(wav_path), "rb") as handle:
            raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return samples / 32768.0


def transcribe(path: Path, cfg) -> dict:
    """Return {"language", "segments", "words"} with times relative to `path`."""
    model = _model(cfg)
    audio = _load_audio(path)
    segments_iter, info = model.transcribe(
        audio,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        language=cfg.asr_language,
        condition_on_previous_text=False,
    )
    segments: list[dict] = []
    words: list[dict] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if text:
            segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text})
        for word in getattr(seg, "words", None) or []:
            token = (word.word or "").strip()
            if not token:
                continue
            words.append({"word": token, "start": round(word.start, 3), "end": round(word.end, 3)})
    debug(f"transcribed {path.name}: {len(words)} words")
    return {
        "language": getattr(info, "language", None),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
        "segments": segments,
        "words": words,
    }


def words_from_global(transcript: dict, start: float, end: float) -> dict:
    """Slice an absolute-time transcript down to a clip window and rebase to 0."""
    words = []
    for w in transcript.get("words") or []:
        if w["end"] <= start or w["start"] >= end:
            continue
        words.append(
            {
                "word": w["word"],
                "start": round(max(0.0, w["start"] - start), 3),
                "end": round(max(0.0, min(end, w["end"]) - start), 3),
            }
        )
    segments = []
    for s in transcript.get("segments") or []:
        if s["end"] <= start or s["start"] >= end:
            continue
        segments.append(
            {
                "start": round(max(0.0, s["start"] - start), 3),
                "end": round(max(0.0, min(end, s["end"]) - start), 3),
                "text": s["text"],
            }
        )
    return {"language": transcript.get("language"), "segments": segments, "words": words}
