import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TZ = datetime(2026, 10, 12, 12).astimezone().tzinfo


def at(hour: int, minute: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "usage"
    monkeypatch.setenv("CLAUDE_USAGE_DIR", str(d))
    return d


class Transcript:
    """Builds a fake Claude Code transcript (main + optional subagents)."""

    def __init__(self, dir: Path, sid: str = "s1"):
        self.path = dir / f"{sid}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def message(self, mid, inp=0, cw=0, cr=0, out=0, lines=1, when=None, path=None):
        usage = {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": out}
        ts = (when or datetime.now().astimezone()).astimezone().isoformat()
        entry = {"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage}}
        with open(path or self.path, "a") as f:
            for _ in range(lines):
                f.write(json.dumps(entry) + "\n")

    def other(self, text="hello"):
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")

    def subagent(self, name="agent-1") -> Path:
        p = self.path.with_suffix("") / "subagents" / f"{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p


@pytest.fixture
def transcript(tmp_path) -> Transcript:
    return Transcript(tmp_path / "projects" / "-home-steamos-Works-Frame")


def status(sid="s1", cost=0.0, api_ms=0, transcript_path=None, **extra) -> dict:
    data = {
        "session_id": sid,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "workspace": {"current_dir": "/home/steamos/Works/Frame", "project_dir": "/home/steamos/Works/Frame"},
        "cost": {"total_cost_usd": cost, "total_api_duration_ms": api_ms},
    }
    data.update(extra)
    return data
