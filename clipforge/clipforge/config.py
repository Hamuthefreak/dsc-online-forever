"""Settings, resolved from CLI flags over environment variables over defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
BUNDLED_FONT = ASSET_DIR / "Roboto-Bold.ttf"


@dataclass
class Settings:
    # where runs live
    out_dir: Path = field(default_factory=lambda: Path(_env("CLIPFORGE_OUT", "./runs") or "./runs"))

    # selection
    min_clips: int = field(default_factory=lambda: _env_int("CLIPFORGE_MIN_CLIPS", 4))
    max_clips: int = field(default_factory=lambda: _env_int("CLIPFORGE_MAX_CLIPS", 10))
    clip_min_s: float = field(default_factory=lambda: _env_float("CLIPFORGE_CLIP_MIN_S", 15.0))
    clip_max_s: float = field(default_factory=lambda: _env_float("CLIPFORGE_CLIP_MAX_S", 45.0))
    pre_roll: float = field(default_factory=lambda: _env_float("CLIPFORGE_PRE_ROLL", 6.0))
    post_roll: float = field(default_factory=lambda: _env_float("CLIPFORGE_POST_ROLL", 18.0))
    min_gap_s: float = field(default_factory=lambda: _env_float("CLIPFORGE_MIN_GAP_S", 75.0))
    peak_z: float = field(default_factory=lambda: _env_float("CLIPFORGE_PEAK_Z", 2.5))
    chat_bucket_s: float = 1.0

    # ingest
    max_source_height: int = field(default_factory=lambda: _env_int("CLIPFORGE_MAX_HEIGHT", 1080))
    live_minutes: float = field(default_factory=lambda: _env_float("CLIPFORGE_LIVE_MINUTES", 45.0))
    cookies_file: str | None = field(default_factory=lambda: _env("CLIPFORGE_COOKIES"))
    ytdlp_extra: str = field(default_factory=lambda: _env("CLIPFORGE_YTDLP_EXTRA", "") or "")

    # asr
    asr_model: str = field(default_factory=lambda: _env("CLIPFORGE_ASR_MODEL", "small") or "small")
    asr_device: str = field(default_factory=lambda: _env("CLIPFORGE_ASR_DEVICE", "auto") or "auto")
    asr_compute: str = field(default_factory=lambda: _env("CLIPFORGE_ASR_COMPUTE", "auto") or "auto")
    asr_language: str | None = field(default_factory=lambda: _env("CLIPFORGE_ASR_LANGUAGE"))

    # captions
    captions: bool = field(default_factory=lambda: _env_bool("CLIPFORGE_CAPTIONS", True))
    caption_font: str = field(default_factory=lambda: _env("CLIPFORGE_CAPTION_FONT", "Roboto") or "Roboto")
    caption_font_file: str | None = field(
        default_factory=lambda: _env(
            "CLIPFORGE_CAPTION_FONT_FILE", str(BUNDLED_FONT) if BUNDLED_FONT.is_file() else None
        )
    )
    caption_font_size: int = field(default_factory=lambda: _env_int("CLIPFORGE_CAPTION_FONT_SIZE", 76))
    caption_margin_v: int = field(default_factory=lambda: _env_int("CLIPFORGE_CAPTION_MARGIN_V", 300))
    caption_upper: bool = field(default_factory=lambda: _env_bool("CLIPFORGE_CAPTION_UPPER", True))
    caption_words_per_line: int = field(default_factory=lambda: _env_int("CLIPFORGE_CAPTION_WORDS", 3))

    # audio + video
    loudness_target: float = field(default_factory=lambda: _env_float("CLIPFORGE_LOUDNESS", -14.0))
    loudness_tp: float = field(default_factory=lambda: _env_float("CLIPFORGE_TRUE_PEAK", -1.5))
    reframe: str = field(default_factory=lambda: _env("CLIPFORGE_REFRAME", "fit") or "fit")
    encoder: str = field(default_factory=lambda: _env("CLIPFORGE_ENCODER", "auto") or "auto")
    crf: int = field(default_factory=lambda: _env_int("CLIPFORGE_CRF", 19))
    music: str | None = field(default_factory=lambda: _env("CLIPFORGE_MUSIC"))
    music_gain: float = field(default_factory=lambda: _env_float("CLIPFORGE_MUSIC_GAIN", 0.16))

    # thumbnails
    thumbnails: bool = field(default_factory=lambda: _env_bool("CLIPFORGE_THUMBNAILS", True))

    # metadata
    openai_api_key: str | None = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "")
    openai_model: str = field(default_factory=lambda: _env("CLIPFORGE_LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini")

    # posting
    poster: str = field(default_factory=lambda: _env("CLIPFORGE_POSTER", "none") or "none")
    uploadpost_key: str | None = field(default_factory=lambda: _env("UPLOADPOST_API_KEY"))
    uploadpost_user: str | None = field(default_factory=lambda: _env("UPLOADPOST_USER"))
    uploadpost_url: str = field(default_factory=lambda: _env("UPLOADPOST_URL", "https://api.upload-post.com/api/upload") or "")
    postiz_key: str | None = field(default_factory=lambda: _env("POSTIZ_API_KEY"))
    postiz_url: str = field(default_factory=lambda: _env("POSTIZ_URL", "https://api.postiz.com/public/v1") or "")

    def as_dict(self) -> dict:
        data = asdict(self)
        data["out_dir"] = str(self.out_dir)
        for secret in ("openai_api_key", "uploadpost_key", "postiz_key"):
            if data.get(secret):
                data[secret] = "***set***"
        return data

    @property
    def youtube(self) -> bool:  # pragma: no cover - trivial
        return True


def settings_with(settings: Settings, **overrides) -> Settings:
    data = asdict(settings)
    for key, value in overrides.items():
        if value is not None and key in data:
            data[key] = value
    return Settings(**data)
