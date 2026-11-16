"""Packaging: thumbnails, platform metadata and the delivery manifest."""
from __future__ import annotations

import json
import re
import shutil
import urllib.request
from pathlib import Path

from .util import find_font_file, ffmpeg, log, probe_summary, sha256, write_json

PLATFORM_TAGS = {
    "tiktok": ["#gta6", "#gta", "#gaming", "#clips", "#fyp"],
    "youtube": ["#gta6", "#gta6clips", "#gaming", "#shorts"],
    "instagram": ["#gta6", "#gta", "#gamingclips", "#reels"],
}

THUMB_W, THUMB_H = 1080, 1920


# ---------------------------------------------------------------- thumbnails
def _sharpness(path: Path) -> float:
    """Variance of edges: picks the frame that is not mid-motion-blur."""
    try:
        from PIL import Image, ImageFilter, ImageStat
    except Exception:
        return float(path.stat().st_size)
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            return float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0])
    except Exception:
        return float(path.stat().st_size)


def _wrap(text: str, limit: int = 24, max_lines: int = 2) -> list[str]:
    words = re.sub(r"\s+", " ", text or "").strip().split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit and current:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines[:max_lines]


TEXT_MAX_W = 900


def _fit_text(draw, text: str, font_file: Path | None, *, max_lines: int = 2) -> tuple[list[str], object]:
    """Wrap by measured width, shrinking the font until it fits the frame."""
    from PIL import ImageFont

    words = text.split()
    size = 108
    lines: list[str] = []
    font: object = ImageFont.load_default()
    while size >= 54:
        try:
            font = ImageFont.truetype(str(font_file), size) if font_file else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and draw.textlength(trial, font=font) > TEXT_MAX_W:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        widest = max((draw.textlength(line, font=font) for line in lines), default=0)
        if len(lines) <= max_lines and widest <= TEXT_MAX_W:
            return lines, font
        size -= 8
    return lines[:max_lines], font


