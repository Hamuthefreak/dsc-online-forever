"""Review gate. Nothing is posted until a clip is explicitly approved here."""
from __future__ import annotations

from pathlib import Path

from .util import now_stamp, read_json, write_json

STATE_FILE = "review.json"


def state_path(run_dir: Path) -> Path:
    return Path(run_dir) / STATE_FILE


def load(run_dir: Path) -> dict:
    return read_json(state_path(run_dir), {"clips": {}, "created": now_stamp()}) or {"clips": {}}


def save(run_dir: Path, state: dict) -> dict:
    write_json(state_path(run_dir), state)
    return state


def register(run_dir: Path, clips: list[dict]) -> dict:
    """Add freshly rendered clips to the review queue as pending."""
    state = load(run_dir)
    for clip in clips:
        entry = state["clips"].setdefault(clip["id"], {})
        entry.update(
            {
                "id": clip["id"],
                "dir": clip["dir"],
                "video": clip["video"],
                "start": clip["start"],
                "end": clip["end"],
                "duration": clip["duration"],
                "score": clip.get("score"),
                "qc_passed": clip.get("qc_passed"),
                "status": entry.get("status", "pending"),
                "posted": entry.get("posted", {}),
            }
        )
    save(run_dir, state)
    return state


def decide(run_dir: Path, approve: list[str] | None = None, reject: list[str] | None = None,
           *, approve_all: bool = False, note: str = "") -> dict:
    state = load(run_dir)
    if not state.get("clips"):
        raise RuntimeError(f"no clips registered in {run_dir}")
    approve = approve or []
    reject = reject or []
    unknown = [cid for cid in approve + reject if cid not in state["clips"]]
    if unknown:
        raise RuntimeError(f"unknown clip ids: {unknown}. Known: {sorted(state['clips'])}")

    targets = list(state["clips"]) if approve_all else approve
    for cid in targets:
        state["clips"][cid]["status"] = "approved"
        state["clips"][cid]["note"] = note
        state["clips"][cid]["decided_at"] = now_stamp()
    for cid in reject:
        state["clips"][cid]["status"] = "rejected"
        state["clips"][cid]["note"] = note
        state["clips"][cid]["decided_at"] = now_stamp()
    save(run_dir, state)
    return state


def approved_clips(run_dir: Path) -> list[dict]:
    state = load(run_dir)
    return [c for c in state.get("clips", {}).values() if c.get("status") == "approved"]


def pending_clips(run_dir: Path) -> list[dict]:
    state = load(run_dir)
    return [c for c in state.get("clips", {}).values() if c.get("status") == "pending"]


def mark_posted(run_dir: Path, clip_id: str, platform: str, result: dict) -> dict:
    state = load(run_dir)
    entry = state.get("clips", {}).get(clip_id)
    if entry is None:
        raise RuntimeError(f"unknown clip {clip_id}")
    entry.setdefault("posted", {})[platform] = {"at": now_stamp(), **result}
    save(run_dir, state)
    return state


def summary(run_dir: Path) -> dict:
    state = load(run_dir)
    counts: dict[str, int] = {}
    for clip in state.get("clips", {}).values():
        counts[clip.get("status", "pending")] = counts.get(clip.get("status", "pending"), 0) + 1
    return {
        "run": str(run_dir),
        "counts": counts,
        "clips": sorted(state.get("clips", {}).values(), key=lambda c: c["id"]),
    }
