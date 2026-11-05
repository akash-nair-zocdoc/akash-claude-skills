#!/usr/bin/env python3
"""The only way team-api-test sends HTTP to Zocdoc prod.

Every request goes through this script. It enforces, in code rather than in a prompt:

* prod ``.com`` hosts only: the public hosts in ``allowed_hosts``, or any host under an
  ``internal_host_suffixes`` domain (internal mode); https only, no ``.net``, no userinfo, no redirects;
* writes (anything but GET/HEAD/OPTIONS) only when every pr_/pt_/org_/lo_ ID in the URL and body
  is all zeros (obviously fake) or flagged as test data in non-PHI Snowflake, on public and
  internal hosts alike;
* a per-run request budget and no retries for writes;
* redacted, truncated evidence on disk and no tokens or bodies in the log.

Subcommands::

    preflight  --run-dir DIR                       check the auth proxy
    sf-query   --run-dir DIR ID [ID ...]           print the SQL that verifies IDs (with a one-time nonce)
    sf-ingest  --run-dir DIR --nonce N --result F  accept the snowflake_pr001 output for that SQL
    check-ids  --run-dir DIR ID [ID ...]           show cached verdicts without sending anything
    send       --run-dir DIR --method M --url URL  send one request (see --help)

Snowflake runs in the Claude Code session, not here: the zd-data 2.0 MCP server is remote and signs in
with OAuth this script can't use. A write is sent only when every real ID has a fresh ``test`` verdict
from ``sf-ingest`` in the cache.

Exit codes: 0 sent / ok, 2 bad input, 3 blocked by the safety check, 4 network or proxy failure,
5 request budget used up. stdout is always one JSON object.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("team-api-test.safe_request")

TEAM_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = TEAM_DIR / "config.json"
ID_CACHE_PATH = TEAM_DIR / "cache" / "id_checks.json"
PENDING_DIR = TEAM_DIR / "cache" / "sf_pending"

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_BLOCKED = 3
EXIT_NETWORK = 4
EXIT_BUDGET = 5

ID_CHARS = "A-Za-z0-9_-"  # prod IDs are base64url: pr_TestProviderAAAAAAAAAA
REDACTED = "[redacted]"
SANDBOX_BLOCKED_STATUS = 567  # the Zocdoc agentic sandbox's "blocked by network policy"
HOST_LABELS = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")
FORBIDDEN_HEADERS = frozenset({"authorization", "cookie", "host", "x-target-host", "proxy-authorization"})


class Outcome(str, Enum):  # StrEnum needs 3.11; this machine has 3.10
    SENT = "sent"
    BLOCKED = "blocked"
    ERROR = "error"


class Verdict(str, Enum):
    FAKE = "fake"  # all zeros: cannot exist
    TEST = "test"  # flagged is_test in Snowflake
    REAL = "real"
    UNKNOWN = "unknown"  # not found, NULL flag, or Snowflake unreachable


class AuthMode(str, Enum):
    PROXY = "proxy"  # through localhost auth proxy, which adds a bearer token
    NONE = "none"  # straight to the host with no credentials, for auth tests


class SafetyError(Exception):
    """The request breaks a safety rule and must not be sent."""


class InputError(Exception):
    """The request is malformed (bad URL, bad header, unreadable body)."""


class NetworkError(Exception):
    """The proxy or host could not be reached."""


@dataclass(frozen=True)
class Config:
    allowed_hosts: tuple[str, ...]
    internal_host_suffixes: tuple[str, ...]
    proxy_url: str
    proxy_start_hint: str
    id_types: dict[str, str]
    id_body_length: int
    verification_max_age_hours: int
    max_requests_per_run: int
    timeout_seconds: int
    body_preview_chars: int
    redact_keys: frozenset[str]
    snowflake_account: str
    sf_nonce_max_age_minutes: int
    read_methods: frozenset[str]
    proxy_401_retry_seconds: int
    keep_response_headers: frozenset[str]

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            allowed_hosts=tuple(h.lower() for h in raw["allowed_hosts"]),
            internal_host_suffixes=tuple(s.lower().lstrip(".") for s in raw.get("internal_host_suffixes", [])),
            proxy_url=raw["proxy_url"].rstrip("/"),
            proxy_start_hint=raw["proxy_start_hint"],
            id_types=dict(raw["id_types"]),
            id_body_length=int(raw["id_body_length"]),
            verification_max_age_hours=int(raw["verification_max_age_hours"]),
            max_requests_per_run=int(raw["max_requests_per_run"]),
            timeout_seconds=int(raw["timeout_seconds"]),
            body_preview_chars=int(raw["body_preview_chars"]),
            redact_keys=frozenset(k.lower() for k in raw["redact_keys"]),
            snowflake_account=str(raw["snowflake_account"]).upper(),
            sf_nonce_max_age_minutes=int(raw["sf_nonce_max_age_minutes"]),
            read_methods=frozenset(m.upper() for m in raw["read_methods"]),
            proxy_401_retry_seconds=int(raw["proxy_401_retry_seconds"]),
            keep_response_headers=frozenset(h.lower() for h in raw["keep_response_headers"]),
        )

    def id_pattern(self) -> re.Pattern[str]:
        prefixes = "|".join(sorted(self.id_types, key=len, reverse=True))
        return re.compile(
            rf"(?<![{ID_CHARS}])({prefixes})_([{ID_CHARS}]{{{self.id_body_length}}})(?![{ID_CHARS}])"
        )

    def foreign_id_pattern(self) -> re.Pattern[str]:
        """Any prefix_<22 chars> token, so a write carrying an unknown ID type can be refused."""
        return re.compile(rf"(?<![{ID_CHARS}])([a-z]{{2,5}})_([{ID_CHARS}]{{{self.id_body_length}}})(?![{ID_CHARS}])")

    def is_internal_host(self, host: str) -> bool:
        """A host under an internal suffix with a real label in front, e.g. svc-v1.east.zocdoccloud.com."""
        return any(host.endswith("." + s) and HOST_LABELS.fullmatch(host[: -len(s) - 1])
                   for s in self.internal_host_suffixes)


@dataclass(frozen=True)
class IdCheck:
    id: str
    kind: str
    verdict: Verdict
    detail: str = ""


@dataclass(frozen=True)
class Target:
    method: str
    url: str
    host: str
    path_and_query: str
    internal: bool = False


@dataclass
class Result:
    outcome: Outcome
    reason: str = ""
    seq: int | None = None
    test_id: str = ""
    method: str = ""
    url: str = ""
    auth: str = ""
    http_status: int | None = None
    elapsed_ms: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    body_preview: str = ""
    body_truncated: bool = False
    ids: list[dict[str, str]] = field(default_factory=list)
    override: str = ""

    def to_json(self) -> str:
        data = asdict(self)
        data["outcome"] = self.outcome.value
        return json.dumps(data, indent=2)


# --------------------------------------------------------------------------------------------
# URL and ID rules


def validate_target(cfg: Config, method: str, url: str) -> Target:
    method = method.upper()
    if not re.fullmatch(r"[A-Z]{3,7}", method):
        raise InputError(f"unsupported method {method!r}")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise InputError("URL contains whitespace or control characters")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith(".net") or ".net." in host:
        raise SafetyError(f"{host} is a .net host; this workflow only calls prod .com hosts")
    if parts.scheme != "https":
        raise SafetyError("only https URLs are allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SafetyError("URLs with user info are not allowed")
    if parts.port not in (None, 443):
        raise SafetyError(f"port {parts.port} is not allowed")
    internal = cfg.is_internal_host(host)
    if host not in cfg.allowed_hosts and not internal:
        raise SafetyError(f"{host or '(no host)'} is not on the allowed list {list(cfg.allowed_hosts)} "
                          f"and not under an internal domain {list(cfg.internal_host_suffixes)}")
    path = parts.path or "/"
    path_and_query = path + (f"?{parts.query}" if parts.query else "")
    return Target(method=method, url=urllib.parse.urlunsplit(("https", host, path, parts.query, "")), host=host,
                  path_and_query=path_and_query, internal=internal)


def _strings_in(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings_in(v)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


def searchable_text(url: str, body: str | None) -> list[str]:
    """URL (raw and percent-decoded) plus body (raw and, for JSON, every decoded string)."""
    texts = [url, urllib.parse.unquote(url), urllib.parse.unquote_plus(url)]
    if body:
        texts.append(body)
        with contextlib.suppress(json.JSONDecodeError):
            texts.extend(_strings_in(json.loads(body)))
    return texts


def extract_ids(cfg: Config, texts: Iterable[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(kind_prefix, full_id)], [foreign IDs]) found in the texts, de-duplicated in order."""
    known: dict[str, str] = {}
    foreign: dict[str, None] = {}
    pattern, foreign_pattern = cfg.id_pattern(), cfg.foreign_id_pattern()
    for text in texts:
        for m in pattern.finditer(text):
            known.setdefault(m.group(0), m.group(1))
        for m in foreign_pattern.finditer(text):
            if m.group(1) not in cfg.id_types:
                foreign.setdefault(m.group(0), None)
    return [(prefix, full) for full, prefix in known.items()], list(foreign)


