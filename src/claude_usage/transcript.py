"""Incremental token counting from Claude Code transcripts.

A session's API usage is recorded in its transcript (``<project>/<session>.jsonl``) and in
one transcript per subagent (``<project>/<session>/subagents/*.jsonl``). Each assistant
message's ``usage`` is repeated on every line written for that message (one line per
content block), always on consecutive lines. So when a repeated message id shows up we
*replace* that message's contribution instead of adding it again.

Files are read incrementally: a cursor per file remembers the byte offset of the last
complete line consumed, so each status-line run only parses what was appended since.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

USAGE_FIELDS = {
    "input": "input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "output": "output_tokens",
}
MAX_READ = 16 * 1024 * 1024  # per file per run; the rest is picked up next time
TAIL_PEEK = 256 * 1024


def session_files(transcript_path: str | None) -> list[Path]:
    if not transcript_path:
        return []
    main = Path(transcript_path)
    files = [main] if main.is_file() else []
    subagents = main.with_suffix("") / "subagents"
    if subagents.is_dir():
        files.extend(sorted(subagents.glob("*.jsonl")))
    return files


def _usage(raw: bytes, since: datetime | None = None) -> tuple[str | None, dict[str, int]] | None:
    if b'"usage"' not in raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    msg = entry.get("message") if isinstance(entry, dict) else None
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    if since is not None:
        try:
            if datetime.fromisoformat(str(entry.get("timestamp"))) < since:
                return None  # e.g. history copied into a resumed session's transcript
        except ValueError:
            pass
    counts = {}
    for name, key in USAGE_FIELDS.items():
        try:
            counts[name] = max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            counts[name] = 0
    mid = msg.get("id")
    return (mid if isinstance(mid, str) else None), counts


def baseline_cursor(path: Path) -> dict:
    """Cursor positioned at the end of ``path`` (nothing before it will be counted).

    Also remembers the last message seen, so if Claude Code is midway through writing
    that message, its remaining lines aren't counted as a new message.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_PEEK))
            tail = f.read()
    except OSError:
        return {"pos": 0, "last_id": None, "last": {}}
    end = tail.rfind(b"\n")
    last_id, last = None, {}
    for raw in reversed(tail[: end + 1].splitlines()):
        parsed = _usage(raw)
        if parsed:
            last_id, last = parsed
            break
    return {"pos": size - (len(tail) - end - 1), "last_id": last_id, "last": last}


def scan(files: list[Path], cursors: dict[str, dict], totals: dict[str, int], since: datetime | None = None) -> None:
    """Advance ``cursors`` over newly appended lines, adding their usage to ``totals``.

    Files without a cursor are read from the start (they appeared after the session was
    first seen); entries timestamped before ``since`` are ignored. Both dicts are updated
    in place.
    """
    for path in files:
        key = str(path)
        cur = cursors.get(key) or {"pos": 0, "last_id": None, "last": {}}
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < cur["pos"]:  # file was rewritten/truncated: re-baseline, never double count
            cursors[key] = baseline_cursor(path)
            continue
        if size == cur["pos"]:
            cursors[key] = cur
            continue
        try:
            with open(path, "rb") as f:
                f.seek(cur["pos"])
                chunk = f.read(min(size - cur["pos"], MAX_READ))
        except OSError:
            continue
        end = chunk.rfind(b"\n")
        if end < 0:  # no complete line yet
            cursors[key] = cur
            continue
        for raw in chunk[: end + 1].splitlines():
            parsed = _usage(raw, since)
            if not parsed:
                continue
            mid, counts = parsed
            same = mid is not None and mid == cur["last_id"]
            for name, n in counts.items():
                totals[name] = totals.get(name, 0) + n - (cur["last"].get(name, 0) if same else 0)
            cur["last_id"], cur["last"] = mid, counts
        cur["pos"] += end + 1
        cursors[key] = cur


def prune_cursors(cursors: dict[str, dict]) -> None:
    """Drop cursors for files that no longer exist (keeps state files small)."""
    for key in [k for k in cursors if not os.path.exists(k)]:
        del cursors[key]
