"""Rendering: one ffmpeg pass per clip (cut + reframe + captions + audio)."""
from __future__ import annotations

import functools
import shutil
from pathlib import Path

from .util import debug, ffmpeg, log, run

WIDTH, HEIGHT = 1080, 1920
FPS = 30


@functools.lru_cache(maxsize=1)
def _available_encoders() -> str:
    try:
        return run(["ffmpeg", "-hide_banner", "-encoders"], check=False).stdout or ""
    except Exception:
        return ""


def pick_encoder(cfg) -> tuple[str, list[str]]:
    """Return (codec, quality args). 'auto' prefers NVENC when the GPU supports it."""
    requested = cfg.encoder
    encoders = _available_encoders()
    if requested == "auto":
        if "h264_nvenc" in encoders:
            requested = "h264_nvenc"
        elif "h264_videotoolbox" in encoders:
            requested = "h264_videotoolbox"
        else:
            requested = "libx264"

    if requested == "h264_nvenc":
        return "h264_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", str(cfg.crf), "-b:v", "0"]
    if requested == "hevc_nvenc":
        return "hevc_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", str(cfg.crf + 2), "-b:v", "0"]
    if requested == "h264_videotoolbox":
        return "h264_videotoolbox", ["-q:v", str(max(40, 100 - cfg.crf * 3))]
    return "libx264", ["-preset", "veryfast", "-crf", str(cfg.crf)]


def _video_chain(cfg, ass_name: str | None, fonts_dir: str | None = None) -> str:
    if cfg.reframe == "crop":
        head = (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT}[v0];"
        )
    elif cfg.reframe == "fit":
        # Full frame visible over a blurred copy: HUD, minimap and on-screen
        # subtitles survive instead of being cropped off.
        head = (
            f"[0:v]split=2[bg0][fg0];"
            f"[bg0]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},boxblur=luma_radius=32:luma_power=2,"
            f"eq=brightness=-0.09:saturation=0.55[bg];"
            f"[fg0]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2-70[v0];"
        )
    else:
        raise ValueError(f"unknown reframe mode: {cfg.reframe}")

    if ass_name:
        arg = f"ass={ass_name}" + (f":fontsdir={fonts_dir}" if fonts_dir else "")
        head += f"[v0]{arg}[v1];"
    else:
        head += "[v0]null[v1];"
    head += f"[v1]fps={FPS},setsar=1,format=yuv420p[vout]"
    return head


def _loudnorm(cfg, stats: dict | None) -> str:
    base = f"loudnorm=I={cfg.loudness_target}:TP={cfg.loudness_tp}:LRA=11"
    if not stats:
        return base
    try:
        return (
            base
            + f":measured_I={stats['input_i']}"
            + f":measured_TP={stats['input_tp']}"
            + f":measured_LRA={stats.get('input_lra', 7.0)}"
            + f":measured_thresh={stats.get('input_thresh', -30.0)}"
            + f":offset={stats.get('target_offset', 0.0)}"
            + ":linear=true"
        )
    except (KeyError, TypeError):
        return base


def _audio_chain(cfg, with_music: bool, stats: dict | None) -> str:
    norm = _loudnorm(cfg, stats)
    if with_music:
        return (
            f"[1:a]aloop=loop=-1:size=2147483647,volume={cfg.music_gain},"
            f"aformat=sample_fmts=fltp:sample_rates=48000[mus];"
            f"[mus][0:a]sidechaincompress=threshold=0.03:ratio=12:attack=20:release=350[duck];"
            f"[0:a][duck]amix=inputs=2:duration=first:dropout_transition=0,{norm},aresample=48000[aout]"
        )
    return f"[0:a]{norm},aresample=48000[aout]"


def render_clip(
    source: Path,
    out_path: Path,
    start: float,
    duration: float,
    cfg,
    *,
    ass_name: str | None = None,
    fonts_dir: str | None = None,
    loudnorm_stats: dict | None = None,
) -> Path:
    """Cut `source` to a 1080x1920 clip. `ass_name` and `fonts_dir` are relative to out_path.parent."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    codec, quality = pick_encoder(cfg)
    music = Path(cfg.music).expanduser().resolve() if cfg.music else None
    if music and not music.is_file():
        log(f"music file missing, ignoring: {music}")
        music = None

    graph = _video_chain(cfg, ass_name, fonts_dir) + ";" + _audio_chain(cfg, bool(music), loudnorm_stats)

    args: list = [
        "-y",
        "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{max(0.5, duration):.3f}",
        "-i", str(source),
    ]
    if music:
        args += ["-i", str(music)]
    args += [
        "-filter_complex", graph,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", codec,
        *quality,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        out_path.name,
    ]
    debug(f"render {out_path.name} from {start:.2f}s for {duration:.2f}s")
    ffmpeg(args, cwd=out_path.parent, timeout=3600)
    return out_path


def extract_frame(video: Path, at: float, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(
        ["-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", out_path.name],
        cwd=out_path.parent,
    )
    return out_path


def ffmpeg_present() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
