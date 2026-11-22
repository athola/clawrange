"""Tests for the daily outreach rundown: evidence, citations, rendering."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import rundown
from reddit_search import SearchHealth
from rundown import Evidence, SourceStatus

PROJECT = {
    "slug": "clawrange",
    "owner": "athola",
    "repo": "clawrange",
    "topics": '["llm proxy"]',
    "search_terms": '["openclaw", "llm proxy"]',
    "subreddits": "[]",
    "posture": "",
}


def _ev(ref: str, url: str, source: str = "reddit", score: int = 5) -> Evidence:
    return Evidence(
        source=source,
        group="Reddit",
        project="clawrange",
        title=f"Thread {ref}",
        url=url,
        facts="r/x · 5 pts",
        score=score,
        ref=ref,
    )


class TestRelevance:
    def test_terms_outweigh_topics(self):
        assert rundown.relevance("an llm proxy", ["llm proxy"], []) == 1.0
        assert rundown.relevance("an llm proxy", [], ["llm proxy"]) == 1.5
        assert rundown.relevance("nothing here", ["llm proxy"], ["x"]) == 0.0


class TestParseHnHits:
    """GIVEN an Algolia response
    WHEN it is parsed for a project
    THEN only relevant stories come back, linked to the HN discussion."""

    def test_keeps_relevant_stories_with_discussion_link(self):
        payload = {
            "hits": [
                {
                    "objectID": "111",
                    "title": "Show HN: an LLM proxy with tiered routing",
                    "url": "https://example.com/proxy",
                    "points": 42,
                    "num_comments": 7,
                },
                {"objectID": "222", "title": "Unrelated gardening post", "points": 900},
            ]
        }
        out = rundown.parse_hn_hits(payload, PROJECT)
        assert [e.url for e in out] == ["https://news.ycombinator.com/item?id=111"]
        assert "42 pts" in out[0].facts
        assert "example.com" in out[0].facts


class TestParseWebLines:
    def test_parses_pipe_lines_and_ignores_prose(self):
        text = (
            "Here is what I found:\n"
            "- LLM gateways thread | https://lobste.rs/s/abc | asks for OSS\n"
            "- bad line without url | nope | x\n"
        )
        out = rundown.parse_web_lines(text, PROJECT)
        assert len(out) == 1
        assert out[0].url == "https://lobste.rs/s/abc"
        assert out[0].source == "web"


class TestLinkCitations:
    """The synthesis may only point at evidence we actually gathered."""

    def test_known_refs_become_links_unknown_refs_and_urls_dropped(self):
        ev = [_ev("E1", "https://reddit.com/r/x/comments/1")]
        text = "Reply in [E1] and see [E9] at https://made.up/url for my_idea"
        out = rundown.link_citations(text, ev)
        assert "[E1](https://reddit.com/r/x/comments/1)" in out
        assert "E9" not in out
        assert "made.up" not in out
        assert "my\\_idea" in out  # escaped for Telegram Markdown


class TestBuildReport:
    NOW = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)

    def _blocked_reddit(self) -> SearchHealth:
        h = SearchHealth(mode="public")
        for _ in range(3):
            h.record(403)
        return h

    def test_empty_evidence_still_explains_itself(self):
        statuses = rundown.reddit_status(self._blocked_reddit())
        report = rundown.build_report(
            projects=[PROJECT],
            evidence=[],
            statuses=[statuses],
            synthesis=None,
            coverage="",
            now=self.NOW,
        )
        assert "Daily outreach rundown" in report
        assert "HTTP 403" in report
        assert "Setup needed" in report
        assert "reddit.com/prefs/apps" in report
        assert "No fresh evidence" in report

    def test_fallback_action_items_when_synthesis_missing(self):
        ev = [
            _ev("", "https://reddit.com/r/x/comments/1", score=3),
            _ev("", "https://news.ycombinator.com/item?id=2", "hn", score=90),
        ]
        report = rundown.build_report(
            projects=[PROJECT],
            evidence=ev,
            statuses=[SourceStatus("Hacker News", True, "2 stories")],
            synthesis=None,
            coverage="",
            now=self.NOW,
        )
        assert "Action items" in report
        # Highest-engagement evidence leads the fallback list.
        first_item = report.split("Action items")[1].splitlines()[1]
        assert "E2" in first_item
        assert "Setup needed" not in report

    def test_synthesis_is_rendered_with_links(self):
        ev = [_ev("", "https://reddit.com/r/x/comments/1")]
        report = rundown.build_report(
            projects=[PROJECT],
            evidence=ev,
            statuses=[],
            synthesis="Demand is rising for proxies [E1].",
            coverage="",
            now=self.NOW,
        )
        assert "[E1](https://reddit.com/r/x/comments/1)" in report


class TestGatherOtherChannels:
    @pytest.mark.asyncio
    async def test_one_failing_channel_does_not_sink_the_rest(self, monkeypatch):
        monkeypatch.setattr(rundown, "fetch_hn", AsyncMock(side_effect=RuntimeError))
        monkeypatch.setattr(rundown, "fetch_github", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            rundown,
            "fetch_web",
            AsyncMock(return_value=[_ev("", "https://lobste.rs/s/a", "web")]),
        )
        evidence, statuses = await rundown.gather_other_channels([PROJECT])
        assert [e.url for e in evidence] == ["https://lobste.rs/s/a"]
        by_name = {s.name: s for s in statuses}
        assert by_name["Hacker News"].ok is False
        assert by_name["Web search"].ok is True


class TestLooserMatching:
    def test_hn_keeps_hits_matching_every_query_word(self):
        payload = {
            "hits": [
                {
                    "objectID": "5",
                    "title": "My Claude Code setup, with a custom plugin",
                    "points": 3,
                }
            ]
        }
        project = {**PROJECT, "search_terms": '["claude code plugin"]'}
        out = rundown.parse_hn_hits(payload, project, query="claude code plugin")
        assert len(out) == 1

    def test_web_title_markdown_link_is_unwrapped(self):
        line = "- [Nice post](https://dev.to/a) | https://dev.to/a | fits\n"
        out = rundown.parse_web_lines(line, PROJECT)
        assert out[0].title == "Nice post"


class TestMarkdownSafety:
    def test_env_var_names_are_escaped(self):
        h = SearchHealth(mode="public")
        h.record(403)
        report = rundown.build_report(
            projects=[PROJECT],
            evidence=[],
            statuses=[rundown.reddit_status(h)],
            synthesis=None,
            coverage="",
            now=datetime(2026, 9, 26, 13, 0, tzinfo=UTC),
        )
        assert "REDDIT\\_CLIENT\\_ID" in report
        assert "REDDIT_CLIENT_ID" not in report.replace("\\_", "")


class TestPrecision:
    def test_grouped_refs_each_become_links(self):
        ev = [_ev("E8", "https://a.test/8"), _ev("E9", "https://a.test/9")]
        out = rundown.link_citations("see [E8, E9]", ev)
        assert "[E8](https://a.test/8)" in out
        assert "[E9](https://a.test/9)" in out

    @pytest.mark.asyncio
    async def test_github_query_matches_titles_only(self, monkeypatch):
        search = AsyncMock(return_value=[])
        monkeypatch.setattr(rundown.github_search, "search_issues", search)
        await rundown.fetch_github(PROJECT)
        assert "in:title" in search.await_args.args[0]


class TestNovember14Rundown:
    """Regressions from the 2026-11-14 rundown: third-party issues read as
    the operator's own, refs rendered as 'E1E2E3', invented project
    features, and action items with no venue in the evidence."""

    THIRD_PARTY = Evidence(
        source="github",
        group="GitHub issues (other repos)",
        project="clawrange",
        title="Use a Claude subscription as a provider plugin",
        url="https://github.com/valdo766hi/pal/issues/76",
        facts="issue on valdo766hi/pal (not yours) · open #76",
        ref="E1",
    )

    def test_adjacent_refs_are_separated(self):
        ev = [_ev("E1", "https://a.test/1"), _ev("E2", "https://a.test/2")]
        out = rundown.link_citations("pain points [E1][E2] today", ev)
        assert "[E1](https://a.test/1), [E2](https://a.test/2)" in out

    @pytest.mark.asyncio
    async def test_github_issue_on_other_repo_is_labelled_not_yours(self, monkeypatch):
        issue = type(
            "Issue",
            (),
            {
                "url": "https://github.com/valdo766hi/pal/issues/76",
                "title": "Use a Claude subscription as a provider plugin",
                "number": 76,
            },
        )()
        monkeypatch.setattr(
            rundown.github_search, "search_issues", AsyncMock(return_value=[issue])
        )
        (ev,) = await rundown.fetch_github(PROJECT)
        assert "valdo766hi/pal (not yours)" in ev.facts
        assert "other repos" in ev.group

    @pytest.mark.asyncio
    async def test_prompt_carries_venue_rules_and_grounded_descriptions(
        self, monkeypatch
    ):
        seen = {}

        async def fake_call(prompt, max_tokens=0, **kw):
            seen["prompt"] = prompt
            return "ok"

        monkeypatch.setattr(rundown.llm_proxy, "_llm_call", fake_call)
        project = {**PROJECT, "posture": "Lead with: a single-host AI ops stack."}
        bare = {**PROJECT, "slug": "bare", "owner": "a", "repo": "bare"}
        await rundown.synthesize([project, bare], [self.THIRD_PARTY])
        p = seen["prompt"]
        assert "about: a single-host AI ops stack." in p
        assert "no description" in p  # bare project: do not describe it
        assert "you cannot label" in p  # per-item allowed action
        assert "1 evidence item" in p  # count-aware hedging
        assert "cross-post" in p  # off-venue actions named and banned

    def test_off_venue_and_uncited_actions_are_dropped(self):
        ev = [self.THIRD_PARTY, _ev("E2", "https://news.ycombinator.com/item?id=2")]
        text = (
            "TODAY'S READ\n"
            "1. One item asks about provider plugins [E1].\n"
            "\n"
            "ACTION ITEMS\n"
            "1. Comment on [E1] with the config that worked for you,\n"
            "   since nobody has replied yet.\n"
            "2. Tag [E1] with help wanted and cross-post to Discord.\n"
            "3. Write a dev.to guide on plugin directories.\n"
            "4. Answer the setup question in the HN thread [E2].\n"
        )
        out = rundown.filter_actions(text, ev)
        assert "Comment on [E1]" in out
        assert "since nobody has replied yet." in out  # wrapped line kept
        assert "Tag [E1]" not in out
        assert "dev.to guide" not in out
        assert "2. Answer the setup question" in out  # renumbered
        assert "2 suggested actions dropped" in out
        assert "1. One item asks" in out  # TODAY'S READ untouched

    def test_little_fits_one_liner_survives(self):
        text = "ACTION ITEMS\nLittle fits today; nothing worth acting on."
        assert rundown.filter_actions(text, []) == text

    def test_label_as_plain_verb_is_not_a_triage_action(self):
        # Live replay 2026-11-14: "align how entries define and label
        # plugins" is a comment, not triage on someone else's issue.
        ev = [self.THIRD_PARTY]
        text = "ACTION ITEMS\n1. Comment on [E1] on how entries label plugins.\n"
        assert "label plugins" in rundown.filter_actions(text, ev)

    def test_bare_known_refs_are_linked_product_names_are_not(self):
        ev = [_ev("E1", "https://a.test/1")]
        out = rundown.link_citations("Comment on E1 about the A6000 and E9", ev)
        assert "[E1](https://a.test/1)" in out
        assert "A6000" in out and "[A6000]" not in out
        assert "E9" in out  # unknown bare token: plain text, not a link

    def test_long_ref_groups_link_every_ref(self):
        refs = ["E1", "E2", "E3", "E10"]
        ev = [_ev(r, f"https://a.test/{r}") for r in refs]
        out = rundown.link_citations("see [E1, E2, E3] and [E1, E2, E3, E10]", ev)
        for r in refs:
            assert f"[{r}](https://a.test/{r})" in out
        assert "\\[" not in out  # no half-escaped bracket left behind

    def test_triage_on_a_bare_ref_is_dropped(self):
        text = "ACTION ITEMS\n1. Tag E1 with help wanted.\n"
        assert "Tag E1" not in rundown.filter_actions(text, [self.THIRD_PARTY])


class TestCapabilityCheck:
    """Live replay 2026-11-14 claimed clawrange "handles CLAUDE_CODE_PLUGIN_DIRS
    discovery". A second pass names unsupported sentences; code deletes
    them, so the check can only remove text, never add it."""

    PROJECTS = [{**PROJECT, "posture": "Lead with: a single-host AI ops stack."}]
    DRAFT = (
        "TODAY'S READ\n"
        "• One item asks about plugin loading [E1]. clawrange already handles "
        "plugin directory discovery. This maps to clawrange.\n"
        "ACTION ITEMS\n"
        "1. Comment on [E1] with the debug steps that worked for you."
    )

    @pytest.mark.asyncio
    async def test_flagged_sentence_is_deleted(self, monkeypatch):
        seen = {}

        async def fake_call(prompt, max_tokens=0, **kw):
            seen["prompt"] = prompt
            return "1"

        monkeypatch.setattr(rundown.llm_proxy, "_llm_call", fake_call)
        out = await rundown.check_capabilities(self.PROJECTS, self.DRAFT)
        assert "already handles" not in out
        assert "One item asks about plugin loading [E1]." in out
        assert "1. Comment on [E1]" in out
        # Only sentences naming a project are offered for review.
        assert "[1] clawrange already handles" in seen["prompt"]
        assert "a single-host AI ops stack" in seen["prompt"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", ["1", "[1]", "[1], [2]", "1 and 2", " 1,2. "])
    async def test_number_list_formats_are_accepted(self, monkeypatch, reply):
        # Live replies seen 2026-11-14: "2,3" and "[2], [3]".
        async def fake_call(prompt, max_tokens=0, **kw):
            return reply

        monkeypatch.setattr(rundown.llm_proxy, "_llm_call", fake_call)
        out = await rundown.check_capabilities(self.PROJECTS, self.DRAFT)
        assert "already handles" not in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply", ["NONE", "None.", "garbage", "7, 9", None, "Sentence 1 is fine"]
    )
    async def test_unusable_reply_keeps_the_draft(self, monkeypatch, reply):
        async def fake_call(prompt, max_tokens=0, **kw):
            return reply

        monkeypatch.setattr(rundown.llm_proxy, "_llm_call", fake_call)
        assert await rundown.check_capabilities(self.PROJECTS, self.DRAFT) == self.DRAFT

    @pytest.mark.asyncio
    async def test_check_failure_keeps_the_draft(self, monkeypatch):
        monkeypatch.setattr(
            rundown.llm_proxy, "_llm_call", AsyncMock(side_effect=RuntimeError)
        )
        assert await rundown.check_capabilities(self.PROJECTS, self.DRAFT) == self.DRAFT
