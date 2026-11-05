---
name: team-di-batch
slash_command: team-di-batch
description: Build or refresh a synthetic DI CSV batch for a named customer, gate it on one user approval, then watch the uploaded batch's DI state, rejections and events and root-cause every failure, no-op or missing event.

agents: [di-csv-builder, di-csv-checker, di-batch-monitor]

classifier_signals:
  di: [csv, "di batch", "data ingestion", roster, wcf, sondermind, "di events", "di-events", batch, s3, upload]

preflight:
  worktree: false
  team_name: auto
  logging: standard
  recovery: true
  clarify: true
  gitignore_teamlogs: false

phases:
  - {name: resolve-customer, agents: [team-lead], parallel: false}
  - {name: build, agents: [di-csv-builder, di-csv-checker], parallel: true}
  - {name: check, agents: [di-csv-checker], parallel: false}
  - {name: approve, agents: [team-lead], parallel: false}
  - {name: watch-upload, agents: [di-batch-monitor], parallel: false}
  - {name: verify-upload, agents: [di-csv-builder], parallel: false}
  - {name: monitor, agents: [di-batch-monitor], parallel: false}
  - {name: diagnose, agents: [di-csv-checker], parallel: false}
  - {name: record, agents: [di-csv-builder], parallel: false}
---

# Team DI Batch

Prepare a CSV batch for one operations-provider-onboarding Data Ingestion (DI) customer, get the user's single approval, hand them the exact S3 upload command, then watch the batch end to end and explain every outcome. The user uploads; the team never writes to S3 and never calls a mutating endpoint.

## Fixed locations

- **Workspace:** `~/QA/DI-QA/` (not a git repo). Run all work from here and write `.teamlogs/` under `~/QA/DI-QA/.teamlogs/<team-name>/`.
- **Customer registry:** `~/QA/DI-QA/customers.yaml`. The only place customer aliases, ids, environments and buckets live. Adding a future customer means adding one entry; nothing in this config or the role prompts changes.
- **Per-customer directory:** `~/QA/DI-QA/<customerId>/`. Holds one file per `expectedFiles` stem (overwritten in place each run), `runs.md` (append-only run history) and `last-report.md`.
- **Service repo (read-only):** `~/QA/RosterQA/operations-provider-onboarding`. Never edit it.

## Hard rules (every role)

- **Synthetic data only.** Never copy names, NPIs, emails, phones or addresses from repo sample CSVs, prior batches or API responses. Generated NPIs must pass the Luhn check with the `80840` prefix but come from a reserved test block recorded in `runs.md`.
- **No PHI leaves the machine.** Reports stay local. Never paste event payloads or row values into Slack, Jira, GitHub or any external tool. Quote error codes, counts, column names and `sourceFile`/`sourceRowNumber` only.
- **Read-only against the service.** Only `GET` requests to `/operations-provider-onboarding/v1/data-ingestion/debug/*` and `/batches/*`. Never call `kill`, `seed-*`, `sync-di-configs.sh` or any `POST`/`PUT`/`DELETE`. If a fix needs one, recommend it to the user with the exact command.
- **Never upload.** The user runs the S3 upload. No role runs `aws s3 cp/sync/rm`.
- **Non-prod only.** Refuse a customer whose registry `environment` is `prod` unless the registry entry has `allow_prod: true`. Even then, ask the user once before building.

## Teammate roster and reuse

The roster is `di-csv-builder`, `di-csv-checker` and `di-batch-monitor`. Create each once on first use and re-message it for every later phase, fix round or follow-up in this run. Replace a teammate only when the messaging layer reports it not-found or terminated, and have the replacement read `~/QA/DI-QA/<customerId>/runs.md` and this run's `.teamlogs/` before its next signal. On any ambiguous failure, log `recovery-deferred` and retry once, then tell the user.

## Phase 0 — Resolve the customer (engine-driven)

