"""The core stays generic: tenant/domain specifics live in profile YAML.

Other operators build their own assistant by writing a profile; nothing
about this repo's example tenant (its timezone, its LLM providers, its
hardware, its eBay categories) may be baked into the core modules.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

import deals
import rundown
import tenant_profile
from tenant_profile import ProfileError

ROOT = Path(__file__).resolve().parents[2]
# Every shipped Python module outside the test suites.
CORE = sorted(
    str(p.relative_to(ROOT))
    for base in ("workflows", "scripts")
    for p in (ROOT / base).rglob("*.py")
    if "tests" not in p.parts and "__pycache__" not in p.parts
)
# Values that belong to one tenant's profile, never to core code.
TENANT_TOKENS = [
    "america/chicago",
    # Provider env names (OPENROUTER_API_KEY) are framework plumbing; how
    # one tenant uses its tiers is profile text.
    "openrouter free",
    "z.ai glm,",
    "strix halo",
    "poweredge",
    "0x0ram",
    "27386",
    "11211",
    "athola",
    "laptop",
    "homelab owner",
    # Operator identity and the example persona's lore.
    "john-117",
    "halopedia",
    "master chief",
    # One tenant's communities and curated lists.
    "claudeai",
    "localllama",
    "awesome-claude",
    "texas",
    # One tenant's income-testbed strategy.
    "usdc",
    "20-week",
    "$50",
]
# Shipped non-Python surfaces OpenClaw, Docker and operators read.
TEXT_SURFACES = [
    "openclaw/HEARTBEAT.md",
    "openclaw/soul-ops.md",
    "openclaw/soul.template.md",
    "openclaw/identity.template.md",
    "openclaw/config/openclaw.json",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    ".env.example",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "scripts").glob("*.sh")),
]
TEXT_TOKENS = [r"\balex\b", r"america/chicago", r"\btexas\b", r"athola"]
# Shipped example profiles other operators copy. `marketing` is excluded on
# purpose: it is the maintainer's own profile, and tenant data belongs there.
EXAMPLE_PROFILES = sorted(
    str(p.relative_to(ROOT))
    for p in (ROOT / "config" / "profiles").glob("*/*.yaml")
    if p.parent.name != "marketing"
)


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


@pytest.mark.parametrize("module", CORE)
def test_core_string_literals_carry_no_tenant_config(module):
    path = ROOT / module
    hits = [
        (tok, lit[:60])
        for lit in _string_literals(path)
        for tok in TENANT_TOKENS
        if tok in lit.lower()
    ]
    assert hits == [], f"{module} hard-codes tenant config: {hits}"


def test_core_has_no_operator_name_in_literals():
    """Prompts and labels name the operator via config, never by name."""
    import re

    hits = [
        (m, lit[:60])
        for m in CORE
        for lit in _string_literals(ROOT / m)
        if re.search(r"\balex\b", lit, re.IGNORECASE)
    ]
    assert hits == []


@pytest.mark.parametrize("surface", TEXT_SURFACES + EXAMPLE_PROFILES)
def test_shipped_text_surfaces_carry_no_operator_identity(surface):
    import re

    path = ROOT / surface
    if not path.exists():
        pytest.skip(f"{surface} not shipped")
    text = path.read_text()
    hits = [t for t in TEXT_TOKENS if re.search(t, text, re.IGNORECASE)]
    assert hits == [], f"{surface} carries operator identity: {hits}"


class TestOperatorTimezone:
    def test_env_overrides_profile(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_TZ", "Europe/Berlin")
        assert tenant_profile.operator_timezone() == "Europe/Berlin"

    def test_profile_value_then_utc(self, monkeypatch):
        monkeypatch.delenv("SCHEDULER_TZ", raising=False)
        marketing = tenant_profile.load_profile("marketing", env={})
        assert tenant_profile.operator_timezone(marketing) == "America/Chicago"
        starter = tenant_profile.load_profile("starter", env={})
        assert tenant_profile.operator_timezone(starter) == "UTC"

    def test_unknown_zone_fails_at_load(self, tmp_path):
        _write(tmp_path, "assistant: {name: A, timezone: Mars/Olympus}\n")
        with pytest.raises(ProfileError, match="timezone"):
            tenant_profile.load_profile("t", profiles_dir=tmp_path, env={})


def _write(root: Path, body: str) -> None:
    (root / "t").mkdir()
    (root / "t" / "profile.yaml").write_text("profile: t\n" + body)


class TestPolicyValidation:
    @pytest.mark.parametrize(
        "block,match",
        [
            ("homelab_deals: {too_good_share: 2}", "too_good_share"),
            ("homelab_deals: {max_per_target: 0}", "max_per_target"),
            ("homelab_deals: {red_flags: {x: nope}}", "red_flags"),
            (
                "homelab_deals: {sources: {ebay: {categories: {gpu: abc}}}}",
                "categories",
            ),
            ("homelab_deals: {advisor: {rules: nope}}", "advisor"),
            ("outreach: {venue_actions: {mastodon: reply}}", "unknown"),
            ("outreach: {banned_actions: [1, 2]}", "banned_actions"),
            ("outreach: {audience: [not, text]}", "audience"),
        ],
    )
    def test_bad_policy_fails_at_load(self, tmp_path, block, match):
        _write(tmp_path, "assistant: {name: A}\n" + block + "\n")
        with pytest.raises(ProfileError, match=match):
            tenant_profile.load_profile("t", profiles_dir=tmp_path, env={})


class TestDealPolicyFromProfile:
    BLOCK = {
        "too_good_share": 0.3,
        "red_flags": {"refurb: check warranty": ["refurbished"]},
        "part_categories": ["widget"],
        "sources": {"ebay": {"categories": {"widget": "123"}}},
        "targets": [
            {
                "name": "W",
                "query": "widget",
                "category": "widget",
                "max_price_great": 100,
            }
        ],
    }

    def test_block_policy_is_applied_to_targets(self):
        (t,) = deals.load_targets(self.BLOCK)
        assert (t.too_good_floor, t.ebay_category, t.part) == (30.0, "123", True)
        listing = deals.Listing("x", "1", "Widget, refurbished", "", 80.0)
        assert "refurb: check warranty" in deals.red_flags(listing, t)

    @pytest.mark.asyncio
    async def test_no_advisor_means_generic_prompt(self, monkeypatch):
        import llm_proxy

        seen = {}

        async def fake_call(prompt, max_tokens=0):
            seen["p"] = prompt
            return "ok"

        monkeypatch.setattr(llm_proxy, "_llm_call", fake_call)
        (t,) = deals.load_targets(self.BLOCK)
        d = deals.classify(deals.Listing("x", "1", "Widget", "", 50.0), t, None)
        assert d is not None
        deals.order_and_number([d])
        await deals.synthesize([d], "")
        assert deals.DEFAULT_AUDIENCE in seen["p"]
        # Live run 2026-11-14 lumped populated R730s in as "barebone".
        assert "lacks parts only when its FLAGS or its own title" in seen["p"]
        assert "Rules for this setup" not in seen["p"]
        await deals.synthesize([d], "", advisor={"audience": "a café", "rules": ["R1"]})
        assert "You advise a café." in seen["p"] and "- R1" in seen["p"]

    def test_report_title_and_goal_come_from_config(self):
        out = deals.render_report(
            [],
            [],
            [],
            None,
            datetime.now(UTC),
            goal="cheap widgets",
            title="Widget watch",
        )
        assert out.startswith("*Widget watch")
        assert "Goal: cheap widgets" in out


class TestOutreachPolicyFromProfile:
    EV = [
        rundown.Evidence(
            "hn", "Hacker News", "p", "T", "https://h.test/1", "HN · 1 pts", ref="E1"
        )
    ]

    @pytest.mark.asyncio
    async def test_venue_action_override_reaches_the_prompt(self, monkeypatch):
        seen = {}

        async def fake_call(prompt, max_tokens=0, **kw):
            seen["p"] = prompt
            return "ok"

        monkeypatch.setattr(rundown.llm_proxy, "_llm_call", fake_call)
        project = {
            "slug": "p",
            "owner": "o",
            "repo": "r",
            "topics": "[]",
            "search_terms": "[]",
            "posture": "",
        }
        await rundown.synthesize(
            [project], self.EV, {"venue_actions": {"hn": "read only, never post"}}
        )
        assert "Allowed: read only, never post" in seen["p"]

    def test_banned_action_phrases_drop_items(self):
        text = "ACTION ITEMS\n1. Comment on [E1] and mention the newsletter.\n"
        assert "Comment" in rundown.filter_actions(text, self.EV)
        out = rundown.filter_actions(text, self.EV, {"banned_actions": ["newsletter"]})
        assert "Comment" not in out


class TestMarketAndAudienceFromProfile:
    @pytest.mark.asyncio
    async def test_ebay_marketplace_currency_country(self, monkeypatch):
        import httpx

        import deal_sources

        monkeypatch.setenv("EBAY_CLIENT_ID", "id")
        monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
        seen: dict = {}

        def handler(request):
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "t"})
            seen["mkt"] = request.headers["X-EBAY-C-MARKETPLACE-ID"]
            seen["filter"] = request.url.params["filter"]
            return httpx.Response(200, json={"itemSummaries": []})

        (t,) = deals.load_targets(TestDealPolicyFromProfile.BLOCK)
        cfg = {"marketplace": "EBAY_DE", "currency": "EUR", "delivery_country": "DE"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await deal_sources.fetch_ebay(c, [t], pace=0, config=cfg)
        assert seen["mkt"] == "EBAY_DE"
        assert "priceCurrency:EUR" in seen["filter"]
        assert "deliveryCountry:DE" in seen["filter"]

    @pytest.mark.asyncio
    async def test_outreach_audience_and_web_venues(self, monkeypatch):
        prompts = []

        async def fake_call(prompt, max_tokens=0, **kw):
            prompts.append(prompt)
            return "- A | https://forum.test/a | fits"

        monkeypatch.setattr(rundown.llm_proxy, "_llm_call", fake_call)
        project = {
            "slug": "p",
            "owner": "o",
            "repo": "r",
            "topics": "[]",
            "search_terms": "[]",
            "posture": "",
        }
        policy = {"audience": "a bakery owner", "web_venues": "local food forums"}
        await rundown.fetch_web(project, policy)
        assert "local food forums" in prompts[-1]
        await rundown.synthesize([project], TestOutreachPolicyFromProfile.EV, policy)
        assert "brief for a bakery owner" in prompts[-1]


class TestIncomeReviewChecklist:
    def test_default_is_generic(self, monkeypatch):
        import generators

        monkeypatch.setattr(
            tenant_profile,
            "load_profile",
            lambda name=None: tenant_profile.Profile("x", {}),
        )
        text = generators._income_review_checklist()
        assert "P&L" in text and "$" not in text

    def test_profile_block_overrides(self, monkeypatch):
        import generators

        prof = tenant_profile.Profile("x", {"income_review": {"checklist": "Mine."}})
        monkeypatch.setattr(tenant_profile, "load_profile", lambda name=None: prof)
        assert generators._income_review_checklist() == "Mine."
