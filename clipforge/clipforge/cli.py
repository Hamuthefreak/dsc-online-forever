"""clipforge command line: run / review / post / status / doctor / selftest."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__, ingest
from .config import Settings
from .pipeline import RunOptions, run_pipeline
from .review import decide, load, summary
from .util import human_duration, log, read_json, write_json


def _ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    out = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        out.append(part if part.startswith("clip_") else f"clip_{int(part):02d}")
    return out


def _apply_flags(cfg: Settings, args) -> Settings:
    for attr, key in (
        ("min_clips", "min_clips"), ("max_clips", "max_clips"),
        ("clip_min_s", "clip_min_s"), ("clip_max_s", "clip_max_s"),
        ("asr_model", "asr_model"), ("reframe", "reframe"), ("encoder", "encoder"),
        ("music", "music"), ("caption_font", "caption_font"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, key, value)
    if getattr(args, "out", None):
        cfg.out_dir = Path(args.out)
    if getattr(args, "no_captions", False):
        cfg.captions = False
    if getattr(args, "no_thumbnails", False):
        cfg.thumbnails = False
    return cfg


def cmd_run(args) -> int:
    cfg = _apply_flags(Settings(), args)
    result = run_pipeline(RunOptions(
        urls=list(args.urls or []),
        settings=cfg,
        run_id=args.run_id,
        live=args.live,
        source=Path(args.source) if args.source else None,
        chat_json=Path(args.chat_json) if args.chat_json else None,
        transcript_json=Path(args.transcript_json) if args.transcript_json else None,
        keep_source=args.keep_source,
    ))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for run in result["runs"]:
        print(f"\n{run['run_id']}  ({run.get('signal')})  -> {run['dir']}")
        if run.get("error"):
            print(f"  ERROR: {run['error']}")
        for clip in run["clips"]:
            flags = "QC ok" if clip.get("qc_passed") else f"QC FAIL {clip.get('qc_failed')}"
            print(f"  {clip['id']}  {clip['duration']:>5.1f}s  {flags}  {clip.get('title') or ''}")
        if run["clips"]:
            print(f"\nNext: clipforge review {run['run_id']} --approve-all")
    return 0


def _run_dir(name: str, args) -> Path:
    cfg = Settings()
    if getattr(args, "out", None):
        cfg.out_dir = Path(args.out)
    path = Path(cfg.out_dir).expanduser().resolve() / name
    if not path.is_dir():
        raise SystemExit(f"run not found: {path}")
    return path


def cmd_review(args) -> int:
    run_dir = _run_dir(args.run, args)
    state = decide(run_dir, _ids(args.approve), _ids(args.reject),
                   approve_all=args.approve_all, note=args.note or "")
    if not (args.approve or args.reject or args.approve_all):
        print(json.dumps(summary(run_dir), indent=2))
        return 0
    for cid, entry in sorted(state["clips"].items()):
        print(f"  {cid}  {entry['status']:<9} {entry.get('title') or ''}")
    return 0


def cmd_post(args) -> int:
    from .post import post_run

    run_dir = _run_dir(args.run, args)
    cfg = Settings()
    if args.poster:
        cfg.poster = args.poster
    platforms = [p.strip() for p in (args.platforms or "tiktok,youtube,instagram").split(",") if p.strip()]
    dry_run = not args.yes
    result = post_run(run_dir, platforms, cfg, dry_run=dry_run)
    print(json.dumps(result, indent=2))
    if dry_run:
        print("\nDry run only. Re-run with --yes to actually publish (needs CLIPFORGE_POSTER + keys).")
    return 0


def cmd_status(args) -> int:
    run_dir = _run_dir(args.run, args)
    data = summary(run_dir)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"run: {data['run']}")
    print(f"counts: {data['counts']}")
    for clip in data["clips"]:
        print(f"  {clip['id']}  {clip.get('status'):<9} {human_duration(clip.get('duration') or 0)}  "
              f"qc={'ok' if clip.get('qc_passed') else 'fail'}  {clip.get('video')}")
    return 0


def cmd_doctor(args) -> int:
    cfg = Settings()
    rows = []
    rows.append(("ffmpeg", shutil.which("ffmpeg") or "MISSING"))
    rows.append(("ffprobe", shutil.which("ffprobe") or "MISSING"))
    rows.append(("yt-dlp", shutil.which("yt-dlp") or "MISSING"))
    try:
        import faster_whisper  # noqa: F401

        rows.append(("faster-whisper", "ok"))
    except Exception:
        rows.append(("faster-whisper", "MISSING (pip install faster-whisper)"))
    try:
        import chat_downloader  # noqa: F401

        rows.append(("chat-downloader", "ok"))
    except Exception:
        rows.append(("chat-downloader", "MISSING (optional, pip install chat-downloader)"))
    try:
        import mcp  # noqa: F401

        rows.append(("mcp", "ok"))
    except Exception:
        rows.append(("mcp", "MISSING (optional, pip install mcp)"))
    try:
        import ctranslate2

        rows.append(("cuda devices", str(ctranslate2.get_cuda_device_count())))
    except Exception:
        rows.append(("cuda devices", "unknown"))
    from .util import find_font_file

    rows.append(("font file", str(find_font_file(cfg.caption_font_file) or "not found (thumb text disabled)")))
    has_llm = "set" if cfg.openai_api_key else "not set (heuristic metadata)"
    rows.append(("OPENAI_API_KEY", has_llm))
    rows.append(("poster", cfg.poster))
    print(f"clipforge {__version__}")
    for name, value in rows:
        print(f"  {name:<18} {value}")
    print(f"  {'out_dir':<18} {Path(cfg.out_dir).expanduser().resolve()}")
    ok = shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("yt-dlp")
    return 0 if ok else 1


def cmd_selftest(args) -> int:
    from .selftest import run_selftest

    ok = run_selftest()
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("clipforge", description="Stream VOD -> platform-ready vertical clips.")
    p.add_argument("--version", action="version", version=f"clipforge {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="ingest -> clips in one command")
    run.add_argument("urls", nargs="*", help="Twitch/Kick/YouTube VOD or live URL(s)")
    run.add_argument("--source", help="local video file instead of a URL")
    run.add_argument("--live", action="store_true", help="capture a live stream instead of a VOD")
    run.add_argument("--min-clips", type=int, dest="min_clips")
    run.add_argument("--max-clips", type=int, dest="max_clips")
    run.add_argument("--clip-min-s", type=float, dest="clip_min_s")
    run.add_argument("--clip-max-s", type=float, dest="clip_max_s")
    run.add_argument("--reframe", choices=["fit", "crop"], help="fit keeps the HUD, crop fills the frame")
    run.add_argument("--encoder", help="auto | libx264 | h264_nvenc | hevc_nvenc | h264_videotoolbox")
    run.add_argument("--asr-model", dest="asr_model", help="tiny | base | small | medium | large-v3")
    run.add_argument("--caption-font", dest="caption_font")
    run.add_argument("--music", help="background music file (ducked under speech)")
    run.add_argument("--no-captions", action="store_true")
    run.add_argument("--no-thumbnails", action="store_true")
    run.add_argument("--chat-json", help="pre-downloaded chat/audio series JSON")
    run.add_argument("--transcript-json", help="pre-computed transcript JSON (absolute times)")
    run.add_argument("--run-id")
    run.add_argument("--out", help="runs directory")
    run.add_argument("--keep-source", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    rev = sub.add_parser("review", help="approve or reject clips (the posting gate)")
    rev.add_argument("run")
    rev.add_argument("--approve", help="ids, e.g. 1,3")
    rev.add_argument("--reject", help="ids, e.g. 2")
    rev.add_argument("--approve-all", action="store_true", dest="approve_all")
    rev.add_argument("--note")
    rev.add_argument("--out")
    rev.set_defaults(func=cmd_review)

    post = sub.add_parser("post", help="publish approved clips")
    post.add_argument("run")
    post.add_argument("--platforms", default="tiktok,youtube,instagram")
    post.add_argument("--poster", choices=["none", "uploadpost", "postiz"])
    post.add_argument("--yes", action="store_true", help="actually publish (default is a dry run)")
    post.add_argument("--out")
    post.set_defaults(func=cmd_post)

    st = sub.add_parser("status", help="show a run and its review state")
    st.add_argument("run")
    st.add_argument("--json", action="store_true")
    st.add_argument("--out")
    st.set_defaults(func=cmd_status)

    sub.add_parser("doctor", help="check dependencies and keys").set_defaults(func=cmd_doctor)
    sub.add_parser("selftest", help="render a synthetic clip end-to-end").set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        log(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
