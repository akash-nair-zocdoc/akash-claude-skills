---
name: team-di-batch
description: Build or refresh a synthetic DI CSV batch for a named customer (WCF, SonderMind, or any customer in ~/QA/DI-QA/customers.yaml), gate it on one approval, then monitor the uploaded batch's DI state and events and root-cause failures, no-ops and missing events. Routes to the team-lead engine with the team-di-batch config.
argument-hint: "[customer] [N providers] [any requirements, e.g. '3 locations in NJ, all Pediatrics']"
---

# Team DI Batch

Run all work from `~/QA/DI-QA/`.

Invoke the **team-lead** skill with the single argument `team-di-batch <customer and scenario>`. The engine parses the first whitespace-separated token as the team name and the rest as the task.

The full workflow is in `~/.claude/teams/team-di-batch.md`.
