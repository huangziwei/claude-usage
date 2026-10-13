from datetime import timedelta

from claude_usage import statusline, store, transcript as tr

from .conftest import at, status


def log_lines(root, day=12):
    return list(store.iter_records(at(0, day=day).date(), at(0, day=day).date(), root))


def counted(root, name="cost_usd", day=12):
    return round(sum(i.deltas.get(name, 0) for i in store.increments(log_lines(root, day))), 6)


def test_new_session_baseline_then_changes(root):
    statusline.track(status(cost=0.0), at(9), root)
    statusline.track(status(cost=0.5, api_ms=1000), at(9, 1), root)
    statusline.track(status(cost=0.5, api_ms=1000), at(9, 2), root)  # unchanged -> no new line
    statusline.track(status(cost=1.25, api_ms=2500), at(9, 3), root)
    assert len(log_lines(root)) == 3
    assert counted(root) == 1.25
    assert counted(root, "api_ms") == 2500


def test_resumed_session_inherited_cost_not_counted(root):
    statusline.track(status(sid="old", cost=0.0), at(9), root)
    statusline.track(status(sid="old", cost=4.0), at(10), root)
    # /resume -> Claude Code reports the parent's cumulative cost under a new session id
    statusline.track(status(sid="new", cost=4.0), at(11), root)
    statusline.track(status(sid="new", cost=4.75), at(11, 5), root)
    assert counted(root) == 4.0 + 0.75


def test_session_first_seen_mid_way_counts_from_then(root):
    statusline.track(status(cost=3.0), at(9), root)  # status line installed mid-session
    statusline.track(status(cost=3.5), at(9, 1), root)
    assert counted(root) == 0.5


def test_counter_going_backwards_is_a_reset(root):
    statusline.track(status(cost=0.0), at(9), root)
    statusline.track(status(cost=2.0), at(9, 1), root)
    statusline.track(status(cost=0.25), at(9, 2), root)
    statusline.track(status(cost=1.0), at(9, 3), root)
    lines = log_lines(root)
    assert lines[2].get("reset") is True
    assert counted(root) == 2.0 + 0.75


def test_cancelled_after_append_before_save_does_not_double_count(root, monkeypatch):
    statusline.track(status(cost=0.0), at(9), root)
    real_save = store.save_state

    def crash(*a, **k):
        raise KeyboardInterrupt  # stands in for Claude Code cancelling the script

    monkeypatch.setattr(store, "save_state", crash)
    try:
        statusline.track(status(cost=1.0), at(9, 1), root)
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(store, "save_state", real_save)
    statusline.track(status(cost=1.5), at(9, 2), root)  # appends [0.0, 1.5] again from old state
    assert len(log_lines(root)) == 3
    assert counted(root) == 1.5


def test_locked_session_is_skipped(root):
    statusline.track(status(cost=0.0), at(9), root)
    with store.session_lock("s1", root):
        statusline.track(status(cost=1.0), at(9, 1), root)
    assert len(log_lines(root)) == 1
    statusline.track(status(cost=1.0), at(9, 2), root)  # caught up on the next run
    assert counted(root) == 1.0


def test_midnight_split_and_today_cost(root):
    statusline.track(status(cost=0.0), at(23, 0, day=11), root)
    statusline.track(status(cost=2.0), at(23, 30, day=11), root)
    statusline.track(status(cost=2.6), at(0, 30), root)
    assert counted(root, day=11) == 2.0
    assert round(statusline.today_cost(at(1), root), 6) == 0.6


def test_today_cost_sums_sessions(root):
    for sid, c in (("a", 1.0), ("b", 2.5)):
        statusline.track(status(sid=sid, cost=0.0), at(9), root)
        statusline.track(status(sid=sid, cost=c), at(10), root)
    assert round(statusline.today_cost(at(11), root), 6) == 3.5


def test_missing_session_id_is_ignored(root):
    statusline.track({"cost": {"total_cost_usd": 3}}, at(9), root)
    assert not (root / "log").exists()


