---
name: ship-test-pr
description: Use when shipping a tests-only PR end to end (unit or integration tests, shift-left / PE coverage-gap tickets, gap-analysis follow-ups, or Playwright specs in Zocdoc/sandbox) and team-implementation would be too heavy. Also use for "ship this test PR", "light team-implementation", or "quick test PR".
argument-hint: "<Jira key or task> [repo]"
---

# Ship Test PR

A light version of team-implementation for PRs that only add or change tests. The lead (this session) does the work. One fresh reviewer subagent checks it. The human gates are the same as team-implementation: **plan approval**, **human review-comment decisions**, and **merge authorization**. The workflow runs without the team-lead engine, `.teamlogs`, simplify rounds, or a retrospective. Its only cron is the reviewer watchdog (step 4a).

**Run dir:** `~/.claude/ship-test-pr/runs/<KEY>/`, created in step 1. It holds `review-progress.md` (the reviewer's appended progress lines) and `watchdog.md` (the watchdog's state).

## Repos

| Kind | Local path | Base | Verify locally | CI signal |
|---|---|---|---|---|
| Shift-left (PE repos) | `~/QA/shiftLeft/repos/<repo>` | `origin/main` | Usually blocked (no dotnet/sbt/pants). Rely on CI | TeamCity, falling back to `gh api repos/Zocdoc/<repo>/commits/<sha>/status` |
| Playwright automation | `~/sandbox` (`Zocdoc/sandbox`) | `origin/main` | `npm run lint`, type-check, `npx playwright test <spec> --project=chromium` | GitHub checks |

For a sandbox branch name, follow the repo's `automation-branch-naming` skill. For spec style, use `zd-frontend:playwright-testing` and the repo's `CLAUDE.md`. For C# tests, follow the existing test file's conventions.

Git on this machine: use `/Library/Developer/CommandLineTools/usr/bin/git`. Run `gh` with `PATH=/Library/Developer/CommandLineTools/usr/bin:$PATH`. If an SSH push or fetch fails, retry it once.

## Steps

### 1. Scope (lead)
1. Read the ticket (Jira MCP) or the task text. Create the run dir `~/.claude/ship-test-pr/runs/<KEY>/`.
2. Run `git fetch origin`, then create the branch, or a worktree for parallel tickets (`<repo>-<KEY>` beside the clone), from **`origin/main`**. Never branch from local `main`.
3. Check the ticket against `origin/main`: does the class, method, gap or line number it names still exist? Run `git log` on the file under test and look for an existing test file. If the ticket doesn't match `origin/main`, stop and report the mismatch with the PR that superseded it.

### 2. Plan → **GATE 1: user approves**
Show a plan of 15 lines or fewer in chat (no committed spec doc). Give one line for each item:
- the files to create or edit (tests only)
- each test case: the behavior it covers and the branch or gap it closes
- how it will be verified: the local command, or "CI only"
- out of scope.

Ask with `AskUserQuestion`: **Approve (Recommended)** / **Change** / **Stop**. Do not write code until the user approves.

### 3. Implement (lead)
- Write the tests in small commits. Commit messages end with the org attribution lines.
- **Tests-only check:** `git diff --name-only origin/main...HEAD` must list only test files, fixtures or test helpers. If a production file has to change (for example, a bug the tests exposed), stop and ask the user. That is a different kind of PR.
- If a test exposes a real bug, don't weaken the assertion. Ask the user whether to `[Ignore]`/`test.fixme` it with the bug key, or to log the bug (`/log-bug`).
- Run the local verification if the repo supports it. If a run is blocked, say so; don't claim it passed.

### 4. Review (one fresh subagent)
Dispatch one `general-purpose` agent named **`reviewer`** (`Agent(name="reviewer", ...)`) with the plan, the repo path, the `git diff origin/main...HEAD`, and the run dir. In the same brief, require it to append one line to `<run_dir>/review-progress.md` after each file or check it finishes (`<HH:MM> <file or check> done: <one-line finding or "clean">`), and to answer every watchdog message within that turn. Then start the watchdog (step 4a) before you wait. Ask it to review only for:
1. Does each test actually assert the behavior it claims? Look for tautologies, missing awaits, assertions that can never fail, and over-mocking.
2. Flakiness: timing, ordering, shared state, real network, `waitForTimeout`.
3. Whether it matches the neighbouring tests' conventions.
4. PHI or real customer data in fixtures.
5. Coverage of the plan's cases.

It returns `APPROVED` or a list of items, each tagged `[BLOCKER]` or `[HARDENING]`. Fix the blockers and re-run the same reviewer once (`SendMessage`). If a blocker remains after that, show both positions to the user and let them decide. List the `[HARDENING]` items in the PR body under "Follow-ups".

### 4a. Reviewer watchdog
Run it whenever a review is open: the first review, the re-review, and any step 6 re-review.

