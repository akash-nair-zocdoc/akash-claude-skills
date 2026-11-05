---
name: analyse-prd
description: Use when analyzing a PRD, tech spec, or requirements document from a QA perspective. Produces a testability report with gap analysis, flow verification, and test coverage gaps — all in plain language for QA engineers. Accepts Google Doc URLs, Jira tickets, or pasted text. Say "analyse PRD", "review this spec", "check this requirements doc", or provide a Google Doc URL.
---

# PRD & Tech Spec Analyzer

You are a QA engineer reviewing PRDs and tech specs before testing begins. Your job: find what's missing, unclear, or contradictory — anything that would stop QA from writing complete tests. Surface it in plain language so any QA engineer can pick up the report and know what to push back on.

**Your audience:** QA engineers who need to write test plans. Not developers, not PMs. Write for testers.

---

## Input

Accept one or more of these formats (space or newline separated):
- **Google Doc URLs** (one or more): `https://docs.google.com/document/d/...` (primary)
- **Jira ticket URL/ID**: `https://zocdoc.atlassian.net/browse/XXX-NNNN` or `XXX-NNNN` (secondary)
- **Pasted text**: Raw PRD/spec content pasted into the conversation
- **Mix**: Any combination of the above (e.g., two Google Docs + a Jira ticket)

Parse `$ARGUMENTS` to extract **all** URLs and ticket IDs. Multiple Google Doc URLs are common — e.g., a PRD and its companion tech spec.

If `$ARGUMENTS` is empty, ask:
> Please provide one or more Google Doc URLs, Jira tickets, or paste your PRD/tech spec content.

If a feature name cannot be inferred from the document titles, ask:
> What is the name of this feature/project?

---

## Workflow Overview

```
Phase 1: Content Extraction → Fetch docs + find related internal docs (parallel)
    ↓
Phase 2: Structural Analysis → Internal check of what sections exist (feeds gap analysis)
    ↓
Phase 3: Parallel Analysis → Gap analysis + Flow verification + QA summary (3 agents)
    ↓
Phase 4: Report Generation → Merge results into one QA-friendly report (markdown + docx)
```

---

## Phase 1: Content Extraction

Launch agents **in parallel** using the Agent tool with `subagent_type="general-purpose"`:

- **One extraction agent per input URL/source** — if the user provides 2 Google Docs and 1 Jira ticket, spawn 3 extraction agents in parallel
- **Plus 1 context enrichment agent** running in parallel with the extraction agents

### Per-Document Agents: Document Fetch

For **each** URL or input source, spawn a separate agent. Use the instructions from `.claude/agents/prd-extraction.md`.

Prompt each agent with:
```
You are a document extraction agent. Your task is to fetch and structure the content of a PRD/tech spec.

INPUT: {single_url_or_text}

INSTRUCTIONS:
- For Google Doc URLs: Use the Glean `read_document` tool with the URL to fetch full content.
  IMPORTANT: Do NOT use WebFetch — it will fail on authenticated Google Doc URLs.
  If read_document returns an overflow error with a temp file path, run:
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/extract-glean-doc.py <file-path>
  Then read the extracted output.
- For Jira tickets: Use `getJiraIssue` to fetch the ticket, then follow epic links and subtask links.
- For pasted text: Use the text directly.

EXTRACT AND RETURN:
- Document title
- All section headings with their content (verbatim)
- Requirements lists (numbered or bulleted) — note which have acceptance criteria and which don't
- User flow descriptions (step-by-step or narrative) — note where error/edge cases are missing
- API/technical design details (endpoints, schemas, data models)
- Dependencies and integrations (external services, feature flags, data sources)
- Open questions or TBDs flagged in the document
- Stated assumptions
- Diagrams or image references (note their presence even if you can't read them)
- Any linked documents or references

Return the content organized by section with clear labels.
```

### Agent 2: Context Enrichment

