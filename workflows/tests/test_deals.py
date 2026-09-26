"""Tests for the homelab deal scanner's pure core: matching, red flags,
price parsing, and deal classification against targets and history."""

import pytest

import deals
from deals import Listing, WatchTarget

RTX3090 = WatchTarget(
    name="RTX 3090 24GB",
    query="rtx 3090",
    category="gpu",
    great_price=650.0,
    exclude=["3090 ti"],
)
MS01 = WatchTarget(
    name="Minisforum MS-01",
    query="ms-01",
    category="mini_pc",
    great_price=450.0,
)


def _listing(title: str, price: float | None = 600.0, **kw) -> Listing:
    return Listing(
        source=kw.pop("source", "ebay"),
        listing_id=kw.pop("listing_id", title[:12]),
        title=title,
        url=kw.pop("url", "https://example.test/item"),
        price=price,
        **kw,
    )


class TestParsePrice:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[FS][US-TX] EVGA RTX 3090 FTW3 - $675 shipped", 675.0),
            ("Minisforum MS-01 i9 for $1,049.99 at Amazon", 1049.99),
            ("RTX 3090 24GB (no price)", None),
            ("USD 499 Mac mini M4 Pro", 499.0),
        ],
    )
    def test_first_dollar_amount(self, text, expected):
        assert deals.parse_price(text) == expected


class TestMatchTarget:
    def test_all_query_words_must_appear(self):
        assert deals.match_target(_listing("NVIDIA GeForce RTX 3090 24GB"), [RTX3090])
        assert deals.match_target(_listing("RTX 3080 10GB"), [RTX3090]) is None

    def test_exclusions_win(self):
        assert deals.match_target(_listing("RTX 3090 Ti Founders"), [RTX3090]) is None

    def test_punctuation_insensitive(self):
        assert deals.match_target(_listing("Minisforum MS01 i9-13900H"), [MS01]) is MS01


class TestRedFlags:
    def test_parts_and_untested_listings_are_flagged(self):
        flags = deals.red_flags(_listing("RTX 3090 for parts / not working"), RTX3090)
        assert "for parts" in flags

    def test_too_good_to_be_true(self):
        flags = deals.red_flags(_listing("RTX 3090 24GB", price=120.0), RTX3090)
        assert any("too good" in f for f in flags)

    def test_zero_feedback_seller(self):
        flags = deals.red_flags(_listing("RTX 3090", seller_feedback=0), RTX3090)
        assert any("feedback" in f for f in flags)

    def test_clean_listing_has_no_flags(self):
        assert deals.red_flags(_listing("RTX 3090 24GB", price=600.0), RTX3090) == []


class TestClassify:
    def test_below_great_price_is_great(self):
        d = deals.classify(_listing("RTX 3090", price=630.0), RTX3090, None)
        assert d is not None and d.tier == "great"

    def test_far_below_great_price_is_insane(self):
        d = deals.classify(_listing("RTX 3090", price=520.0), RTX3090, None)
        assert d is not None and d.tier == "insane"

    def test_shipping_counts_toward_total(self):
        d = deals.classify(
            _listing("RTX 3090", price=630.0, shipping=40.0), RTX3090, None
        )
        assert d is None

    def test_history_can_promote_a_deal_above_threshold(self):
        # Asking prices have run ~$950 lately; $700 is 26% under that.
        hist = deals.PriceStats(median=950.0, samples=12)
        d = deals.classify(_listing("RTX 3090", price=700.0), RTX3090, hist)
        assert d is not None and d.tier == "great"
        assert d.pct_below_median == pytest.approx(0.263, abs=0.01)

    def test_thin_history_is_ignored(self):
        hist = deals.PriceStats(median=950.0, samples=3)
        assert deals.classify(_listing("RTX 3090", price=700.0), RTX3090, hist) is None

    def test_unpriced_listing_is_not_a_deal(self):
        assert deals.classify(_listing("RTX 3090", price=None), RTX3090, None) is None


class TestLoadTargets:
    def test_builds_targets_from_profile_block(self):
        block = {
            "targets": [
                {
                    "name": "RTX 3090 24GB",
                    "query": "rtx 3090",
                    "category": "gpu",
                    "max_price_great": 650,
                    "exclude": ["3090 ti"],
                    "min_vram_gb": 24,
                }
            ]
        }
        [t] = deals.load_targets(block)
        assert t.great_price == 650.0
        assert t.exclude == ["3090 ti"]
        assert t.memory_gb == 24


