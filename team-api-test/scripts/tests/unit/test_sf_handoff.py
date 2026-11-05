"""The Snowflake hand-off: sf-query writes nonce-tagged SQL, sf-ingest accepts only a matching result."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import safe_request as sr  # noqa: E402

TEST_ORG = "org_aaaaaaaaaaaaaaaaaaaaaa"
REAL_ORG = "org_bbbbbbbbbbbbbbbbbbbbbb"
TEST_PT = "pt_cccccccccccccccccccccc"
FAKE_ORG = "org_0000000000000000000000"
NOW = 1_800_000_000.0


@pytest.fixture()
def cfg() -> sr.Config:
    return sr.Config.load()


@pytest.fixture()
def cache(tmp_path: Path) -> sr.IdCache:
    return sr.IdCache(tmp_path / "id_checks.json", 24)


@pytest.fixture()
def store(tmp_path: Path) -> sr.PendingStore:
    return sr.PendingStore(tmp_path / "pending")


def tool_output(rows: list[list[Any]], num_rows: int | None = None) -> str:
    """The snowflake_pr001 tool's JSON, shaped like a real reply (booleans come back as strings)."""
    return json.dumps({
        "query_id": "01c8242e-0824-cab0-0021-8e03a78328be",
        "result_set": {
            "data": rows,
            "resultSetMetaData": {"numRows": len(rows) if num_rows is None else num_rows,
                                  "rowType": [{"name": n} for n in ("ID", "IS_TEST_SAFE", "NONCE", "ACCOUNT")]},
        },
    })


def marker(nonce: str, account: str = "ZOCDOC_001") -> list[Any]:
    return [sr.SF_MARKER_ID, "", nonce, account]


def query(cfg: sr.Config, cache: sr.IdCache, store: sr.PendingStore, *ids: str) -> str:
    out = sr.sf_query(cfg, list(ids), cache, store, now=NOW)
    return out["nonce"]


# --- sf-query ---------------------------------------------------------------------------------


def test_sf_query_sql_stamps_nonce_and_account_and_covers_each_kind(cfg, cache, store) -> None:
    out = sr.sf_query(cfg, [TEST_ORG, TEST_PT], cache, store, now=NOW)
    sql = out["sql"]
    assert sr.NONCE_RE.fullmatch(out["nonce"])
    assert f"'{out['nonce']}' as nonce" in sql and "current_account() as account" in sql
    assert sr.SF_MARKER_ID in sql and "practice_organization_latest" in sql and f"'{TEST_PT}'" in sql
    assert out["ids"] == [TEST_ORG, TEST_PT]


def test_sf_query_skips_fake_and_freshly_cached_ids(cfg, cache, store) -> None:
    cache.put(TEST_ORG, sr.Verdict.TEST)
    out = sr.sf_query(cfg, [FAKE_ORG, TEST_ORG], cache, store, now=NOW)
    assert out["nonce"] is None and out["sql"] is None
    assert {k["id"] for k in out["already_known"]} == {FAKE_ORG, TEST_ORG}


def test_sf_query_refresh_requeries_cached_ids(cfg, cache, store) -> None:
    cache.put(TEST_ORG, sr.Verdict.TEST)
    out = sr.sf_query(cfg, [TEST_ORG], cache, store, refresh=True, now=NOW)
    assert out["ids"] == [TEST_ORG]


def test_sf_query_reports_unrecognised_input(cfg, cache, store) -> None:
    out = sr.sf_query(cfg, ["org_bad", TEST_ORG], cache, store, now=NOW)
    assert out["unrecognised"] == ["org_bad"]


# --- sf-ingest: accepted results ----------------------------------------------------------------


def test_sf_ingest_caches_test_and_real_verdicts(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG, REAL_ORG)
    text = tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"],
                        [REAL_ORG, "false", nonce, "ZOCDOC_001"]])
    checks = sr.sf_ingest(cfg, nonce, text, cache, store, now=NOW + 60)
    assert {c.id: c.verdict for c in checks} == {TEST_ORG: sr.Verdict.TEST, REAL_ORG: sr.Verdict.REAL}
    assert "query 01c8242e" in checks[0].detail
    [check] = sr.check_ids(cfg, [("org", TEST_ORG)], cache)
    assert check.verdict is sr.Verdict.TEST


@pytest.mark.parametrize("flag", ["", None])
def test_sf_ingest_null_flag_is_real_not_test(cfg, cache, store, flag: Any) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    [check] = sr.sf_ingest(cfg, nonce, tool_output([marker(nonce), [TEST_ORG, flag, nonce, "ZOCDOC_001"]]),
                           cache, store, now=NOW)
    assert check.verdict is sr.Verdict.REAL


