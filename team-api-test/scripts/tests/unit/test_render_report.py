from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import render_report as rr  # noqa: E402

PATH = "/provider-grouping/v2/organization/{organizationId}/business-verification-status/history"


def _evidence(seq: int, **kw: Any) -> dict[str, Any]:
    base = {"outcome": "sent", "seq": seq, "method": "GET", "url": f"https://api.zocdoc.com/x/{seq}",
            "auth": "proxy", "http_status": 200, "elapsed_ms": 12, "reason": "", "body_preview": "{}",
            "body_truncated": False, "ids": []}
    base.update(kw)
    return base


def _write_run(tmp_path: Path, tests: list[dict[str, Any]], evidence: list[dict[str, Any]],
               host: str = "api.zocdoc.com") -> Path:
    (tmp_path / "responses").mkdir()
    for ev in evidence:
        (tmp_path / "responses" / f"{ev['seq']:03d}.json").write_text(json.dumps(ev), encoding="utf-8")
    plan = {"endpoint": {"method": "GET", "path": PATH, "host": host, "service": "provider-grouping"},
            "related_tickets": ["PE-5998", "not a ticket"], "gaps": [{"id": "G1", "title": "No format check"}],
            "tests": tests}
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return tmp_path


def _test(tid: str, seq: int | None, expected: list[int], **kw: Any) -> dict[str, Any]:
    return {"id": tid, "name": f"case {tid}", "expected_status": expected, "request_seq": seq, **kw}


@pytest.mark.parametrize("evidence,expected,status,actual", [
    (_evidence(1, http_status=404), [404], rr.Status.PASS, "404"),
    (_evidence(1, http_status=500), [400], rr.Status.FAIL, "500"),
    (_evidence(1, outcome="blocked", http_status=None), [200], rr.Status.BLOCKED, "Not sent"),
    (_evidence(1, outcome="error", http_status=None), [200], rr.Status.ERROR, "No response"),
])
def test_load_report_status_comes_from_evidence(tmp_path: Path, evidence: dict[str, Any], expected: list[int],
                                                status: rr.Status, actual: str) -> None:
    report = rr.load_report(_write_run(tmp_path, [_test("T1", 1, expected)], [evidence]))
    assert report.tests[0].status is status and report.tests[0].actual == actual


def test_load_report_missing_evidence_is_not_run(tmp_path: Path) -> None:
    report = rr.load_report(_write_run(tmp_path, [_test("T1", 9, [200])], []))
    assert report.tests[0].status is rr.Status.NOT_RUN


def test_load_report_drops_malformed_ticket_keys(tmp_path: Path) -> None:
    report = rr.load_report(_write_run(tmp_path, [], []))
    assert report.related_tickets == ["PE-5998"]


def test_load_report_missing_plan_raises_plan_error(tmp_path: Path) -> None:
    with pytest.raises(rr.PlanError):
        rr.load_report(tmp_path)


def test_bug_description_has_no_response_body(tmp_path: Path) -> None:
    secret_body = '{"detail": "SECRET-BODY-TEXT"}'
    run = _write_run(tmp_path, [_test("T1", 1, [400], contract="400 for invalid ID", code="OrgController.cs:42",
                                      steps="call with org_abc")],
                     [_evidence(1, http_status=500, body_preview=secret_body)])
    row = rr.load_report(run).tests[0]
    assert row.bug_text.startswith(f"/log-bug GET {PATH} returns 500 instead of 400")
    assert "Production (api.zocdoc.com)" in row.bug_text and "OrgController.cs:42" in row.bug_text
    assert "PE-5998" in row.bug_text
    assert "SECRET-BODY-TEXT" not in row.bug_text


def test_internal_host_is_labelled_in_bug_text_and_report(tmp_path: Path) -> None:
    host = "provider-grouping-v1.east.zocdoccloud.com"
    run = _write_run(tmp_path, [_test("T1", 1, [404])], [_evidence(1, http_status=500)], host=host)
    report = rr.load_report(run)
    assert f"Production ({host}, internal host called directly" in report.tests[0].bug_text
    assert "<b>Internal host</b>" in rr.render(report)


def test_bug_description_only_for_failed_tests(tmp_path: Path) -> None:
    run = _write_run(tmp_path, [_test("T1", 1, [200]), _test("T2", 2, [400])],
                     [_evidence(1), _evidence(2, http_status=500)])
    rows = rr.load_report(run).tests
    assert rows[0].bug_text == "" and rows[1].bug_text


def test_render_escapes_html_from_names_and_bodies(tmp_path: Path) -> None:
    run = _write_run(tmp_path, [_test("T1", 1, [200], name="<script>alert(1)</script>")],
                     [_evidence(1, body_preview="<img src=x onerror=alert(1)>")])
    page = rr.render(rr.load_report(run))
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_render_counts_tiles(tmp_path: Path) -> None:
    run = _write_run(tmp_path, [_test("T1", 1, [200]), _test("T2", 2, [400]), _test("T3", 3, [200])],
                     [_evidence(1), _evidence(2, http_status=500), _evidence(3, outcome="blocked", http_status=None)])
    page = rr.render(rr.load_report(run))
    for label in ('<b>1</b>Passed', '<b>1</b>Failed', '<b>1</b>Blocked', '<b>1</b>Gaps found'):
        assert label in page


def test_main_writes_report_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _write_run(tmp_path, [_test("T1", 1, [200])], [_evidence(1)])
    assert rr.main(["--run-dir", str(run)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["report"]).exists() and out["counts"]["PASS"] == 1


def test_findings_render_escaped_and_feed_bug_text(tmp_path: Path) -> None:
    tests = [_test("T1", 1, [201], findings=["<b>sent once</b>", "", "no tin in body"]),
             _test("T2", None, [409], findings=["not sent"])]
    report = rr.load_report(_write_run(tmp_path, tests, [_evidence(1, http_status=500)]))
    t1, t2 = report.tests
    assert t1.findings == ("<b>sent once</b>", "no tin in body")
    assert "Notes: <b>sent once</b> no tin in body" in t1.bug_text
    assert t2.findings == ("not sent",) and t2.bug_text == ""
    html_out = rr.render(report)
    assert "&lt;b&gt;sent once&lt;/b&gt;" in html_out and "<b>sent once</b>" not in html_out
    assert html_out.count('class="findings"') == 2
