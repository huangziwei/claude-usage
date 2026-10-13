"""claude-usage: summarize the usage log written by claude-statusline.

    claude-usage                    overview: today, yesterday, this week/month, by project
    claude-usage daily   [-n 14]    one row per day
    claude-usage weekly  [-n 8]     one row per ISO week (Mon–Sun)
    claude-usage monthly [-n 6]     one row per month
    claude-usage sessions [-n 20]   most recent sessions

Options: --projects (break each period down by project), --project TEXT (filter),
--no-color. All times are local. Costs are Claude Code's estimates at list prices.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import __version__, store

# ----------------------------------------------------------------------------- aggregation


@dataclass
class Agg:
    cost: float = 0.0
    input: int = 0  # uncached input + cache writes ("new" tokens)
    cache_read: int = 0
    output: int = 0
    sessions: set[str] = field(default_factory=set)
    first: datetime | None = None
    last: datetime | None = None
    models: set[str] = field(default_factory=set)
    project: str = ""

    def add(self, inc: store.Increment) -> None:
        tokens = sum(inc.tokens(k) for k in store.TOKEN_COUNTERS)
        if inc.cost <= 0 and tokens <= 0:
            return
        self.cost += inc.cost
        self.input += inc.tokens("input") + inc.tokens("cache_write")
        self.cache_read += inc.tokens("cache_read")
        self.output += inc.tokens("output")
        self.sessions.add(inc.session_id)
        self.models.add(inc.model)
        self.project = inc.project
        self.first = inc.time if self.first is None else min(self.first, inc.time)
        self.last = inc.time if self.last is None else max(self.last, inc.time)

    @property
    def empty(self) -> bool:
        return not self.sessions

    @property
    def cache_ratio(self) -> float | None:
        total = self.input + self.cache_read
        return self.cache_read / total if total else None


def aggregate(incs: Iterable[store.Increment], key: Callable[[store.Increment], str]) -> dict[str, Agg]:
    out: dict[str, Agg] = defaultdict(Agg)
    for inc in incs:
        out[key(inc)].add(inc)
    return out


def midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def day_key(inc: store.Increment) -> str:
    return inc.time.date().isoformat()


def week_key(inc: store.Increment) -> str:
    iso = inc.time.date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_key(inc: store.Increment) -> str:
    return inc.time.strftime("%Y-%m")


# ----------------------------------------------------------------------------- formatting


class Style:
    def __init__(self, color: bool):
        codes = {"bold": 1, "dim": 90, "red": 31, "green": 32, "yellow": 33, "blue": 34, "magenta": 35, "cyan": 36}
        for name, code in codes.items():
            setattr(self, name, (lambda c: lambda s: f"\033[{c}m{s}\033[0m")(code) if color else (lambda s: s))


def tok(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def money(x: float) -> str:
    return f"${x:,.2f}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def row(st: Style, label: str, a: Agg, width: int, scale: float, bar_width: int = 24) -> str:
    cache = f"{a.cache_ratio:.0%}" if a.cache_ratio is not None else "–"
    bar = "█" * (round(a.cost / scale * bar_width) if scale > 0 else 0)
    return (
        f"  {label:<{width}}  {st.bold(f'{money(a.cost):>10}')}  {st.dim(f'{plural(len(a.sessions), "session"):>12}')}"
        f"  {st.blue(f'{tok(a.input):>7}')} in  {st.blue(f'{tok(a.output):>7}')} out  {st.dim(f'cache {cache:>4}')}"
        f"  {st.magenta(bar)}"
    )


# ----------------------------------------------------------------------------- commands


def load(start: datetime, end: datetime, project: str | None) -> list[store.Increment]:
    incs = store.increments_between(start, end)
    if project:
        incs = [i for i in incs if project.lower() in i.project.lower()]
    return incs


def cmd_overview(args, st: Style, now: datetime) -> None:
    today = now.date()
    start = midnight(min(month_start(today), today - timedelta(days=29), week_start(today)))
    incs = load(start, now + timedelta(seconds=1), args.project)
    spans = [
        ("Today", midnight(today)),
        ("Yesterday", midnight(today - timedelta(days=1))),
        (f"This week (since {week_start(today):%a %d %b})", midnight(week_start(today))),
        (f"This month ({today:%B})", midnight(month_start(today))),
        ("Last 30 days", midnight(today - timedelta(days=29))),
    ]
    print(st.bold("Claude Code usage") + st.dim("  · estimated at list prices, local time"))
    width = max(len(s[0]) for s in spans)
    aggs = []
    for i, (label, since) in enumerate(spans):
        until = midnight(today) if label == "Yesterday" else now + timedelta(seconds=1)
        a = Agg()
        for inc in incs:
            if since <= inc.time < until:
                a.add(inc)
        aggs.append((label, a))
    scale = max((a.cost for _, a in aggs), default=0)
    for label, a in aggs:
        print(row(st, label, a, width, scale))

    by_project = aggregate((i for i in incs if i.time >= midnight(week_start(today))), lambda i: i.project)
    if by_project:
        print()
        print(st.bold("This week by project"))
        _print_projects(st, by_project)
    _print_footer(st)


def _print_projects(st: Style, aggs: dict[str, Agg], indent: str = "  ") -> None:
    items = sorted(((k, a) for k, a in aggs.items() if not a.empty), key=lambda kv: -kv[1].cost)
    if not items:
        return
    width = max(len(k) for k, _ in items)
    scale = max(a.cost for _, a in items)
    for k, a in items:
        print(indent + row(st, st.cyan(k) + " " * (width - len(k)), a, width, scale).lstrip())


def cmd_periods(args, st: Style, now: datetime) -> None:
    today = now.date()
    n = max(1, args.n)
    if args.cmd == "daily":
        first, key, title = today - timedelta(days=n - 1), day_key, "Daily"
        labels = [(today - timedelta(days=i)).isoformat() for i in range(n)]
    elif args.cmd == "weekly":
        first, key, title = week_start(today) - timedelta(weeks=n - 1), week_key, "Weekly (ISO weeks, Mon–Sun)"
        labels = []
        for i in range(n):
            iso = (week_start(today) - timedelta(weeks=i)).isocalendar()
            labels.append(f"{iso.year}-W{iso.week:02d}")
    else:
        first, key, title = month_start(today, n - 1), month_key, "Monthly"
        labels = [month_start(today, i).strftime("%Y-%m") for i in range(n)]
    incs = load(midnight(first), now + timedelta(seconds=1), args.project)
    periods = aggregate(incs, key)
    print(st.bold(f"Claude Code usage · {title}") + st.dim("  · estimated at list prices"))
    width = max(len(lbl) for lbl in labels)
    scale = max((periods[lbl].cost for lbl in labels if lbl in periods), default=0)
    total = Agg()
    for inc in incs:
        total.add(inc)
    for lbl in labels:  # newest first; empty periods shown dimmed
        a = periods.get(lbl, Agg())
        line = row(st, lbl, a, width, scale)
        print(line if not a.empty else st.dim(f"  {lbl:<{width}}  {'–':>10}"))
        if args.projects and not a.empty:
            _print_projects(st, aggregate((i for i in incs if key(i) == lbl), lambda i: i.project), indent="      ")
    print(st.dim("  " + "─" * (width + 60)))
    print(row(st, "Total", total, width, 0))
    _print_footer(st)


def cmd_sessions(args, st: Style, now: datetime) -> None:
    incs = load(midnight(now.date() - timedelta(days=max(1, args.days) - 1)), now + timedelta(seconds=1), args.project)
    sessions = [a for a in aggregate(incs, lambda i: i.session_id).values() if not a.empty]
    sessions.sort(key=lambda a: a.last, reverse=True)
    print(st.bold(f"Recent sessions") + st.dim(f"  · last {plural(args.days, 'day')}, newest first"))
    for a in sessions[: args.n]:
        span = f"{a.first:%a %d %b %H:%M}–{a.last:%H:%M}" if a.first.date() == a.last.date() else f"{a.first:%d %b %H:%M}–{a.last:%d %b %H:%M}"
        models = ", ".join(sorted(m.removeprefix("claude-") for m in a.models))
        print(
            f"  {st.dim(span)}  {st.bold(f'{money(a.cost):>9}')}  {st.blue(f'{tok(a.input):>7}')} in"
            f"  {st.blue(f'{tok(a.output):>7}')} out  {st.cyan(a.project)}  {st.dim(models)}"
        )
    if not sessions:
        print(st.dim("  (none)"))
    _print_footer(st)


def _print_footer(st: Style) -> None:
    logs = sorted((store.data_dir() / "log").glob("*.jsonl"))
    since = logs[0].stem if logs else "–"
    print(st.dim(f"\nTracking since {since} · data in {store.data_dir()}"))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="claude-usage", description="Summarize Claude Code usage logged by claude-statusline.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--project", help="only projects whose path contains TEXT")
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    for name, default, help_ in (("daily", 14, "days"), ("weekly", 8, "weeks"), ("monthly", 6, "months")):
        s = sub.add_parser(name, help=f"one row per {help_[:-1]}")
        s.add_argument("-n", type=int, default=default, help=f"number of {help_} (default {default})")
        s.add_argument("--projects", action="store_true", help="break each period down by project")
    s = sub.add_parser("sessions", help="most recent sessions")
    s.add_argument("-n", type=int, default=20, help="how many (default 20)")
    s.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    args = p.parse_args(argv)

    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    st = Style(color)
    now = datetime.now().astimezone()
    try:
        if args.cmd in ("daily", "weekly", "monthly"):
            cmd_periods(args, st, now)
        elif args.cmd == "sessions":
            cmd_sessions(args, st, now)
        else:
            cmd_overview(args, st, now)
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
