"""Hot pulse: Markdown safety, message splitting, capped marking, health."""

from unittest.mock import AsyncMock

import pytest

import generators
import reddit_search
from reddit_search import RedditPost
from tests.testapp import brain_db


@pytest.fixture(autouse=True)
def _pulse_state(monkeypatch):
    monkeypatch.setattr(generators, "_is_quiet_hours", lambda *a, **kw: False)
    monkeypatch.setattr(generators, "_HOT_PULSE_IN_QUIET_HOURS", False)
    monkeypatch.setattr(generators, "_LAST_HOT_PULSE_DELIVERY_AT", None)
    brain_db.upsert_project(
        "p1",
        "o",
        "r",
        topics=["claude code plugin"],
        subreddits=["ClaudeAI"],
        search_terms=["claude code plugin"],
    )
    brain_db.upsert_schedule("hot_pulse", "hp", "hot_pulse", "*/5 * * * *")


def _post(i: int, title: str | None = None) -> RedditPost:
    return RedditPost(
        id=f"id{i}",
        url=f"https://reddit.com/r/ClaudeAI/comments/id{i}/x",
        title=title or f"claude code plugin question number {i} " + "word " * 12,
        subreddit="ClaudeAI",
        score=10 + i,
        comments=1,
        created_utc="2026-05-09T17:55:00+00:00",
    )


def _wire(monkeypatch, posts, sent):
    async def fake_search(*a, **kw):
        return posts

    async def fake_notify(text):
        sent.append(text)
        return True

    monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
    monkeypatch.setattr("telegram.notify", fake_notify)


@pytest.mark.asyncio
async def test_title_cannot_break_or_inject_markdown_link(monkeypatch):
    sent: list[str] = []
    evil = "claude code plugin](http://evil.test) *bold* _x_ [y"
    _wire(monkeypatch, [_post(1, evil)], sent)
    await generators.hot_pulse_generator(brain_db)
    msg = "\n".join(sent)
    assert msg.count("](") == 1
    assert "evil.test)](" not in msg and "*bold*" not in msg


@pytest.mark.asyncio
async def test_long_pulse_is_split_and_nothing_over_limit(monkeypatch):
    sent: list[str] = []
    _wire(monkeypatch, [_post(i) for i in range(25)], sent)
    await generators.hot_pulse_generator(brain_db)
    assert len(sent) > 1
    assert all(len(m) <= 4096 for m in sent)
    assert all(brain_db.is_seen("reddit_pulse", f"id{i}", "p1") for i in range(25))


@pytest.mark.asyncio
async def test_only_rendered_picks_are_marked_seen(monkeypatch):
    sent: list[str] = []
    _wire(monkeypatch, [_post(i) for i in range(5)], sent)
    await generators.hot_pulse_generator(brain_db, max_per_project=2)
    msg = "\n".join(sent)
    rendered = [i for i in range(5) if f"/id{i}/" in msg]
    assert len(rendered) == 2
    for i in range(5):
        assert brain_db.is_seen("reddit_pulse", f"id{i}", "p1") == (i in rendered)


@pytest.mark.asyncio
async def test_partial_delivery_marks_nothing_and_records_failure(monkeypatch):
    monkeypatch.setattr(
        "telegram.notify_long", AsyncMock(return_value=(1, 2)), raising=True
    )

    async def fake_search(*a, **kw):
        return [_post(1)]

    monkeypatch.setattr("reddit_search.search_subreddits", fake_search)
    await generators.hot_pulse_generator(brain_db)
    assert not brain_db.is_seen("reddit_pulse", "id1", "p1")
    assert brain_db.get_schedule("hot_pulse")["last_status"].startswith(
        "FAILED telegram 1/2"
    )


@pytest.mark.asyncio
async def test_reddit_outage_is_visible_in_status(monkeypatch):
    seen_kwargs: dict = {}

    async def blocked(*a, **kw):
        seen_kwargs.update(kw)
        for _ in range(3):
            kw["health"].record(403)
        return []

    monkeypatch.setattr("reddit_search.search_subreddits", blocked)
    monkeypatch.setattr("telegram.notify", AsyncMock(return_value=True))
    await generators.hot_pulse_generator(brain_db)
    assert isinstance(seen_kwargs["health"], reddit_search.SearchHealth)
    assert seen_kwargs["feed_fallback"] is True
    status = brain_db.get_schedule("hot_pulse")["last_status"]
    assert status.startswith("reddit outage") and "403" in status
