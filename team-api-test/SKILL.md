---
name: team-api-test
description: E2E API test from a pasted cURL or endpoint. Finds the API contract, prod route and service code, looks for gaps (unknown, malformed or wrong-type IDs, batch edge cases, auth), runs tests on prod .com through the local auth proxy (writes only on verified test data), and writes an HTML report with /log-bug descriptions. Routes to the team-lead engine with the team-api-test config.
argument-hint: "<cURL or METHOD /path> [related Jira keys]"
---

# Team API Test

Invoke the **team-lead** skill with the single argument `team-api-test <the user's cURL or endpoint, plus any Jira keys>`. Pass the input through exactly as given; the team's Phase 1 parses it and drops any auth headers.

The full workflow is in `~/.claude/teams/team-api-test.md`. Before starting, the auth proxy must be running (`cd ~/tools/auth-proxy-server && npm start`).
