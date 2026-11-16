"""Subprocess, ffprobe, loudness and small IO helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

VERBOSE = os.environ.get("CLIPFORGE_VERBOSE", "") not in ("", "0", "false")


def log(msg: str) -> None:
    print(f"[clipforge] {msg}", file=sys.stderr, flush=True)


def debug(msg: str) -> None:
    if VERBOSE:
        print(f"[clipforge:debug] {msg}", file=sys.stderr, flush=True)


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def run(
    cmd: Sequence[Any],
    *,
    check: bool = True,
    cwd: Path | None = None,
    timeout: float | None = None,
    stdin: Any = None,
) -> subprocess.CompletedProcess:
    argv = [str(c) for c in cmd]
    debug("$ " + " ".join(argv))
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=stdin,
        errors="replace",
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-4000:]
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(argv)}\n{tail}")
    return proc


def ffmpeg(args: Sequence[Any], *, cwd: Path | None = None, check: bool = True, timeout: float | None = None):
    return run(["ffmpeg", "-hide_banner", "-nostdin", *args], cwd=cwd, check=check, timeout=timeout)


def ffprobe(path: Path) -> dict:
    proc = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    )
    return json.loads(proc.stdout or "{}")


def probe_summary(path: Path) -> dict:
    info = ffprobe(path)
    vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    astreams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    v = vstreams[0] if vstreams else {}
    fmt = info.get("format", {})

    def _fps(rate: str | None) -> float:
        if not rate or "/" not in rate:
            return 0.0
        num, den = rate.split("/", 1)
        try:
            return round(float(num) / float(den), 3) if float(den) else 0.0
        except ValueError:
            return 0.0

    return {
        "path": str(path),
        "duration": round(float(fmt.get("duration") or 0.0), 3),
        "size_bytes": int(fmt.get("size") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": _fps(v.get("avg_frame_rate")),
        "vcodec": v.get("codec_name"),
        "acodec": (astreams[0].get("codec_name") if astreams else None),
        "has_audio": bool(astreams),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def measure_loudness(path: Path, target: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> dict:
    """Run loudnorm in analysis mode and return its measurement block."""
    proc = ffmpeg(
        [
            "-i", path,
            "-map", "0:a:0",
            "-af", f"loudnorm=I={target}:TP={tp}:LRA={lra}:print_format=json",
            "-f", "null", "-",
        ],
        check=False,
    )
    blob = (proc.stderr or "")
    match = _LOUDNORM_JSON.search(blob) or _LOUDNORM_JSON.search(blob[::-1])  # last block wins normally
    text = match.group(0) if match else None
    if not text:
        # fall back to the last balanced object
        start = blob.rfind("{")
        end = blob.rfind("}")
        text = blob[start : end + 1] if start != -1 and end > start else None
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    try:
        data["input_i"] = float(data.get("input_i"))
        data["input_tp"] = float(data.get("input_tp"))
    except (TypeError, ValueError):
        return {}
    return data


def ebur128_integrated(path: Path) -> float | None:
    """Independent integrated-LUFS measurement via ebur128 (used by QC)."""
    proc = ffmpeg(
        ["-i", path, "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
        check=False,
    )
    text = proc.stderr or ""
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", text)
    if not matches:
        return None
    return float(matches[-1])


def black_segments(path: Path, min_duration: float = 0.5, pix_th: float = 0.10) -> list[tuple[float, float]]:
    proc = ffmpeg(
        ["-i", path, "-an", "-vf", f"blackdetect=d={min_duration}:pix_th={pix_th}", "-f", "null", "-"],
        check=False,
    )
    out: list[tuple[float, float]] = []
    for m in re.finditer(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)", proc.stderr or ""):
        out.append((float(m.group(1)), float(m.group(2))))
    return out


def audio_energy_series(path: Path, bucket: float = 1.0) -> list[dict]:
    """Cheap loudness curve (RMS per bucket) used when no chat data exists."""
    rate = 16000
    samples = max(1, int(rate * bucket))
    proc = ffmpeg(
        [
            "-i", path,
            "-map", "0:a:0",
            "-af", f"aresample={rate},asetnsamples=n={samples},astats=metadata=1:reset=1,"
                   "ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-f", "null", "-",
        ],
        check=False,
        timeout=1800,
    )
    points: list[dict] = []
    t = 0.0
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if line.startswith("frame:") and "pts_time:" in line:
            m = re.search(r"pts_time:(\d+(?:\.\d+)?)", line)
            if m:
                t = float(m.group(1))
        elif "lavfi.astats.Overall.RMS_level" in line and "=" in line:
            try:
                value = float(line.rsplit("=", 1)[1])
            except ValueError:
                continue
            points.append({"t": round(t, 2), "db": value})
    return points


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:limit] or "run").strip("-")


def human_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def find_font_file(explicit: str | None = None) -> Path | None:
    candidates = [explicit] if explicit else []
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/impact.ttf",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.is_file():
            return p
    return None


def iter_files(root: Path, pattern: str) -> Iterable[Path]:
    return sorted(p for p in Path(root).glob(pattern) if p.is_file())
