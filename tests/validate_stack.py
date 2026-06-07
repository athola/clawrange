"""
AI MSP Testbed — Structural and Config Validation

Validates project files, configs, and conventions without running services.
For live HTTP endpoint tests, use: ./scripts/test_all.sh

Run with: python3 tests/validate_stack.py
"""

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.message = ""

    def pass_(self, msg: str = ""):
        self.passed = True
        self.message = msg

    def fail_(self, msg: str = ""):
        self.passed = False
        self.message = msg


def check_required_files() -> TestResult:
    """Verify all required project files exist."""
    result = TestResult("Required Files")
    required = [
        "docker-compose.yml",
        ".env.example",
        ".gitignore",
        "Makefile",
        "openclaw/soul.md",
        "openclaw/config/openclaw.json",
        "deerflow/config.yaml",
        "workflows/app.py",
        "workflows/Dockerfile",
        "workflows/requirements.txt",
        "scripts/start.sh",
        "scripts/stop.sh",
        "scripts/reset.sh",
        "scripts/test_all.sh",
        "scripts/test_openclaw.sh",
        "scripts/test_workflows.sh",
        "scripts/test_deerflow.sh",
        "scripts/test_ollama.sh",
    ]
    missing = [f for f in required if not os.path.exists(os.path.join(PROJECT_ROOT, f))]
    if missing:
        result.fail_(f"Missing: {', '.join(missing)}")
    else:
        result.pass_(f"All {len(required)} required files present")
    return result


def check_scripts_executable() -> TestResult:
    """Verify shell scripts have execute permission."""
    result = TestResult("Script Permissions")
    scripts_dir = os.path.join(PROJECT_ROOT, "scripts")
    not_exec = []
    for f in os.listdir(scripts_dir):
        if f.endswith(".sh"):
            path = os.path.join(scripts_dir, f)
            if not os.access(path, os.X_OK):
                not_exec.append(f)
    if not_exec:
        result.fail_(f"Not executable: {', '.join(not_exec)}")
    else:
        result.pass_("All scripts executable")
    return result


def check_env_example() -> TestResult:
    """Verify .env.example has all required keys."""
    result = TestResult("Env Template")
    path = os.path.join(PROJECT_ROOT, ".env.example")
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        result.fail_(".env.example not found")
        return result
    required_keys = [
        "OPENROUTER_API_KEY",
        "OPENCLAW_GATEWAY_TOKEN",
        "WORKFLOWS_PORT",
        "TIMEZONE",
    ]
    missing = [k for k in required_keys if k not in content]
    if missing:
        result.fail_(f"Missing keys in .env.example: {', '.join(missing)}")
    else:
        result.pass_(f"All {len(required_keys)} required keys present")
    return result


def check_env_not_committed() -> TestResult:
    """Verify .env is gitignored."""
    result = TestResult("Env Security")
    gitignore_path = os.path.join(PROJECT_ROOT, ".gitignore")
    try:
        with open(gitignore_path) as f:
            content = f.read()
    except FileNotFoundError:
        result.fail_(".gitignore not found")
        return result
    lines = content.splitlines()
    if any(line.strip() in (".env", "/.env") for line in lines):
        result.pass_(".env is in .gitignore")
    else:
        result.fail_(".env is NOT gitignored — secrets may leak")
    return result


def check_n8n_workflows() -> TestResult:
    """Validate workflow service has required files and endpoints."""
    result = TestResult("Workflow Service")
    app_path = os.path.join(PROJECT_ROOT, "workflows", "app.py")
    errors = []
    if not os.path.exists(app_path):
        errors.append("workflows/app.py not found")
    else:
        with open(app_path) as f:
            source = f.read()
        for endpoint in [
            "/webhook/test",
            "/healthz",
            "/brain",
            "/task",
            "/tier",
        ]:
            if endpoint not in source:
                errors.append(f"missing endpoint: {endpoint}")
    dockerfile = os.path.join(PROJECT_ROOT, "workflows", "Dockerfile")
    if not os.path.exists(dockerfile):
        errors.append("workflows/Dockerfile not found")
    if errors:
        result.fail_("; ".join(errors))
    else:
        result.pass_("Workflow service has all required endpoints and Dockerfile")
    return result


