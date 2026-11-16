"""Posting. Gated: only approved clips are ever sent, and dry-run is the default."""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .review import approved_clips, load, mark_posted
from .util import log, now_stamp, read_json, write_json

PLATFORM_MAP = {
    "tiktok": "tiktok",
    "youtube": "youtube",
    "shorts": "youtube",
    "instagram": "instagram",
    "reels": "instagram",
}


def _multipart(fields: dict[str, list[str] | str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----clipforge{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{item}\r\n".encode()
            )
    filename = file_path.name
    ctype = mimetypes.guess_type(filename)[0] or "video/mp4"
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post_uploadpost(clip: dict, platforms: list[str], caption: str, cfg, *, dry_run: bool) -> dict:
    if not cfg.uploadpost_key or not cfg.uploadpost_user:
        raise RuntimeError("UPLOADPOST_API_KEY and UPLOADPOST_USER are required for --poster uploadpost")
    video = Path(clip["video"])
    fields = {
        "user": cfg.uploadpost_user,
        "title": caption[:2200],
        "platform[]": platforms,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "poster": "upload-post", "url": cfg.uploadpost_url,
                "fields": {k: v for k, v in fields.items() if k != "title"}, "file": str(video)}
    body, ctype = _multipart(fields, "video", video)
    req = urllib.request.Request(
        cfg.uploadpost_url,
        data=body,
        headers={"Authorization": f"Apikey {cfg.uploadpost_key}", "Content-Type": ctype},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return {"ok": True, "poster": "upload-post", "status": resp.status,
                    "body": resp.read().decode()[:2000]}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "poster": "upload-post", "status": exc.code, "body": exc.read().decode()[:2000]}
    except Exception as exc:
        return {"ok": False, "poster": "upload-post", "error": f"{type(exc).__name__}: {exc}"}


def _post_postiz(clip: dict, platforms: list[str], caption: str, cfg, *, dry_run: bool) -> dict:
    if not cfg.postiz_key:
        raise RuntimeError("POSTIZ_API_KEY is required for --poster postiz")
    raw = os.environ.get("POSTIZ_INTEGRATIONS", "")
    try:
        integrations = json.loads(raw) if raw else {}
    except ValueError:
        integrations = {}
    missing = [p for p in platforms if p not in integrations]
    if missing:
        raise RuntimeError(f"POSTIZ_INTEGRATIONS is missing ids for {missing} (JSON like {{\"tiktok\":\"id\"}})")
    payload = {
        "type": "now",
        "posts": [
            {
                "integration": {"id": integrations[p]},
                "value": [{"content": caption[:2200], "image": [{"path": str(Path(clip["video"]))}]}],
            }
            for p in platforms
        ],
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "poster": "postiz", "url": f"{cfg.postiz_url}/posts", "payload": payload}
    req = urllib.request.Request(
        f"{cfg.postiz_url.rstrip('/')}/posts",
        data=json.dumps(payload).encode(),
        headers={"Authorization": cfg.postiz_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return {"ok": True, "poster": "postiz", "status": resp.status, "body": resp.read().decode()[:2000]}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "poster": "postiz", "status": exc.code, "body": exc.read().decode()[:2000]}
    except Exception as exc:
        return {"ok": False, "poster": "postiz", "error": f"{type(exc).__name__}: {exc}"}


def _caption_for(clip: dict, platform: str) -> str:
    meta = read_json(Path(clip["dir"]) / "metadata.json", {}) or {}
    platforms = meta.get("platforms") or {}
    key = "youtube" if platform == "youtube" else platform
    entry = platforms.get(key) or {}
    base = entry.get("caption") or meta.get("title") or ""
    if entry.get("description") and platform == "youtube":
        base = f"{entry.get('title', meta.get('title', ''))}\n\n{entry['description']}"
    return base or "GTA 6 clip"


def post_run(run_dir: Path, platforms: list[str], cfg, *, dry_run: bool = True) -> dict:
    """Send approved clips. Refuses to send anything that is not approved."""
    run_dir = Path(run_dir)
    approved = approved_clips(run_dir)
    state = load(run_dir)
    pending = [c["id"] for c in state.get("clips", {}).values() if c.get("status") == "pending"]
    if not approved:
        raise RuntimeError(
            f"nothing approved in {run_dir}. Approve first: clipforge review {run_dir.name} --approve-all"
            + (f" (pending: {pending})" if pending else "")
        )

    canonical = sorted({PLATFORM_MAP.get(p, p) for p in platforms})
    poster = (cfg.poster or "none").lower()
    results: list[dict] = []

    if poster in ("none", "dryrun", "dry-run"):
        queue = {
            "created": now_stamp(),
            "poster": "none",
            "platforms": canonical,
            "items": [
                {
                    "clip_id": clip["id"],
                    "video": clip["video"],
                    "platforms": canonical,
                    "caption": _caption_for(clip, canonical[0]),
                }
                for clip in approved
            ],
        }
        write_json(run_dir / "post_queue.json", queue)
        log(f"dry run: wrote {run_dir / 'post_queue.json'} ({len(approved)} clip(s))")
        return {"dry_run": True, "queued": len(approved), "queue": str(run_dir / "post_queue.json"),
                "platforms": canonical}

    for clip in approved:
        already = set((clip.get("posted") or {}).keys())
        todo = [p for p in canonical if p not in already]
        if not todo:
            results.append({"clip_id": clip["id"], "skipped": "already posted to all platforms"})
            continue
        caption = _caption_for(clip, todo[0])
        if poster == "uploadpost":
            result = _post_uploadpost(clip, todo, caption, cfg, dry_run=dry_run)
        elif poster == "postiz":
            result = _post_postiz(clip, todo, caption, cfg, dry_run=dry_run)
        else:
            raise RuntimeError(f"unknown poster '{poster}' (use: none, uploadpost, postiz)")
        results.append({"clip_id": clip["id"], "platforms": todo, **result})
        if result.get("ok") and not dry_run:
            for platform in todo:
                mark_posted(run_dir, clip["id"], platform, {"poster": poster, "status": result.get("status")})

    write_json(run_dir / "post_results.json", {"at": now_stamp(), "results": results})
    return {"dry_run": dry_run, "poster": poster, "results": results}