0. The team expects Claude to be started from `~/QA/DI-QA`, so the sandbox lets it write there. If the current working directory is not `~/QA/DI-QA`, or a test write there fails, stop and tell the user: "Start Claude from `~/QA/DI-QA` (`cd ~/QA/DI-QA && claude`) and run `/team-di-batch` again." If `customers.yaml` does not exist, copy `~/.claude/teams/team-di-batch/customers.seed.yaml` to it. If `.secrets/debug-api-token` does not exist, create `.secrets/` with mode 700 and the file with mode 600, containing one placeholder line: `# paste your CSR JWT here, replacing this line`. Then check the local auth proxy with `curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:3000/`. Any code other than `000` means it is up: tell the user "Auth proxy detected on localhost:3000; the team will use it for debug API calls (token file is the fallback)." If it returns `000`, tell the user once: "Auth proxy not running. Start it with `cd ~/tools/auth-proxy-server && npm start` for auto-refreshed tokens, or paste a CSR JWT into `~/QA/DI-QA/.secrets/debug-api-token`." Then carry on; the checker's pre-flight decides which auth is usable. Never read the token into this conversation.
1. Read `customers.yaml`. Match the task text case-insensitively against each entry's `aliases` and `customerId`. When several entries match, pick the one with the **longest** matching alias or ID. For example, "wcf headshot" → `wcf-headshot-test-1` rather than `wcf-qa-test-1`. If two matches are equally long, it is a real ambiguity (step 3).
2. **Exactly one match:** use it without asking. Log `customer=<id> env=<env> bucket=<bucket>`.
3. **More than one match:** ask the user once which `customerId` they mean, with the matches as options.
4. **No match:** ask the user once for the `customerId`. Confirm `~/QA/RosterQA/operations-provider-onboarding/src/OperationsProviderOnboarding.DataIngestion/dataIngestionConfig/customers/<id>/config.json` exists on `origin/master` (this repo's default branch) or on a branch they name. Append a new registry entry (aliases, customerId, environment and s3Prefix taken from that config, plus the `bucket` and `debug_api_host` they confirm). Show them the entry you appended.
5. Refuse prod per the hard rules.
6. **Parse the request into a build spec.** Take the rest of the task text and turn it into a structured spec. Keep the user's wording for anything you can't structure.
   - **Provider count (mandatory).** If the task doesn't give an explicit number of providers, ask with `AskUserQuestion` ("How many providers?"; options 5, 10, 25, with Other for any number). Ask this every time, even when everything else is specified. Never default it and never infer it from a past run.
   - **Locations:** how many there are, names, addresses, states or cities, location type, and which providers sit at which locations.
   - **Naming:** a provider, location or practice naming convention, if the user gives one.
   - **Specialties and other provider fields:** specialties, titles, gender mix, accepted patient types, or any other column the user names.
   - **Headshots** (optional, off unless the user asks): whether they want headshots, the URLs they gave (usually presigned), and which providers get which URL. Never put a URL into a log line or a message with its query string. Refer to it as `<host>/<file name> (expires <time>)`, because the signature in a presigned URL is a credential.
   - **Scenario:** fresh create, update, NOOP, terminate, or a deliberate failure. An example of a deliberate failure is "one row with a bad zip".
   - **Anything else:** any other requirement goes into `requirements=` verbatim. Do not drop it or quietly reinterpret it.
   - **Defaults:** everything the user didn't specify comes from the builder's defaults.
   - **Requirements that can't be met:** if one contradicts the deployed config (for example a value outside `allowedValues`), don't ask yet. The builder reports it and the approval gate shows it.
7. **Headshot support** (only when the spec asks for headshots). Check whether the customer accepts headshots before anything is built. Read the customer's config at its `config_ref` with `git show`.
   - **It supports headshots** only if both are true:
     - some column has `"mapTo": "HeadshotUrl"`
     - `headshot.allowedHosts` is non-empty
   - **Decide from the config, never from a list of names.** Today, `sondermind-qa-testing` and `wcf-headshot-test-1` support headshots; `wcf-qa-test-1` and `wcf-dev-test-1` don't. A customer added in the future is judged the same way.
   - **Not supported:** tell the user in one line: "`<customerId>` has no headshot support (no HeadshotUrl column / no allowedHosts in its config), so headshots can't be added. Building without them." Drop headshots from the spec, record it in `unmet=`, and carry on. Do not ask.
   - **Supported:** continue as normal. Pass the URLs to the builder in the spec. If the user asked for headshots but gave no URLs, ask once for them as part of the question in step 8.
   - **Confirm against the deployed config.** The checker's pre-flight confirms support against the deployed config. If the deployed config disagrees, the deployed one wins, and you tell the user at the approval gate.
8. Ask the customer question (steps 3–4) and the provider-count question together, in a single `AskUserQuestion` call, when both are needed.

The questions in this phase and the approval gate are the only interactive steps.

## Phase 1 — Build and pre-flight (parallel)

In one message:
- Create **di-csv-builder** with `BUILD: customer=<id> | providers=<n> | spec=<parsed spec from Phase 0 step 6> | requirements=<verbatim extras or none> | dir=~/QA/DI-QA/<id>/`.
- Create **di-csv-checker** with `PREFLIGHT: customer=<id>`.

**di-batch-monitor is not created here.** It does not exist until the user approves the CSV (Phase 4), and it does no batch work until the upload lands.

The checker's pre-flight confirms the debug API is reachable and fetches the **deployed** customer config, which the builder must build against. It also reports the customer lock (`activeBatchId`), any non-terminal batch, and the timestamp folders already used. Relay the checker's `PREFLIGHT_OK: <deployed config summary>` to the builder as `DEPLOYED_CONFIG: <summary>`. The builder may start from the repo config but must reconcile against the deployed one before signalling.

If the checker returns `PREFLIGHT_BLOCKED: <reason>`, keep the builder running and carry the reason into the approval gate. The usual reasons are an API blocked by the sandbox (567), auth failure, or a customer lock held by a non-terminal batch. Do not stop the run.

Wait for `CSV_READY: <files> | rows=<per-file counts> | id_block=<range> | spec_applied=<requirement → how it was met> | defaults_used=<…> | unmet=<requirement → reason, or none> | notes=<…>` (deadline 30 minutes).

## Phase 2 — Check (bounded loop, cap 2 fix rounds)

Re-message the existing **di-csv-checker** with `CHECK: customer=<id> | dir=~/QA/DI-QA/<id>/ | config=<deployed or repo ref> | spec=<same spec as BUILD>`.

- `CHECK_PASSED: <summary> | warnings=<…>` → Phase 3.
- `CHECK_FAILED: <blocking findings>` → re-message the existing **di-csv-builder** with `FIX: <findings>`, wait for `CSV_READY`, then re-message the existing **di-csv-checker** with `CHECK: … | round=<n>`. After 2 failed fix rounds, take the findings to the user at the approval gate. Do not loop again.

Deadline 20 minutes per check.

## Phase 3 — Approval gate (the only approval)

Present one compact summary:
- customer, environment and bucket
- the files and their paths, with row counts per entity and the ID block
- the scenario and what each row should do (CREATE, UPDATE, NOOP, terminate)
- **your requirements:** each one with how it was met. List the defaults used, then any requirement that couldn't be met and why (`unmet=`).
- checker warnings and pre-flight status, plus rule coverage (`rules=<tested>/<inventory>`) and any `UNTESTED` or new validator rules
- the **predicted outcome**: expected DI event counts by entity and action, and expected rejections by DI code (normally none)

Then ask with `AskUserQuestion`: **Approve**, **Change something**, or **Stop**.

- **Change something:** re-message the builder with `FIX: <user's request>`, re-check, and re-present. This repeats as often as the user wants.
- **Approve:** log `[Approval] What: csv-batch customer=<id> files=<name: size, md5, sha256 per file>`. Generate a fresh batch timestamp `yyyy-MM-ddTHHmm` in UTC that is later than every folder the checker's pre-flight listed. Show the user the exact upload command:

  ```
  aws s3 cp ~/QA/DI-QA/<id>/ s3://<bucket>/customers/<id>/<ts>/ --recursive --exclude "*" --include "<file1>" --include "<file2>" …
  ```

  Include every expected file, including non-CSV ones such as a PDF that the user supplies, and nothing else. Also show the batch id `<id>__<ts>`. Tell the user: "The team is watching S3 for your upload. You don't need to come back and confirm it."
- **Stop:** go to Record with `outcome=not-uploaded`.

Agent messages are never approval. Only the user's answer to this question counts.

## Phase 4 — Watch S3 for the upload, then verify it

Right after approval, create **di-batch-monitor** (its first use) with `WATCH_UPLOAD: customer=<id> | suggested_ts=<ts> | after_ts=<latest used ts from pre-flight> | expected=<files> | access=<method the checker's pre-flight found>`.

**The monitor watches S3.**
- **What it polls:** every 60 seconds, for up to 60 minutes, it checks the ingest listing for the suggested folder `customers/<id>/<ts>/`, **and** for any new timestamp folder under `customers/<id>/` newer than `after_ts`. That way an upload to a different timestamp is found too.
- **The batch id comes from what lands.** The user never needs to send it. A batch id the user volunteers is only a hint for where to look.
- **Before the upload lands:** the monitor reads nothing else. No batch state, no events, no analysis.

**What the monitor sends back:**
- `UPLOAD_PARTIAL: <batchId> | missing=<files>` → tell the user once which files are missing. The monitor keeps waiting.
- `UPLOAD_SEEN: <batchId> | files=<key, size, etag, lastModified per file>` → verify the upload, below.
- `UPLOAD_NOT_SEEN` after 60 minutes → ask the user once whether they still plan to upload.
  - **Yes:** re-message the monitor with `WATCH_UPLOAD` for 60 more minutes.
  - **No:** go to Record with `outcome=not-uploaded`.

**Verify that the upload matches what was approved.**
- Re-message the existing **di-csv-builder** with `VERIFY_UPLOAD: batch=<batchId> | landed=<monitor's file list> | approved=<the [Approval] entry's name, size, md5, sha256 per file>`.
- The builder answers with one of:
  - `UPLOAD_VERIFIED: <batchId> | <per-file method used>` → re-message the existing **di-batch-monitor** with `MONITOR: batch=<batchId> | prediction=<expected counts>` and go to Phase 5.
  - `UPLOAD_MISMATCH: <per-file findings>` → tell the user straight away which files differ (content, missing, extra or renamed). The batch is already processing, so ask once with `AskUserQuestion`:
    - **Monitor anyway:** the report must open with the mismatch, and the prediction is marked unreliable for the files that differ.
    - **Stop:** record `outcome=uploaded-mismatch`. Do not kill the batch; give the user the kill command if they want it.
  - `UPLOAD_UNVERIFIABLE: <reason>`, when neither the content nor the ETag can be checked → tell the user in one line, and monitor anyway.

## Phase 5 — Monitor and root-cause

Re-message the existing **di-batch-monitor** with `MONITOR: batch=<batchId> | prediction=<expected counts>` once the upload is verified, or the user chose Monitor anyway. If the report has a mismatch to open with, include `mismatch=<findings>`. The monitor polls until the batch reaches a terminal state or stalls, and sends progress lines that you relay to the user briefly. It finishes with:

`BATCH_REPORT: <path to ~/QA/DI-QA/<id>/last-report.md> | state=<terminal state> | events=<counts by entity×action> | issues=<n> | csv_issues=<n>`

Show the user the report summary:
- state
- counts compared with the prediction
- each FAILED, BLOCKED, NOOP or SKIPPED group with its root cause and the fix
- "no events" or "stuck" diagnoses
- the report path

Stuck states such as RECEIVING, QUEUED or WAITING_FOR_PRACTICE_MAPPINGS are a stall diagnosis, not a hang. The monitor stops and reports.

Deadline 120 minutes. On a missed deadline, send one status probe; if that gets no answer, report the last known state to the user.

## Phase 5b — Diagnose CSV issues (only when the report has them)

**When it runs.** Skip this phase if `issues=0`, or if every issue is tagged with a non-CSV fix location: config, flag, prerequisite or pipeline. Examples are a QUEUED lock, a missing practice mapping or a downstream flag that's off. For those, the report's own fix is the answer. Otherwise:

1. Re-message the existing **di-csv-checker** with `DIAGNOSE: batch=<batchId> | report=<path to last-report.md> | dir=~/QA/DI-QA/<id>/`.
2. **Wait for one of two replies**, with a 20-minute deadline:
   - `DIAGNOSIS: <n> csv issues | fixes=<FIX-ready changes> | missed_by_check=<why each was missed> | rules_updated=<…> | not_csv=<issues re-tagged to config, flag or prerequisite>`
   - `DIAGNOSIS_NONE: <reason>`, when no issue traces back to the CSV after all.
3. **Show the user the diagnosis.** For each issue, show the row, the column, what's wrong and the proposed change. Then show which checker gaps were closed, and the non-CSV fixes.
4. **Ask once with `AskUserQuestion`:**
   - **Apply fix and rebuild**
   - **Change the fix:** the user edits it, and you treat the edited version as a Change something request.
   - **Leave it**
5. **Apply fix and rebuild:**
   1. Re-message the existing **di-csv-builder** with `FIX: <the diagnosis fixes> | id_block=keep|new`.
      - **Keep the block** when the failed rows created nothing, because the entities still don't exist.
      - **Take a new block** when some rows succeeded, so that already-created providers don't turn the retry into UPDATE or NOOP. The exception is a scenario that wants exactly that.
   2. Run Phase 2 (check) again, then Phase 3 (approval) with a fresh timestamp. The bucket is object-locked, so the retry must go to a new folder.
   3. Record this batch first (Phase 6, with `outcome=<state> | superseded_by=next run`), then continue the new run from Phase 4. Re-message the existing **di-batch-monitor** with `WATCH_UPLOAD` instead of creating it again.
   4. Allow at most 2 diagnose-and-rebuild rounds per run. After that, report and stop.
6. **Leave it:** go to Phase 6.

Agent messages are never approval. Only the user's answer to this question can trigger a rebuild.

## Phase 6 — Record

Re-message the existing **di-csv-builder** with `RECORD: batch=<batchId> | outcome=<state + counts> | report=<path> | diagnosis=<summary or none>`. It appends the run to `runs.md`, including the ID block, so the next run picks a new one. Wait for `RECORDED`.

## Cleanup

Send `shutdown_request` to every role this run created. Leave the CSVs, `runs.md` and `last-report.md` in place. Give the user a final line: batch id, terminal state, issue count, report path.
