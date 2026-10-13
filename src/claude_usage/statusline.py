"""Claude Code status line: show model / context / cost / limits, and log usage.

Claude Code runs this on every update with the session JSON on stdin and shows what it
prints. Tracking is best effort and never stops the line from rendering.

    Opus 5.5 high │ ctx ███░░░░░░░ 31% 62k/200k │ $3.42 session · $11.80 today
    ~/Works/Frame  main* ↑1 │ 5h 23% ↻18:30 · 7d 41% │ +598 −9
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import store, transcript

# ----------------------------------------------------------------------------- tracking


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def short_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def track(data: dict, now: datetime, root: Path | None = None) -> None:
    """Record this session's progress in the usage log (see :mod:`claude_usage.store`).

    The first time a session is seen, its current totals become the baseline and are
    *not* counted: cost a resumed session inherited was already counted (or happened
    before tracking existed). After that, every change is logged as [previous, new].
    """
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    cost = data.get("cost") or {}
    live = {"cost_usd": _num(cost.get("total_cost_usd")), "api_ms": _num(cost.get("total_api_duration_ms"))}
    ws = data.get("workspace") or {}
    project = short_path(str(ws.get("project_dir") or data.get("cwd") or "?"))
    model = str((data.get("model") or {}).get("id") or "?")
    files = transcript.session_files(data.get("transcript_path"))

    with store.session_lock(sid, root) as locked:
        if not locked:
            return
        state = store.load_state(sid, root)
        reset = False
        if state is None:
            since = now
            cursors = {str(p): transcript.baseline_cursor(p) for p in files}
            tokens = dict.fromkeys(store.TOKEN_COUNTERS, 0)
            prev = {**live, **tokens}
            first = True
        else:
            try:
                since = datetime.fromisoformat(state["since"])
            except (KeyError, TypeError, ValueError):
                since = now
            cursors = state.get("cursors") or {}
            tokens = {k: int(state.get("last", {}).get(k, 0)) for k in store.TOKEN_COUNTERS}
            transcript.scan(files, cursors, tokens, since)
            transcript.prune_cursors(cursors)
            prev = {k: _num(state.get("last", {}).get(k)) for k in store.COUNTERS}
            first = False
            # Claude Code's own counters went backwards: start them over from here.
            if live["cost_usd"] < prev["cost_usd"] - 1e-9 or live["api_ms"] < prev["api_ms"]:
                reset = True
                prev.update(live)
        current = {**live, **tokens}

        if first or reset or any(current[k] != prev[k] for k in store.COUNTERS):
            record = {"t": now.isoformat(timespec="seconds"), "sid": sid, "project": project, "model": model}
            record.update({k: [prev[k], current[k]] for k in store.COUNTERS})
            if reset:
                record["reset"] = True
            store.append_record(record, now, root)
        store.save_state(sid, {
            "last": current,
            "cursors": cursors,
            "project": project,
            "since": since.isoformat(),
            "seen": now.isoformat(timespec="seconds"),
        }, root)


def today_cost(now: datetime, root: Path | None = None) -> float:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    recs = store.iter_records(now.date(), now.date(), root)
    return sum(i.cost for i in store.increments(recs) if i.time >= start)


# ----------------------------------------------------------------------------- rendering

RESET = "\033[0m"
DIM = "\033[90m"
BOLD = "\033[1m"
GREEN, YELLOW, RED, CYAN, MAGENTA, BLUE = (f"\033[{c}m" for c in (32, 33, 31, 36, 35, 34))
SEP = f" {DIM}│{RESET} "
DOT = f" {DIM}·{RESET} "
ANSI = re.compile(r"\033\[[0-9;]*m")


def visible_len(s: str) -> int:
    return len(ANSI.sub("", s))


def level_color(pct: float) -> str:
    return GREEN if pct < 50 else YELLOW if pct < 80 else RED


def tok(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def money(x: float) -> str:
    return f"${x:,.2f}" if x < 1000 else f"${x:,.0f}"


def bar(pct: float, width: int = 10) -> str:
    filled = round(max(0.0, min(100.0, pct)) / 100 * width)
    return f"{level_color(pct)}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def git_info(cwd: str) -> tuple[str, bool, int, int] | None:
    """(branch, dirty, ahead, behind) or None outside a repo. One git call."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "--no-optional-locks", "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True, timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    branch, oid, ahead, behind, dirty = "", "", 0, 0, False
    for line in out.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2][:7]
        elif line.startswith("# branch.ab "):
            a, b = line.split(" ")[2:4]
            ahead, behind = int(a), -int(b)
        elif line and not line.startswith("#"):
            dirty = True
    if branch == "(detached)":
        branch = oid or "detached"
    return branch, dirty, ahead, behind


def reset_label(epoch, now: float, long: bool) -> str:
    """Local time when a rate-limit window resets: ↻18:30, or ↻Fri 09:00 if not today."""
    try:
        when = datetime.fromtimestamp(float(epoch))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    if when.timestamp() <= now:
        return ""
    same_day = when.date() == datetime.fromtimestamp(now).date()
    return f"↻{when:%H:%M}" if same_day or not long else f"↻{when:%a %H:%M}"


