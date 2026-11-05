#!/usr/bin/env python3
"""PreToolUse hook: block raw HTTP calls to prod Zocdoc `.com` hosts from Bash.

Every request to a `*.zocdoc.com` or `*.zocdoccloud.com` host (direct, or through
the auth proxy via X-Target-Host) must go through team-api-test's safe_request.py,
which enforces the write-safety rules. This hook refuses any other Bash command
that both names such a host and runs an HTTP-capable program. `*.zocdoccloud.net`
is left alone: team-di-batch calls its debug API there with raw curl.

It is a guardrail against accidental raw calls, not a sandbox: a script file
that builds the URL itself is not inspected.

Exit codes follow the Claude Code hook contract: 0 allows, 2 blocks (stderr is
shown to the model).
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

ALLOWED_SCRIPTS = frozenset({"safe_request.py", "render_report.py"})

# Any host under zocdoc.com or zocdoccloud.com. Does not match zocdoccloud.net.
PROTECTED_HOST = re.compile(r"(?<![\w-])(?:[a-z0-9-]+\.)*zocdoc(?:cloud)?\.com(?![\w-])", re.IGNORECASE)

HTTP_TOOL = re.compile(
    r"(?<![\w.-])(?:curl|wget|xh|httpie|ncat|nc|telnet|socat|aria2c|openssl|lynx|w3m)(?![\w.-])"
    r"|(?<![\w./-])https?(?=\s)"  # httpie's `http` / `https` commands
)
INTERPRETER = re.compile(r"(?<![\w-])(?:python[\d.]*|node|deno|bun|ruby|perl|php)(?![\w.-])")

SEGMENT_SPLIT = re.compile(r"\|\||&&|\$\(|[;|&\n`(){}]")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

BLOCK_MESSAGE = (
    "Blocked by team-api-test hook: raw HTTP to a prod *.zocdoc.com / *.zocdoccloud.com host.\n"
    "Send it through the safety gate instead:\n"
    "  python3 $HOME/.claude/teams/team-api-test/scripts/safe_request.py send "
    "--run-dir <run_dir> --method <M> --url \"https://<host>/<path>\"\n"
    "Don't rewrite the command to get around this block."
)


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def is_gate_invocation(segment: str) -> bool:
    """True when the segment is exactly `[VAR=x ...] python3 <.../safe_request.py|render_report.py> ...`."""
    if HTTP_TOOL.search(segment):
        return False
    words = [w for w in _tokens(segment)]
    while words and ENV_ASSIGN.match(words[0]):
        words.pop(0)
    if len(words) < 2 or not re.fullmatch(r"python[\d.]*", Path(words[0]).name):
        return False
    return Path(words[1]).name in ALLOWED_SCRIPTS


def segment_makes_http(segment: str) -> bool:
    if not segment.strip():
        return False
    if HTTP_TOOL.search(segment):
        return True
    return bool(INTERPRETER.search(segment)) and not is_gate_invocation(segment)


def should_block(command: str) -> bool:
    if not PROTECTED_HOST.search(command):
        return False
    return any(segment_makes_http(s) for s in SEGMENT_SPLIT.split(command))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if should_block(command):
        print(BLOCK_MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
