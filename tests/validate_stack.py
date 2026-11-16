"""
AI MSP Testbed — Structural and Config Validation

Validates project files, configs, and conventions without running services.
For live HTTP endpoint tests, use: ./scripts/test_all.sh

Run with: python3 tests/validate_stack.py
"""

import json
import os
import re
import sys
from typing import Any

import yaml

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


REQUIRED_FILES = (
    "docker-compose.yml",
    ".env.example",
    ".gitignore",
    "Makefile",
    "openclaw/soul.template.md",
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
)


def check_required_files() -> TestResult:
    """Verify all required project files exist."""
    result = TestResult("Required Files")
    missing = [
        f for f in REQUIRED_FILES if not os.path.exists(os.path.join(PROJECT_ROOT, f))
    ]
    if missing:
        result.fail_(f"Missing: {', '.join(missing)}")
    else:
        result.pass_(f"All {len(REQUIRED_FILES)} required files present")
    return result


def check_scripts_executable() -> TestResult:
    """Verify shell scripts have execute permission.

    A .sh file without a shebang is a library meant to be sourced
    (e.g. workflows_auth.sh), so it is exempt.
    """
    result = TestResult("Script Permissions")
    scripts_dir = os.path.join(PROJECT_ROOT, "scripts")
    not_exec = []
    for f in os.listdir(scripts_dir):
        if f.endswith(".sh"):
            path = os.path.join(scripts_dir, f)
            with open(path, "rb") as fh:
                if fh.read(2) != b"#!":
                    continue
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
        "PROXY_AUTH_TOKEN",
        "SCHEDULER_TZ",
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


def check_workflow_service() -> TestResult:
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
    # v2026.8+ schema: gateway, agents.defaults.model required
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
            f"Direct endpoints found: {', '.join(violations)} "
            f"— must route through OpenRouter"
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


def check_soul_template() -> TestResult:
    """Verify the shipped persona template is present and generic.

    soul.md is rendered per operator (and gitignored); the committed source is
    the identity-free soul.template.md, filled from a profile's structured
    fields. Validate the template form, not a rendered persona.
    """
    result = TestResult("Soul Template")
    path = os.path.join(PROJECT_ROOT, "openclaw", "soul.template.md")
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        result.fail_("openclaw/soul.template.md not found")
        return result
    if "{{name}}" not in content:
        result.fail_("soul.template.md missing {{name}} placeholder")
    elif "clawrange" not in content.lower():
        result.fail_("soul.template.md missing ClawRange infrastructure reference")
    else:
        result.pass_("Generic persona template present with placeholders")
    return result


_PROFILE_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _profile_env_refs(value: Any) -> set[str]:
    """Collect ``${VAR}`` names from a parsed profile (comments excluded)."""
    if isinstance(value, str):
        return set(_PROFILE_ENV_RE.findall(value))
    if isinstance(value, dict):
        return set().union(*(_profile_env_refs(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_profile_env_refs(v) for v in value))
    return set()


def check_profile_env_passthrough() -> TestResult:
    """Every ``${VAR}`` a shipped profile uses reaches the workflows container.

    Compose passes variables to workflows one by one (an ``env_file`` would
    hand every secret in .env to the container), so a profile variable with
    no environment line silently resolves to "" inside the container.
    """
    result = TestResult("Profile Env Passthrough")
    try:
        with open(os.path.join(PROJECT_ROOT, "docker-compose.yml")) as f:
            compose = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        result.fail_(f"docker-compose.yml unreadable: {e}")
        return result
    service = (compose.get("services") or {}).get("workflows")
    if not isinstance(service, dict):
        result.fail_("docker-compose.yml has no workflows service")
        return result
    env = service.get("environment") or []
    if isinstance(env, dict):
        passed = set(env)
    else:
        passed = {str(item).split("=", 1)[0] for item in env}

    missing: list[str] = []
    profiles = os.path.join(PROJECT_ROOT, "config", "profiles")
    names = sorted(os.listdir(profiles)) if os.path.isdir(profiles) else []
    for name in names:
        path = os.path.join(profiles, name, "profile.yaml")
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            refs = _profile_env_refs(yaml.safe_load(f))
        missing.extend(f"{var} ({name})" for var in sorted(refs - passed))
    if missing:
        result.fail_("Not in the workflows service environment: " + ", ".join(missing))
    else:
        result.pass_(f"All profile variables reach workflows ({len(names)} profiles)")
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
        check_workflow_service,
        check_openclaw_config,
        check_deerflow_data_sovereignty,
        check_soul_template,
        check_profile_env_passthrough,
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