def render(data: dict, today: float | None, columns: int, git=git_info) -> str:
    """Two lines of segments. Each segment is (priority, group, text): segments in the same
    group are joined with ·, groups with │, and the highest priority numbers are dropped
    first when the terminal is too narrow."""
    model = data.get("model") or {}
    name = str(model.get("display_name") or model.get("id") or "Claude")
    effort = (data.get("effort") or {}).get("level")
    head = f"{BOLD}{name}{RESET}" + (f" {DIM}{effort}{RESET}" if effort else "")
    if data.get("fast_mode"):
        head += f" {YELLOW}fast{RESET}"
    line1 = [(0, "model", head)]

    ctx = data.get("context_window") or {}
    if ctx.get("used_percentage") is not None:
        pct = _num(ctx.get("used_percentage"))
        usage = ctx.get("current_usage") or {}
        used = sum(_num(usage.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        size = _num(ctx.get("context_window_size"))
        line1.append((1, "ctx", f"{DIM}ctx{RESET} {bar(pct)} {level_color(pct)}{pct:.0f}%{RESET}"))
        if used and size:
            line1.append((4, "ctx", f"{DIM}{tok(used)}/{tok(size)}{RESET}"))
    cost = data.get("cost") or {}
    line1.append((2, "cost", f"{money(_num(cost.get('total_cost_usd')))} {DIM}session{RESET}"))
    if today is not None:
        line1.append((3, "cost", f"{money(max(today, 0.0))} {DIM}today{RESET}"))

    cwd = str((data.get("workspace") or {}).get("current_dir") or data.get("cwd") or "")
    line2 = [(0, "where", f"{CYAN}{short_path(cwd)}{RESET}")] if cwd else []
    g = git(cwd) if cwd else None
    if g:
        branch, dirty, ahead, behind = g
        seg = f"{MAGENTA}{branch}{'*' if dirty else ''}{RESET}"
        if ahead:
            seg += f" {DIM}↑{ahead}{RESET}"
        if behind:
            seg += f" {DIM}↓{behind}{RESET}"
        line2.append((1, "where", seg))
    pr = data.get("pr") or {}
    if pr.get("number"):
        state = pr.get("review_state")
        line2.append((5, "where", f"{BLUE}#{pr['number']}{RESET}" + (f" {DIM}{state}{RESET}" if state else "")))
    limits = data.get("rate_limits") or {}
    now = time.time()
    for prio, key, label in ((2, "five_hour", "5h"), (3, "seven_day", "7d")):
        lim = limits.get(key) or {}
        if lim.get("used_percentage") is None:
            continue
        pct = _num(lim.get("used_percentage"))
        when = reset_label(lim.get("resets_at"), now, long=key == "seven_day")
        line2.append((prio, "limits", f"{DIM}{label}{RESET} {level_color(pct)}{pct:.0f}%{RESET}" + (f" {DIM}{when}{RESET}" if when else "")))
    spend = limits.get("spend_limit") or {}
    if spend.get("used_percentage") is not None:
        pct = _num(spend.get("used_percentage"))
        line2.append((3, "limits", f"{DIM}spend{RESET} {level_color(pct)}{pct:.0f}%{RESET}"))
    added, removed = int(_num(cost.get("total_lines_added"))), int(_num(cost.get("total_lines_removed")))
    if added or removed:
        line2.append((6, "lines", f"{GREEN}+{added}{RESET} {RED}−{removed}{RESET}"))

    return "\n".join(line for line in (fit(line1, columns), fit(line2, columns)) if line)


Segment = tuple[int, str, str]  # (priority: lower = keep longer, group, text)


def fit(segments: list[Segment], columns: int) -> str:
    """Join segments, dropping the highest priority numbers until the line fits."""
    segs = list(segments)
    while True:
        line = _join(segs)
        if visible_len(line) <= columns or len(segs) <= 1:
            return line
        segs.remove(max(segs, key=lambda s: s[0]))


def _join(segs: list[Segment]) -> str:
    out = ""
    for i, (_, group, text) in enumerate(segs):
        if i:
            out += DOT if group == segs[i - 1][1] else SEP
        out += text
    return out


# ----------------------------------------------------------------------------- entry point


def _log_error(root: Path) -> None:
    try:
        path = root / "errors.log"
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.unlink()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n{traceback.format_exc()}")
    except OSError:
        pass


def main() -> None:
    root = store.data_dir()
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except ValueError:
        print(f"{DIM}claude-statusline: unreadable input{RESET}")
        return
    now = datetime.now().astimezone()
    today = None
    try:
        store.write_json_atomic(root / "last-input.json", data)  # for debugging / exploring fields
        track(data, now, root)
        today = today_cost(now, root)
    except Exception:
        _log_error(root)
    columns = int(_num(os.environ.get("COLUMNS"), 120)) or 120
    print(render(data, today, columns))


if __name__ == "__main__":
    main()
