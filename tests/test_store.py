import json

from claude_usage import store

from .conftest import at


def rec(sid, t, cost, **counters):
    r = {"t": t.isoformat(), "sid": sid, "project": "~/p", "model": "m", "cost_usd": list(cost)}
    r.update({k: list(v) for k, v in counters.items()})
    return r


def total(records, name="cost_usd"):
    return round(sum(i.deltas.get(name, 0) for i in store.increments(records)), 6)


def test_first_record_is_baseline():
    assert total([rec("a", at(9), (5.0, 5.0))]) == 0
    assert total([rec("a", at(9), (5.0, 5.0)), rec("a", at(10), (5.0, 6.5))]) == 1.5


def test_duplicated_record_counts_once():
    # status line appended, then was cancelled before saving state -> next run re-appends from the old prev
    records = [rec("a", at(9), (1.0, 2.0)), rec("a", at(10), (1.0, 3.0))]
    assert total(records) == 2.0


def test_identical_duplicate_line():
    r = rec("a", at(9), (1.0, 2.0))
    assert total([r, dict(r)]) == 1.0


def test_sessions_are_independent_when_interleaved():
    records = [
        rec("a", at(9), (0.0, 1.0)),
        rec("b", at(9, 1), (10.0, 10.0)),  # resumed session: inherited $10 is baseline
        rec("a", at(9, 2), (1.0, 2.5)),
        rec("b", at(9, 3), (10.0, 10.25)),
    ]
    assert total(records) == 2.5 + 0.25


def test_reset_restarts_running_max():
    records = [
        rec("a", at(9), (0.0, 4.0)),
        {**rec("a", at(10), (1.0, 1.0)), "reset": True},  # Claude Code counter went backwards
        rec("a", at(11), (1.0, 3.0)),
    ]
    assert total(records) == 4.0 + 2.0


def test_counters_never_negative():
    assert total([rec("a", at(9), (3.0, 2.0))]) == 0


def test_malformed_values_are_skipped():
    records = [{"t": at(9).isoformat(), "sid": "a", "cost_usd": "oops"}, {"t": "not a time", "sid": "a", "cost_usd": [0, 1]}]
    assert total(records) == 0


def test_log_roundtrip_and_bad_lines(root):
    store.append_record(rec("a", at(9), (0.0, 1.0)), at(9), root)
    with open(store.log_path(at(9).date(), root), "a") as f:
        f.write('{"t": "2026-10-12T09:30:00+02:00", "sid": "a", "cost_us')  # torn write
        f.write("\n")
    store.append_record(rec("a", at(10), (1.0, 1.5)), at(10), root)
    got = list(store.iter_records(at(9).date(), at(9).date(), root))
    assert len(got) == 2
    assert total(got) == 1.0 + 0.5


def test_slice_starting_mid_session_uses_prev_as_baseline(root):
    # yesterday: 0 -> 2 ; today: 2 -> 3.  Reading only today must give 1, not 3.
    store.append_record(rec("a", at(23, day=11), (0.0, 2.0)), at(23, day=11), root)
    store.append_record(rec("a", at(1), (2.0, 3.0)), at(1), root)
    today = list(store.iter_records(at(1).date(), at(1).date(), root))
    assert total(today) == 1.0
    both = store.increments_between(at(0, day=11), at(23, day=12), root)
    assert round(sum(i.cost for i in both), 6) == 3.0


def test_increments_between_filters_by_local_time(root):
    store.append_record(rec("a", at(23, 59, day=11), (0.0, 1.0)), at(23, 59, day=11), root)
    store.append_record(rec("a", at(0, 1), (1.0, 4.0)), at(0, 1), root)
    today = store.increments_between(at(0), at(0, day=13), root)
    assert round(sum(i.cost for i in today), 6) == 3.0


def test_state_roundtrip_and_lock(root):
    assert store.load_state("x", root) is None
    store.save_state("x", {"last": {"cost_usd": 1}}, root)
    assert store.load_state("x", root) == {"last": {"cost_usd": 1}}
    with store.session_lock("x", root) as first:
        assert first
        with store.session_lock("x", root) as second:
            assert not second
    with store.session_lock("x", root) as again:
        assert again


def test_unsafe_session_ids_are_hashed(root):
    p = store.state_path("../../etc/passwd", root)
    assert p.parent == root / "state" and ".." not in p.name


def test_corrupt_state_is_ignored(root):
    p = store.state_path("x", root)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert store.load_state("x", root) is None
    p.write_text(json.dumps([1, 2]))
    assert store.load_state("x", root) is None
