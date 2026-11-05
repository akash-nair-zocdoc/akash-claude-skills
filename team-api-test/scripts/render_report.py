#!/usr/bin/env python3
"""Render a team-api-test run into one standalone HTML report (layout A: test results first).

Input is ``<run-dir>/plan.json`` (written by the tester) plus the evidence that safe_request.py saved
under ``<run-dir>/responses/``. The report decides PASS/FAIL/BLOCKED from that evidence, not from what
an agent claims, and builds each bug description from fixed fields so no response body can leak in.

    render_report.py --run-dir DIR [--out FILE]

plan.json::

    {
      "endpoint": {"method": "GET", "path": "/provider-grouping/v2/...", "host": "api.zocdoc.com",
                   "service": "provider-grouping", "contract": "api-specs/provider-grouping/http/service.yaml"},
      "run_by": "QA engineer",
      "related_tickets": ["PE-5998"],
      "gaps": [{"id": "G1", "title": "...", "contract": "...", "code": "Foo.cs:42"}],
      "tests": [{"id": "T1", "name": "...", "expected_status": [400], "request_seq": 3,
                 "gap": "G1", "contract": "...", "code": "...", "steps": "...", "expected": "...",
                 "findings": ["optional tester notes; never response bodies"]}]
    }
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("team-api-test.render_report")

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
MAX_FIELD_CHARS = 400


class Status(str, Enum):  # StrEnum needs 3.11; this machine has 3.10
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    NOT_RUN = "NOT RUN"


BADGE_CLASS = {Status.PASS: "b-pass", Status.FAIL: "b-fail", Status.BLOCKED: "b-block",
               Status.ERROR: "b-error", Status.NOT_RUN: "b-error"}


class PlanError(Exception):
    """plan.json is missing or malformed."""


@dataclass(frozen=True)
class Evidence:
    outcome: str
    method: str
    url: str
    auth: str
    http_status: int | None
    elapsed_ms: int | None
    reason: str
    body_preview: str
    body_truncated: bool
    ids: tuple[dict[str, str], ...]

    @classmethod
    def load(cls, path: Path) -> Evidence:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(outcome=raw.get("outcome", ""), method=raw.get("method", ""), url=raw.get("url", ""),
                   auth=raw.get("auth", ""), http_status=raw.get("http_status"), elapsed_ms=raw.get("elapsed_ms"),
                   reason=raw.get("reason", ""), body_preview=raw.get("body_preview", ""),
                   body_truncated=bool(raw.get("body_truncated")), ids=tuple(raw.get("ids", [])))


@dataclass(frozen=True)
class TestRow:
    id: str
    name: str
    expected_status: tuple[int, ...]
    status: Status
    actual: str
    gap: str
    contract: str
    code: str
    steps: str
    expected: str
    evidence: Evidence | None
    findings: tuple[str, ...] = ()
    bug_text: str = ""


@dataclass
class Report:
    method: str
    path: str
    host: str
    service: str
    contract: str
    run_by: str
    generated_at: str
    related_tickets: list[str]
    gaps: list[dict[str, str]]
    tests: list[TestRow] = field(default_factory=list)


def _clip(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 1] + "…"


def _status_for(expected: tuple[int, ...], ev: Evidence | None) -> tuple[Status, str]:
    if ev is None:
        return Status.NOT_RUN, "Not run"
    if ev.outcome == "blocked":
        return Status.BLOCKED, "Not sent"
    if ev.outcome != "sent" or ev.http_status is None:
        return Status.ERROR, "No response"
    actual = str(ev.http_status)
    return (Status.PASS if ev.http_status in expected else Status.FAIL), actual


def is_internal(host: str) -> bool:
    return host.lower().endswith(".zocdoccloud.com")


def bug_description(report: Report, row: TestRow) -> str:
    """One paragraph for /log-bug built only from the endpoint, IDs in the URL, codes and file refs."""
    expected = " or ".join(str(c) for c in row.expected_status)
    parts = [
        f"/log-bug {report.method} {report.path} returns {row.actual} instead of {expected} "
        f"for this case: {row.name}.",
        f"Environment: Production ({report.host}"
        f"{', internal host called directly, not through the public gateway' if is_internal(report.host) else ''}).",
    ]
    if row.contract or row.code:
        parts.append(" ".join(p for p in (
            f"The API contract says: {row.contract}." if row.contract else "",
            f"In the code: {row.code}." if row.code else "") if p))
    if row.steps:
        parts.append(f"Steps: {row.steps}.")
    if row.findings:
        parts.append(f"Notes: {' '.join(row.findings)}")
    parts.append(f"Expected {expected}{f' ({row.expected})' if row.expected else ''}; actual {row.actual}.")
    if report.related_tickets:
        parts.append(f"Related: {', '.join(report.related_tickets)}.")
    parts.append(f"Service: {report.service}. Found by the team-api-test workflow.")
    return re.sub(r"\.\.+", ".", " ".join(parts))


def load_report(run_dir: Path) -> Report:
    plan_path = run_dir / "plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"{plan_path} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"{plan_path} is not valid JSON: {exc.msg} at line {exc.lineno}") from exc
    try:
        ep = plan["endpoint"]
        report = Report(
            method=_clip(ep["method"]).upper(), path=_clip(ep["path"]), host=_clip(ep.get("host", "api.zocdoc.com")),
            service=_clip(ep.get("service", "")), contract=_clip(ep.get("contract", "")),
            run_by=_clip(plan.get("run_by", "QA engineer")),
            generated_at=datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M"),
            related_tickets=[t for t in plan.get("related_tickets", []) if TICKET_RE.match(str(t))],
            gaps=[{k: _clip(g.get(k, "")) for k in ("id", "title", "contract", "code")} for g in plan.get("gaps", [])],
        )
        for t in plan["tests"]:
            expected = tuple(int(c) for c in t["expected_status"])
            seq = t.get("request_seq")
            ev_path = run_dir / "responses" / f"{int(seq):03d}.json" if seq is not None else None
            ev = Evidence.load(ev_path) if ev_path and ev_path.exists() else None
            status, actual = _status_for(expected, ev)
            row = TestRow(id=_clip(t["id"]), name=_clip(t["name"]), expected_status=expected, status=status,
                          actual=actual, gap=_clip(t.get("gap", "")), contract=_clip(t.get("contract", "")),
                          code=_clip(t.get("code", "")), steps=_clip(t.get("steps", "")),
                          expected=_clip(t.get("expected", "")), evidence=ev,
                          findings=tuple(_clip(f) for f in t.get("findings", []) if str(f or "").strip()))
            if status is Status.FAIL:
                row = TestRow(**{**row.__dict__, "bug_text": bug_description(report, row)})
            report.tests.append(row)
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"{plan_path} is missing or has a bad field: {exc}") from exc
    return report


# --------------------------------------------------------------------------------------------
# HTML

CSS = """
:root { color-scheme: light; }
body { margin: 0; background: #f4f5f7; color: #1a1a1a; font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.page { max-width: 1100px; margin: 0 auto; padding: 32px 24px 64px; }
.card { background: #fff; border: 1px solid #d0d4da; border-radius: 10px; overflow: hidden; }
.head { background: #1f2a44; color: #fff; padding: 14px 20px; }
.head h1 { margin: 0; font-size: 20px; }
.body { padding: 20px; }
.endpoint { font-family: Menlo, monospace; font-size: 14px; background: #eef1f5; padding: 10px 12px; border-radius: 6px; word-break: break-all; }
.meta { color: #444; font-size: 14px; margin: 8px 0 20px; }
.tiles { display: flex; gap: 12px; margin-bottom: 20px; }
.tile { flex: 1; border-radius: 8px; padding: 14px; text-align: center; border: 1px solid; }
.tile b { display: block; font-size: 30px; line-height: 1.1; }
.pass { background: #e6f4ea; border-color: #34a853; color: #0b5d1e; }
.fail { background: #fce8e6; border-color: #d93025; color: #8c1d18; }
.block { background: #fef3d6; border-color: #e3a008; color: #6b4400; }
.gap { background: #e8eefc; border-color: #2563eb; color: #173a8a; }
table { width: 100%; border-collapse: collapse; font-size: 15px; }
th { text-align: left; background: #eef1f5; padding: 10px; border-bottom: 2px solid #c5cad3; }
td { padding: 10px; border-bottom: 1px solid #e1e4e8; vertical-align: top; }
tr.row { cursor: pointer; }
tr.row:hover td { background: #f7f9fc; }
tr.detail { display: none; }
tr.detail.open { display: table-row; }
.badge { display: inline-block; font-size: 13px; font-weight: 600; padding: 2px 10px; border-radius: 12px; }
.b-pass { background: #34a853; color: #fff; }
.b-fail { background: #d93025; color: #fff; }
.b-block { background: #e3a008; color: #1a1a1a; }
.b-error { background: #6b7280; color: #fff; }
code { font-family: Menlo, monospace; font-size: 13px; background: #eef1f5; padding: 1px 5px; border-radius: 4px; word-break: break-all; }
.expand { background: #fafbfc; border: 1px solid #e1e4e8; border-radius: 6px; padding: 12px; font-size: 14px; }
.expand pre { margin: 6px 0 10px; background: #1f2a44; color: #f1f3f6; padding: 10px; border-radius: 6px; font-size: 13px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; max-height: 420px; }
.kv { margin: 4px 0; }
.hint, .note { color: #555; font-size: 14px; }
.hasbug { font-size: 13px; color: #8c1d18; margin-left: 6px; }
.bugbox { margin-top: 14px; border: 1px solid #f1b5b0; border-left: 6px solid #d93025; border-radius: 8px; background: #fff; padding: 12px 14px; }
.bugbox-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.bugbox-head b { color: #8c1d18; font-size: 16px; }
.bugbox-head button { font-size: 14px; padding: 6px 14px; border-radius: 6px; border: 1px solid #1f2a44; background: #1f2a44; color: #fff; cursor: pointer; }
.bugtext { font-size: 15px; line-height: 1.55; background: #fafbfc; border: 1px solid #e1e4e8; border-radius: 6px; padding: 10px 12px; white-space: pre-wrap; }
h2 { font-size: 18px; margin: 28px 0 10px; }
.gaplist li { margin-bottom: 6px; }
.findings { margin-top: 12px; border-left: 4px solid #2563eb; background: #f3f6fd; padding: 8px 12px; border-radius: 6px; }
.findings ul { margin: 6px 0 0; padding-left: 20px; }
.hasnote { font-size: 13px; color: #173a8a; margin-left: 6px; }
"""

JS = """
document.querySelectorAll('tr.row').forEach(function (r) {
  r.addEventListener('click', function () { document.getElementById(r.dataset.target).classList.toggle('open'); });
});
function copyBug(btn) {
  var t = btn.closest('.bugbox').querySelector('.bugtext').innerText;
  navigator.clipboard.writeText(t).then(function () {
    btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
  });
}
"""


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _detail_html(report: Report, row: TestRow) -> str:
    ev = row.evidence
    out = ['<div class="expand">']
    if ev is None:
        out.append("<p>This test was planned but no request was recorded for it.</p>")
    else:
        auth = "auth proxy (bearer token added by the proxy)" if ev.auth == "proxy" else "no credentials"
        out.append(f'<div class="kv"><b>Request:</b> <code>{e(ev.method)} {e(ev.url)}</code> · {e(auth)}</div>')
        if ev.outcome == "blocked":
            out.append(f'<div class="kv"><b>Blocked by the safety check:</b> {e(ev.reason)}</div>')
        elif ev.outcome != "sent":
            out.append(f'<div class="kv"><b>Error:</b> {e(ev.reason)}</div>')
        else:
            out.append(f'<div class="kv"><b>Response:</b> {e(ev.http_status)} in {e(ev.elapsed_ms)} ms</div>')
            if ev.body_preview:
                trunc = " (truncated)" if ev.body_truncated else ""
                out.append(f'<div class="kv"><b>Body, redacted{e(trunc)}:</b></div><pre>{e(ev.body_preview)}</pre>')
        if ev.ids:
            ids = ", ".join(f"{i.get('id')} ({i.get('verdict')})" for i in ev.ids)
            out.append(f'<div class="kv"><b>IDs checked:</b> {e(ids)}</div>')
    if row.contract:
        out.append(f'<div class="kv"><b>Contract:</b> {e(row.contract)}</div>')
    if row.code:
        out.append(f'<div class="kv"><b>Code:</b> {e(row.code)}</div>')
    if row.findings:
        items = "".join(f"<li>{e(f)}</li>" for f in row.findings)
        out.append(f'<div class="findings"><b>Findings</b><ul>{items}</ul></div>')
    if row.bug_text:
        out.append(
            '<div class="bugbox"><div class="bugbox-head"><b>Bug description for Claude</b>'
            '<button onclick="event.stopPropagation(); copyBug(this)">Copy</button></div>'
            f'<div class="bugtext">{e(row.bug_text)}</div>'
            '<p class="note">Contains only the endpoint, the fake or test IDs used, status codes and file '
            "references. No response bodies.</p></div>")
    out.append("</div>")
    return "".join(out)


def render(report: Report) -> str:
    counts = {s: sum(1 for t in report.tests if t.status is s) for s in Status}
    rows = []
    for n, t in enumerate(report.tests, 1):
        badge = f'<span class="badge {BADGE_CLASS[t.status]}">{e(t.status.value)}</span>'
        if t.bug_text:
            badge += ' <span class="hasbug">bug description ▸</span>'
        if t.findings:
            badge += ' <span class="hasnote">findings ▸</span>'
        expected = " / ".join(str(c) for c in t.expected_status)
        rows.append(f'<tr class="row" data-target="d{n}"><td>{n}</td><td>{e(t.name)}</td><td>{e(expected)}</td>'
                    f"<td>{e(t.actual)}</td><td>{badge}</td></tr>")
        rows.append(f'<tr class="detail" id="d{n}"><td colspan="5">{_detail_html(report, t)}</td></tr>')
    gaps = "".join(
        f"<li><b>{e(g['id'])}: {e(g['title'])}</b>"
        + (f" · Contract: {e(g['contract'])}" if g["contract"] else "")
        + (f" · Code: <code>{e(g['code'])}</code>" if g["code"] else "") + "</li>"
        for g in report.gaps)
    errors = counts[Status.ERROR] + counts[Status.NOT_RUN]
    error_note = f'<p class="note">{errors} test(s) did not get a response; open them for details.</p>' if errors else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>API test report · {e(report.method)} {e(report.path)}</title>
<style>{CSS}</style></head>
<body><div class="page"><div class="card">
<div class="head"><h1>API test report · {e(report.service or 'unknown service')}</h1></div>
<div class="body">
<div class="endpoint">{e(report.method)} https://{e(report.host)}{e(report.path)}</div>
<div class="meta">{"<b>Internal host</b> (prod, bypasses the public gateway)" if is_internal(report.host) else "Prod <code>.com</code>"} via auth proxy localhost:3000 · Run {e(report.generated_at)} · Run by: {e(report.run_by)}
{f" · Contract: <code>{e(report.contract)}</code>" if report.contract else ""}</div>
<div class="tiles">
<div class="tile pass"><b>{counts[Status.PASS]}</b>Passed</div>
<div class="tile fail"><b>{counts[Status.FAIL]}</b>Failed</div>
<div class="tile block"><b>{counts[Status.BLOCKED]}</b>Blocked</div>
<div class="tile gap"><b>{len(report.gaps)}</b>Gaps found</div>
</div>
{error_note}
<table><tr><th>#</th><th>Test</th><th>Expected</th><th>Actual</th><th>Result</th></tr>
{''.join(rows)}
</table>
<p class="hint">Click any row to open its request, response, and the matching contract and code lines. Failed rows also have a bug description.</p>
{f'<h2>Gaps between the contract and the code</h2><ul class="gaplist">{gaps}</ul>' if gaps else ''}
</div></div></div>
<script>{JS}</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="default: <run-dir>/report.html")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    run_dir = args.run_dir.expanduser()
    try:
        report = load_report(run_dir)
    except PlanError as exc:
        log.error("%s", exc)
        return 2
    out = (args.out or run_dir / "report.html").expanduser()
    out.write_text(render(report), encoding="utf-8")
    summary = {s.value: sum(1 for t in report.tests if t.status is s) for s in Status}
    print(json.dumps({"report": str(out), "counts": summary, "gaps": len(report.gaps)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
