"""Append-only usage log and per-session state.

Layout, under ``~/.claude/usage`` (override with ``CLAUDE_USAGE_DIR``)::

    log/YYYY-MM-DD.jsonl     one line each time a session's cumulative totals change
                             (file chosen by local date at write time)
    state/<session>.json     last totals seen for that session + transcript read positions
    state/<session>.lock     flock target, so two status-line runs never update one session at once

Every log line carries the *previous and new* cumulative value of each counter::

    {"t": "2026-10-12T16:40:01+02:00", "sid": "...", "project": "~/Works/Frame",
     "model": "claude-opus-5-5", "cost_usd": [3.21, 3.25], "output": [70100, 71353], ...}

so a reader can turn any slice of the log into increments without consulting other files,
and a line that gets written twice (status line cancelled after appending but before saving
its state) is never counted twice. See :func:`increments`.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

COUNTERS = ("cost_usd", "input", "cache_write", "cache_read", "output", "api_ms")
TOKEN_COUNTERS = ("input", "cache_write", "cache_read", "output")


def data_dir() -> Path:
    return Path(os.environ.get("CLAUDE_USAGE_DIR") or Path.home() / ".claude" / "usage")


def log_path(day: date, root: Path | None = None) -> Path:
    return (root or data_dir()) / "log" / f"{day.isoformat()}.jsonl"


def _safe_name(session_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return session_id
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def state_path(session_id: str, root: Path | None = None) -> Path:
    return (root or data_dir()) / "state" / f"{_safe_name(session_id)}.json"


# --------------------------------------------------------------------------- writing


def append_record(record: dict, now: datetime, root: Path | None = None) -> None:
    """Append one record to the log file for ``now``'s local date.

    A single ``write`` on an ``O_APPEND`` descriptor is atomic for lines this small, so
    concurrent sessions can append to the same file without interleaving.
    """
    path = log_path(now.date(), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_state(session_id: str, root: Path | None = None) -> dict | None:
    try:
        with open(state_path(session_id, root), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save_state(session_id: str, state: dict, root: Path | None = None) -> None:
    write_json_atomic(state_path(session_id, root), state)


@contextlib.contextmanager
def session_lock(session_id: str, root: Path | None = None) -> Iterator[bool]:
    """Non-blocking per-session lock. Yields False if another run holds it.

    Skipping a tick is harmless: the next run picks up everything that changed.
    """
    path = state_path(session_id, root).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing releases the lock


# --------------------------------------------------------------------------- reading


def iter_records(start: date, end: date, root: Path | None = None) -> Iterator[dict]:
    """Yield raw records from the log files for ``start``..``end`` (inclusive), in order.

    Malformed lines (e.g. a partially written line after a crash) are skipped.
    """
    day = start
    while day <= end:
        path = log_path(day, root)
        try:
            f = open(path, encoding="utf-8")
        except OSError:
            day += timedelta(days=1)
            continue
        with f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("sid"):
                    yield rec
        day += timedelta(days=1)


@dataclass
class Increment:
    """What one log record added on top of everything seen before it."""

    time: datetime  # local time
    session_id: str
    project: str
    model: str
    deltas: dict[str, float] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return self.deltas.get("cost_usd", 0.0)

    def tokens(self, name: str) -> int:
        return int(self.deltas.get(name, 0))


def _pair(value) -> tuple[float, float] | None:
    try:
        prev, cur = value
        return float(prev), float(cur)
    except (TypeError, ValueError):
        return None


def increments(records: Iterable[dict]) -> Iterator[Increment]:
    """Turn log records into increments.

    For every session and counter we keep the highest cumulative value seen so far
    (starting from the first record's ``prev``). A record only contributes what goes
    beyond that, so duplicated records count once and the first record of a slice uses
    its own ``prev`` as the baseline. ``"reset": true`` (a counter went backwards in
    Claude Code) restarts the running maximum for that session.
    """
    high: dict[str, dict[str, float]] = {}
    for rec in records:
        sid = str(rec.get("sid"))
        try:
            when = datetime.fromisoformat(str(rec.get("t"))).astimezone()
        except ValueError:
            continue
        seen = high.setdefault(sid, {})
        if rec.get("reset"):
            seen.clear()
        deltas: dict[str, float] = {}
        for name in COUNTERS:
            pair = _pair(rec.get(name))
            if pair is None:
                continue
            prev, cur = pair
            base = max(prev, seen.get(name, prev))
            deltas[name] = max(0.0, cur - base)
            seen[name] = max(base, cur)
        yield Increment(when, sid, str(rec.get("project") or "?"), str(rec.get("model") or "?"), deltas)


def increments_between(start: datetime, end: datetime, root: Path | None = None) -> list[Increment]:
    """Increments whose local time falls in [start, end). Reads one extra day either side
    so a timezone change can't drop records at the edges."""
    recs = iter_records(start.date() - timedelta(days=1), end.date() + timedelta(days=1), root)
    return [inc for inc in increments(recs) if start <= inc.time < end]
