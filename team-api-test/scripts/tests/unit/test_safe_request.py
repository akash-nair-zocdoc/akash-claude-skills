from __future__ import annotations

import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import safe_request as sr  # noqa: E402

TEST_PR = "pr_TestProviderAAAAAAAAAA"
REAL_PR = "pr_RealProviderBBBBBBBBBB"
TEST_PT = "pt_TestPracticeCCCCCCCCCC"
FAKE_ORG = "org_0000000000000000000000"
REAL_ORG = "org_RealOrgDDDDDDDDDDDDDDD"
TEST_LO = "lo_TestLocationEEEEEEEEEE"
BASE = "https://api.zocdoc.com/provider-grouping/v2"


@pytest.fixture()
def cfg() -> sr.Config:
    return sr.Config.load()


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.status = status
        msg = Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeOpener:
    """Stands in for urlopen; records every request it is given."""

    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, req: Any, timeout: int) -> FakeResponse:
        self.requests.append(req)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class StubSnowflake:
    """Verdicts already ingested from Snowflake: True -> test, False -> real, None -> never verified.

    ``fail=True`` stands for "Snowflake was never reached", so nothing is cached at all.
    """

    def __init__(self, flags: dict[str, bool | None], fail: bool = False) -> None:
        self.flags = flags
        self.fail = fail

    def cache(self, path: Path) -> sr.IdCache:
        cache = sr.IdCache(path / "sfcache" / "cache.json", 24)
        if not self.fail:
            for full, flag in self.flags.items():
                if flag is not None:
                    cache.put(full, sr.Verdict.TEST if flag else sr.Verdict.REAL)
        return cache


def _send(cfg: sr.Config, tmp_path: Path, opener: FakeOpener, sf: StubSnowflake | None = None,
          **kwargs: Any) -> tuple[sr.Result, int]:
    args: dict[str, Any] = {"method": "GET", "url": f"{BASE}/organization/{FAKE_ORG}/x", "body": None,
                            "headers": {}, "auth": sr.AuthMode.PROXY, "test_id": "T1"}
    args.update(kwargs)
    cache = sf.cache(tmp_path) if sf else None
    return sr.send(cfg, sr.RunDir(tmp_path), cache=cache, opener=opener, sleep=lambda _: None, **args)


# --- host rules ------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://provider-grouping-v1.east.zocdoccloud.net/x",
    "https://api.zocdoc.net/x",
    "http://api.zocdoc.com/x",
    "https://evil.com/x",
    "https://api.zocdoc.com.evil.com/x",
    "https://user@api.zocdoc.com/x",
    "https://api.zocdoc.com:8443/x",
    "https://api.zocdoc.com.net/x",
])
def test_validate_target_rejects_unsafe_hosts(cfg: sr.Config, url: str) -> None:
    with pytest.raises(sr.SafetyError):
        sr.validate_target(cfg, "GET", url)


@pytest.mark.parametrize("url", [f"{BASE}/x", "https://pulse.zocdoc.com/x?a=1", "https://API.ZocDoc.com/x"])
def test_validate_target_accepts_prod_com_hosts(cfg: sr.Config, url: str) -> None:
    target = sr.validate_target(cfg, "get", url)
    assert target.method == "GET"
    assert target.host in cfg.allowed_hosts


@pytest.mark.parametrize("url", [
    "https://provider-grouping-v1.east.zocdoccloud.com/x",
    "https://api-private.east.zocdoccloud.com/operations-provider-onboarding/v1/x",
    "https://Provider-Reference-V1.East.ZocdocCloud.com/x",
])
def test_validate_target_accepts_internal_com_hosts(cfg: sr.Config, url: str) -> None:
    target = sr.validate_target(cfg, "GET", url)
    assert target.internal
    assert target.host.endswith(".zocdoccloud.com")