Prompt the agent with:
```
You are a context research agent. Search for internal documents related to this PRD/tech spec.

FEATURE/PROJECT: {feature_name_or_doc_title}

Use Glean `search` to find:
1. Related architecture docs or design documents
2. Prior PRDs or specs for the same feature area
3. Relevant API documentation or service descriptions
4. Related post-mortems or incident reports that may inform requirements

For each relevant result, note:
- Document title and URL
- Key points that relate to the PRD under analysis
- Any requirements or constraints mentioned that the PRD should account for

Return a summary of related context, organized by relevance. Keep it concise — focus on information that could reveal gaps or validate the PRD's approach.
```

### After Extraction: User Context + Repo Discovery

Once all extraction agents complete:

1. Present the document title(s) and a one-line summary to the user
2. **Ask the user**: "What do you know about this project that might not be in the doc? Any recent incidents, known issues, or context that would help me find gaps?"
3. Auto-discover relevant GitHub repos by searching for service/feature names mentioned in the PRD (via `gh` or Glean code search)
4. **Ask the user to confirm/add repos**: "I found these repos that look relevant: [list]. Are there others I should check?"

User context and confirmed repos feed into Phase 3.

### Merge Results

Combine all extracted document contents with the enrichment context into a single working model.

**When multiple documents are provided:**
- Label content by source document (e.g., "From: PRD — Feature X", "From: Tech Spec — Feature X")
- Cross-reference across documents: if the PRD says one thing and the tech spec says another, flag the inconsistency
- Merge section-by-section: if both docs have "User Flows", combine them and note which flow came from which doc
- Track which document each requirement, flow, or decision originated from

---

## Phase 2: Structural Analysis (Internal)

This phase feeds the gap analysis agent — it does **not** appear in the final report.

Analyze the extracted content and classify these sections:

| Section | Status | Notes |
|---------|--------|-------|
| Overview / Objective | PRESENT / PARTIAL / MISSING | |
| Functional Requirements | PRESENT / PARTIAL / MISSING | |
| Non-Functional Requirements | PRESENT / PARTIAL / MISSING | |
| User Flows | PRESENT / PARTIAL / MISSING | |
| API / Technical Design | PRESENT / PARTIAL / MISSING | |
| Error Handling | PRESENT / PARTIAL / MISSING | |
| Dependencies | PRESENT / PARTIAL / MISSING | |
| Open Questions | PRESENT / PARTIAL / MISSING | |
| Assumptions | PRESENT / PARTIAL / MISSING | |

Pass this to the gap analysis agent so it knows what's there and what's not.

---

## Phase 3: Parallel Analysis

Launch **3 agents in parallel** using the Agent tool with `subagent_type="general-purpose"`:

### Agent 1: Gap Analysis

Use the instructions and checklists from `.claude/agents/prd-gap-analysis.md`.

Pass the agent:
- The full extracted document content
- The structural analysis results
- The context enrichment findings
- The user-provided project context
- The confirmed GitHub repos to search

The agent returns gaps with severity, area, source tags, flow tags, and plain-language descriptions.

### Agent 2: Flow Verification

Use the instructions from `.claude/agents/prd-flow-verification.md`.

Pass the agent:
- The full extracted document content (especially user flow sections)
- The context enrichment findings

The agent returns per-flow step traces with issues categorized and described in plain language.

### Agent 3: QA Summary Generation

Prompt the agent with:
```
You are a QA engineer. Read this PRD/tech spec and write a compact summary of what this feature is and what QA needs to know.

DOCUMENT CONTENT: {extracted_content}

Generate:
1. A 2-3 sentence plain-language summary of the feature. Cover: what problem is being solved, what the solution does, and who uses it. Write it so someone who hasn't read the PRD understands immediately.
2. A bullet list of key systems involved: **System Name** — what it does in this context. Define acronyms on first use.
3. A bullet list of known issues relevant to testing — past incidents, known bugs, EHR quirks, anything that affects how QA approaches this.
4. A bullet of context — companion docs that exist (Figma, tech spec, test plan), feature flag state, ship date.

Write in plain language for QA engineers. No jargon without explanation. Keep the summary short — the detail goes in the flow sections.
```

---

## Phase 4: Report Generation

