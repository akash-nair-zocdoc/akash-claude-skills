---
name: team-api-test
slash_command: team-api-test
description: Take a pasted cURL or endpoint, find its API contract, prod route and service code, look for contract/code gaps (unknown, malformed or wrong-type IDs, batch edge cases, auth), run the tests on prod .com (the public gateway, or any internal *.zocdoccloud.com host) through the local auth proxy behind a script-enforced safety gate, and write an HTML report with a /log-bug description for every failure.

agents: [api-researcher, api-tester]

classifier_signals:
  api: [curl, endpoint, "api test", "e2e api", contract, "api-specs", "gap", "404", "400", "prod api"]

preflight:
  worktree: false
  team_name: auto
  logging: standard
  recovery: true
  clarify: true
  gitignore_teamlogs: false

phases:
  - {name: preflight, agents: [team-lead], parallel: false}
  - {name: parse, agents: [team-lead], parallel: false}
  - {name: research, agents: [api-researcher], parallel: false}
  - {name: review-plan, agents: [team-lead], parallel: false}
  - {name: test, agents: [api-tester], parallel: false}
  - {name: report, agents: [team-lead], parallel: false}
---

# Team API Test

A QA engineer pastes a cURL or an endpoint. The team finds the contract and the code, looks for gaps, runs tests against **prod `.com`** through the local auth proxy, and writes one HTML report. Every HTTP request to Zocdoc goes through `scripts/safe_request.py`, which enforces the safety rules in code.

## Fixed locations

- **Team dir:** `$HOME/.claude/teams/team-api-test/`
  - `config.json` holds the public hosts (`allowed_hosts`), the internal domain (`internal_host_suffixes`: `zocdoccloud.com`), the request budget, timeouts and redaction keys.
  - `scripts/safe_request.py` is the only HTTP gate.
  - `scripts/render_report.py` builds the report.
  - `hooks/block_raw_http.py` is a global PreToolUse hook in `~/.claude/settings.json`. It blocks Bash commands that send raw HTTP to `*.zocdoc.com` or `*.zocdoccloud.com`. `*.zocdoccloud.net` traffic is still allowed, so team-di-batch keeps working.
  - `cache/id_checks.json` holds Snowflake verdicts, kept for 24h. Only `sf-ingest` writes it; `cache/sf_pending/` holds the one-time nonces `sf-query` issued.
  - `test-data.json` is optional: the team's known test IDs.
- **Run dir:** `$HOME/.claude/teams/team-api-test/runs/<YYYYMMDD-HHMM>-<method>-<slug>/`. It holds `research.json`, `contract.yaml`, `plan.json`, `bodies/`, `responses/<seq>.json` (redacted), `requests.jsonl` and `report.html`.
- `.teamlogs/` goes in the current working directory.

## Hard rules (every role)

- **Prod `.com` only, in one of two modes.**
  - **Public (default):** `api.zocdoc.com` or `pulse.zocdoc.com`, picked by the route lookup.
  - **Internal:** any prod `*.zocdoccloud.com` host, e.g. a service host like `provider-grouping-v1.east.zocdoccloud.com` or the private gateway `api-private.east.zocdoccloud.com`. Use it for services or paths the public gateway doesn't expose (OPO, provider-profile, provider-reference, POGS, …).
  - Never call a `.net` host. If the user pastes a `.net` URL, swap it for the same host name on `.com` and say so (`svc.east.zocdoccloud.net` becomes `svc.east.zocdoccloud.com`, internal mode). If they paste localhost, keep only the path and use public mode.- **Only the gate sends requests.** No role calls a Zocdoc host with `curl`, `wget`, inline Python or anything else. The one exception is the lead's proxy probe to `localhost:3000/`, which sends nothing upstream. The `block_raw_http.py` hook enforces this rule: a blocked command means use the gate, never a workaround.