class TestPriceHistory:
    """GIVEN no sold-price API
    WHEN the scanner records every matched asking price
    THEN a rolling median per target becomes the second reference."""

    @pytest.fixture
    def db(self, tmp_path):
        from brain_db import BrainDB

        db = BrainDB(str(tmp_path / "brain.db"))
        db.init_db()
        return db

    def test_median_over_recorded_observations(self, db):
        for i, price in enumerate([900, 950, 1000]):
            db.record_deal_observation("ebay", f"id{i}", "RTX 3090 24GB", price)
        stats = db.deal_price_stats("RTX 3090 24GB", days=30)
        assert (stats.median, stats.samples) == (950.0, 3)

    def test_same_listing_counts_once(self, db):
        db.record_deal_observation("ebay", "dup", "RTX 3090 24GB", 900)
        db.record_deal_observation("ebay", "dup", "RTX 3090 24GB", 500)
        stats = db.deal_price_stats("RTX 3090 24GB", days=30)
        assert (stats.median, stats.samples) == (900.0, 1)

    def test_no_history_returns_none(self, db):
        assert db.deal_price_stats("Nothing", days=30) is None


class TestRenderReport:
    from datetime import UTC, datetime

    NOW = datetime(2026, 9, 26, 13, 15, tzinfo=UTC)

    def _deal(self, title, price, target=RTX3090, **kw):
        return deals.classify(_listing(title, price=price, **kw), target, None)

    def test_sections_order_and_flagged_split(self):
        from rundown import SourceStatus

        insane = self._deal("RTX 3090 24GB", 500.0, url="https://e.test/1")
        great = self._deal("RTX 3090 FE", 640.0, url="https://e.test/2")
        flagged = self._deal("RTX 3090 for parts", 300.0, url="https://e.test/3")
        report = deals.render_report(
            found=[great, flagged, insane],
            misses=[],
            statuses=[SourceStatus("eBay", True, "40 listings")],
            synthesis="Buy [D1] today; skip [D9].",
            now=self.NOW,
        )
        assert "Homelab deal rundown" in report
        assert report.index("Insane deals") < report.index("Great deals")
        assert report.index("Great deals") < report.index("Look twice")
        # Clean deals are numbered first, so the insane one is D1.
        assert "[D1](https://e.test/1)" in report
        assert "D9" not in report
        assert "for parts" in report.split("Look twice")[1]

    def test_quiet_day_lists_closest_misses(self):
        miss = _listing("RTX 3090 24GB", price=690.0, url="https://e.test/m")
        report = deals.render_report(
            found=[],
            misses=[(miss, RTX3090)],
            statuses=[],
            synthesis=None,
            now=self.NOW,
        )
        assert "No listing beat a target today" in report
        assert "https://e.test/m" in report
        assert "6% over" in report

    def test_setup_fixes_are_escaped(self):
        from rundown import SourceStatus

        report = deals.render_report(
            found=[],
            misses=[],
            statuses=[
                SourceStatus("eBay", False, "not configured", "Set EBAY_CLIENT_ID")
            ],
            synthesis=None,
            now=self.NOW,
        )
        assert "EBAY\\_CLIENT\\_ID" in report


class TestCoreAdditions:
    def test_global_exclude_applies_to_every_target(self):
        block = {
            "exclude": ["waterblock", "backplate"],
            "targets": [
                {"name": "RTX 3090", "query": "rtx 3090", "max_price_great": 700},
                {"name": "RTX 4090", "query": "rtx 4090", "max_price_great": 1400},
            ],
        }
        targets = deals.load_targets(block)
        assert all("waterblock" in t.exclude for t in targets)
        wb = _listing("EKWB waterblock for RTX 3090 FE")
        assert deals.match_target(wb, targets) is None

    def test_deal_line_shows_history_sample_count(self):
        hist = deals.PriceStats(median=950.0, samples=12)
        d = deals.classify(_listing("RTX 3090", price=700.0), RTX3090, hist)
        report = deals.render_report(
            found=[d], misses=[], statuses=[], synthesis=None, now=TestRenderReport.NOW
        )
        assert "12 seen" in report

    def test_unpriced_leads_get_their_own_section(self):
        lead = _listing("[FS][US-TX] RTX 3090 FTW3", price=None, url="https://r.test/1")
        report = deals.render_report(
            found=[],
            misses=[],
            statuses=[],
            synthesis=None,
            now=TestRenderReport.NOW,
            leads=[(lead, RTX3090)],
        )
        assert "Leads" in report
        assert "https://r.test/1" in report