@pytest.mark.parametrize("url", [
    "https://zocdoccloud.com/x",
    "https://evilzocdoccloud.com/x",
    "https://svc.zocdoccloud.com.evil.com/x",
    "https://svc.east.zocdoccloud.net/x",
    "https://-bad.zocdoccloud.com/x",
    "http://svc.east.zocdoccloud.com/x",
])
def test_validate_target_rejects_bad_internal_hosts(cfg: sr.Config, url: str) -> None:
    with pytest.raises(sr.SafetyError):
        sr.validate_target(cfg, "GET", url)


def test_public_hosts_are_not_internal(cfg: sr.Config) -> None:
    assert not sr.validate_target(cfg, "GET", f"{BASE}/x").internal


def test_internal_write_allows_all_zeros_ids(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(FakeResponse(404, b"{}"))
    result, code = _send(cfg, tmp_path, opener, method="POST",
                         url=f"https://provider-grouping-v1.east.zocdoccloud.com/v2/organization/{FAKE_ORG}/kyb/verify",
                         body="{}")
    assert code == sr.EXIT_OK
    assert opener.requests[0].headers["X-target-host"] == "https://provider-grouping-v1.east.zocdoccloud.com"


def test_internal_write_allows_verified_test_ids(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(FakeResponse(200, b"{}"))
    sf = StubSnowflake({TEST_PT: True})
    _, code = _send(cfg, tmp_path, opener, sf, method="PUT",
                    url=f"https://api-private.east.zocdoccloud.com/provider-grouping/v2/practice/{TEST_PT}/x",
                    body="{}")
    assert code == sr.EXIT_OK
    assert len(opener.requests) == 1


def test_internal_write_blocks_real_ids(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    sf = StubSnowflake({REAL_ORG: False})
    result, code = _send(cfg, tmp_path, opener, sf, method="PUT",
                         url=f"https://provider-grouping-v1.east.zocdoccloud.com/v2/organization/{REAL_ORG}/kyb/verify",
                         body="{}")
    assert code == sr.EXIT_BLOCKED
    assert REAL_ORG in result.reason
    assert opener.requests == []


def test_sandbox_567_is_a_network_error_not_an_api_response(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(FakeResponse(567, b"blocked by network policy"))
    result, code = _send(cfg, tmp_path, opener, auth=sr.AuthMode.NONE,
                         url="https://provider-grouping-v1.east.zocdoccloud.com/x")
    assert code == sr.EXIT_NETWORK
    assert result.outcome is sr.Outcome.ERROR and result.http_status is None
    assert "sandbox" in result.reason


def test_validate_target_rejects_whitespace_in_url(cfg: sr.Config) -> None:
    with pytest.raises(sr.InputError):
        sr.validate_target(cfg, "GET", f"{BASE}/x\r\nHost: evil")


# --- ID extraction ---------------------------------------------------------------------------


def test_extract_ids_finds_ids_in_url_and_nested_json_body(cfg: sr.Config) -> None:
    body = json.dumps({"items": [{"practiceId": TEST_PT}, {"loc": {"id": TEST_LO}}]})
    found, foreign = sr.extract_ids(cfg, sr.searchable_text(f"{BASE}/organization/{FAKE_ORG}/x", body))
    assert [f for _, f in found] == [FAKE_ORG, TEST_PT, TEST_LO]
    assert foreign == []


def test_extract_ids_decodes_percent_encoded_url(cfg: sr.Config) -> None:
    url = f"{BASE}/x?ids=" + REAL_PR.replace("_", "%5F")
    found, _ = sr.extract_ids(cfg, sr.searchable_text(url, None))
    assert [f for _, f in found] == [REAL_PR]


def test_extract_ids_flags_unknown_id_types(cfg: sr.Config) -> None:
    _, foreign = sr.extract_ids(cfg, ["pa_abcdefghijklmnopqrstuv"])
    assert foreign == ["pa_abcdefghijklmnopqrstuv"]


@pytest.mark.parametrize("full_id,expected", [(FAKE_ORG, True), ("pr_0000000000000000000001", False),
                                              (TEST_PR, False)])
def test_is_obviously_fake_only_all_zeros(full_id: str, expected: bool) -> None:
    assert sr.is_obviously_fake(full_id) is expected


# --- write guard ------------------------------------------------------------------------------


def test_send_get_with_real_id_is_sent_without_snowflake(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(FakeResponse(200, b'{"ok": true}'))
    result, code = _send(cfg, tmp_path, opener, StubSnowflake({}), url=f"{BASE}/organization/{REAL_ORG}/x")
    assert code == sr.EXIT_OK and result.http_status == 200
    assert result.ids == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_send_write_to_real_org_is_blocked_and_never_sent(cfg: sr.Config, tmp_path: Path, method: str) -> None:
    opener = FakeOpener()
    result, code = _send(cfg, tmp_path, opener, StubSnowflake({REAL_ORG: False}), method=method,
                         url=f"{BASE}/organization/{REAL_ORG}/kyb/verify")
    assert code == sr.EXIT_BLOCKED and result.outcome is sr.Outcome.BLOCKED
    assert opener.requests == []
    assert REAL_ORG in result.reason


@pytest.mark.parametrize("flags,body_ids", [
    ({TEST_PT: True}, [TEST_PT]),
    ({TEST_PT: True, TEST_LO: True}, [TEST_PT, TEST_LO, FAKE_ORG]),
    ({}, [FAKE_ORG]),
])
def test_send_write_with_only_fake_or_test_ids_is_sent(cfg: sr.Config, tmp_path: Path,
                                                        flags: dict[str, bool], body_ids: list[str]) -> None:
    opener = FakeOpener(FakeResponse(200, b"{}"))
    body = json.dumps({"updates": [{"id": i} for i in body_ids]})
    result, code = _send(cfg, tmp_path, opener, StubSnowflake(flags), method="POST",
                         url=f"{BASE}/business-verification-status~batchUpdate", body=body)
    assert code == sr.EXIT_OK, result.reason
    assert len(opener.requests) == 1


@pytest.mark.parametrize("flags,sf_fail,why", [
    ({TEST_PT: True, REAL_ORG: False}, False, "real ID mixed into a batch"),
    ({TEST_PT: None}, False, "NULL flag"),
    ({}, False, "not found in Snowflake"),
    ({TEST_PT: True}, True, "Snowflake unreachable"),
])
def test_send_write_blocked_when_any_id_not_proven_test(cfg: sr.Config, tmp_path: Path, flags: dict[str, Any],
                                                        sf_fail: bool, why: str) -> None:
    opener = FakeOpener()
    ids = list(flags) or [TEST_PT]
    body = json.dumps({"updates": [{"id": i} for i in ids]})
    _, code = _send(cfg, tmp_path, opener, StubSnowflake(flags, fail=sf_fail), method="POST",
                    url=f"{BASE}/business-verification-status~batchUpdate", body=body)
    assert code == sr.EXIT_BLOCKED, why
    assert opener.requests == []


def test_send_write_blocked_without_any_verdict_cache(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    _, code = _send(cfg, tmp_path, opener, None, method="POST", url=f"{BASE}/practice/{TEST_PT}/x")
    assert code == sr.EXIT_BLOCKED and opener.requests == []


def test_send_write_without_any_id_is_blocked(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    _, code = _send(cfg, tmp_path, opener, StubSnowflake({}), method="POST", url=f"{BASE}/things", body="{}")
    assert code == sr.EXIT_BLOCKED and opener.requests == []


@pytest.mark.parametrize("literal", ["org_bad", "pt_notanorgid"])
def test_send_write_without_any_id_is_sent_with_opt_in(cfg: sr.Config, tmp_path: Path, literal: str) -> None:
    opener = FakeOpener(FakeResponse(400, b"{}"))
    result, code = _send(cfg, tmp_path, opener, StubSnowflake({}), method="POST",
                         url=f"{BASE}/organization/{literal}/kyb/verify", body="{}", allow_no_id_write=True)
    assert code == sr.EXIT_OK and len(opener.requests) == 1
    assert result.override == "allow-no-id-write"


def test_opt_in_still_blocks_real_ids(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    _, code = _send(cfg, tmp_path, opener, StubSnowflake({REAL_ORG: False}), method="POST",
                    url=f"{BASE}/organization/{REAL_ORG}/kyb/verify", allow_no_id_write=True)
    assert code == sr.EXIT_BLOCKED and opener.requests == []


def test_opt_in_still_blocks_unknown_id_types(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    _, code = _send(cfg, tmp_path, opener, StubSnowflake({}), method="POST", url=f"{BASE}/things",
                    body=json.dumps({"other": "pa_abcdefghijklmnopqrstuv"}), allow_no_id_write=True)
    assert code == sr.EXIT_BLOCKED and opener.requests == []


def test_opt_in_is_not_recorded_when_ids_are_present(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(FakeResponse(404, b"{}"))
    result, code = _send(cfg, tmp_path, opener, StubSnowflake({}), method="POST",
                         url=f"{BASE}/organization/{FAKE_ORG}/kyb/verify", allow_no_id_write=True)
    assert code == sr.EXIT_OK and result.override == ""


def test_send_write_with_unknown_id_type_is_blocked(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    _, code = _send(cfg, tmp_path, opener, StubSnowflake({}), method="POST", url=f"{BASE}/practice/{FAKE_ORG}/x",
                    body=json.dumps({"other": "pa_abcdefghijklmnopqrstuv"}))
    assert code == sr.EXIT_BLOCKED and opener.requests == []


def test_send_net_host_is_blocked_even_for_get(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener()
    _, code = _send(cfg, tmp_path, opener, url="https://provider-grouping-v1.east.zocdoccloud.net/x")
    assert code == sr.EXIT_BLOCKED and opener.requests == []


# --- routing, retries, budget -----------------------------------------------------------------


def test_send_proxy_mode_routes_through_localhost_with_target_header(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(FakeResponse(200, b"{}"))
    _send(cfg, tmp_path, opener, url=f"{BASE}/organization/{FAKE_ORG}/x?a=1")
    req = opener.requests[0]
    assert req.full_url == f"{cfg.proxy_url}/provider-grouping/v2/organization/{FAKE_ORG}/x?a=1"
    assert req.get_header("X-target-host") == "https://api.zocdoc.com"
    assert req.get_header("Authorization") is None


def test_send_no_auth_mode_goes_direct_without_credentials(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(urllib.error.HTTPError(f"{BASE}/x", 401, "Unauthorized", Message(), io.BytesIO(b"")))
    result, code = _send(cfg, tmp_path, opener, auth=sr.AuthMode.NONE)
    req = opener.requests[0]
    assert req.full_url.startswith("https://api.zocdoc.com/")
    assert req.get_header("X-target-host") is None and req.get_header("Authorization") is None
    assert code == sr.EXIT_OK and result.http_status == 401


def test_send_read_401_through_proxy_retries_once(cfg: sr.Config, tmp_path: Path) -> None:
    unauthorized = urllib.error.HTTPError(f"{BASE}/x", 401, "Unauthorized", Message(), io.BytesIO(b""))
    opener = FakeOpener(unauthorized, FakeResponse(200, b"{}"))
    result, _ = _send(cfg, tmp_path, opener)
    assert len(opener.requests) == 2 and result.http_status == 200


def test_send_write_401_is_never_retried(cfg: sr.Config, tmp_path: Path) -> None:
    unauthorized = urllib.error.HTTPError(f"{BASE}/x", 401, "Unauthorized", Message(), io.BytesIO(b""))
    opener = FakeOpener(unauthorized)
    result, _ = _send(cfg, tmp_path, opener, method="POST", url=f"{BASE}/organization/{FAKE_ORG}/kyb/verify")
    assert len(opener.requests) == 1 and result.http_status == 401


def test_send_proxy_down_reports_start_hint(cfg: sr.Config, tmp_path: Path) -> None:
    opener = FakeOpener(urllib.error.URLError(ConnectionRefusedError(61, "refused")))
    result, code = _send(cfg, tmp_path, opener)
    assert code == sr.EXIT_NETWORK and cfg.proxy_start_hint in result.reason


def test_send_proxy_502_is_a_network_error(cfg: sr.Config, tmp_path: Path) -> None:
    bad = urllib.error.HTTPError(f"{BASE}/x", 502, "Bad Gateway", Message(), io.BytesIO(b"Proxy error: ECONNRESET"))
    _, code = _send(cfg, tmp_path, FakeOpener(bad))
    assert code == sr.EXIT_NETWORK


def test_send_stops_when_budget_used_up(cfg: sr.Config, tmp_path: Path) -> None:
    small = sr.Config(**{**cfg.__dict__, "max_requests_per_run": 1})
    opener = FakeOpener(FakeResponse(200, b"{}"), FakeResponse(200, b"{}"))
    _send(small, tmp_path, opener)
    _, code = _send(small, tmp_path, opener)
    assert code == sr.EXIT_BUDGET and len(opener.requests) == 1


def test_send_rejects_redirects_by_not_following(cfg: sr.Config, tmp_path: Path) -> None:
    moved = urllib.error.HTTPError(f"{BASE}/x", 302, "Found", Message(), io.BytesIO(b""))
    opener = FakeOpener(moved)
    result, _ = _send(cfg, tmp_path, opener)
    assert result.http_status == 302 and len(opener.requests) == 1


# --- redaction and evidence -------------------------------------------------------------------


def test_send_saves_redacted_evidence_and_log_without_body(cfg: sr.Config, tmp_path: Path) -> None:
    body = json.dumps({"email": "a@b.com", "note": "call 212-555-0100", "token": "eyJaaaaaaaaaa.bbbbbbbbbb.cccccccccc",
                       "status": "Verified"}).encode()
    opener = FakeOpener(FakeResponse(200, body, {"Set-Cookie": "s=1", "Content-Type": "application/json"}))
    result, _ = _send(cfg, tmp_path, opener)
    saved = (tmp_path / "responses" / f"{result.seq:03d}.json").read_text(encoding="utf-8")
    for secret in ("a@b.com", "212-555-0100", "eyJaaaaaaaaaa", "s=1"):
        assert secret not in saved
    assert "Verified" in saved
    log_line = (tmp_path / "requests.jsonl").read_text(encoding="utf-8")
    assert "Verified" not in log_line and "body_preview" not in log_line


def test_redact_body_truncates_long_bodies(cfg: sr.Config) -> None:
    preview, truncated = sr.redact_body(cfg, "x" * (cfg.body_preview_chars + 10))
    assert truncated and len(preview) == cfg.body_preview_chars


@pytest.mark.parametrize("header", ["Authorization: Bearer x", "Cookie: a=b", "X-Target-Host: https://evil.com",
                                    "Host: evil.com"])
def test_parse_headers_rejects_auth_and_routing_headers(header: str) -> None:
    with pytest.raises(sr.InputError):
        sr.parse_headers([header])


# --- Snowflake queries ------------------------------------------------------------------------


def test_build_queries_org_requires_every_linked_practice_to_be_test() -> None:
    [(kind, sql)] = sr.build_queries({"org": [REAL_ORG]})
    assert kind == "org"
    assert "practice_organization_latest" in sql and "booland_agg" in sql


def test_check_ids_all_zero_ids_are_fake_without_a_cache(cfg: sr.Config) -> None:
    [check] = sr.check_ids(cfg, [("org", FAKE_ORG)], None)
    assert check.verdict is sr.Verdict.FAKE


def test_id_cache_never_stores_unknown(tmp_path: Path) -> None:
    cache = sr.IdCache(tmp_path / "c.json", 24)
    cache.put(TEST_PR, sr.Verdict.UNKNOWN)
    cache.put(TEST_PT, sr.Verdict.TEST)
    cache.save()
    reloaded = sr.IdCache(tmp_path / "c.json", 24)
    assert reloaded.get(TEST_PR) is None and reloaded.get(TEST_PT) is sr.Verdict.TEST
