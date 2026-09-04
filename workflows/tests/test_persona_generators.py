"""Tests for persona-related generators: reflect loop and profile seeding."""

from unittest.mock import AsyncMock, patch

import pytest

import generators
from brain_db import BrainDB


@pytest.fixture
def db(tmp_path):
    d = BrainDB(str(tmp_path / "b.db"))
    d.init_db()
    return d


class TestPersonaReflectGenerator:
    @pytest.mark.asyncio
    async def test_none_response_queues_nothing(self, db):
        with patch("llm_proxy._llm_call", new_callable=AsyncMock, return_value="none"):
            await generators.persona_reflect_generator(db, profile_name="cos")
        assert db.list_learnings(profile="cos") == []

    @pytest.mark.asyncio
    async def test_none_with_period_and_case_queues_nothing(self, db):
        """Regression for 3920d1d: models answer 'None.' as often as 'none'."""
        with patch("llm_proxy._llm_call", new_callable=AsyncMock, return_value="None."):
            await generators.persona_reflect_generator(db, profile_name="cos")
        assert db.list_learnings(profile="cos") == []

    @pytest.mark.asyncio
    async def test_empty_response_queues_nothing(self, db):
        with patch("llm_proxy._llm_call", new_callable=AsyncMock, return_value=None):
            await generators.persona_reflect_generator(db, profile_name="cos")
        assert db.list_learnings(profile="cos") == []

    @pytest.mark.asyncio
    async def test_real_suggestion_queues_reflect_proposal(self, db):
        with patch(
            "llm_proxy._llm_call",
            new_callable=AsyncMock,
            return_value="Lead with the recommendation.",
        ):
            await generators.persona_reflect_generator(db, profile_name="cos")
        rows = db.list_learnings(profile="cos", status="pending")
        assert len(rows) == 1
        assert rows[0]["source"] == "reflect"
        assert rows[0]["content"] == "Lead with the recommendation."


class TestSeedFromProfileOverlayGuard:
    def test_malformed_learned_yaml_does_not_crash_boot(
        self, db, tmp_path, monkeypatch
    ):
        """seed_from_profile must survive a broken learned.yaml (boot guard)."""
        import tenant_profile

        profdir = tmp_path / "cos"
        profdir.mkdir()
        (profdir / "learned.yaml").write_text("learned: [unclosed")
        monkeypatch.setattr(tenant_profile, "default_profiles_dir", lambda: tmp_path)
        profile = tenant_profile.Profile(
            name="cos", raw={"profile": "cos", "assistant": {"name": "Max"}}
        )
        out = generators.seed_from_profile(db, profile)
        assert out == []
        assert db.list_learnings(profile="cos") == []

    def test_valid_learned_yaml_seeds_approved_learnings(
        self, db, tmp_path, monkeypatch
    ):
        import tenant_profile

        profdir = tmp_path / "cos"
        profdir.mkdir()
        (profdir / "learned.yaml").write_text(
            "learned:\n"
            "  - kind: persona\n"
            "    target: Tone\n"
            "    content: Be terse.\n"
            "    source: seed\n"
        )
        monkeypatch.setattr(tenant_profile, "default_profiles_dir", lambda: tmp_path)
        profile = tenant_profile.Profile(
            name="cos", raw={"profile": "cos", "assistant": {"name": "Max"}}
        )
        generators.seed_from_profile(db, profile)
        rows = db.list_learnings(profile="cos", status="approved")
        assert [r["content"] for r in rows] == ["Be terse."]