class TestHomelabDealsGenerator:
    """GIVEN the profile's homelab_deals block
    WHEN the daily generator runs
    THEN it always delivers a report, builds price history, and never
    repeats a delivered deal."""

    BLOCK = {
        "sources": {"slickdeals": {}},
        "exclude": ["waterblock"],
        "current_setup": "one laptop with an 8 GB GPU",
        "targets": [
            {"name": "RTX 3090 24GB", "query": "rtx 3090", "max_price_great": 750}
        ],
    }

    @pytest.fixture
    def db(self, tmp_path):
        from brain_db import BrainDB

        db = BrainDB(str(tmp_path / "brain.db"))
        db.init_db()
        db.upsert_schedule("homelab_deals", "Deals", "homelab_deals", "30 8 * * *")
        return db

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        from unittest.mock import AsyncMock

        import tenant_profile

        monkeypatch.setattr(
            tenant_profile,
            "load_profile",
            lambda *a, **k: tenant_profile.Profile(
                name="t", raw={"homelab_deals": self.BLOCK}
            ),
        )
        monkeypatch.setattr(deals, "synthesize", AsyncMock(return_value=None))
        self.sent: list[str] = []

        async def fake_notify(text):
            self.sent.append(text)
            return True

        monkeypatch.setattr("telegram.notify", fake_notify)

    def _gather(self, monkeypatch, listings):
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "deal_sources.gather", AsyncMock(return_value=(listings, []))
        )

    @pytest.mark.asyncio
    async def test_quiet_day_still_delivers(self, db, monkeypatch):
        from generators import homelab_deals_generator

        self._gather(monkeypatch, [])
        await homelab_deals_generator(db)
        assert "Homelab deal rundown" in "\n".join(self.sent)
        assert db.get_schedule("homelab_deals")["last_status"].startswith("delivered")

    @pytest.mark.asyncio
    async def test_deal_reported_once_and_history_recorded(self, db, monkeypatch):
        from generators import homelab_deals_generator

        deal = _listing(
            "RTX 3090 24GB", price=600.0, url="https://s.test/d", listing_id="d1"
        )
        pricey = _listing(
            "RTX 3090 FE", price=1100.0, url="https://s.test/p", listing_id="p1"
        )
        block = _listing("RTX 3090 waterblock", price=90.0, listing_id="w1")
        self._gather(monkeypatch, [deal, pricey, block])

        await homelab_deals_generator(db)
        first = "\n".join(self.sent)
        assert "https://s.test/d" in first
        stats = db.deal_price_stats("RTX 3090 24GB", days=30)
        assert stats.samples == 2  # deal + pricey; the waterblock never matched

        self.sent.clear()
        await homelab_deals_generator(db)
        assert "https://s.test/d" not in "\n".join(self.sent)

    @pytest.mark.asyncio
    async def test_failed_delivery_is_recorded_and_not_marked_seen(
        self, db, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from generators import homelab_deals_generator

        self._gather(monkeypatch, [_listing("RTX 3090", price=600.0, listing_id="f1")])
        monkeypatch.setattr("telegram.notify", AsyncMock(return_value=False))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        await homelab_deals_generator(db)
        assert db.get_schedule("homelab_deals")["last_status"].startswith("FAILED")
        assert not db.is_seen("deal", "ebay:f1")

    def test_registered_and_caught_up(self):
        from generators import GENERATORS
        from scheduler import CATCH_UP_SCHEDULES

        assert "homelab_deals" in GENERATORS
        assert "homelab_deals" in CATCH_UP_SCHEDULES


class TestHaveWantTitles:
    def test_matches_only_the_have_side(self):
        have = _listing("[USA-CA] [H] RTX 3090 FE [W] PayPal, Local Cash")
        want = _listing("[USA-TX] [H] PayPal [W] RTX 3090")
        assert deals.match_target(have, [RTX3090]) is RTX3090
        assert deals.match_target(want, [RTX3090]) is None

    def test_homelabsales_want_posts_are_skipped(self):
        assert (
            deals.match_target(_listing("[W][US-NY] RTX 3090 or 4090"), [RTX3090])
            is None
        )


class TestPartsBenchmark:
    """Parts are a price benchmark, not a ranking preference: a bundled PC
    or mini PC that costs less than its parts bought separately is a good
    deal, even above the target's great price."""

    STRIX = WatchTarget(
        name="Strix Halo 128GB",
        query="ai max 395 128gb",
        category="unified_memory",
        great_price=2200.0,
        parts_cost=3350.0,
    )

    def _deal(self, title, price, target=RTX3090):
        return deals.classify(_listing(title, price=price), target, None)

    def test_gpu_is_a_part(self):
        assert self._deal("EVGA RTX 3090 FTW3", 600.0).form == "part"

    def test_mini_pc_is_a_system(self):
        assert self._deal("Minisforum MS-01 i9", 400.0, target=MS01).form == "system"

    def test_prebuilt_pc_with_target_gpu_is_a_system(self):
        deal = self._deal("Prebuilt gaming PC with RTX 3090, 32GB RAM", 640.0)
        assert deal.form == "system"

    def test_parts_do_not_inherently_outrank_systems(self):
        system = self._deal(
            "Corsair AI Workstation 300 AI Max+ 395 128GB", 1899.0, target=self.STRIX
        )  # 14% under: great
        part = self._deal("RTX 3090 FE", 640.0)  # 2% under: great
        clean, _ = deals.order_and_number([part, system])
        assert clean[0] is system

    def test_system_under_parts_cost_is_a_deal_above_great_price(self):
        deal = self._deal("GMKtec EVO-X2 AI Max+ 395 128GB", 2600.0, target=self.STRIX)
        assert deal is not None and deal.tier == "great"
        assert deal.pct_below_parts == pytest.approx(1 - 2600 / 3350)

    def test_system_over_parts_cost_and_great_price_is_no_deal(self):
        assert (
            self._deal("GMKtec EVO-X2 AI Max+ 395 128GB", 3400.0, target=self.STRIX)
            is None
        )

    def test_no_parts_benchmark_without_parts_cost(self):
        assert self._deal("RTX 3090 FE", 640.0).pct_below_parts is None

    def test_load_targets_reads_parts_cost(self):
        (t,) = deals.load_targets(
            {
                "targets": [
                    {"name": "X", "query": "x", "max_price_great": 1, "parts_cost": 2}
                ]
            }
        )
        assert t.parts_cost == 2.0

    def test_profile_rejects_bad_parts_cost(self):
        import tenant_profile

        with pytest.raises(tenant_profile.ProfileError, match="parts_cost"):
            tenant_profile._validate_homelab_deals(
                {
                    "targets": [
                        {
                            "name": "X",
                            "query": "x",
                            "max_price_great": 1,
                            "parts_cost": 0,
                        }
                    ]
                }
            )

    def test_report_shows_parts_saving(self):
        from datetime import UTC, datetime

        report = deals.render_report(
            found=[
                self._deal(
                    "Corsair AI Workstation 300 AI Max+ 395 128GB",
                    1899.0,
                    target=self.STRIX,
                )
            ],
            misses=[],
            statuses=[],
            synthesis=None,
            now=datetime(2026, 9, 26, tzinfo=UTC),
        )
        assert "43% under ~$3,350 parts cost" in report

    @pytest.mark.asyncio
    async def test_synthesis_prompt_weighs_parts_cost(self, monkeypatch):
        import llm_proxy

        seen = {}

        async def fake_call(prompt, max_tokens=0):
            seen["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(llm_proxy, "_llm_call", fake_call)
        system = self._deal(
            "Corsair AI Workstation 300 AI Max+ 395 128GB", 1899.0, target=self.STRIX
        )
        deals.order_and_number([system])
        await deals.synthesize([system], "")
        assert "under ~$3,350 parts cost" in seen["prompt"]
        assert "Prefer part" not in seen["prompt"]


class TestShippedProfileRouting:
    """The live profile must route a bare Strix Halo mainboard to its own
    part target and a complete Strix Halo PC to the system target."""

    @pytest.fixture
    def targets(self):
        import tenant_profile

        profile = tenant_profile.load_profile("marketing", env={})
        return deals.load_targets(profile.raw["homelab_deals"])

    def test_mainboard_title_hits_mainboard_target(self, targets):
        t = deals.match_target(
            _listing(
                "[FS] Framework Desktop Mainboard - Ryzen AI Max+ 395 - 128GB LPDDR5x"
            ),
            targets,
        )
        assert t is not None and t.category == "mainboard"

    def test_corsair_system_hits_system_target(self, targets):
        t = deals.match_target(
            _listing(
                "Corsair AI Workstation 300 PC: Ryzen AI Max+ 395, 128GB "
                "RAM, 1TB NVMe $1899 at Origin PC"
            ),
            targets,
        )
        assert t is not None and t.category == "unified_memory"

    def test_strix_system_target_benchmarks_parts(self, targets):
        t = next(
            t for t in targets if t.category == "unified_memory" and "395" in t.query
        )
        assert t.parts_cost is not None and t.parts_cost > 1899


class TestDesirabilityTags:
    """Tags say how much the stack wants an item, apart from the price tier:
    the tier stays a pure price verdict and tags reorder deals inside it."""

    NEED = WatchTarget(
        name="RTX 3090 24GB",
        query="rtx 3090",
        category="gpu",
        great_price=650.0,
        need="need",
        fills=["vram"],
    )
    WATCH = WatchTarget(
        name="Tesla P40 24GB",
        query="p40",
        category="gpu",
        great_price=200.0,
        need="watch",
    )
    STRIX = WatchTarget(
        name="Strix Halo 128GB",
        query="ai max 395 128gb",
        category="unified_memory",
        great_price=2200.0,
        parts_cost=3350.0,
    )

    def _deal(self, title, price, target, history=None):
        return deals.classify(_listing(title, price=price), target, history)

    def test_default_need_is_want(self):
        assert MS01.need == "want" and MS01.fills == []

    def test_need_and_gap_tags(self):
        d = self._deal("RTX 3090 FE", 640.0, self.NEED)
        assert d.tags == ["need", "fills vram"]

    def test_bundle_beats_parts_tag(self):
        d = self._deal(
            "Corsair AI Workstation 300 AI Max+ 395 128GB", 1899.0, self.STRIX
        )
        assert "bundle < parts -43%" in d.tags

    def test_no_bundle_tag_over_parts_cost(self):
        d = self._deal("Corsair AI Max+ 395 128GB", 2100.0, self.STRIX)
        d.pct_below_parts = -0.05
        assert not any(t.startswith("bundle") for t in d.tags)

    def test_price_drop_tag(self):
        hist = deals.PriceStats(median=950.0, samples=12)
        d = self._deal("RTX 3090 FE", 640.0, self.NEED, hist)
        assert "price drop -33%" in d.tags

    def test_need_outranks_watch_within_a_tier(self):
        watch = self._deal("Tesla P40", 180.0, self.WATCH)  # 10% under: great
        need = self._deal("RTX 3090 FE", 640.0, self.NEED)  # 2% under: great
        clean, _ = deals.order_and_number([watch, need])
        assert clean[0] is need

    def test_tier_still_beats_need(self):
        watch = self._deal("Tesla P40", 100.0, self.WATCH)  # insane
        need = self._deal("RTX 3090 FE", 640.0, self.NEED)  # great
        clean, _ = deals.order_and_number([need, watch])
        assert clean[0] is watch

    def test_gap_filler_outranks_same_need_level(self):
        plain = WatchTarget(
            name="RX 7900 XTX",
            query="7900 xtx",
            category="gpu",
            great_price=650.0,
            need="need",
        )
        a = self._deal("RX 7900 XTX", 600.0, plain)  # 8% under
        b = self._deal("RTX 3090 FE", 640.0, self.NEED)  # 2% under, fills vram
        clean, _ = deals.order_and_number([a, b])
        assert clean[0] is b

    def test_load_targets_reads_need_and_fills(self):
        (t,) = deals.load_targets(
            {
                "targets": [
                    {
                        "name": "X",
                        "query": "x",
                        "max_price_great": 1,
                        "need": "need",
                        "fills": ["vram"],
                    }
                ]
            }
        )
        assert (t.need, t.fills) == ("need", ["vram"])

    @pytest.mark.parametrize(
        "extra, match",
        [({"need": "must"}, "need"), ({"fills": ["nope"]}, "stack_gaps")],
    )
    def test_profile_rejects_bad_tags(self, extra, match):
        import tenant_profile

        target = {"name": "X", "query": "x", "max_price_great": 1, **extra}
        with pytest.raises(tenant_profile.ProfileError, match=match):
            tenant_profile._validate_homelab_deals(
                {"stack_gaps": {"vram": "more VRAM"}, "targets": [target]}
            )

    def test_report_shows_tags(self):
        from datetime import UTC, datetime

        report = deals.render_report(
            found=[self._deal("RTX 3090 FE", 640.0, self.NEED)],
            misses=[],
            statuses=[],
            synthesis=None,
            now=datetime(2026, 9, 26, tzinfo=UTC),
        )
        # Telegram Markdown escapes "[", which still renders as "[".
        assert "\\[need] \\[fills vram]" in report

    @pytest.mark.asyncio
    async def test_synthesis_prompt_carries_tags_and_gaps(self, monkeypatch):
        import llm_proxy

        seen = {}

        async def fake_call(prompt, max_tokens=0):
            seen["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(llm_proxy, "_llm_call", fake_call)
        d = self._deal("RTX 3090 FE", 640.0, self.NEED)
        deals.order_and_number([d])
        await deals.synthesize([d], "", stack_gaps={"vram": "more VRAM for 70B"})
        assert "tags: need, fills vram" in seen["prompt"]
        assert "vram: more VRAM for 70B" in seen["prompt"]