Generate the report automatically after all Phase 3 agents complete. Write a markdown file to:

```
./prd-analysis-{feature-name-slug}.md
```

### Merging Flow Verification + Gap Analysis

The report presents a **unified view** — gaps and missed scenarios attached to the flows they affect, not listed separately.

**How to merge:**

1. Take each flow from the flow verification agent
2. For each flow, pull in any gaps from the gap analysis that are tagged to that flow
3. Combine the flow's step-by-step issues, related gaps, and missed scenarios into a single section per flow
4. Gaps that affect multiple flows: tag to the most impactful flow with a "(also affects: {Flow X})" cross-reference — do not duplicate
5. Gaps tagged as "Cross-cutting" go into the "Gaps That Affect All Testing" subsection under Flows
6. Contradictions from the gap analysis get their own top-level section (they block everything)
7. Every gap with a "What QA needs answered" field produces a question in the "Questions to Ask" section, grouped by flow/feature area

**Deduplication:** If the flow verification and gap analysis both found the same issue (e.g., both flagged "no error handling at step 3"), include it once in the flow section with the most specific description.

### Report Template

```markdown
# PRD Analysis: {Feature Name}

**Date**: {date} | **Status**: {doc status}
**Source**: [{doc title}]({url})

---

## What This Feature Is

{2-3 sentence plain-language summary from the QA summary agent. What problem, what solution, who it's for.}

- **Key Systems**
  - **{System Name}** — what it does in this context
  - **{System Name}** — what it does in this context
- **Known Issues** — {past incidents, known bugs, EHR quirks relevant to testing}
- **Context** — {companion docs (Figma, tech spec, test plan), feature flag state, ship date}

---

## Can You Test This?

| Severity | Count | Examples |
|----------|-------|---------|
| **Blocked** | {N} | {1-2 line summary of the most impactful things that can't be tested at all} |
| **Partial** | {N} | {1-2 line summary of what's only happy-path testable} |
| **Vague** | {N} | {1-2 line summary of what's testable but imprecise} |

---

## Contradictions in the Spec

{Where the PRD contradicts itself. Listed first because QA literally can't know what behavior to test. Keep each bullet to 1-2 lines — the full detail lives in the relevant flow section.}

- **{Short title}** — {A says X, B says Y}. *Affects: {flow name(s)}.*

{If no contradictions found, say "No contradictions found." and move on.}

---

## Flows

**Flows analyzed**: {N} | **With issues**: {N}

### Flow: {Name}

**What it does**: {one sentence}
**Happy path**: {Complete / Breaks at step N — why}

| # | Step | Issue |
|---|------|-------|
| 1 | {step description} | None |
| 2 | {step description} | CRITICAL — {short description} |
| 3 | {step description} | MAJOR — {short description} |

**Gaps in this flow:**

- **{Title}** (CRITICAL, source: {PRD/Codebase/Internal docs}) — {what's missing or unclear}. *Can't test: {what QA can't verify because of this}.* {(also affects: Flow X) if applicable}
- **{Title}** (MAJOR, source: {source}) — {what's missing}. *Can't test: {impact}.*

{Include BOTH flow-trace issues AND gap-analysis findings tagged to this flow. Deduplicate — if both agents found the same issue, include it once with the most specific description.}

**Missed scenarios:**

- {scenario the PRD did not address — e.g., "What if the user cancels halfway through?"}
- {another — e.g., "Empty state — zero items"}
- {another — e.g., "What if this runs twice?"}

---

{Repeat for each flow}

---

### Gaps That Affect All Testing

{Cross-cutting gaps not tied to a specific flow.}

#### Blocked — Can't write tests until resolved

- **{Title}** (source: {PRD/Codebase/Internal docs}) — {what's missing}. *Can't test: {impact}.*

#### Limited — Happy path only

- **{Title}** — {what's ambiguous or incomplete}. *Missing: {what edge/error cases you can't cover}.*

#### Vague — Testable but you'll be guessing

- {description of what's imprecise}

---

## Questions to Ask

{Every gap that needs an answer from the PRD author, grouped by flow/feature area. Plain bullet questions — no severity tags, no jargon. This is what QA copies into a Slack message or meeting agenda.}

### {Flow / Feature Area Name}

- {plain question}
- {plain question}

### {Another Flow / Feature Area}

- {plain question}

### General

- {questions that span the whole feature}

---

## Reference

### Related Docs

{Only include docs that reveal test risks or affect what QA does. Not background reading.}

- [{title}]({url}) — {why it matters for testing}

### Document Completeness

| Section | Status |
|---------|--------|
| Overview / Objective | {Present / Partial / **Missing**} |
| Functional Requirements | {status} |
| Non-Functional Requirements | {status} |
| User Flows | {status} |
| API / Technical Design | {status} |
| Error Handling | {status} |
| Dependencies | {status} |
| Open Questions | {status} |
| Assumptions | {status} |

### Gap Summary

| Category | Blocked | Limited | Vague |
|----------|---------|---------|-------|
| Requirements Completeness | {n} | {n} | {n} |
| Error & Failure Handling | {n} | {n} | {n} |
| User Flow Coverage | {n} | {n} | {n} |
| Permissions & Access | {n} | {n} | {n} |
| Performance & Reliability | {n} | {n} | {n} |
| Dependencies | {n} | {n} | {n} |
| Post-Launch Visibility | {n} | {n} | {n} |
```

