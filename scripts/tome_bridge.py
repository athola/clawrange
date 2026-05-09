#!/usr/bin/env python3
"""tome_bridge.py - Bridge research:tome tasks to /tome:research.

John-117 (running in OpenClaw) cannot directly invoke the tome
plugin because tome lives in Alex's Claude Code installation, not
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
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("tome_bridge")

DEFAULT_BASE = os.environ.get("WORKFLOWS_BASE", "http://localhost:5678")
DEFAULT_CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_TIMEOUT = int(os.environ.get("TOME_BRIDGE_TIMEOUT", "1800"))

_TOME_PATTERNS = (
    re.compile(r"^research:tome:\s*(?P<topic>.+)$", re.IGNORECASE),
    re.compile(r"^\[research:tome\]\s*(?P<topic>.+)$", re.IGNORECASE),
    re.compile(r"^research via tome:\s*(?P<topic>.+)$", re.IGNORECASE),
)


def extract_topic(description: str) -> Optional[str]:
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


def http_json(method: str, url: str, payload: Optional[dict] = None) -> dict:
    """Minimal stdlib JSON HTTP helper (avoids httpx dependency)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
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
    cmd = [claude_bin, "--print", f"/tome:research {topic}"]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
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
    except urllib.error.HTTPError as exc:
        logger.warning("tome bridge: claim failed for %s: %s", task_id, exc)
        return True

    rc, output = run_tome_research(claude_bin, topic, timeout)
    status = "completed" if rc == 0 else "failed"
    body = output[:8000] if output else f"tome /research returned exit code {rc}"

    try:
        complete_task(base, task_id, body, status)
        logger.info("tome bridge: task %s -> %s", task_id, status)
    except urllib.error.HTTPError as exc:
        logger.error("tome bridge: complete_task failed for %s: %s", task_id, exc)
    return True


def run_once(base: str, claude_bin: str, timeout: int, dry_run: bool) -> int:
    try:
        tasks = list_pending_tasks(base)
    except urllib.error.URLError as exc:
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
        rc = run_once(args.base_url, args.claude_bin, args.timeout, args.dry_run)
        if rc == 1:
            # Workflows down; back off but keep watching.
            time.sleep(min(args.interval * 2, 300))
        else:
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
