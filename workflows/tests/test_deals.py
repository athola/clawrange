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
        assert "Deal rundown" in report  # generic default title
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
        assert "Deal rundown" in "\n".join(self.sent)
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

    @pytest.mark.asyncio
    async def test_synthesis_prompt_treats_title_as_ground_truth(self, monkeypatch):
        # 2026-10-08 rundown: the buy read said STRONG BUY for D1 even though
        # the deal line carried "Title: Sony Alpha A6000 ... Digital Camera".
        # The model must be told the title outranks the target name.
        import llm_proxy

        seen = {}

        async def fake_call(prompt, max_tokens=0):
            seen["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(llm_proxy, "_llm_call", fake_call)
        deal = self._deal("RTX 3090 FE", 600.0)
        deals.order_and_number([deal])
        await deals.synthesize([deal], "")
        assert "title is what the item actually is" in seen["prompt"]


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

    def test_sony_camera_titles_match_no_target(self, targets):
        # 2026-10-08 rundown: a Sony Alpha A6000 camera kit was D1, a STRONG
        # BUY for the RTX A6000 48GB target. Sony Alpha bodies share the
        # A5000/A6000 model tokens with Nvidia pro GPUs, so bare model-token
        # queries pull cameras in via eBay search pollution.
        camera = _listing(
            "Sony Alpha A6000 24.3MP Digital Camera - Black (Kit with "
            "16-50mm & 55-210mm Zoom Lens)",
            price=575.0,
        )
        assert deals.match_target(camera, targets) is None

    def test_rtx_a6000_gpu_title_still_matches(self, targets):
        gpu = _listing("NVIDIA RTX A6000 48GB GDDR6 Workstation GPU", price=575.0)
        t = deals.match_target(gpu, targets)
        assert t is not None and t.name == "RTX A6000 48GB"

    def test_accessory_and_laptop_pollution_match_no_target(self, targets):
        # Titles captured from the 2026-10-31 live run: NVLink bridges,
        # cooler kits, laptop GPU modules, Mac mini docks and an L40s
        # robot vacuum all carry a target's model token without being it.
        pollution = [
            "New NVLINK BRIDGE 2 SLOT for RTX A100 A800 A6000 A5500 "
            "A5000 A4500 4090 vv",
            "EVGA HYBRID Cooler Kit for GeForce RTX 3090/3080 Ti/3080 "
            "XC3 Graphics Cards",
            "LENOVO THINKPAD P15 GEN 2 RTX A5000 16GB PCIE 4.0 X16 "
            "VIDEO CARD 5C51C94209",
            "GIISSMO Mac mini Dock w/ 8TB M.2 NVMe Enclosure for Mac "
            "mini M6, M5 Pro, M4 at Amazon",
            "DREAME L40s Ultra AE Robot Vacuum and Mop Combo, 19,000Pa",
        ]
        for title in pollution:
            assert deals.match_target(_listing(title, price=500.0), targets) is None


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


from datetime import UTC, datetime  # noqa: E402


class TestNovember14DealRundown:
    """Regressions from the 2026-11-14 deal rundown: a $500 'RTX 4090' was a
    BUY, drive caddies and cooling kits matched server/GPU targets,
    barebones servers read as deals, the model put a desktop GPU in a
    laptop, and the heading printed twice."""

    @pytest.fixture
    def targets(self):
        import tenant_profile

        profile = tenant_profile.load_profile("marketing", env={})
        return deals.load_targets(profile.raw["homelab_deals"])

    def test_half_off_a_great_price_is_too_good_to_be_true(self, targets):
        rtx4090 = next(t for t in targets if t.name == "RTX 4090 24GB")
        flags = deals.red_flags(_listing("RTX 4090 24GB", price=500.0), rtx4090)
        assert any("too good" in f for f in flags)

    @pytest.mark.parametrize(
        "title",
        [
            "Dell PowerEdge R740, 1x CC, 1x NC, 2x 750w PSU NO CPU, NO RAM, NO HDD",
            "Dell PowerEdge R740 0 bay,2-HS,0x0ram,0x0,6fans, motherboard,"
            "I350-ndc-2x1100w",
            "Supermicro 4028GR-TR 4U Barebone 10 GPU Slots SATA 2x 2000W",
            "Dell PowerEdge R730, 2 sinks, System Board, 8 trays,H730,Idrac 8 "
            "Ent, 2x750w Psu",
        ],
    )
    def test_barebones_servers_are_flagged(self, title, targets):
        # The barebone phrases are profile config (red_flags), not code.
        r740 = next(t for t in targets if t.name == "Dell PowerEdge R740")
        flags = deals.red_flags(_listing(title), r740)
        assert any("barebone" in f for f in flags)

    def test_real_gpu_titles_still_match(self, targets):
        for title, name in [
            ("ASUS ROG Strix GeForce RTX 4090 White OC Edition", "RTX 4090 24GB"),
            ("NVIDIA RTX 3090 24GB EVGA FTW3 for AI", "RTX 3090 24GB"),
            ("Apple Mac mini M4 Pro 24GB 512GB SSD 12-core", "Mac mini M4 Pro"),
            (
                "NVIDIA Tesla P100 SXM2 16GB GPU, Server Accelerator Card",
                "Tesla P100 16GB",
            ),
        ]:
            t = deals.match_target(_listing(title), targets)
            assert t is not None and t.name == name, title

    def test_accessories_match_no_target(self, targets):
        # Real titles from the 2026-11-14 dry run against eBay/Slickdeals.
        pollution = [
            '3.5" 2.5" HDD Tray Adapter for Dell 14-16th R440 R640 R740',
            "NVIDIA Tesla P40 P100 M40 graphics card cooling fan-",
            "NVIDIA Tesla P40 P100 M40 graphics card cooling turbine fan BFB1012VH",
            "Water cooling head for NVIDIA Tesla V100 P100 GPU SXM2 US",
            "Bykski GPU Block For ASRock AMD RX 7900 XTX",
            "Barrow Liquid Cooling GPU Block for ASUS TUF RTX3090 3080 BS-AST3090-PA2",
            "CASE for GIGABYTE AORUS RTX 3090 AI Box GV-N3090IXEB-32GD",
            "NEW 4TB M4PRO SSD For Apple Mac mini M4 Pro 10-layered Expand "
            "Hard Drive Upgrade",
            "Orico 10-in-1 Hub Stand for Mac Mini M4/M4 Pro w/ M.2 SSD Slot",
            "SXM2 To PCIE Adapter Board Supports Nvidia Tesla P100 V100 16GB 32GB",
            "256GB 8x 32GB PC4-2666 RDIMM Supermicro 4028GR-TRT2 740GP-TNRBT "
            "Memory RAM",
            "DELL PRECISION 7760 SERIES NVIDIA RTX A3000 A4000 A5000 6GB "
            "VIDEO CARD. Lot 3",
            "Water Panther 18TB 7200 RPM SAS 12Gb/s 3.5-Inch Enterprise Hard "
            "Disk Drive in 13G Tray Compatible with Dell R730",
            "Apple Mac mini M4 (2024) - 16GB / 256GB - in Mac Pro Style Case Bundle",
            "1pcs Tesla V100 SXM2 copper water cooling for V100/P100/SXM2 "
            "interface card",
            "External Nvidia Tesla P100 V100 SXM2 PCI-E X16 +SFF-8654 Adapter "
            "+2 50CM Cable",
        ]
        for title in pollution:
            assert deals.match_target(_listing(title, price=40.0), targets) is None

    def test_server_listing_with_caddies_still_matches(self, targets):
        t = deals.match_target(
            _listing("Dell PowerEdge R740 2x Gold 6148 256GB w/ 8 drive caddies"),
            targets,
        )
        assert t is not None and "R740" in t.name

    @pytest.mark.asyncio
    async def test_prompt_requires_a_pcie_host_for_gpus(self, monkeypatch):
        import llm_proxy

        seen = {}

        async def fake_call(prompt, max_tokens=0):
            seen["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(llm_proxy, "_llm_call", fake_call)
        deal = deals.classify(_listing("RTX 3090 FE", price=600.0), RTX3090, None)
        deals.order_and_number([deal])
        import tenant_profile

        block = tenant_profile.load_profile("marketing", env={}).raw["homelab_deals"]
        await deals.synthesize([deal], "a laptop", advisor=block["advisor"])
        p = seen["prompt"]
        assert "cannot go in a laptop" in p  # a profile rule
        assert "Cite each deal in one bullet only" in p  # a generic rule

    def test_repeated_buy_read_heading_is_stripped(self):
        deal = deals.classify(_listing("RTX 3090 FE", price=600.0), RTX3090, None)
        deals.order_and_number([deal])
        report = deals.render_report(
            [deal], [], [], "BUY READ\n\n* BUY [D1]: solid.", datetime.now(UTC)
        )
        body = report.split("*Buy read*")[1]
        assert "BUY READ" not in body
        assert "BUY [D1]" in body


class TestHistoryIgnoresJunk:
    def test_median_skips_prices_under_the_floor(self, tmp_path):
        from brain_db import BrainDB

        db = BrainDB(str(tmp_path / "b.db"))
        db.init_db()
        for i, price in enumerate([15, 20, 25, 30, 700, 720, 740]):
            db.record_deal_observation("eBay", str(i), "RTX 3090", price)
        assert db.deal_price_stats("RTX 3090").median < 100
        stats = db.deal_price_stats("RTX 3090", floor=300)
        assert stats.median == 720 and stats.samples == 3


class TestOneVerdictPerDeal:
    """2026-11-14: D6 sat in two SKIP bullets with different reasons. The
    model is asked for one verdict per deal; code enforces it."""

    def test_repeated_verdict_refs_are_removed(self):
        text = (
            "* SKIP D4, D6, D10: barebone R740s, real cost is higher.\n"
            "* SKIP D2, D6: titles are drive caddies.\n"
            "* BUY D1: pair with D6 only if you need a host."
        )
        out = deals.one_verdict_per_deal(text)
        assert "SKIP D4, D6, D10:" in out
        assert "SKIP D2: titles are drive caddies." in out
        assert "pair with D6" in out  # a body cross-reference is fine

    def test_bullet_left_with_no_refs_is_dropped(self):
        text = "* BUY D1: good.\n* CONDITIONAL [D1]: also good?\n* SKIP D2: no."
        out = deals.one_verdict_per_deal(text)
        assert "CONDITIONAL" not in out
        assert "SKIP D2: no." in out

    @pytest.mark.asyncio
    async def test_prompt_asks_for_verdict_heads(self, monkeypatch):
        import llm_proxy

        seen = {}

        async def fake_call(prompt, max_tokens=0):
            seen["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(llm_proxy, "_llm_call", fake_call)
        deal = deals.classify(_listing("RTX 3090 FE", price=600.0), RTX3090, None)
        deals.order_and_number([deal])
        await deals.synthesize([deal], "")
        assert "BUY, CONDITIONAL or SKIP" in seen["prompt"]


class TestPerTargetCap:
    """2026-11-14 dry run: nine near-identical R730s filled the clean list.
    The $300 great price is sound (p10 of ~280 populated listings is $298);
    the bottom of that market is just crowded."""

    R730 = WatchTarget(
        name="Dell PowerEdge R730", query="r730", category="server", great_price=300.0
    )

    def test_each_target_keeps_at_most_two_and_reports_the_rest(self):
        found = [
            deals.classify(_listing(f"Dell R730 #{i}", price=p), self.R730, None)
            for i, p in enumerate([200, 190, 210, 220, 180])
        ] + [deals.classify(_listing("RTX 3090", price=600.0), RTX3090, None)]
        clean, flagged = deals.order_and_number(found)
        assert [d.target.name for d in clean].count("Dell PowerEdge R730") == 2
        assert any(d.target is RTX3090 for d in clean)
        more = deals.overflow_counts(found, clean + flagged)
        assert more == {"Dell PowerEdge R730": 3}
        report = deals.render_report(
            clean + flagged, [], [], None, datetime.now(UTC), more=more
        )
        assert "+3 more Dell PowerEdge R730" in report


class TestHistoryPrune:
    """Observations keep their title so the history can be re-judged when
    the matching rules improve: a coat recorded as an "L40S" once is a
    coat forever, and should stop counting toward the L40S median."""

    L40S = WatchTarget(
        name="L40S 48GB",
        query="l40s",
        category="gpu",
        great_price=6000.0,
        memory_gb=48,
        exclude=["jacket", "coat"],
    )

    @pytest.fixture
    def db(self, tmp_path):
        from brain_db import BrainDB

        db = BrainDB(str(tmp_path / "b.db"))
        db.init_db()
        return db

    def test_title_is_stored(self, db):
        db.record_deal_observation("eBay", "1", "RTX 3090", 700, title="RTX 3090 FE")
        assert db.deal_observations()[0]["title"] == "RTX 3090 FE"

    def test_stale_rows_are_skipped_not_deleted(self, db):
        rows = [
            ("1", "L40S 48GB", 5200, "NVIDIA L40S 48GB PCIe"),  # keep
            ("2", "L40S 48GB", 5500, "Polo Long Coat Khaki L 40S"),  # junk title
            ("3", "L40S 48GB", 900, ""),  # under the floor
            ("4", "L40S 48GB", 5400, ""),  # legacy, no title: keep
            ("5", "Retired target", 100, "anything"),  # target gone
        ]
        for lid, target, price, title in rows:
            db.record_deal_observation("eBay", lid, target, price, title=title)
        stats = deals.history_stats(db.deal_observations(), [self.L40S])
        assert stats["L40S 48GB"].samples == 2  # rows 1 and 4
        assert stats["L40S 48GB"].median == 5300
        assert len(db.deal_observations()) == 5  # filtered, never deleted

    def test_legacy_db_gains_the_title_column(self, tmp_path):
        import sqlite3

        from brain_db import BrainDB

        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE deal_observations (source TEXT NOT NULL, listing_id "
            "TEXT NOT NULL, target TEXT NOT NULL, price REAL NOT NULL, "
            "observed_at TEXT NOT NULL, PRIMARY KEY(source, listing_id))"
        )
        conn.commit()
        conn.close()
        db = BrainDB(path)
        db.init_db()
        db.record_deal_observation("eBay", "1", "X", 1.0, title="t")
        assert db.deal_observations()[0]["title"] == "t"