**Start:** write `watchdog.md` with `progress_lines=<current line count of review-progress.md>`, `nudges=0`, `forced=no`, `replacements=0`, and the dispatch time. Then `CronCreate` with recurring cron `7,22,37,52 * * * *` (every 15 minutes, off the hour) and this prompt: `ship-test-pr watchdog tick for <KEY>: follow step 4a in the ship-test-pr skill`. Record the job ID in `watchdog.md`.

**Each tick:**
1. If the reviewer's verdict (`APPROVED` or a tagged list) has arrived, `CronDelete` the job, log `closed`, and stop. When the verdict arrives between ticks, delete the job right away; don't wait for the next tick.
2. Count the lines in `review-progress.md`.
   - **The count went up** since the last tick: save the new count, reset `nudges=0` and `forced=no`, and send nothing. It's working.
   - **No change and `nudges` < 2:** `SendMessage` to `reviewer`: `Watchdog nudge <n>/2: no progress lines since <HH:MM>. Append a progress line now, then keep reviewing.` Here `<HH:MM>` is the time on the last progress line, or the dispatch time if there are none. Increment `nudges`.
   - **No change after 2 nudges:** send FORCE: `FORCE: two nudges went unanswered. In this turn, finish the remaining checks yourself, and do not delegate. Append a progress line for each, then return your full verdict. Do not end this turn without a verdict.` Set `forced=yes`.
   - **Still no change or verdict on the tick after FORCE, and `replacements=0`:** tell the user. Then `TaskStop` the reviewer, and spawn a replacement `reviewer` with the same brief plus "resume from `review-progress.md`; skip the checks already listed there". Keep the current line count as the baseline, reset `nudges` and `forced`, set `replacements=1`, keep the cron running, and log `replaced`. The replacement inherits the review round, so it doesn't add another re-review.
   - **The replacement also stalls through FORCE:** `CronDelete` the job, log `escalated`, and ask the user with `AskUserQuestion`: **I review it myself (lead)** / **Spawn one more reviewer** / **Stop**. Don't replace the reviewer again without the user's answer.
3. Append the tick to `watchdog.md`: the time, the count, and the action.

Never move to step 5 because the watchdog fired. Only a reviewer verdict closes the review. Cron jobs are session-only: if the session restarts mid-review, recreate the job from `watchdog.md`.

### 5. Ship (draft PR)
1. Push and open a **draft** PR with `zd-technology:create-pr`. It follows the repo's PR template. Link the Jira key.
2. Request reviewers. A team slug needs the REST call `gh api -X POST repos/Zocdoc/<repo>/pulls/<n>/requested_reviewers -f 'team_reviewers[]=<slug>'`, because `--reviewer Zocdoc/<team>` fails with this PAT.
3. Tag CodeRabbit (babysit-pr does this for each push).
4. Every GitHub comment you post ends with `*- Claude*`. If the user approved its text, end it with `*- akash-nair-zocdoc via Claude*` instead.

### 6. CI and feedback (babysit-pr)
Start `babysit-pr` on the PR and follow this routing:
- **CI red:** fix it, re-run the step 3 tests-only check, then push. If "Detect Breaking API Changes" fails on a PR that only touches tests, rebase onto `origin/main`. Never post `approve-breaking-change`.
- **Comments:** comments from the PR author, and AI comments (CodeRabbit) while the PR is draft, are decided by the lead. Comments from **non-author humans** go to the user with a recommendation (apply/skip/defer), and the lead waits for their decision.
- If a fix changes test logic and isn't just a rename or format change, re-send the reviewer from step 4 the new diff before pushing, and restart the watchdog (step 4a).
- Ask the user before marking the PR ready for review.

### 7. Merge → **GATE 2**
Merge only when all three hold:
1. The required checks are green.
2. There is at least one valid GitHub approval from a **human** other than the author. CodeRabbit and other bots don't count.
3. The user authorizes the merge, either in the terminal or in their own PR comment or review.

"Just merge it" covers only condition 3. If a human approval is missing, say so, and offer to ping the requested reviewers. Being in a rush never removes Gate 1 or Gate 2: the light workflow skips ceremony, not approvals.

Then merge, and report the PR URL, the merge commit, and the follow-ups. If the run surfaced something new about the repos or the tooling, save it to memory.

## Common mistakes
| Mistake | Fix |
|---|---|
| Branching from a stale local `main` | Branch from `origin/main` after a fetch |
| Planning a ticket that's already done | Run step 1.3 before step 2 |
| Saying "tests pass" when local runs were blocked | Say "CI only" and point to the build |
| Loosening an assertion so a bug passes | Ask the user: ignore it with the bug key, or log it |
| Requesting the team via `--reviewer` | Use the REST `requested_reviewers` call |
| Acting on a non-author human comment alone | It goes to the user |
| Merging on CI green alone, or on CodeRabbit's approval | All three conditions are required, including a human approval |
| Waiting on a silent reviewer with no watchdog running | Start step 4a before every review wait |
| Nudging because the reviewer took a while, even though progress lines are being added | Nudge only when the line count hasn't moved |
| Patching the production null-check "since it's one line" | That's no longer a tests-only PR. Ask the user |