def is_obviously_fake(full_id: str) -> bool:
    body = full_id.split("_", 1)[1]
    return set(body) == {"0"}


# --------------------------------------------------------------------------------------------
# Snowflake test-flag lookups (non-PHI tables only)
#
# Since zd-data 2.0 the Snowflake MCP server is remote and signs in with OAuth that only the Claude Code
# session holds, so this script cannot query Snowflake itself. Verification is a hand-off instead:
#
# 1. ``sf-query`` writes one SQL statement tagged with a one-time nonce and remembers which IDs it asked about.
# 2. The session runs that SQL with the non-PHI ``snowflake_pr001`` tool and saves the tool's JSON output.
# 3. ``sf-ingest`` accepts the saved output only if every row carries the nonce, came from the non-PHI
#    account, covers only the IDs that were asked, and is complete. Then it caches the verdicts.
#
# ``send`` and ``check-ids`` only read the cache, so a write is still refused until step 3 has run.

OpenFn = Callable[..., Any]
SF_MARKER_ID = "__nonce_marker__"  # a row every genuine result carries, so an empty result can't pass
NONCE_RE = re.compile(r"[a-f0-9]{16}")
SF_COLUMNS = ("ID", "IS_TEST_SAFE", "NONCE", "ACCOUNT")


class IngestError(Exception):
    """A saved Snowflake result can't be trusted, so none of it is used."""