def test_sf_ingest_missing_id_is_unknown_and_not_cached(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    [check] = sr.sf_ingest(cfg, nonce, tool_output([marker(nonce)]), cache, store, now=NOW)
    assert check.verdict is sr.Verdict.UNKNOWN
    assert cache.get(TEST_ORG) is None


def test_sf_ingest_then_send_lets_a_test_org_write_through(cfg, cache, store, tmp_path: Path) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    sr.sf_ingest(cfg, nonce, tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"]]),
                 cache, store, now=NOW)
    checks = sr.check_ids(cfg, [("org", TEST_ORG)], cache)
    sr.write_guard(cfg, "POST", [("org", TEST_ORG)], [], checks)  # does not raise


# --- sf-ingest: rejected results (nothing cached) ----------------------------------------------


def _assert_rejected(cfg, cache, store, nonce: str, text: str, match: str, now: float = NOW) -> None:
    with pytest.raises(sr.IngestError, match=match):
        sr.sf_ingest(cfg, nonce, text, cache, store, now=now)
    assert cache.get(TEST_ORG) is None


def test_sf_ingest_rejects_wrong_nonce_in_rows(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    other = "0" * 16
    _assert_rejected(cfg, cache, store, nonce,
                     tool_output([marker(other), [TEST_ORG, "true", other, "ZOCDOC_001"]]), "different nonce")


def test_sf_ingest_rejects_deid_account(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce,
                     tool_output([marker(nonce, "ZOCDOC_201"), [TEST_ORG, "true", nonce, "ZOCDOC_201"]]),
                     "not the non-PHI")


def test_sf_ingest_rejects_missing_marker(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce, tool_output([[TEST_ORG, "true", nonce, "ZOCDOC_001"]]), "marker")


def test_sf_ingest_rejects_rows_for_ids_not_asked(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce,
                     tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"],
                                  [REAL_ORG, "true", nonce, "ZOCDOC_001"]]), "didn't ask")


def test_sf_ingest_rejects_duplicate_rows(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce,
                     tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"],
                                  [TEST_ORG, "false", nonce, "ZOCDOC_001"]]), "more than one row")


def test_sf_ingest_rejects_partial_result(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce,
                     tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"]], num_rows=5), "partial")


@pytest.mark.parametrize("text,match", [("not json", "not JSON"), ("{}", "no result_set"),
                                        (json.dumps({"result_set": {"data": []}}), "no columns")])
def test_sf_ingest_rejects_malformed_files(cfg, cache, store, text: str, match: str) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce, text, match)


@pytest.mark.parametrize("flag", [1, 0, "1", "yes", 1.0])
def test_sf_ingest_rejects_unexpected_flag_values(cfg, cache, store, flag: Any) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    _assert_rejected(cfg, cache, store, nonce,
                     tool_output([marker(nonce), [TEST_ORG, flag, nonce, "ZOCDOC_001"]]), "unexpected value")


def test_sf_ingest_rejects_expired_nonce(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    text = tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"]])
    _assert_rejected(cfg, cache, store, nonce, text, "older than",
                     now=NOW + cfg.sf_nonce_max_age_minutes * 60 + 1)


def test_sf_ingest_nonce_is_single_use(cfg, cache, store) -> None:
    nonce = query(cfg, cache, store, TEST_ORG)
    text = tool_output([marker(nonce), [TEST_ORG, "true", nonce, "ZOCDOC_001"]])
    sr.sf_ingest(cfg, nonce, text, cache, store, now=NOW)
    with pytest.raises(sr.IngestError, match="already ingested"):
        sr.sf_ingest(cfg, nonce, text, cache, store, now=NOW)


@pytest.mark.parametrize("nonce", ["", "../../etc/passwd", "ABCDEF0123456789", "a" * 15])
def test_sf_ingest_rejects_malformed_nonce(cfg, cache, store, nonce: str) -> None:
    with pytest.raises(sr.IngestError, match="16 hex"):
        sr.sf_ingest(cfg, nonce, "{}", cache, store, now=NOW)


def test_check_ids_unverified_id_points_at_the_handoff(cfg, cache) -> None:
    [check] = sr.check_ids(cfg, [("org", TEST_ORG)], cache)
    assert check.verdict is sr.Verdict.UNKNOWN and "sf-query" in check.detail