- **Reads are free; writes need test data.** GETs may read any prod data. POST, PUT, PATCH and DELETE are sent only when every `pr_`/`pt_`/`org_`/`lo_` ID in the URL and body is all zeros (fake) or verified test data in non-PHI Snowflake. The gate blocks anything else, including a real ID mixed into a batch. On a block: stop, tell the user, ask for test data. Never edit a request to get it past the gate, and never retry a write.
- **Never handle the token.** The proxy adds `Authorization`. If a pasted cURL has an `Authorization`, `Cookie` or `X-Target-Host` header, drop it without repeating its value anywhere, and tell the user it was dropped.
- **No PHI leaves the machine.** The report and run files stay local. Messages and bug descriptions quote endpoints, test or fake IDs, status codes and `path:line` references only, never response bodies. Snowflake is non-PHI only, and only ID columns are selected.
- **Bounded.** At most 30 gh calls for research, at most 20 sends per run (the gate's hard cap is 60), and at most 1 re-plan round.

## Teammate roster and reuse

The roster is `api-researcher` and `api-tester`. Create each once on first use with `Agent(name=...)`, passing its resolved prompt. Re-message it with `SendMessage` for every later step in this run. Replace a teammate only if the messaging layer reports it not-found or terminated. The replacement reads the run dir before its next signal.

## Phase 0: Preflight (lead)

1. **Input check.** If the argument is empty, ask once: "Paste the cURL or the endpoint (method + path) to test."
2. **Auth proxy.** Run `curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:3000/`.
   - `000` means it's down. Tell the user: "Auth proxy isn't running. Start it with `cd ~/tools/auth-proxy-server && npm start`, then reply `done`." Wait, then probe again.
   - Any other code means it's up.
3. **Snowflake.** Run `python3 $HOME/.claude/teams/team-api-test/scripts/safe_request.py preflight` (it checks the proxy only). Then run `select current_account()` with the non-PHI `snowflake_pr001` tool.
   - If the tool needs sign-in, tell the user to run `/mcp` and sign in to `snowflake_pr001`, then try once more.
   - If it still fails, or the account isn't ZOCDOC_001, tell the user: writes will all be blocked, and GET tests can still run. Ask whether to continue.
   - IDs are verified with the hand-off in the tester prompt: `sf-query`, then the `sql` it prints run with `snowflake_pr001`, then the saved output given to `sf-ingest`. The lead uses the same steps whenever it checks IDs itself.
4. **GitHub.** Run `gh auth status`. If it fails, stop and tell the user to run `! gh auth login`.

## Phase 1: Parse (lead)

From a cURL, pull out:
- the method (`-X`/`--request`; it's POST if there's `-d`/`--data*` and no `-X`; otherwise GET)
- the URL path and query
- the body from `-d`/`--data`/`--data-raw`/`--data-binary`, or a file named with `@file`
- any non-auth headers.

From a bare endpoint like `GET /provider-grouping/v2/...` or a full URL, take the method (default GET) and the path.

**Pick the mode.**
- **Internal:** the input has a `*.zocdoccloud.com` or `*.zocdoccloud.net` host, or the user says `internal` or `env=internal`, or names an internal host. Set `host=<that host>` (a `.net` host becomes `.com`).
  - If the user said internal but gave no host, use `host=auto`. The researcher picks the service host.
  - Confirm once, before research starts: "Running against internal host `<host>` on prod. Reads are free; writes need all-zeros or verified test IDs, same as public. OK?"
- **Public:** anything else. Use `host=auto`.

Then do the following:
- **Path template.** Normalize the path into a template by turning each `pr_`/`pt_`/`org_`/`lo_` ID into a `{param}`. Keep the concrete values so you can offer them as test data.
- **Run dir.** Create the run dir and save the parsed request to `<run_dir>/input.json`, with auth headers dropped. Include `mode` and `host`.
- **Related tickets.** Ask nothing yet. If the user mentioned Jira keys, keep them as related tickets.

## Phase 2: Research (api-researcher)

Send `RESEARCH: method=<M> | path=<path template> | mode=<public|internal> | host=<host or auto> | run_dir=<dir> | tickets=<keys or none>`. Wait up to 20 minutes.
- **`ROUTE_NOT_FOUND`** (public mode only): tell the user this path isn't exposed on the public gateway, and list the routing files that were searched. Offer to re-run in internal mode against the service host the researcher found, if it found one. Don't fall back to `.net`.
- **`RESEARCH_BLOCKED`:** tell the user what's missing, and ask for the service repo name or the contract path. Re-send RESEARCH once with that hint.
- **`RESEARCH_DONE`:** go to Phase 3.

## Phase 3: Review the plan (lead)

Read `research.json` and show the user a short plan:
- the prod host and route service, plus the mode. In internal mode, also say whether the path is exposed on the public gateway.
- the contract file
- the gaps found, one line each with `path:line`
- the tests, with their expected codes, marked read or write
- any skipped tests and why.

Then ask once with `AskUserQuestion`:
- **Run these tests (Recommended)**
- **Change the plan:** take their edits, re-send to the researcher once as `REPLAN: <edits>`, and show the plan again.
- **Stop**

If the pasted cURL contained concrete IDs, verify them first with the Snowflake hand-off (`sf-query`, `snowflake_pr001`, `sf-ingest`). Tell the user which ones are test data, and so usable for writes, and which aren't. Pass the test ones to the tester as `data=`.

## Phase 4: Test (api-tester)

Send `TEST: run_dir=<dir> | data=<verified test IDs or none>`. Also pass `related_tickets` for plan.json. Wait up to 20 minutes, and relay the tester's progress briefly.
- **`NEED_TEST_DATA: <type>`:** ask the user for a test `<type>` ID, then verify it yourself with the Snowflake hand-off.
  - If it's `test` or `fake`, re-send `TEST` with `data=`.
  - If it's `real` or `unknown`, say so, and ask again once. After that, offer to run without the tests that need it.
- **`BLOCKED_STOP`:** show the user the test and the gate's reason, and ask once:
  - **Give test data:** the same check as above, then `TEST` again.
  - **Skip that test and continue:** re-send `TEST` with `skip=<test id>`.
  - **Stop with the partial report**
- **`PROXY_DOWN`:** tell the user to restart the proxy, wait for `done`, probe it, then send `RESUME`.
- **`TEST_DONE`:** go to Phase 5.

## Phase 5: Report (lead)

1. **Verify before you announce anything:**
   - The report file exists.
   - `requests.jsonl` has no line with `"outcome": "sent"` whose method is a write and whose `ids` include a `real` or `unknown` verdict. There should be none; if there is one, say so first and loudly.
   - `grep -c "Bearer " report.html` is 0.
2. **Tell the user:**
   - the report path, as a clickable `file://` link plus `open <path>`
   - the host and mode that were tested
  - pass, fail, blocked and error counts
  - in internal mode, if the auth probe got 401/403 on both its no-auth and proxied requests: the service probably needs different auth than the pulse JWT the proxy adds. Say so rather than reporting the failures as API bugs.
   - one line per failed test: expected vs actual and the gap it shows.

   Say that each failed row in the report has a **Bug description for Claude** with a Copy button. They can paste it into a session to run `/log-bug`.
3. **Cleanup.** Send `shutdown_request` to each teammate. Leave the run dir in place.