def _sql_list(ids: Iterable[str]) -> str:
    return ", ".join(f"'{i}'" for i in ids)  # IDs already matched the strict ID regex


def build_queries(by_kind: dict[str, list[str]]) -> list[tuple[str, str]]:
    """One (kind, SQL) per kind. Each row returns ID and IS_TEST_SAFE (true only when provably test)."""
    practice_safe = (
        "select pa.practice_id, coalesce(pa.is_test, false) and coalesce(pp.all_test, false) as safe "
        "from cistern.provider.practice pa left join (select practice_id, "
        "booland_agg(coalesce(is_test_practice, false)) all_test from cistern.provider_profile.practice "
        "group by 1) pp on pp.practice_id = pa.practice_id"
    )
    queries: list[tuple[str, str]] = []
    if ids := by_kind.get("pr"):
        queries.append(("pr", f"with ps as ({practice_safe}) select p.provider_id as id, "
                              f"coalesce(p.is_test, false) and coalesce(ps.safe, false) as is_test_safe "
                              f"from cistern.provider.provider p left join ps on ps.practice_id = p.practice_id "
                              f"where p.provider_id in ({_sql_list(ids)})"))
    if ids := by_kind.get("pt"):
        queries.append(("pt", f"with ps as ({practice_safe}) select practice_id as id, safe as is_test_safe "
                              f"from ps where practice_id in ({_sql_list(ids)})"))
    if ids := by_kind.get("lo"):
        queries.append(("lo", f"with ps as ({practice_safe}) select l.location_id as id, "
                              f"coalesce(ps.safe, false) as is_test_safe from cistern.provider.location l "
                              f"left join ps on ps.practice_id = l.practice_id "
                              f"where l.location_id in ({_sql_list(ids)})"))
    if ids := by_kind.get("org"):
        # An org flagged is_test can still hold real practices (seen in Oct 2026), so every linked
        # practice must be test too.
        queries.append(("org", f"with ps as ({practice_safe}) select o.organization_id as id, "
                               f"coalesce(o.is_test, false) and coalesce(booland_agg(coalesce(ps.safe, "
                               f"pol.practice_id is null)), true) as is_test_safe "
                               f"from cistern.provider_analytics.mart_organizations o "
                               f"left join cistern.provider_analytics.practice_organization_latest pol "
                               f"on pol.organization_id = o.organization_id "
                               f"left join ps on ps.practice_id = pol.practice_id "
                               f"where o.organization_id in ({_sql_list(ids)}) group by o.organization_id, o.is_test"))
    return queries


def build_handoff_sql(by_kind: dict[str, list[str]], nonce: str) -> str:
    """One statement covering every kind, plus a marker row, each row stamped with the nonce and account."""
    if not NONCE_RE.fullmatch(nonce):
        raise ValueError("nonce must be 16 lowercase hex characters")
    parts = [f"({sql})" for _, sql in build_queries(by_kind)]
    parts.append(f"(select '{SF_MARKER_ID}' as id, null::boolean as is_test_safe)")
    return (f"select q.id, q.is_test_safe, '{nonce}' as nonce, current_account() as account "
            f"from ({' union all '.join(parts)}) q")


