#!/usr/bin/env python3
"""tome_bridge.py - Bridge research:tome tasks to /tome:research.

The assistant (running in OpenClaw) cannot directly invoke the tome
plugin because tome lives in the operator's Claude Code installation, not
the workflows container. This script polls the workflows task
queue, finds tasks tagged for tome, runs `claude /tome:research
<topic>` locally, and posts the synthesized output back via
/task/{id}/result.

A task is considered tome-bound when its description starts with
one of:
  - 'research:tome: <topic>'
  - '[research:tome] <topic>'
  - 'Research via tome: <topic>'

Usage:
  scripts/tome_bridge.py                      # one pass
  scripts/tome_bridge.py --watch --interval 60   # poll loop
  scripts/tome_bridge.py --dry-run            # show, don't run
  scripts/tome_bridge.py --base-url URL       # override workflows host

Env:
  WORKFLOWS_BASE       defaults to http://localhost:5678
  PROXY_AUTH_TOKEN     bearer token for the workflows API (required)
  CLAUDE_BIN           defaults to 'claude' on $PATH
  TOME_BRIDGE_TIMEOUT  per-task timeout in seconds (default 1800)

Exit codes:
  0  success (one pass with no tasks, or all processed)
  1  workflows unreachable
  2  claude CLI missing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

logger = logging.getLogger("tome_bridge")

DEFAULT_BASE = os.environ.get("WORKFLOWS_BASE", "http://localhost:5678")
DEFAULT_CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_TIMEOUT = int(os.environ.get("TOME_BRIDGE_TIMEOUT", "1800"))

# The only tools the bridged Claude session gets. The topic comes from the
# workflows task queue, so treat it as untrusted prompt text. An allowlist,
# not a deny-list: a deny-list misses every tool it does not name (Read can
# open .env, MCP servers bring their own). Research needs web search and
# fetch, the tome skill and its channel subagents; nothing touches local
# files or runs commands. --tools limits what exists, --allowedTools
# pre-approves it, and dontAsk denies anything else instead of prompting.
ALLOWED_TOOLS = ("WebSearch", "WebFetch", "Skill", "Agent")
MAX_TOPIC_CHARS = 300

# Workflows API failures that must not escape a pass: HTTPError and URLError
# (connection refused, DNS), TimeoutError/ConnectionResetError (OSError), and
# a non-JSON body (json.JSONDecodeError is a ValueError).
NETWORK_ERRORS = (urllib.error.URLError, OSError, ValueError)
# Posting the result is the step that loses work if it fails, so retry it
# with bounded backoff (2s, 4s, 8s, 16s: about 30s in total).
COMPLETE_ATTEMPTS = 5
COMPLETE_BACKOFF_SECS = 2.0
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_TOME_PATTERNS = (
    re.compile(r"^research:tome:\s*(?P<topic>.+)$", re.IGNORECASE),
    re.compile(r"^\[research:tome\]\s*(?P<topic>.+)$", re.IGNORECASE),
    re.compile(r"^research via tome:\s*(?P<topic>.+)$", re.IGNORECASE),
)


def extract_topic(description: str) -> str | None:
    """Return the topic string if the description is a tome-bound task.

    Pure function so tests can verify dispatch decisions without any
    HTTP or subprocess machinery.
    """
    if not description:
        return None
    text = description.strip()
    for pat in _TOME_PATTERNS:
        m = pat.match(text)
        if m:
            return m.group("topic").strip()
    return None


def topic_problem(topic: str) -> str | None:
    """Why ``topic`` is unsafe to hand to Claude, or None when it is fine."""
    if len(topic) > MAX_TOPIC_CHARS:
        return f"longer than {MAX_TOPIC_CHARS} characters"
    if _UNSAFE_CHARS.search(topic):
        return "contains control characters"
    return None


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    """Minimal stdlib JSON HTTP helper (avoids httpx dependency).

    Sends ``Authorization: Bearer $PROXY_AUTH_TOKEN``: the workflows API
    rejects unauthenticated task-queue calls.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("PROXY_AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def list_pending_tasks(base: str) -> list[dict]:
    return http_json("GET", f"{base}/task?status=pending").get("tasks", [])


def claim_task(base: str, task_id: str) -> dict:
    return http_json("POST", f"{base}/task/{task_id}/claim")


def complete_task(base: str, task_id: str, result: str, status: str) -> dict:
    return http_json(
        "POST",
        f"{base}/task/{task_id}/result",
        {"result": result, "status": status},
    )


def run_tome_research(claude_bin: str, topic: str, timeout: int) -> tuple[int, str]:
    """Run `claude --print '/tome:research <topic>'` and capture output.

    Returns (returncode, combined_stdout_stderr).
    """
    # The prompt comes first and the tool lists use the --flag=a,b form:
    # both flags are variadic and would otherwise swallow a later positional.
    tools = ",".join(ALLOWED_TOOLS)
    cmd = [
        claude_bin,
        "--print",
        f"/tome:research {topic}",
        f"--tools={tools}",
        f"--allowedTools={tools}",
        "--permission-mode",
        "dontAsk",
        # Ignore every MCP server from user/project config: each brings
        # tools the allowlist above never reviewed.
        "--strict-mcp-config",
    ]
    try:
        # An empty scratch dir as cwd: started from the repo root the session
        # would sit next to .env and load the project's CLAUDE.md/settings.
        with tempfile.TemporaryDirectory(prefix="tome-bridge-") as workdir:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
            )
    except subprocess.TimeoutExpired:
        return 124, f"tome bridge: claude timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"tome bridge: claude binary not found: {claude_bin}"

    out = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return result.returncode, out.strip()


