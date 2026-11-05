# akash-claude-skills

Claude Code skills for QA work at Zocdoc. Each folder holds everything that skill needs.

| Skill | What it does |
|---|---|
| [`analyse-prd`](analyse-prd/SKILL.md) | Reviews a PRD or tech spec from a QA angle and writes a testability report (gaps, flow checks, questions to ask). |
| [`async-standup`](async-standup/SKILL.md) | Drafts the daily async standup update from meetings, email and Jira. |
| [`ship-test-pr`](ship-test-pr/SKILL.md) | Ships a tests-only PR end to end, with plan approval, one reviewer subagent and merge gates. |
| [`team-api-test`](team-api-test/SKILL.md) | Runs an E2E API test from a pasted cURL or endpoint and writes an HTML report. |
| [`team-di-batch`](team-di-batch/SKILL.md) | Builds a synthetic DI CSV batch for a customer, then monitors the uploaded batch. |

## Installing

The skills expect their files in specific places under `~/.claude`:

| Skill | File(s) in this repo | Install to |
|---|---|---|
| `analyse-prd` | `SKILL.md` | `~/.claude/skills/analyse-prd/SKILL.md` |
| | `agents/prd-*.md` | `.claude/agents/` in the project you run it from |
| `async-standup` | `SKILL.md` | `~/.claude/skills/async-standup/SKILL.md` |
| `ship-test-pr` | `SKILL.md` | `~/.claude/skills/ship-test-pr/SKILL.md` |
| `team-api-test` | `SKILL.md` | `~/.claude/skills/team-api-test/SKILL.md` |
| | `team-api-test.md` | `~/.claude/teams/team-api-test.md` |
| | everything else (`config.json`, `prompts/`, `scripts/`, `hooks/`) | `~/.claude/teams/team-api-test/` |
| `team-di-batch` | `SKILL.md` | `~/.claude/skills/team-di-batch/SKILL.md` |
| | `team-di-batch.md` | `~/.claude/teams/team-di-batch.md` |
| | everything else (`customers.seed.yaml`, `prompts/`) | `~/.claude/teams/team-di-batch/` |

`team-api-test` also needs its hook registered in `~/.claude/settings.json` as a `PreToolUse` hook on `Bash`:

```json
{ "type": "command", "command": "python3 ~/.claude/teams/team-api-test/hooks/block_raw_http.py" }
```

Its tests run with `python3 -m pytest team-api-test`.

## Requirements

- `team-api-test` and `team-di-batch` run on the `team-lead` engine from the `team-workflows` plugin.
- `team-api-test` and `team-di-batch` need the local auth proxy (`~/tools/auth-proxy-server`, `npm start`).
- `team-di-batch` runs from `~/QA/DI-QA/`.
- `analyse-prd` and `async-standup` use Glean.