@dataclass(frozen=True)
class PendingQuery:
    nonce: str
    ids: dict[str, str]  # full ID -> kind prefix
    created_at: float


class PendingStore:
    """Hand-off SQL waiting for its result. Each nonce can be ingested once, within the age limit."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def create(self, ids: dict[str, str], now: float) -> PendingQuery:
        pending = PendingQuery(nonce=os.urandom(8).hex(), ids=dict(ids), created_at=now)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pending.nonce}.json").write_text(json.dumps(asdict(pending), indent=1), encoding="utf-8")
        return pending

    def take(self, nonce: str, now: float, max_age_seconds: int) -> PendingQuery:
        """Load and consume a pending query; raise IngestError if it's unknown, used or too old."""
        if not NONCE_RE.fullmatch(nonce):
            raise IngestError("nonce must be the 16 hex characters sf-query printed")
        path = self._dir / f"{nonce}.json"
        if not path.exists():
            raise IngestError(f"no pending query for nonce {nonce}: it was never issued or was already ingested")
        raw = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        pending = PendingQuery(nonce=raw["nonce"], ids=dict(raw["ids"]), created_at=float(raw["created_at"]))
        if now - pending.created_at > max_age_seconds:
            raise IngestError(f"nonce {nonce} is older than {max_age_seconds // 60} minutes; run sf-query again")
        return pending


def parse_sf_result(text: str) -> tuple[list[dict[str, Any]], str]:
    """(rows keyed by upper-case column name, query_id) from the saved snowflake_pr001 tool output."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestError("result file is not JSON; save the tool's output exactly as returned") from exc
    result_set = payload.get("result_set") if isinstance(payload, dict) else None
    if not isinstance(result_set, dict):
        raise IngestError("result file has no result_set; save the tool's full JSON output")
    meta = result_set.get("resultSetMetaData") or {}
    columns = [str(col.get("name", "")).upper() for col in meta.get("rowType", [])]
    data = result_set.get("data")
    if not columns or not isinstance(data, list):
        raise IngestError("result_set has no columns or no data")
    if meta.get("numRows") != len(data):
        raise IngestError(f"result is partial: numRows={meta.get('numRows')} but {len(data)} rows saved")
    rows: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, list) or len(raw) != len(columns):
            raise IngestError("a result row doesn't match the result's columns")
        rows.append(dict(zip(columns, raw)))
    query_id = str(payload.get("query_id", ""))
    return rows, query_id if re.fullmatch(r"[0-9a-f-]{36}", query_id) else ""


def _flag(value: Any) -> bool | None:
    """Snowflake's JSON booleans arrive as "true"/"false", and NULL as "". Anything else is refused."""
    if isinstance(value, bool):  # checked by type: 1 == True in Python, and a number is not a flag
        return value
    if value is None or (isinstance(value, str) and value == ""):
        return None
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise IngestError(f"IS_TEST_SAFE has an unexpected value type {type(value).__name__}")


def verdicts_from_result(cfg: Config, pending: PendingQuery, rows: list[dict[str, Any]],
                         query_id: str) -> list[IdCheck]:
    """Turn a validated result into verdicts. All-or-nothing: any doubt raises IngestError."""
    if rows and not set(SF_COLUMNS) <= rows[0].keys():
        raise IngestError(f"result is missing columns {sorted(set(SF_COLUMNS) - rows[0].keys())}; "
                          "run the SQL exactly as sf-query printed it")
    markers = 0
    flags: dict[str, bool | None] = {}
    for row in rows:
        if row["NONCE"] != pending.nonce:
            raise IngestError("a row carries a different nonce; this result is not from the SQL for this nonce")
        if str(row["ACCOUNT"]).upper() != cfg.snowflake_account:
            raise IngestError(f"result came from account {row['ACCOUNT']!r}, not the non-PHI "
                              f"{cfg.snowflake_account}; use the snowflake_pr001 tool")
        if row["ID"] == SF_MARKER_ID:
            markers += 1
            continue
        if row["ID"] not in pending.ids:
            raise IngestError("result has a row for an ID this nonce didn't ask about")
        if row["ID"] in flags:
            raise IngestError(f"result has more than one row for {row['ID']}")
        flags[row["ID"]] = _flag(row["IS_TEST_SAFE"])
    if markers != 1:
        raise IngestError("result has no nonce marker row; run the SQL exactly as sf-query printed it")
    source = f"Snowflake {cfg.snowflake_account}" + (f", query {query_id}" if query_id else "")
    checks = []
    for full, prefix in pending.ids.items():
        kind = cfg.id_types[prefix]
        if full not in flags:
            checks.append(IdCheck(full, kind, Verdict.UNKNOWN,
                                  "not in Snowflake (new records appear after the daily refresh)"))
        elif flags[full] is True:
            checks.append(IdCheck(full, kind, Verdict.TEST, f"is_test in {source}"))
        else:
            checks.append(IdCheck(full, kind, Verdict.REAL, f"not flagged as test in {source}"))
    return checks