def _compose(base: Path, out: Path, text: str, font_file: Path | None) -> tuple[bool, list[str]]:
    """Cover-crop to 1080x1920, darken the top, stamp the hook. Pure Pillow."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False, []
    try:
        with Image.open(base) as raw:
            img = raw.convert("RGB")
        ratio = max(THUMB_W / img.width, THUMB_H / img.height)
        img = img.resize((max(THUMB_W, round(img.width * ratio)), max(THUMB_H, round(img.height * ratio))),
                         Image.LANCZOS)
        left = (img.width - THUMB_W) // 2
        top = (img.height - THUMB_H) // 2
        img = img.crop((left, top, left + THUMB_W, top + THUMB_H))

        draw = ImageDraw.Draw(img, "RGBA")
        draw.rectangle([0, 0, THUMB_W, int(THUMB_H * 0.40)], fill=(0, 0, 0, 120))

        lines, font = _fit_text(draw, text, font_file)
        if not lines:
            img.save(out, "JPEG", quality=90, optimize=True)
            return True, []

        size = getattr(font, "size", 80) or 80
        stroke = max(6, int(size // 11))
        heights = []
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
            heights.append((box[2] - box[0], box[3] - box[1]))

        total = sum(h for _, h in heights) + (len(lines) - 1) * 34
        y = max(200, (int(THUMB_H * 0.40) - total) // 2)
        draw.rectangle([48, y, 62, y + total], fill=(255, 210, 0, 255))
        for line, (width, height) in zip(lines, heights):
            draw.text(((THUMB_W - width) // 2, y), line, font=font, fill=(255, 255, 255, 255),
                      stroke_width=stroke, stroke_fill=(10, 10, 10, 255))
            y += height + 34
        img.save(out, "JPEG", quality=90, optimize=True)
        return True, lines
    except Exception as exc:
        log(f"thumbnail compose failed ({type(exc).__name__}: {exc})")
        return False, []


def make_thumbnail(clip: Path, out: Path, hook: str, cfg, *, at: float | None = None) -> tuple[Path, dict]:
    """Pick the sharpest of three frames, then stamp the hook on it."""
    clip = Path(clip).resolve()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / "_frames"
    tmp.mkdir(parents=True, exist_ok=True)

    info = probe_summary(clip)
    duration = info["duration"] or 1.0
    ratios = [0.25, 0.45, 0.65] if at is None else [max(0.02, min(0.95, at / duration))]
    frames: list[tuple[float, Path]] = []
    for ratio in ratios:
        frame = tmp / f"f_{int(ratio * 100):02d}.jpg"
        ffmpeg(["-y", "-ss", f"{duration * ratio:.3f}", "-i", str(clip),
                "-frames:v", "1", "-q:v", "2", frame.name], cwd=tmp)
        if frame.is_file():
            frames.append((ratio, frame))
    if not frames:
        raise RuntimeError(f"no frame could be extracted from {clip}")

    picked_ratio, base = sorted(frames, key=lambda item: _sharpness(item[1]), reverse=True)[0]

    font = find_font_file(cfg.caption_font_file)
    composed, lines = _compose(base, out, (hook or "WATCH THIS").upper(), font)
    shutil.rmtree(tmp, ignore_errors=True)  # scratch frames, ~250 KB per clip
    if not composed:
        out.write_bytes(base.read_bytes())
        return out, {"frame_at": round(duration * picked_ratio, 2), "text_overlay": False,
                     "reason": "compositor unavailable"}
    return out, {"frame_at": round(duration * picked_ratio, 2), "text_overlay": True, "lines": lines}


# ---------------------------------------------------------------- metadata
def _heuristic_metadata(candidate_title: str, transcript_text: str) -> dict:
    text = re.sub(r"\s+", " ", transcript_text or "").strip()
    hook = candidate_title or "WATCH THIS"
    first_sentence = re.split(r"[.!?]", text)[0][:90].strip() if text else hook
    title = (first_sentence or hook)[:90]
    return {
        "title": title,
        "hook": hook[:40].upper(),
        "description": f"{title}\n\n{text[:280]}".strip(),
        "hashtags": {k: list(v) for k, v in PLATFORM_TAGS.items()},
        "source": "heuristic",
    }


def _llm_metadata(transcript_text: str, cfg, context: dict) -> dict | None:
    if not cfg.openai_api_key:
        return None
    prompt = (
        "You write packaging for short vertical gameplay clips. Given the transcript, return JSON with "
        "keys: title (<=70 chars, no clickbait lies), hook (<=32 chars, uppercase-friendly, shown on the "
        "thumbnail), description (<=240 chars), hashtags (object with tiktok/youtube/instagram arrays of "
        f"4 tags). Context: {json.dumps(context)}\n\nTRANSCRIPT:\n{transcript_text[:4000]}"
    )
    payload = json.dumps(
        {
            "model": cfg.openai_model,
            "messages": [
                {"role": "system", "content": "Reply with JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
        }
    ).encode()
    req = urllib.request.Request(
        f"{cfg.openai_base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.openai_api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
        content = body["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
        data = json.loads(content)
        data.setdefault("hashtags", {k: list(v) for k, v in PLATFORM_TAGS.items()})
        data["source"] = "llm"
        return data
    except Exception as exc:
        log(f"metadata LLM failed ({type(exc).__name__}) -> heuristic")
        return None


def build_metadata(transcript: dict, cfg, *, context: dict, fallback_title: str = "") -> dict:
    text = " ".join(s.get("text", "") for s in transcript.get("segments") or [])
    if not text:
        text = " ".join(w.get("word", "") for w in transcript.get("words") or [])
    meta = _llm_metadata(text, cfg, context) or _heuristic_metadata(fallback_title, text)
    tags = meta.get("hashtags") or PLATFORM_TAGS
    meta["platforms"] = {
        "tiktok": {"caption": f"{meta['title']} {' '.join(tags.get('tiktok', []))}".strip()},
        "youtube": {"title": meta["title"], "description": meta["description"],
                    "tags": tags.get("youtube", [])},
        "instagram": {"caption": f"{meta['hook']}\n\n{meta['title']} {' '.join(tags.get('instagram', []))}".strip()},
    }
    meta["transcript_excerpt"] = text[:600]
    return meta


# ---------------------------------------------------------------- manifest
def write_manifest(clip_dir: Path, *, source: dict, candidate, qc: dict, files: list[Path]) -> dict:
    manifest = {
        "source": source,
        "candidate": candidate.to_dict() if hasattr(candidate, "to_dict") else candidate,
        "qc": {"passed": qc["passed"], "failed": qc["failed"],
               "warnings": qc["warnings"], "lufs": qc.get("lufs")},
        "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)}
                  for p in files if p.is_file()},
    }
    write_json(clip_dir / "manifest.json", manifest)
    return manifest