# ----------------------------------------------------------------------------- tokens


def test_tokens_counted_once_per_message(root, transcript):
    tp = transcript.path
    transcript.message("old", out=999, when=at(8))  # before the session was first seen
    statusline.track(status(transcript_path=tp), at(9), root)
    transcript.message("m1", inp=10, cw=100, cr=1000, out=50, lines=3, when=at(9, 1))  # 3 content blocks
    transcript.other()
    transcript.message("m2", inp=5, out=7, when=at(9, 2))
    statusline.track(status(cost=0.1, transcript_path=tp), at(9, 3), root)
    assert counted(root, "input") == 15
    assert counted(root, "cache_write") == 100
    assert counted(root, "cache_read") == 1000
    assert counted(root, "output") == 57


def test_message_split_across_runs_is_not_double_counted(root, transcript):
    tp = transcript.path
    statusline.track(status(transcript_path=tp), at(9), root)
    transcript.message("m1", out=40, lines=1, when=at(9, 1))
    statusline.track(status(transcript_path=tp), at(9, 2), root)
    transcript.message("m1", out=40, lines=2, when=at(9, 1))  # rest of the same message
    statusline.track(status(transcript_path=tp), at(9, 3), root)
    assert counted(root, "output") == 40


def test_partial_line_waits_for_newline(root, transcript):
    tp = transcript.path
    statusline.track(status(transcript_path=tp), at(9), root)
    transcript.message("m1", out=10, when=at(9, 1))
    with open(tp, "a") as f:
        f.write('{"type": "assistant", "timestamp": "%s", "message": {"id": "m2", "usage": {"output_tokens": 5' % at(9, 1).isoformat())
    statusline.track(status(transcript_path=tp), at(9, 2), root)
    assert counted(root, "output") == 10
    with open(tp, "a") as f:
        f.write("}}}\n")
    statusline.track(status(transcript_path=tp), at(9, 3), root)
    assert counted(root, "output") == 15


def test_subagent_transcripts_are_included(root, transcript):
    tp = transcript.path
    statusline.track(status(transcript_path=tp), at(9), root)
    sub = transcript.subagent()  # appears after the session was first seen -> read from start
    transcript.message("s1", inp=3, out=30, when=at(9, 1), path=sub)
    transcript.message("m1", out=1, when=at(9, 1))
    statusline.track(status(transcript_path=tp), at(9, 2), root)
    assert counted(root, "output") == 31
    assert counted(root, "input") == 3


def test_baseline_skips_existing_history(root, transcript):
    tp = transcript.path
    transcript.message("h1", out=500, when=at(8))
    transcript.message("h2", out=500, lines=2, when=at(8))
    statusline.track(status(transcript_path=tp), at(9), root)
    transcript.message("h2", out=500, when=at(8))  # tail of a message already in progress
    transcript.message("m1", out=20, when=at(9, 1))
    statusline.track(status(transcript_path=tp), at(9, 2), root)
    assert counted(root, "output") == 20


def test_rewritten_transcript_is_rebaselined(root, transcript):
    tp = transcript.path
    statusline.track(status(transcript_path=tp), at(9), root)
    transcript.message("m1", out=20, when=at(9, 1))
    statusline.track(status(transcript_path=tp), at(9, 2), root)
    tp.write_text("")  # truncated
    transcript.message("m2", out=7, when=at(9, 3))
    statusline.track(status(transcript_path=tp), at(9, 4), root)  # shorter than cursor -> re-baseline
    transcript.message("m3", out=3, when=at(9, 5))
    statusline.track(status(transcript_path=tp), at(9, 6), root)
    assert counted(root, "output") == 20 + 3


def test_session_files_layout(transcript):
    sub = transcript.subagent("agent-a")
    assert tr.session_files(str(transcript.path)) == [transcript.path, sub]
    assert tr.session_files(None) == []
    assert tr.session_files(str(transcript.path.with_name("missing.jsonl"))) == []