class IdCache:
    """Remembers Snowflake verdicts for verification_max_age_hours. The only verdict source for writes."""

    def __init__(self, path: Path, max_age_hours: int) -> None:
        self._path = path
        self._max_age = max_age_hours * 3600
        try:
            self._data: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._data = {}
        except json.JSONDecodeError:
            log.warning("ignoring unreadable ID cache %s", path)
            self._data = {}

    def get(self, full_id: str) -> Verdict | None:
        entry = self._data.get(full_id)
        if entry and time.time() - float(entry["at"]) < self._max_age:
            return Verdict(entry["verdict"])
        return None

    def put(self, full_id: str, verdict: Verdict, source: str = "") -> None:
        if verdict in (Verdict.TEST, Verdict.REAL):  # never cache "unknown": it may resolve later
            self._data[full_id] = {"verdict": verdict.value, "at": time.time(), "source": source}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        tmp.replace(self._path)


def ids_needing_query(found: list[tuple[str, str]], cache: IdCache | None, refresh: bool = False) -> dict[str, str]:
    """IDs that need a Snowflake answer: not all zeros, and not cached (or refresh requested)."""
    return {full: prefix for prefix, full in found
            if not is_obviously_fake(full) and (refresh or cache is None or cache.get(full) is None)}


def check_ids(cfg: Config, found: list[tuple[str, str]], cache: IdCache | None) -> list[IdCheck]:
    checks: list[IdCheck] = []
    for prefix, full in found:
        kind = cfg.id_types[prefix]
        if is_obviously_fake(full):
            checks.append(IdCheck(full, kind, Verdict.FAKE, "all zeros"))
        elif cache and (cached := cache.get(full)):
            checks.append(IdCheck(full, kind, cached, "cached Snowflake result"))
        else:
            checks.append(IdCheck(full, kind, Verdict.UNKNOWN,
                                  "not verified yet: run sf-query, run its SQL with snowflake_pr001, then sf-ingest"))
    return checks


def write_guard(cfg: Config, method: str, found: list[tuple[str, str]], foreign: list[str],
                checks: list[IdCheck], allow_no_id: bool = False) -> None:
    """Raise SafetyError unless this request is a read or a write that only touches fake/test IDs.

    ``allow_no_id`` (user opt-in, per request) lets a write with no recognisable ID through, for
    malformed-ID negative tests. Foreign-type, real and unknown IDs are still refused.
    """
    if method in cfg.read_methods:
        return
    if foreign:
        raise SafetyError(f"write carries IDs of a type this check cannot verify: {', '.join(foreign)}")
    if not found and not allow_no_id:
        raise SafetyError("write has no pr_/pt_/org_/lo_ ID, so there is no way to prove it only touches test data")
    unsafe = [c for c in checks if c.verdict not in (Verdict.FAKE, Verdict.TEST)]
    if unsafe:
        listed = "; ".join(f"{c.id} ({c.kind}: {c.verdict.value}, {c.detail})" for c in unsafe)
        raise SafetyError(f"write blocked, these IDs are not proven test data: {listed}")


# --------------------------------------------------------------------------------------------
# Redaction


