"""Caption file generation: word-level ASS (burned) plus portable SRT."""
from __future__ import annotations

import re

_SAFE = re.compile(r"[{}\\]")
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,%(font)s,%(size)d,&H00FFFFFF,&H0000D7FF,&H00121212,&H96000000,-1,0,0,0,100,100,0,0,1,6,4,2,70,70,%(margin_v)d,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total_cs = int(round(seconds * 100))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clean(text: str) -> str:
    text = _SAFE.sub("", text or "").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def group_lines(words: list[dict], *, max_words: int = 3, max_chars: int = 20,
                max_duration: float = 3.0, gap_break: float = 0.65) -> list[list[dict]]:
    """Group word timings into caption lines that read well on a phone."""
    lines: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        if not current:
            current = [word]
            continue
        prev = current[-1]
        gap = word["start"] - prev["end"]
        chars = len(" ".join(w["word"] for w in current + [word]))
        span = word["end"] - current[0]["start"]
        if gap > gap_break or len(current) >= max_words or chars > max_chars or span > max_duration:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)
    return lines


def build_ass(words: list[dict], cfg, *, clip_duration: float | None = None) -> str:
    header = _ASS_HEADER % {
        "font": cfg.caption_font,
        "size": int(cfg.caption_font_size),
        "margin_v": int(cfg.caption_margin_v),
    }
    lines = group_lines(words, max_words=cfg.caption_words_per_line)
    events: list[str] = []
    for line in lines:
        if not line:
            continue
        start = line[0]["start"]
        end = line[-1]["end"]
        if clip_duration:
            if start >= clip_duration:
                continue
            end = min(end, clip_duration)
        if end - start < 0.18:
            end = start + 0.18

        pieces: list[str] = []
        for i, word in enumerate(line):
            nxt = line[i + 1]["start"] if i + 1 < len(line) else word["end"]
            span = max(0.08, nxt - word["start"])
            text = _clean(word["word"])
            if not text:
                continue
            if cfg.caption_upper:
                text = text.upper()
            pieces.append(f"{{\\k{int(round(span * 100))}}}{text}")
        if not pieces:
            continue
        body = " ".join(pieces)
        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{{\\fad(40,40)}}{body}"
        )
    return header + "\n".join(events) + ("\n" if events else "")


def build_srt(segments: list[dict], *, max_chars: int = 42) -> str:
    out: list[str] = []
    for i, seg in enumerate(segments, start=1):
        text = _clean(seg.get("text") or "")
        if not text:
            continue
        words = text.split()
        rows: list[str] = []
        row = ""
        for w in words:
            if len(row) + len(w) + 1 > max_chars and row:
                rows.append(row)
                row = w
            else:
                row = f"{row} {w}".strip()
        if row:
            rows.append(row)
        if len(rows) > 2:
            rows = [" ".join(rows)]
        out.append(f"{i}\n{srt_time(seg['start'])} --> {srt_time(seg['end'])}\n" + "\n".join(rows) + "\n")
    return "\n".join(out)


def caption_span(words: list[dict]) -> tuple[float, float] | None:
    if not words:
        return None
    return (words[0]["start"], words[-1]["end"])
