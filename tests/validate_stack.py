"""
AI MSP Testbed — Stack Validation Suite

Run with: python3 tests/validate_stack.py
Requires: pip install requests (or use the shell scripts instead)
"""

import json
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

OPENCLAW_PORT = os.getenv("OPENCLAW_PORT", "3000")
N8N_PORT = os.getenv("N8N_PORT", "5678")
DEERFLOW_PORT = os.getenv("DEERFLOW_PORT", "2026")
GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "testbed-token-change-me")

OPENCLAW_BASE = f"http://localhost:{OPENCLAW_PORT}"
N8N_BASE = f"http://localhost:{N8N_PORT}"
DEERFLOW_BASE = f"http://localhost:{DEERFLOW_PORT}"


def http_get(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET request, returns (status_code, body)."""
    try:
        req = Request(url)
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except URLError as e:
        return 0, str(e)
    except Exception as e:
        return 0, str(e)


def http_post(url: str, data: dict, headers: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    """POST JSON request, returns (status_code, body)."""
    try:
        body = json.dumps(data).encode()
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = Request(url, data=body, headers=hdrs, method="POST")
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except URLError as e:
        return 0, str(e)
    except Exception as e:
        return 0, str(e)


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.message = ""
        self.response = ""

    def pass_(self, msg: str = "", response: str = ""):
        self.passed = True
        self.message = msg
        self.response = response

    def fail_(self, msg: str = "", response: str = ""):
        self.passed = False
        self.message = msg
        self.response = response


def test_stack_health() -> TestResult:
    result = TestResult("Stack Health")

    status_oc, _ = http_get(f"{OPENCLAW_BASE}/healthz")
    status_n8n, _ = http_get(f"{N8N_BASE}/healthz")

    if status_oc == 200 and status_n8n == 200:
        result.pass_(f"OpenClaw={status_oc}, n8n={status_n8n}")
    else:
        result.fail_(f"OpenClaw={status_oc}, n8n={status_n8n}")

    return result


def test_openclaw_response() -> TestResult:
    result = TestResult("OpenClaw Response")

    status, body = http_post(
        f"{OPENCLAW_BASE}/v1/chat/completions",
        {
            "model": "openclaw:main",
            "messages": [{"role": "user", "content": "What financing options does Longview Home Center offer?"}],
        },
        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
    )

    keywords = ["fha", "va", "conventional", "in-house", "financing"]
    body_lower = body.lower()
    if any(kw in body_lower for kw in keywords):
        result.pass_("Contains financing keywords", body[:200])
    else:
        result.fail_("No financing keywords in response", body[:300])

    return result


def test_n8n_roundtrip() -> TestResult:
    result = TestResult("n8n Roundtrip")

    status, body = http_post(
        f"{N8N_BASE}/webhook-test/test",
        {"message": "ping", "source": "python-test"},
    )

    if "received" in body.lower():
        result.pass_("Webhook echoed payload", body[:200])
    else:
        result.fail_("No 'received' in response", body[:300])

    return result


def test_lead_lookup() -> TestResult:
    result = TestResult("Lead Lookup")

    status, body = http_post(
        f"{N8N_BASE}/webhook-test/lead-status",
        {"name": "John Smith", "phone": "903-555-0100"},
    )

    if "john smith" in body.lower():
        result.pass_("Found John Smith", body[:200])
    else:
        result.fail_("John Smith not in response", body[:300])

    return result


def test_morning_briefing() -> TestResult:
    result = TestResult("Morning Briefing")

    status, body = http_get(f"{N8N_BASE}/api/v1/workflows")
    if "morning" in body.lower():
        result.pass_("Morning Briefing workflow exists in n8n")
    else:
        result.fail_("Morning Briefing workflow not found")

    return result


def test_deerflow_research() -> TestResult:
    result = TestResult("DeerFlow Research")

    status, _ = http_get(f"{DEERFLOW_BASE}/health")
    if status != 200:
        result.message = "DeerFlow not running (optional)"
        result.response = "SKIP"
        return result

    status, body = http_post(
        f"{DEERFLOW_BASE}/api/langgraph/runs",
        {
            "input": {
                "messages": [
                    {"role": "user", "content": "Top 3 manufactured home lenders in Texas for FHA loans"}
                ]
            },
            "config": {},
        },
        timeout=120,
    )

    keywords = ["lender", "fha", "texas", "mortgage", "manufactured"]
    body_lower = body.lower()
    if any(kw in body_lower for kw in keywords):
        result.pass_("Research returned relevant results", body[:200])
    else:
        result.fail_("No relevant keywords in response", body[:300])

    return result


def main():
    print("=" * 50)
    print(" AI MSP TESTBED — PYTHON VALIDATION SUITE")
    print("=" * 50)
    print()

    tests = [
        test_stack_health,
        test_openclaw_response,
        test_n8n_roundtrip,
        test_lead_lookup,
        test_morning_briefing,
        test_deerflow_research,
    ]

    results: list[TestResult] = []
    for test_fn in tests:
        print(f"Running: {test_fn.__name__}...")
        r = test_fn()
        results.append(r)
        status = "PASS" if r.passed else ("SKIP" if r.response == "SKIP" else "FAIL")
        print(f"  [{status}] {r.name}: {r.message}")
        if r.response and r.response != "SKIP":
            print(f"  Response: {r.response}")
        print()

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)

    print("=" * 50)
    print(" STACK VALIDATION REPORT")
    print("=" * 50)
    for i, r in enumerate(results, 1):
        status = "PASS" if r.passed else ("SKIP" if r.response == "SKIP" else "FAIL")
        print(f"  Test {i} — {r.name:20s} [{status}]")
    print("=" * 50)
    print(f"  OVERALL: {passed}/{total} tests passed")
    print("=" * 50)

    sys.exit(0 if passed >= 5 else 1)


if __name__ == "__main__":
    main()
