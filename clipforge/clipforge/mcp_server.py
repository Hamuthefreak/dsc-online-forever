"""MCP server: one tool runs the whole pipeline, two guardrails around it.

Add to an MCP client:
    claude mcp add clipforge -- python -m clipforge.mcp_server
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .pipeline import RunOptions, run_pipeline
from .post import post_run
from .review import decide, summary
from .util import log

INSTRUCTIONS = (
    "clipforge turns long stream VODs into captioned 9:16 clips. "
    "Typical flow: clip_videos -> review_clips (approve) -> post_clips (confirm=true). "
    "Clips are never posted until they are explicitly approved."
)


def _new_server(name: str):
    """mcp 2.x renamed FastMCP to MCPServer; support both SDK generations."""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer(name, instructions=INSTRUCTIONS)
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP(name, instructions=INSTRUCTIONS)
    except Exception as exc:  # pragma: no cover
        raise SystemExit("pip install 'clipforge[mcp]' to run the MCP server") from exc


server = _new_server("clipforge")


def _cfg(**kw) -> Settings:
    cfg = Settings()
    for key, value in kw.items():
        if value is not None:
            setattr(cfg, key, value)
    return cfg


@server.tool()
def clip_videos(
    urls: list[str] | None = None,
    source: str | None = None,
    min_clips: int = 4,
    max_clips: int = 10,
    captions: bool = True,
    thumbnails: bool = True,
    reframe: str = "fit",
    live: bool = False,
    asr_model: str | None = None,
    run_id: str | None = None,
) -> str:
    """Ingest VODs (or one local file) and produce captioned 9:16 clips plus thumbnails.

    Highlight moments are picked from Twitch/YouTube chat replay velocity, falling
    back to an audio-energy curve. Clips land in <out_dir>/<run_id>/clips/.
    """
    cfg = _cfg(captions=captions, thumbnails=thumbnails, reframe=reframe,
               min_clips=min_clips, max_clips=max_clips, asr_model=asr_model)
    result = run_pipeline(RunOptions(
        urls=list(urls or []),
        settings=cfg,
        run_id=run_id,
        live=live,
        source=Path(source) if source else None,
    ))
    return json.dumps(result, indent=2)


@server.tool()
def review_clips(run_id: str, approve: str = "", reject: str = "", approve_all: bool = False) -> str:
    """Approve or reject rendered clips. Nothing can be posted until it is approved."""
    cfg = Settings()
    run_dir = Path(cfg.out_dir).expanduser().resolve() / run_id
    if not run_dir.is_dir():
        return json.dumps({"error": f"run not found: {run_dir}"})

    def ids(raw: str) -> list[str]:
        return [p if p.startswith("clip_") else f"clip_{int(p):02d}"
                for p in raw.replace(" ", "").split(",") if p]

    decide(run_dir, ids(approve), ids(reject), approve_all=approve_all)
    return json.dumps(summary(run_dir), indent=2)


@server.tool()
def post_clips(run_id: str, platforms: str = "tiktok,youtube,instagram", confirm: bool = False) -> str:
    """Publish approved clips. confirm=False performs a dry run and writes post_queue.json."""
    cfg = Settings()
    run_dir = Path(cfg.out_dir).expanduser().resolve() / run_id
    if not run_dir.is_dir():
        return json.dumps({"error": f"run not found: {run_dir}"})
    plist = [p.strip() for p in platforms.split(",") if p.strip()]
    try:
        return json.dumps(post_run(run_dir, plist, cfg, dry_run=not confirm), indent=2)
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})


@server.tool()
def run_status(run_id: str) -> str:
    """Show the review state of a run."""
    cfg = Settings()
    run_dir = Path(cfg.out_dir).expanduser().resolve() / run_id
    if not run_dir.is_dir():
        return json.dumps({"error": f"run not found: {run_dir}"})
    return json.dumps(summary(run_dir), indent=2)


def main() -> None:
    log("clipforge MCP server starting (stdio)")
    server.run()


if __name__ == "__main__":
    main()