def process_one_task(
    base: str, claude_bin: str, task: dict, timeout: int, dry_run: bool
) -> bool:
    """Process a single task. Returns True if it was a tome task."""
    desc = task.get("description", "")
    topic = extract_topic(desc)
    if topic is None:
        return False

    task_id = task["id"]
    logger.info("tome bridge: claiming task %s, topic=%r", task_id, topic)

    if dry_run:
        logger.info("tome bridge: --dry-run, would run /tome:research %r", topic)
        return True

    try:
        claim_task(base, task_id)
    except NETWORK_ERRORS as exc:
        # Unclaimed (or unknown): leave it for the next pass.
        logger.warning("tome bridge: claim failed for %s: %s", task_id, exc)
        return True

    problem = topic_problem(topic)
    if problem is not None:
        logger.warning("tome bridge: task %s topic rejected: %s", task_id, problem)
        complete_with_retry(
            base, task_id, f"tome bridge: topic rejected ({problem})", "failed"
        )
        return True

    rc, output = run_tome_research(claude_bin, topic, timeout)
    status = "completed" if rc == 0 else "failed"
    body = output[:8000] if output else f"tome /research returned exit code {rc}"

    if complete_with_retry(base, task_id, body, status):
        logger.info("tome bridge: task %s -> %s", task_id, status)
    return True


def complete_with_retry(base: str, task_id: str, result: str, status: str) -> bool:
    """Post a task result, retrying transient failures with bounded backoff.

    Returns True once it lands. After the last attempt it logs the result
    itself, so the research output is never lost silently, and returns False.
    """
    delay = COMPLETE_BACKOFF_SECS
    for attempt in range(1, COMPLETE_ATTEMPTS + 1):
        try:
            complete_task(base, task_id, result, status)
            return True
        except NETWORK_ERRORS as exc:
            logger.warning(
                "tome bridge: complete_task attempt %d/%d failed for %s: %s",
                attempt,
                COMPLETE_ATTEMPTS,
                task_id,
                exc,
            )
            if attempt < COMPLETE_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    logger.error(
        "tome bridge: giving up posting %s (%s); task stays active. Result:\n%s",
        task_id,
        status,
        result,
    )
    return False


def run_once(base: str, claude_bin: str, timeout: int, dry_run: bool) -> int:
    try:
        tasks = list_pending_tasks(base)
    except NETWORK_ERRORS as exc:
        logger.error("tome bridge: workflows unreachable at %s: %s", base, exc)
        return 1

    handled = 0
    for t in tasks:
        if process_one_task(base, claude_bin, t, timeout, dry_run):
            handled += 1
    logger.info("tome bridge: pass complete, %d tome tasks handled", handled)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--claude-bin", default=DEFAULT_CLAUDE)
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Per-task timeout in seconds",
    )
    parser.add_argument("--watch", action="store_true", help="Poll forever")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval (seconds) when --watch",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.watch:
        return run_once(args.base_url, args.claude_bin, args.timeout, args.dry_run)

    while True:
        try:
            rc = run_once(args.base_url, args.claude_bin, args.timeout, args.dry_run)
        except Exception:
            # Keep watching: one malformed task or an unforeseen error must
            # not end the poll loop. Back off as if workflows were down.
            logger.exception("tome bridge: pass failed")
            rc = 1
        if rc == 1:
            # Workflows down; back off but keep watching.
            time.sleep(min(args.interval * 2, 300))
        else:
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