def check_openclaw_config() -> TestResult:
    """Validate openclaw.json has required configuration."""
    result = TestResult("OpenClaw Config")
    path = os.path.join(PROJECT_ROOT, "openclaw", "config", "openclaw.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        result.fail_(str(e))
        return result
    errors = []
    # v2026.3+ schema: gateway, agents.defaults.model required
    if "gateway" not in data:
        errors.append("missing 'gateway' section")
    else:
        gw = data["gateway"]
        if "mode" not in gw:
            errors.append("missing 'gateway.mode'")
        if "bind" not in gw:
            errors.append("missing 'gateway.bind'")
        if "port" not in gw:
            errors.append("missing 'gateway.port'")
        if gw.get("auth", {}).get("mode") != "token":
            errors.append("gateway.auth.mode should be 'token'")
    if "agents" not in data:
        errors.append("missing 'agents' section")
    else:
        primary = (
            data.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary", "")
        )
        if not primary:
            errors.append("missing 'agents.defaults.model.primary'")
    if errors:
        result.fail_("; ".join(errors))
    else:
        result.pass_("Config valid with gateway (mode/bind/auth) and agent model")
    return result


def check_deerflow_data_sovereignty() -> TestResult:
    """Verify DeerFlow config routes all calls through OpenRouter."""
    result = TestResult("Data Sovereignty")
    path = os.path.join(PROJECT_ROOT, "deerflow", "config.yaml")
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        result.fail_("deerflow/config.yaml not found")
        return result
    # Only check non-comment lines for blocked endpoints
    config_lines = [line for line in lines if not line.strip().startswith("#")]
    config_text = " ".join(config_lines).lower()
    blocklist = ["doubao", "volcengine", "bytedance", "deepseek.com"]
    violations = [endpoint for endpoint in blocklist if endpoint in config_text]
    if violations:
        result.fail_(
            f"Direct endpoints found: {', '.join(violations)} — must route through OpenRouter"
        )
        return result
    # Check that base_url values specifically contain openrouter.ai
    base_urls = [line.strip() for line in config_lines if "base_url:" in line]
    if not base_urls:
        result.fail_("No base_url entries found — all models must specify a base_url")
    elif all("openrouter.ai" in url for url in base_urls):
        result.pass_("All model base_url entries route through OpenRouter")
    else:
        non_or = [url for url in base_urls if "openrouter.ai" not in url]
        result.fail_(f"Non-OpenRouter base_url found: {', '.join(non_or)}")
    return result


def check_soul_md() -> TestResult:
    """Verify soul.md has required persona sections."""
    result = TestResult("Soul.md Persona")
    path = os.path.join(PROJECT_ROOT, "openclaw", "soul.md")
    try:
        with open(path) as f:
            content = f.read().lower()
    except FileNotFoundError:
        result.fail_("openclaw/soul.md not found")
        return result
    required = ["john-117", "executive assistant", "clawrange"]
    missing = [r for r in required if r not in content]
    if missing:
        result.fail_(f"Missing references: {', '.join(missing)}")
    else:
        result.pass_("Persona includes identity, role, and infrastructure")
    return result


def main():
    print("=" * 50)
    print(" CLAWRANGE — CONFIG VALIDATION")
    print("=" * 50)
    print()

    tests = [
        check_required_files,
        check_scripts_executable,
        check_env_example,
        check_env_not_committed,
        check_n8n_workflows,
        check_openclaw_config,
        check_deerflow_data_sovereignty,
        check_soul_md,
    ]

    results: list[TestResult] = []
    for test_fn in tests:
        r = test_fn()
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.message}")

    passed = sum(1 for r in results if r.passed)
    total = len(results)

    print()
    print("=" * 50)
    print(f"  {passed}/{total} checks passed")
    print("=" * 50)

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
