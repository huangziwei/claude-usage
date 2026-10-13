import io
import json
import time
from contextlib import redirect_stdout

from claude_usage import report, statusline

from .conftest import at, status


def no_git(cwd):
    return None


def plain(s):
    return statusline.ANSI.sub("", s)


FULL = status(
    cost=3.4239,
    effort={"level": "high"},
    context_window={
        "context_window_size": 200000,
        "used_percentage": 31,
        "current_usage": {"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 61000, "output_tokens": 500},
    },
    rate_limits={
        "five_hour": {"used_percentage": 23.5, "resets_at": time.time() + 3600},
        "seven_day": {"used_percentage": 81, "resets_at": time.time() + 3 * 86400},
    },
)
FULL["cost"].update(total_lines_added=598, total_lines_removed=9)


def test_render_full_line():
    out = plain(statusline.render(FULL, 11.8, 200, git=lambda cwd: ("main", True, 1, 0)))
    line1, line2 = out.split("\n")
    assert line1.startswith("Opus 5.5 high │ ctx ")
    assert "31% · 63k/200k │ $3.42 session · $11.80 today" in line1
    assert line2.startswith("~/Works/Frame · main* ↑1 │ 5h 24% ↻")  # 23.5 rounds to 24
    assert "· 7d 81% ↻" in line2 and line2.endswith("│ +598 −9")


def test_render_drops_low_priority_segments_when_narrow():
    out = plain(statusline.render(FULL, 11.8, 60, git=lambda cwd: ("main", False, 0, 0)))
    for line in out.split("\n"):
        assert len(line) <= 60
    assert "Opus 5.5" in out and "$3.42 session" in out
    assert "+598" not in out and "63k/200k" not in out


def test_render_minimal_input():
    out = plain(statusline.render({"model": {"display_name": "Sonnet"}}, None, 120, git=no_git))
    assert out == "Sonnet │ $0.00 session"


def test_render_tolerates_garbage_values():
    data = status(context_window={"used_percentage": "n/a"}, rate_limits={"five_hour": {"used_percentage": None}})
    data["cost"]["total_cost_usd"] = "x"
    assert "$0.00 session" in plain(statusline.render(data, None, 120, git=no_git))


def test_main_end_to_end(root, monkeypatch, capsys):
    monkeypatch.setattr(statusline, "git_info", no_git)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(status(cost=0.0))))
    statusline.main()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(status(cost=0.42))))
    statusline.main()
    out = plain(capsys.readouterr().out)
    assert "$0.42 session · $0.42 today" in out
    assert (root / "last-input.json").exists()
    assert not (root / "errors.log").exists()


def test_main_bad_input(monkeypatch, capsys, root):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    statusline.main()
    assert "unreadable input" in capsys.readouterr().out


def test_week_key_is_iso_monday_based():
    from claude_usage.store import Increment

    sunday = Increment(at(12, day=11), "a", "p", "m")  # Sun 11 Oct 2026
    monday = Increment(at(12, day=12), "a", "p", "m")
    assert report.week_key(sunday) == "2026-W41"
    assert report.week_key(monday) == "2026-W42"
    assert report.week_start(at(12, day=11).date()).isoformat() == "2026-10-05"


def test_report_commands_run(root, monkeypatch):
    now = statusline.datetime.now().astimezone()
    statusline.track(status(sid="a", cost=0.0), now, root)
    statusline.track(status(sid="a", cost=1.5), now, root)
    for argv in ([], ["daily", "--projects"], ["weekly"], ["monthly"], ["sessions"], ["--project", "nomatch", "daily"]):
        buf = io.StringIO()
        with redirect_stdout(buf):
            report.main(argv)
        text = buf.getvalue()
        assert "Tracking since" in text
        if argv and argv[0] == "--project":
            assert "$1.50" not in text
        else:
            assert "$1.50" in text, argv