def redact_value(cfg: Config, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if str(k).lower() in cfg.redact_keys else redact_value(cfg, v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(cfg, v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    text = JWT_RE.sub(REDACTED, text)
    text = EMAIL_RE.sub(REDACTED, text)
    return PHONE_RE.sub(REDACTED, text)


def redact_body(cfg: Config, body: str) -> tuple[str, bool]:
    """Return (redacted preview, truncated?)."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        cleaned = redact_text(body)
    else:
        cleaned = json.dumps(redact_value(cfg, parsed), indent=2)
    limit = cfg.body_preview_chars
    return (cleaned[:limit], True) if len(cleaned) > limit else (cleaned, False)


def redact_url(cfg: Config, url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = urllib.parse.urlencode([(k, REDACTED if k.lower() in cfg.redact_keys else redact_text(v)) for k, v in pairs])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


# --------------------------------------------------------------------------------------------
# Run directory: budget counter, request log, evidence


class RunDir:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "responses").mkdir(exist_ok=True)

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        lock_path = self.path / ".state.lock"
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state_path = self.path / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
                "next_seq": 1, "sent": 0, "blocked": 0}
            yield state
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def reserve(self, cfg: Config, will_send: bool) -> int:
        """Allocate a sequence number; refuse if a send would exceed the budget."""
        with self._locked_state() as state:
            if will_send and state["sent"] >= cfg.max_requests_per_run:
                raise BudgetError(f"request budget of {cfg.max_requests_per_run} for this run is used up")
            seq = state["next_seq"]
            state["next_seq"] = seq + 1
            if will_send:
                state["sent"] += 1
            else:
                state["blocked"] += 1
            return seq

    def record(self, result: Result) -> None:
        entry = {k: v for k, v in asdict(result).items() if k not in ("body_preview", "response_headers")}
        entry["outcome"] = result.outcome.value
        entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (self.path / "requests.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if result.seq is not None:
            (self.path / "responses" / f"{result.seq:03d}.json").write_text(result.to_json(), encoding="utf-8")


class BudgetError(Exception):
    """The run has sent max_requests_per_run requests."""


# --------------------------------------------------------------------------------------------
# HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102 - never follow redirects
        return None


def _local_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _remote_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    body: str
    elapsed_ms: int


def http_call(req: urllib.request.Request, timeout: int, opener: OpenFn) -> RawResponse:
    started = time.monotonic()
    try:
        with opener(req, timeout=timeout) as resp:
            status, headers, raw = resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        status, headers, raw = exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"could not reach {req.full_url.split('?')[0]}: {getattr(exc, 'reason', exc)}") from exc
    elapsed = int((time.monotonic() - started) * 1000)
    return RawResponse(status, {k.lower(): v for k, v in headers.items()}, raw.decode("utf-8", "replace"), elapsed)


def build_request(cfg: Config, target: Target, auth: AuthMode, headers: dict[str, str],
                  body: str | None) -> urllib.request.Request:
    hdrs = {"Accept": "application/json", "User-Agent": "team-api-test/1"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers)
    if auth is AuthMode.PROXY:
        hdrs["X-Target-Host"] = f"https://{target.host}"
        url = cfg.proxy_url + target.path_and_query
    else:
        url = target.url
    data = body.encode("utf-8") if body is not None else None
    return urllib.request.Request(url, data=data, headers=hdrs, method=target.method)


def parse_headers(raw: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        name, sep, value = item.partition(":")
        name = name.strip()
        if not sep or not re.fullmatch(r"[A-Za-z0-9-]+", name):
            raise InputError(f"bad header {item.split(':')[0]!r}; use 'Name: value'")
        if name.lower() in FORBIDDEN_HEADERS:
            raise InputError(f"header {name} is not allowed; the auth proxy handles auth and routing")
        out[name] = value.strip()
    return out


def is_proxy_failure(resp: RawResponse) -> bool:
    return resp.status == 502 and "proxy error" in resp.body.lower()


def send(cfg: Config, run: RunDir, *, method: str, url: str, body: str | None, headers: dict[str, str],
         auth: AuthMode, test_id: str, cache: IdCache | None,
         opener: OpenFn | None = None, sleep: Callable[[float], None] = time.sleep,
         allow_no_id_write: bool = False) -> tuple[Result, int]:
    result = Result(outcome=Outcome.BLOCKED, test_id=test_id, method=method.upper(), auth=auth.value,
                    url=redact_url(cfg, url))
    try:
        target = validate_target(cfg, method, url)
        result.url = redact_url(cfg, target.url)
        found, foreign = extract_ids(cfg, searchable_text(target.url, body))
        is_write = target.method not in cfg.read_methods
        checks = check_ids(cfg, found, cache) if is_write else []
        result.ids = [{"id": c.id, "type": c.kind, "verdict": c.verdict.value, "detail": c.detail} for c in checks]
        write_guard(cfg, target.method, found, foreign, checks, allow_no_id=allow_no_id_write)
        if is_write and not found and allow_no_id_write:
            result.override = "allow-no-id-write"
            log.warning("OVERRIDE allow-no-id-write: %s %s carries no recognisable ID", result.method, result.url)
    except SafetyError as exc:
        result.reason = str(exc)
        result.seq = run.reserve(cfg, will_send=False)
        log.warning("BLOCKED %s %s: %s", result.method, result.url, exc)
        run.record(result)
        return result, EXIT_BLOCKED

    try:
        result.seq = run.reserve(cfg, will_send=True)
    except BudgetError as exc:
        result.outcome, result.reason = Outcome.ERROR, str(exc)
        return result, EXIT_BUDGET

    open_fn = opener or (_local_opener().open if auth is AuthMode.PROXY else _remote_opener().open)
    req = build_request(cfg, target, auth, headers, body)
    try:
        resp = http_call(req, cfg.timeout_seconds, open_fn)
        if auth is AuthMode.PROXY and resp.status == 401 and not is_write:
            log.info("401 through the proxy; waiting %ss for a token refresh, then retrying the read once",
                     cfg.proxy_401_retry_seconds)
            sleep(cfg.proxy_401_retry_seconds)
            resp = http_call(build_request(cfg, target, auth, headers, body), cfg.timeout_seconds, open_fn)
    except NetworkError as exc:
        result.outcome = Outcome.ERROR
        result.reason = (f"{exc}. Is the auth proxy running? Start it with: {cfg.proxy_start_hint}"
                         if auth is AuthMode.PROXY else str(exc))
        run.record(result)
        return result, EXIT_NETWORK

    if resp.status == SANDBOX_BLOCKED_STATUS:
        result.outcome = Outcome.ERROR
        result.reason = (f"the local sandbox's network policy blocked this call to {target.host} (567); "
                         "no API response. Direct (--auth none) calls to internal hosts can't run here")
        run.record(result)
        return result, EXIT_NETWORK

    if auth is AuthMode.PROXY and is_proxy_failure(resp):
        result.outcome = Outcome.ERROR
        result.reason = "auth proxy could not reach the host (502 Proxy error)"
        run.record(result)
        return result, EXIT_NETWORK

    result.outcome = Outcome.SENT
    result.http_status = resp.status
    result.elapsed_ms = resp.elapsed_ms
    result.response_headers = {k: redact_text(v) for k, v in resp.headers.items() if k in cfg.keep_response_headers}
    result.body_preview, result.body_truncated = redact_body(cfg, resp.body) if resp.body else ("", False)
    run.record(result)
    log.info("%s %s -> %s (%sms)", result.method, result.url, resp.status, resp.elapsed_ms)
    return result, EXIT_OK


def preflight(cfg: Config, opener: OpenFn | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {}
    req = urllib.request.Request(cfg.proxy_url + "/", method="GET")
    try:
        resp = http_call(req, 5, opener or _local_opener().open)
        report["proxy"] = {"up": True, "status": resp.status}
    except NetworkError:
        report["proxy"] = {"up": False, "hint": f"Start the auth proxy: {cfg.proxy_start_hint}"}
    report["snowflake"] = {"mode": "session-handoff", "account": cfg.snowflake_account,
                           "hint": "verify IDs with sf-query, the snowflake_pr001 tool, then sf-ingest"}
    report["ok"] = bool(report["proxy"]["up"])
    return report


def sf_query(cfg: Config, ids: list[str], cache: IdCache | None, store: PendingStore, *,
             refresh: bool = False, now: float | None = None) -> dict[str, Any]:
    """The SQL to run with snowflake_pr001, or no SQL if every ID is fake or freshly cached."""
    found, foreign = extract_ids(cfg, ids)
    needed = ids_needing_query(found, cache, refresh=refresh)
    out: dict[str, Any] = {"unrecognised": [i for i in ids if not any(i == f for _, f in found)] + foreign,
                           "already_known": [{"id": c.id, "verdict": c.verdict.value}
                                             for c in check_ids(cfg, found, cache) if c.id not in needed]}
    if not needed:
        out.update(nonce=None, sql=None)
        return out
    pending = store.create(needed, time.time() if now is None else now)
    by_kind: dict[str, list[str]] = {}
    for full, prefix in needed.items():
        by_kind.setdefault(prefix, []).append(full)
    out.update(nonce=pending.nonce, ids=list(needed), sql=build_handoff_sql(by_kind, pending.nonce),
               expires_in_minutes=cfg.sf_nonce_max_age_minutes)
    return out


def sf_ingest(cfg: Config, nonce: str, result_text: str, cache: IdCache, store: PendingStore, *,
              now: float | None = None) -> list[IdCheck]:
    """Validate a saved snowflake_pr001 result for a nonce and cache its verdicts (all or nothing)."""
    pending = store.take(nonce, time.time() if now is None else now, cfg.sf_nonce_max_age_minutes * 60)
    rows, query_id = parse_sf_result(result_text)
    checks = verdicts_from_result(cfg, pending, rows, query_id)
    for check in checks:
        cache.put(check.id, check.verdict, source=check.detail)
    cache.save()
    log.info("ingested %d Snowflake verdicts for nonce %s", len(checks), nonce)
    return checks


# --------------------------------------------------------------------------------------------
# CLI


def _read_body(args: argparse.Namespace) -> str | None:
    if args.body_file:
        try:
            return Path(args.body_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(f"cannot read body file {args.body_file}") from exc
    return args.body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "sf-query", "sf-ingest", "check-ids", "send"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=name != "preflight")
    sub.choices["check-ids"].add_argument("ids", nargs="+")
    q = sub.choices["sf-query"]
    q.add_argument("ids", nargs="+")
    q.add_argument("--refresh", action="store_true", help="re-verify IDs even if a fresh verdict is cached")
    q.add_argument("--sql-out", type=Path, help="also write the SQL to this file")
    i = sub.choices["sf-ingest"]
    i.add_argument("--nonce", required=True)
    i.add_argument("--result", type=Path, required=True, help="file holding the snowflake_pr001 JSON output")
    s = sub.choices["send"]
    s.add_argument("--method", required=True)
    s.add_argument("--url", required=True,
                   help="full https://api.zocdoc.com/... URL, or https://<svc>.east.zocdoccloud.com/... (internal)")
    s.add_argument("--body", help="JSON body as a string")
    s.add_argument("--body-file", help="path to a JSON body file")
    s.add_argument("--header", action="append", default=[], help="'Name: value' (no auth headers)")
    s.add_argument("--auth", choices=[m.value for m in AuthMode], default=AuthMode.PROXY.value)
    s.add_argument("--test-id", default="")
    s.add_argument("--allow-no-id-write", action="store_true",
                   help="user opt-in: send a write that has no recognisable ID (malformed-ID tests); logged")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    cfg = Config.load(args.config)
    if args.cmd == "preflight":
        report = preflight(cfg)
        print(json.dumps(report, indent=2))
        return EXIT_OK if report["ok"] else EXIT_NETWORK

    cache = IdCache(ID_CACHE_PATH, cfg.verification_max_age_hours)
    store = PendingStore(PENDING_DIR)
    if args.cmd == "sf-query":
        out = sf_query(cfg, args.ids, cache, store, refresh=args.refresh)
        if args.sql_out and out["sql"]:
            args.sql_out.expanduser().write_text(out["sql"], encoding="utf-8")
        print(json.dumps(out, indent=2))
        return EXIT_OK
    if args.cmd == "sf-ingest":
        try:
            result_text = args.result.expanduser().read_text(encoding="utf-8")
            checks = sf_ingest(cfg, args.nonce, result_text, cache, store)
        except OSError as exc:
            print(json.dumps({"ok": False, "reason": f"cannot read result file {args.result}: {exc.strerror}"}))
            return EXIT_BAD_INPUT
        except IngestError as exc:
            log.warning("sf-ingest rejected the result for nonce %s: %s", args.nonce, exc)
            print(json.dumps({"ok": False, "reason": str(exc)}))
            return EXIT_BLOCKED
        print(json.dumps({"ok": True, "ids": [{"id": c.id, "type": c.kind, "verdict": c.verdict.value,
                                               "detail": c.detail} for c in checks]}, indent=2))
        return EXIT_OK
    if args.cmd == "check-ids":
        found, foreign = extract_ids(cfg, args.ids)
        checks = check_ids(cfg, found, cache)
        print(json.dumps({"ids": [{"id": c.id, "type": c.kind, "verdict": c.verdict.value, "detail": c.detail}
                                  for c in checks], "unrecognised": [i for i in args.ids if not any(
                                      i == f for _, f in found)] + foreign}, indent=2))
        return EXIT_OK

    try:
        body = _read_body(args)
        headers = parse_headers(args.header)
    except InputError as exc:
        print(json.dumps({"outcome": Outcome.ERROR.value, "reason": str(exc)}))
        return EXIT_BAD_INPUT
    try:
        result, code = send(cfg, RunDir(args.run_dir.expanduser()), method=args.method, url=args.url, body=body,
                            headers=headers, auth=AuthMode(args.auth), test_id=args.test_id,
                            cache=cache, allow_no_id_write=args.allow_no_id_write)
    except InputError as exc:
        print(json.dumps({"outcome": Outcome.ERROR.value, "reason": str(exc)}))
        return EXIT_BAD_INPUT
    print(result.to_json())
    return code


if __name__ == "__main__":
    sys.exit(main())
