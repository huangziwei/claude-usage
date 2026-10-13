# claude-usage

A Claude Code status line that also keeps a reliable log of cost and token usage, plus `claude-usage`, a report command for that log. Inspired by [huangziwei/dotclaude](https://github.com/huangziwei/dotclaude)'s `statusline-command.py` and [huangziwei/claude-costs](https://github.com/huangziwei/claude-costs), redesigned so the numbers can be trusted.

```
Opus 5.5 high │ ctx ███░░░░░░░ 30% · 296k/1.0M │ $9.87 session · $11.80 today
~/Works/Frame · main* ↑1 │ 5h 5% ↻21:50 · 7d 25% ↻Thu 10:00 │ +2494 −33
```

| Segment | Meaning |
|---|---|
| `Opus 5.5 high` | model and effort level (plus `fast` in fast mode) |
| `ctx … 30% · 296k/1.0M` | context window used. Green below 50%, yellow below 80%, red above. |
| `$9.87 session · $11.80 today` | this session's estimated cost and the total across all sessions today (local time) |
| `~/Works/Frame · main* ↑1` | folder, git branch (`*` = uncommitted changes, `↑`/`↓` = ahead/behind) |
| `#123 approved` | open PR for the branch, if any |
| `5h 5% ↻21:50 · 7d 25% ↻Thu 10:00` | plan rate limits used, and when each window resets |
| `+2494 −33` | lines changed this session |

When the terminal is narrow, the least important segments are dropped first.

## Reports

```bash
claude-usage                    # today / yesterday / this week / this month / 30 days, + this week by project
claude-usage daily   [-n 14]    # one row per day     (--projects to break down by project)
claude-usage weekly  [-n 8]     # ISO weeks, Mon–Sun
claude-usage monthly [-n 6]
claude-usage sessions [-n 20] [--days 7]
claude-usage --project Frame daily   # filter by project path
```

Costs are Claude Code's own estimates at list prices (`cost.total_cost_usd`). On a subscription plan they show the API-equivalent value, not what you're billed.

## How tracking works

Data lives in `~/.claude/usage/` (override with `CLAUDE_USAGE_DIR`):

```
log/YYYY-MM-DD.jsonl   append-only, one line whenever a session's totals change (local date)
state/<session>.json   last totals seen per session + transcript read positions
last-input.json        the latest JSON Claude Code sent (handy for exploring fields)
errors.log             tracking errors, if any (the status line still renders)
```

Each log line records the **previous and new cumulative value** of every counter: `cost_usd`, `api_ms`, and tokens (`input`, `cache_write`, `cache_read`, `output`). Readers turn lines into increments by taking only what exceeds the highest value seen so far for that session.

| Problem in the CSV approach | How it's handled here |
|---|---|
| Several sessions rewrite the same CSV, so updates get lost | Appends of one short line are atomic. Each session has its own state file and a per-session lock. |
| The whole CSV is rewritten and the whole transcript re-parsed on every refresh | One appended line, only when totals change. Transcripts are read incrementally from a saved byte offset. |
| Resumed sessions report the parent's cumulative cost, so it's counted twice; detecting this by heuristic misfires on parallel sessions | The first time a session is seen, its totals are a **baseline** and aren't counted. Only growth after that counts. No heuristic needed. |
| Claude Code cancels a slow status-line run midway | A line appended without the state being saved gets re-appended later with the same `prev`; the running maximum counts it once. Torn lines are skipped. |
| Whole session cost credited to the last-seen time, in UTC | Each increment is stored with its own local timestamp, so a session spanning midnight is split correctly. |
| `context_window.total_*_tokens` used as session totals (they're the *current context*, not cumulative) | Tokens come from the transcripts. Repeated lines of one message count once, and subagent transcripts are included. |
| One bad CSV value crashes the viewer | Malformed lines and values are skipped. |

**Limits:**
- Tracking starts the first time the status line sees a session. Sessions started before installation, or `claude -p` runs (which have no status line), aren't logged.
- Token counts come from the session and subagent transcripts. Small background calls (e.g. title generation) appear only in Claude Code's cost total, not in the token counts.

## Development

```bash
uv run pytest                       # tests: tracking edge cases, rendering, reports
uv tool install --editable .        # installs claude-statusline + claude-usage into ~/.local/bin
```

Settings (`~/.claude/settings.json`):

```json
"statusLine": { "type": "command", "command": "/home/steamos/.local/bin/claude-statusline" }
```
