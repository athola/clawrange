"""
Unit tests for validate_stack.py check functions.

Tests the edge cases fixed in the review:
- Gitignore line-based matching (not substring)
- Data sovereignty base_url field checking
- Required files expanded list
- Error handling for missing files

Run with: pytest tests/test_validate_stack.py -v
"""

import os
import json
import sys
import pytest

# Add project root so validate_stack can find PROJECT_ROOT correctly
sys.path.insert(0, os.path.dirname(__file__))

import validate_stack


@pytest.fixture
def project_tree(tmp_path):
    """Create a minimal project tree for testing."""
    # Override PROJECT_ROOT for the duration of each test
    original = validate_stack.PROJECT_ROOT
    validate_stack.PROJECT_ROOT = str(tmp_path)
    yield tmp_path
    validate_stack.PROJECT_ROOT = original


def _create_minimal_project(root):
    """Scaffold the minimum files so check_required_files passes."""
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
    for f in required:
        path = root / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        if f.endswith(".sh"):
            path.chmod(0o755)


# ─── check_env_not_committed ──────────────────────────────────────


class TestCheckEnvNotCommitted:
    """Tests for .env gitignore validation."""

    def test_pass_when_gitignore_has_bare_dotenv(self, project_tree):
        """GIVEN .gitignore contains '.env' on its own line
        THEN the check should pass."""
        (project_tree / ".gitignore").write_text(".env\n*.pyc\n")
        result = validate_stack.check_env_not_committed()
        assert result.passed is True

    def test_pass_when_gitignore_has_slash_dotenv(self, project_tree):
        """GIVEN .gitignore contains '/.env'
        THEN the check should pass."""
        (project_tree / ".gitignore").write_text("/.env\nnode_modules/\n")
        result = validate_stack.check_env_not_committed()
        assert result.passed is True

    def test_fail_when_gitignore_only_has_env_example(self, project_tree):
        """GIVEN .gitignore only contains '.env.example' (not '.env')
        THEN the check should fail — substring match is insufficient."""
        (project_tree / ".gitignore").write_text(".env.example\n*.log\n")
        result = validate_stack.check_env_not_committed()
        assert result.passed is False
        assert "NOT gitignored" in result.message

    def test_fail_when_gitignore_missing(self, project_tree):
        """GIVEN .gitignore does not exist
        THEN the check should fail gracefully (not crash)."""
        result = validate_stack.check_env_not_committed()
        assert result.passed is False
        assert "not found" in result.message

    def test_fail_when_gitignore_empty(self, project_tree):
        """GIVEN .gitignore is empty
        THEN the check should fail."""
        (project_tree / ".gitignore").write_text("")
        result = validate_stack.check_env_not_committed()
        assert result.passed is False


# ─── check_deerflow_data_sovereignty ──────────────────────────────


class TestCheckDataSovereignty:
    """Tests for DeerFlow config data sovereignty validation."""

    def test_pass_with_all_openrouter_urls(self, project_tree):
        """GIVEN all base_url entries point to openrouter.ai
        THEN the check should pass."""
        config_dir = project_tree / "deerflow"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            "models:\n"
            "  - name: researcher\n"
            "    base_url: https://openrouter.ai/api/v1\n"
            "  - name: writer\n"
            "    base_url: https://openrouter.ai/api/v1\n"
        )
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is True
        assert "OpenRouter" in result.message

    def test_fail_with_bytedance_endpoint(self, project_tree):
        """GIVEN a base_url pointing to bytedance infrastructure
        THEN the check should fail with a sovereignty violation."""
        config_dir = project_tree / "deerflow"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            "models:\n"
            "  - name: researcher\n"
            "    base_url: https://api.bytedance.com/v1\n"
        )
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is False
        assert "bytedance" in result.message

    def test_fail_with_doubao_endpoint(self, project_tree):
        """GIVEN a base_url referencing doubao
        THEN the check should fail."""
        config_dir = project_tree / "deerflow"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            "models:\n"
            "  - name: writer\n"
            "    base_url: https://doubao.volcengine.com/api/v1\n"
        )
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is False

    def test_blocklist_in_comments_ignored(self, project_tree):
        """GIVEN blocklist terms appear only in comments
        THEN the check should pass (comments are stripped)."""
        config_dir = project_tree / "deerflow"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            "# Never use bytedance or doubao endpoints\n"
            "models:\n"
            "  - name: researcher\n"
            "    base_url: https://openrouter.ai/api/v1\n"
        )
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is True

    def test_fail_when_no_base_url_entries(self, project_tree):
        """GIVEN config has no base_url fields at all
        THEN the check should fail."""
        config_dir = project_tree / "deerflow"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            "models:\n  - name: researcher\n    model: deepseek/deepseek-chat\n"
        )
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is False
        assert "No base_url" in result.message

    def test_fail_when_config_missing(self, project_tree):
        """GIVEN deerflow/config.yaml does not exist
        THEN the check should fail gracefully."""
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is False
        assert "not found" in result.message

    def test_fail_with_mixed_urls(self, project_tree):
        """GIVEN one base_url is openrouter and another is not
        THEN the check should fail."""
        config_dir = project_tree / "deerflow"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            "models:\n"
            "  - name: researcher\n"
            "    base_url: https://openrouter.ai/api/v1\n"
            "  - name: writer\n"
            "    base_url: https://api.deepseek.com/v1\n"
        )
        result = validate_stack.check_deerflow_data_sovereignty()
        assert result.passed is False


