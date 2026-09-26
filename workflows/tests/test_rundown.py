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