### Formatting Rules

- **Bullet lists for long text** — any content where a table cell would exceed ~10 words. Use `- **Bold title** — description` format.
- **Tables only for short structured data** — where every cell is brief (5-10 words max).
- **Why**: Pandoc gives equal column widths to pipe tables. Long text in narrow columns renders as tall, unreadable strips in docx.

### Report Principles

- **Plain language only**. No jargon without explanation. Write for QA, not developers.
- **Everything framed as test impact**. Every gap, every flow issue, every finding ends with what QA can or can't do because of it.
- **Self-contained**. A QA engineer should understand the feature and all the gaps without reading the PRD.
- **No overlap**. Each finding appears once — either in a flow section or in cross-cutting gaps, never both.
- **Only include what changes what QA does**. If a finding doesn't affect testing, cut it.

### What NOT to Flag

- **Missing error handling** if Figma designs exist for error states — the PRD delegates UI error handling to design files
- **Missing rollback plans** if separate launch plans exist — rollback is handled outside the PRD
- **Stakeholder names/roles** — not relevant to QA
- **Release timelines/milestones** — not relevant to QA
- **Rollout & migration plans** — unless they create testability gaps

### After Writing

Convert to docx using pandoc:

```bash
pandoc ./prd-analysis-{feature-name-slug}.md -o ./prd-analysis-{feature-name-slug}.docx
```

Tell the user:
> Report written to `./prd-analysis-{name}.md` and `./prd-analysis-{name}.docx`.

---

## Important Notes

- **Never use WebFetch for Google Doc URLs** — it will fail. Always use Glean `read_document`.
- **Glean subtask pattern**: Always use the Agent tool for Glean calls to keep large responses out of the main context.
- **PHI awareness**: If the PRD contains anything that looks like patient data, stop and ask the user to review before proceeding.
- **No jargon**: If you catch yourself writing "API contract", "idempotency", "circuit breaker", "NFR", or similar — rewrite in plain language.
- **Be specific about gaps**: Don't say "error handling is missing." Say which step in which flow has no defined failure behavior and what QA can't test because of it.
- **Cross-reference with context**: Use Glean findings and GitHub code to validate or challenge the PRD. If the PRD proposes something that contradicts existing code or past incidents, flag it as a test risk.
- **Merge, don't duplicate**: Flow issues and gap findings often overlap. Put each finding in one place — the flow section if it's flow-specific, cross-cutting gaps if it's broader.
- **Questions to Ask is derived, not invented**: Every question in that section must trace back to a specific gap. Don't add generic questions — only questions that resolve a flagged gap.
- **Report is the sole deliverable**: Generate the full report automatically. No interactive menu.