# ─── check_required_files ─────────────────────────────────────────


class TestCheckRequiredFiles:
    """Tests for required file existence validation."""

    def test_pass_with_all_files(self, project_tree):
        """GIVEN all required files exist
        THEN the check should pass."""
        _create_minimal_project(project_tree)
        result = validate_stack.check_required_files()
        assert result.passed is True
        assert "18 required files" in result.message

    def test_fail_with_missing_test_script(self, project_tree):
        """GIVEN test_ollama.sh is missing
        THEN the check should report it."""
        _create_minimal_project(project_tree)
        (project_tree / "scripts" / "test_ollama.sh").unlink()
        result = validate_stack.check_required_files()
        assert result.passed is False
        assert "test_ollama.sh" in result.message

    def test_fail_with_missing_reset_script(self, project_tree):
        """GIVEN reset.sh is missing
        THEN the check should report it."""
        _create_minimal_project(project_tree)
        (project_tree / "scripts" / "reset.sh").unlink()
        result = validate_stack.check_required_files()
        assert result.passed is False
        assert "reset.sh" in result.message


# ─── check_openclaw_config ────────────────────────────────────────


class TestCheckOpenclawConfig:
    """Tests for OpenClaw config validation."""

    def test_pass_with_valid_config(self, project_tree):
        """GIVEN a valid openclaw.json with v2026.3 schema
        THEN the check should pass."""
        config_dir = project_tree / "openclaw" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text(
            json.dumps(
                {
                    "gateway": {
                        "port": 18789,
                        "mode": "local",
                        "bind": "lan",
                        "auth": {"mode": "token"},
                    },
                    "agents": {
                        "defaults": {
                            "model": {
                                "primary": "openrouter/anthropic/claude-haiku-4-5"
                            }
                        }
                    },
                }
            )
        )
        result = validate_stack.check_openclaw_config()
        assert result.passed is True

    def test_fail_with_missing_gateway_auth(self, project_tree):
        """GIVEN gateway.auth.mode is not token
        THEN the check should fail."""
        config_dir = project_tree / "openclaw" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text(
            json.dumps(
                {
                    "gateway": {"port": 18789, "mode": "local", "bind": "lan"},
                    "agents": {"defaults": {"model": {"primary": "test"}}},
                }
            )
        )
        result = validate_stack.check_openclaw_config()
        assert result.passed is False
        assert "token" in result.message

    def test_fail_with_missing_sections(self, project_tree):
        """GIVEN openclaw.json is missing required sections
        THEN the check should fail listing each missing section."""
        config_dir = project_tree / "openclaw" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text(json.dumps({}))
        result = validate_stack.check_openclaw_config()
        assert result.passed is False
        assert "gateway" in result.message
        assert "agents" in result.message

    def test_fail_with_invalid_json(self, project_tree):
        """GIVEN openclaw.json contains invalid JSON
        THEN the check should fail gracefully."""
        config_dir = project_tree / "openclaw" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text("{invalid json")
        result = validate_stack.check_openclaw_config()
        assert result.passed is False


# ─── check_env_example ────────────────────────────────────────────


class TestCheckEnvExample:
    """Tests for .env.example template validation."""

    def test_pass_with_all_keys(self, project_tree):
        """GIVEN .env.example contains all required keys
        THEN the check should pass."""
        (project_tree / ".env.example").write_text(
            "OPENROUTER_API_KEY=test\n"
            "OPENCLAW_GATEWAY_TOKEN=test\n"
            "WORKFLOWS_PORT=5678\n"
            "TIMEZONE=America/Chicago\n"
        )
        result = validate_stack.check_env_example()
        assert result.passed is True

    def test_fail_with_missing_key(self, project_tree):
        """GIVEN .env.example is missing TIMEZONE
        THEN the check should fail."""
        (project_tree / ".env.example").write_text(
            "OPENROUTER_API_KEY=test\n"
            "OPENCLAW_GATEWAY_TOKEN=test\n"
            "WORKFLOWS_PORT=5678\n"
        )
        result = validate_stack.check_env_example()
        assert result.passed is False
        assert "TIMEZONE" in result.message

    def test_fail_when_file_missing(self, project_tree):
        """GIVEN .env.example does not exist
        THEN the check should fail gracefully."""
        result = validate_stack.check_env_example()
        assert result.passed is False
        assert "not found" in result.message


# ─── check_scripts_executable ─────────────────────────────────────


