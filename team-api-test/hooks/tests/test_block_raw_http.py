from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "block_raw_http.py"
sys.path.insert(0, str(HOOK.parent))

from block_raw_http import should_block  # noqa: E402

GATE = "python3 $HOME/.claude/teams/team-api-test/scripts/safe_request.py"


@pytest.mark.parametrize(
    "command",
    [
        "curl -s https://api.zocdoc.com/provider-grouping/v2/x",
        "curl -X POST -H 'X-Target-Host: https://api.zocdoc.com' http://localhost:3000/v1/x -d '{}'",
        "wget -qO- https://pulse.zocdoc.com/api/x",
        "http POST api.zocdoc.com/v1/x a=b",
        "python3 -c \"import urllib.request as u; u.urlopen('https://api.zocdoc.com/x')\"",
        "node -e \"fetch('https://api.zocdoc.com/x')\"",
        "U=https://api.zocdoc.com/x; curl \"$U\"",
        f"{GATE} preflight; curl https://api.zocdoc.com/x",
        f"{GATE} send --url https://api.zocdoc.com/x && wget https://api.zocdoc.com/y",
        "bash -c 'curl https://API.ZOCDOC.COM/x'",
        "echo https://api.zocdoc.com/x | xargs curl",
        "/usr/bin/curl https://www.zocdoc.com/",
        "python3 /tmp/other.py https://api.zocdoc.com/x",
        "curl https://provider-grouping-v1.east.zocdoccloud.com/v2/x",
        "curl -H 'X-Target-Host: https://api-private.east.zocdoccloud.com' http://localhost:3000/x",
    ],
)
def test_blocks_raw_http_to_prod_com(command: str) -> None:
    assert should_block(command)


@pytest.mark.parametrize(
    "command",
    [
        f"TEAM_API_TEST_SF_TOKEN=abc {GATE} send --run-dir /tmp/r --method GET --url \"https://api.zocdoc.com/x\"",
        f"{GATE} check-ids --run-dir /tmp/r org_0000000000000000000000",
        "python3 /Users/x/.claude/teams/team-api-test/scripts/render_report.py --run-dir /tmp/r",
        "curl -s -o /dev/null -w \"%{http_code}\" --max-time 5 http://localhost:3000/",
        "curl -sS -H \"X-Target-Host: https://operations-provider-onboarding-v1.east.zocdoccloud.net\" http://localhost:3000/v1/x",
        "curl -sS -H \"Authorization: Bearer $TOKEN\" https://operations-provider-onboarding-v1.east.zocdoccloud.net/v1/x",
        f"{GATE} send --run-dir /tmp/r --method GET --url \"https://provider-reference-v1.east.zocdoccloud.com/x\"",
        "gh search code \"api.zocdoc.com\" --repo Zocdoc/routing-configs",
        "grep -rn api.zocdoc.com ~/.claude/teams",
        "git commit -m 'route via api.zocdoc.com'",
        "ls -la",
    ],
)
def test_allows_gate_and_unrelated_commands(command: str) -> None:
    assert not should_block(command)


def _run(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=False
    )


def test_main_exit_2_with_message_on_block() -> None:
    result = _run({"tool_name": "Bash", "tool_input": {"command": "curl https://api.zocdoc.com/x"}})
    assert result.returncode == 2
    assert "safe_request.py" in result.stderr


def test_main_exit_0_on_allowed_and_non_bash() -> None:
    assert _run({"tool_name": "Bash", "tool_input": {"command": "ls"}}).returncode == 0
    assert _run({"tool_name": "Read", "tool_input": {"file_path": "x"}}).returncode == 0


def test_main_fails_open_on_bad_json() -> None:
    result = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, check=False)
    assert result.returncode == 0
