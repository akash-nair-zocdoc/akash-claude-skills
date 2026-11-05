---
name: async-standup
description: Use when user asks to generate their daily async standup update, fill in standup template, or says "standup", "daily update", "what did I do yesterday"
---

# Async Standup Generator

Generates the user's daily async standup update by pulling data from Glean (meetings, emails, Jira).

## Template

```
1. What did you accomplish yesterday?
(Keep it short and focused)

2. What will you be working on today?
(Please include links to your tickets)

3. Any blockers or issues?
(Call them out early so we can help)

4. Other updates (if any):
(Keep it super crisp)
```

## How to Generate

### Step 1: Gather Data (parallel subagents)

Launch 5 subagents in parallel using the Agent tool with Glean:

1. **Yesterday's Meetings** — Use `meeting_lookup` for yesterday's date. Return title, time, duration, and attendee response status for each meeting. Only include meetings scheduled between 11:00 AM – 9:00 PM IST (05:30 – 15:30 UTC). Exclude "Out of Office" blocks, optional/social meetings (e.g., "Chai Shai", town halls marked optional), meetings with "(HOLD)" in the name, and meetings the user declined.

2. **Emails** — Use `gmail_search` for yesterday's date. Return subject, sender, and brief description. Focus on actionable items (Jira assignments, mentions, important threads). Skip calendar invites and automated digests.

3. **Jira** — Use `search` for Jira issues updated yesterday involving the user. Return ticket key, title, status, and what action the user took (created, closed, commented, etc.).

4. **Today's Meetings** — Use `meeting_lookup` for today's date. Return title, time, and attendee response status for each meeting. Only include meetings scheduled between 11:00 AM – 9:00 PM IST (05:30 – 15:30 UTC). Exclude "Out of Office" blocks, optional/social meetings, meetings with "(HOLD)" in the name, and meetings the user declined.

5. **Upcoming OOO** — Use `meeting_lookup` to check for "Out of Office" blocks starting from the next business day. If an OOO block is found, check the following business day too, and keep checking consecutive business days until no OOO block is found. If the first business day has no OOO, stop checking.

### Step 2: Build Section 1 (Accomplished)

Group and consolidate:
- **Daily regressions** — One bullet only: "Completed daily regressions". No test names, no ticket IDs.
- **Bug triage** — One bullet per bug with action taken (escalated, closed, etc.)
- **Meetings** — One bullet: "Spent ~Xh Ymin in meetings". Calculate total duration from scheduled times plus a 30-minute buffer. Do not list individual meeting names. Exclude OOO blocks, optional/social meetings, meetings with "(HOLD)" in the name, and declined meetings.
- **Other work** — Any remaining items get their own bullets

### Step 3: Draft Sections 2-4

- **Section 2** — One bullet only: "~Xh Ymin scheduled in meetings". Calculate total duration from today's accepted meetings plus a 30-minute buffer. Do not list meeting names or Jira assignments.
- **Section 3** — Leave blank (user will fill in manually).
- **Section 4** — If upcoming OOO days found (from subagent 5), add "OOO from [start date] to [end date]". Otherwise leave blank.

### Step 4: Present and Iterate

Present the filled template and ask user if they want adjustments (reword, combine, remove items).