class TestCheckScriptsExecutable:
    """Tests for shell script execute permission validation."""

    def test_pass_when_all_scripts_executable(self, project_tree):
        """GIVEN all .sh files in scripts/ have execute permission
        THEN the check should pass."""
        scripts_dir = project_tree / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for name in ["start.sh", "stop.sh", "test_all.sh"]:
            s = scripts_dir / name
            s.write_text("#!/usr/bin/env bash\n")
            s.chmod(0o755)
        result = validate_stack.check_scripts_executable()
        assert result.passed is True
        assert "executable" in result.message.lower()

    def test_fail_when_script_not_executable(self, project_tree):
        """GIVEN one .sh file lacks execute permission
        THEN the check should fail listing that file."""
        scripts_dir = project_tree / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        good = scripts_dir / "start.sh"
        good.write_text("#!/usr/bin/env bash\n")
        good.chmod(0o755)
        bad = scripts_dir / "stop.sh"
        bad.write_text("#!/usr/bin/env bash\n")
        bad.chmod(0o644)
        result = validate_stack.check_scripts_executable()
        assert result.passed is False
        assert "stop.sh" in result.message

    def test_ignores_non_sh_files(self, project_tree):
        """GIVEN scripts/ contains a non-.sh file without execute permission
        THEN the check should still pass (only .sh files matter)."""
        scripts_dir = project_tree / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        sh = scripts_dir / "start.sh"
        sh.write_text("#!/usr/bin/env bash\n")
        sh.chmod(0o755)
        txt = scripts_dir / "README.txt"
        txt.write_text("not a script")
        txt.chmod(0o644)
        result = validate_stack.check_scripts_executable()
        assert result.passed is True


# ─── check_n8n_workflows (now checks workflow service) ───────────


class TestCheckN8nWorkflows:
    """Tests for workflow service validation."""

    def test_pass_with_valid_service(self, project_tree):
        """GIVEN workflows/app.py with all required endpoints and Dockerfile
        THEN the check should pass."""
        wf_dir = project_tree / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "app.py").write_text(
            '"/webhook/test"\n"/webhook/lead-status"\n'
            '"/webhook/morning-briefing"\n"/healthz"\n'
        )
        (wf_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
        result = validate_stack.check_n8n_workflows()
        assert result.passed is True

    def test_fail_with_missing_endpoint(self, project_tree):
        """GIVEN app.py is missing an endpoint
        THEN the check should fail."""
        wf_dir = project_tree / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "app.py").write_text('"/webhook/test"\n"/healthz"\n')
        (wf_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
        result = validate_stack.check_n8n_workflows()
        assert result.passed is False
        assert "lead-status" in result.message

    def test_fail_with_missing_app(self, project_tree):
        """GIVEN workflows/app.py does not exist
        THEN the check should fail."""
        wf_dir = project_tree / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
        result = validate_stack.check_n8n_workflows()
        assert result.passed is False
        assert "app.py" in result.message

    def test_fail_with_missing_dockerfile(self, project_tree):
        """GIVEN workflows/Dockerfile does not exist
        THEN the check should fail."""
        wf_dir = project_tree / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "app.py").write_text(
            '"/webhook/test"\n"/webhook/lead-status"\n'
            '"/webhook/morning-briefing"\n"/healthz"\n'
        )
        result = validate_stack.check_n8n_workflows()
        assert result.passed is False
        assert "Dockerfile" in result.message


# ─── check_soul_md ────────────────────────────────────────────────


class TestCheckSoulMd:
    """Tests for soul.md persona validation."""

    def test_pass_with_all_required_references(self, project_tree):
        """GIVEN soul.md mentions all required persona references
        THEN the check should pass."""
        soul_dir = project_tree / "openclaw"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "soul.md").write_text(
            "You are the AI assistant for Longview Home Center.\n"
            "Located in Jessup, PA. We sell Titanium brand homes.\n"
            "We offer FHA and VA financing options.\n"
        )
        result = validate_stack.check_soul_md()
        assert result.passed is True
        assert "Persona" in result.message or "dealership" in result.message

    def test_fail_when_missing_brand(self, project_tree):
        """GIVEN soul.md is missing the 'titanium' reference
        THEN the check should fail."""
        soul_dir = project_tree / "openclaw"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "soul.md").write_text(
            "You are the AI for Longview Home Center in Jessup.\n"
            "We offer FHA and VA loans.\n"
        )
        result = validate_stack.check_soul_md()
        assert result.passed is False
        assert "titanium" in result.message

    def test_fail_when_file_missing(self, project_tree):
        """GIVEN soul.md does not exist
        THEN the check should fail gracefully."""
        result = validate_stack.check_soul_md()
        assert result.passed is False
        assert "not found" in result.message

    def test_case_insensitive_matching(self, project_tree):
        """GIVEN soul.md uses uppercase for brand names
        THEN the check should still pass (case-insensitive)."""
        soul_dir = project_tree / "openclaw"
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "soul.md").write_text(
            "LONGVIEW HOME CENTER in JESSUP sells TITANIUM homes.\n"
            "Financing: FHA and VA approved.\n"
        )
        result = validate_stack.check_soul_md()
        assert result.passed is True
