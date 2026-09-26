"""Homelab deal scanner: pure scoring core.

A daily scan pulls listings from several sources (see `deal_sources.py`),
matches each to a watch target from the profile's `homelab_deals` block,
and classifies it against two references:

- the target's `max_price_great`: a researched "great deal" price, and
- the rolling median of asking prices this scanner has itself observed
  for that target (no sold-price API is available to a normal developer,
  so history builds up from our own scans).

Everything here is pure so the thresholds are easy to test and tune.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A listing at or under this share of max_price_great is "insane".
INSANE_OF_TARGET = 0.85
# History qualifies a deal when the total is this far under the median.
GREAT_UNDER_MEDIAN = 0.20
INSANE_UNDER_MEDIAN = 0.40
# Medians from fewer observations are too noisy to act on.
MIN_HISTORY_SAMPLES = 8
# Under this share of max_price_great, assume scam, typo, or parts-only.
TOO_GOOD_OF_TARGET = 0.35

RED_FLAG_PHRASES = (
    "for parts",
    "parts only",
    "not working",
    "untested",
    "as is",
    "as-is",
    "broken",
    "faulty",
    "damaged",
    "read description",
    "box only",
    "empty box",
    "no returns",
    "locked",
)

_PRICE_RE = re.compile(r"(?:\$|\bUSD\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")


# How much the stack wants a target, apart from price. Orders deals inside
# a price tier; never moves one across tiers.
NEED_LEVELS = ("need", "want", "watch")

# Target categories bought as parts. Everything else is a whole system, and
# so is a part-category listing whose title reads as a prebuilt PC.
COMPONENT_CATEGORIES = frozenset({"gpu", "mainboard"})
_PREBUILT_RE = re.compile(
    r"\b(?:pre-?built|gaming (?:pc|desktop|rig)|desktop pc"
    r"|complete (?:pc|system|build))\b",
    re.IGNORECASE,
)


@dataclass
class WatchTarget:
    name: str
    query: str
    category: str
    great_price: float
    exclude: list[str] = field(default_factory=list)
    memory_gb: int | None = None  # VRAM or unified memory, for the report
    note: str = ""
    # What the same build costs bought as separate parts. A bundle under it
    # is a deal even above great_price.
    parts_cost: float | None = None
    need: str = "want"  # one of NEED_LEVELS
    fills: list[str] = field(default_factory=list)  # stack_gaps keys


@dataclass
class Listing:
    source: str
    listing_id: str
    title: str
    url: str
    price: float | None
    shipping: float | None = None
    condition: str = ""
    seller_feedback: int | None = None
    location: str = ""

    @property
    def total(self) -> float | None:
        if self.price is None:
            return None
        return self.price + (self.shipping or 0.0)


@dataclass
class PriceStats:
    median: float
    samples: int


@dataclass
class Deal:
    listing: Listing
    target: WatchTarget
    total: float
    tier: str  # "insane" | "great"
    pct_below_target: float
    pct_below_median: float | None
    flags: list[str]
    history_samples: int = 0
    ref: str = ""
    pct_below_parts: float | None = None

    @property
    def tags(self) -> list[str]:
        """Desirability tags: need level, gaps filled, and price signals
        beyond the tier (bundle under its parts, drop versus history)."""
        tags = [self.target.need] + [f"fills {g}" for g in self.target.fills]
        if self.form == "system" and self.pct_below_parts and self.pct_below_parts > 0:
            tags.append(f"bundle < parts -{self.pct_below_parts:.0%}")
        if self.pct_below_median is not None and (
            self.pct_below_median >= GREAT_UNDER_MEDIAN
        ):
            tags.append(f"price drop -{self.pct_below_median:.0%}")
        return tags

    @property
    def form(self) -> str:
        """'part' or 'system': prebuilt PCs usually cost more than parts."""
        if self.target.category in COMPONENT_CATEGORIES and not _PREBUILT_RE.search(
            self.listing.title
        ):
            return "part"
        return "system"


def parse_price(text: str) -> float | None:
    """First dollar amount in free text (feed titles), or None."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    whole = float(m.group(1).replace(",", ""))
    return whole + (float(m.group(2)) / 100 if m.group(2) else 0.0)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_WANT_POST_RE = re.compile(r"^\s*\[(w|wtb)\]", re.IGNORECASE)
_HAVE_RE = re.compile(r"\[h\](.*?)(?:\[w\]|$)", re.IGNORECASE)


def _offered_text(title: str) -> str | None:
    """The part of a sale-post title describing what is offered.

    r/homelabsales want posts start with [W]; r/hardwareswap titles read
    "[H] <have> [W] <want>", and only the have side should match.
    """
    if _WANT_POST_RE.match(title):
        return None
    m = _HAVE_RE.search(title)
    return m.group(1) if m else title


def match_target(listing: Listing, targets: list[WatchTarget]) -> WatchTarget | None:
    """First target whose query words all appear in the offered title.

    Matching ignores case and punctuation ("MS01" matches "ms-01");
    any exclusion phrase vetoes the target.
    """
    offered = _offered_text(listing.title)
    if offered is None:
        return None
    title = _squash(offered)
    for t in targets:
        words = [_squash(w) for w in t.query.split() if _squash(w)]
        if not words or not all(w in title for w in words):
            continue
        if any(_squash(x) and _squash(x) in title for x in t.exclude):
            continue
        return t
    return None


def red_flags(listing: Listing, target: WatchTarget) -> list[str]:
    haystack = f"{listing.title} {listing.condition}".lower()
    flags = [p for p in RED_FLAG_PHRASES if p in haystack]
    total = listing.total
    if total is not None and total < TOO_GOOD_OF_TARGET * target.great_price:
        flags.append(
            f"too good to be true (under {TOO_GOOD_OF_TARGET:.0%} of great price)"
        )
    if listing.seller_feedback == 0:
        flags.append("seller has no feedback")
    return flags


def classify(
    listing: Listing, target: WatchTarget, history: PriceStats | None
) -> Deal | None:
    """Return a Deal when the total beats the target or recent history."""
    total = listing.total
    if total is None or total <= 0:
        return None
    usable = history if history and history.samples >= MIN_HISTORY_SAMPLES else None
    below_median = 1 - total / usable.median if usable else None

    insane = total <= INSANE_OF_TARGET * target.great_price or (
        below_median is not None and below_median >= INSANE_UNDER_MEDIAN
    )
    below_parts = 1 - total / target.parts_cost if target.parts_cost else None
    great = (
        total <= target.great_price
        or (below_median is not None and below_median >= GREAT_UNDER_MEDIAN)
        or (below_parts is not None and below_parts > 0)
    )
    if not (insane or great):
        return None
    return Deal(
        listing=listing,
        target=target,
        total=total,
        tier="insane" if insane else "great",
        pct_below_target=1 - total / target.great_price,
        pct_below_median=below_median,
        flags=red_flags(listing, target),
        history_samples=usable.samples if usable else 0,
        pct_below_parts=below_parts,
    )


def load_targets(block: dict) -> list[WatchTarget]:
    """Build watch targets from a profile's `homelab_deals` block.

    The block's shared `exclude` list (accessories, laptops) is appended to
    every target's own exclusions.
    """
    shared = list(block.get("exclude") or [])
    out = []
    for t in block.get("targets") or []:
        out.append(
            WatchTarget(
                name=t["name"],
                query=t["query"],
                category=t.get("category", "other"),
                great_price=float(t["max_price_great"]),
                exclude=list(t.get("exclude") or []) + shared,
                memory_gb=t.get("min_vram_gb") or t.get("memory_gb"),
                note=t.get("note", ""),
                parts_cost=float(t["parts_cost"]) if t.get("parts_cost") else None,
                need=t.get("need", "want"),
                fills=list(t.get("fills") or []),
            )
        )
    return out


# ─── Report ──────────────────────────────────────────────────────────

MAX_CLEAN = 12
MAX_FLAGGED = 5
MAX_MISSES = 3
MAX_LEADS = 5


def order_and_number(found: list[Deal]) -> tuple[list[Deal], list[Deal]]:
    """Split clean from flagged deals, rank, cap, and assign D# refs.

    Clean deals rank insane-first, then by need level, then gap-fillers
    first, then by discount to the target price. Flagged deals are numbered
    after them.
    """
    clean = [d for d in found if not d.flags]
    flagged = [d for d in found if d.flags]
    clean.sort(
        key=lambda d: (
            d.tier != "insane",
            NEED_LEVELS.index(d.target.need),
            not d.target.fills,
            -d.pct_below_target,
        )
    )
    flagged.sort(key=lambda d: -d.pct_below_target)
    clean, flagged = clean[:MAX_CLEAN], flagged[:MAX_FLAGGED]
    for i, d in enumerate(clean + flagged, 1):
        d.ref = f"D{i}"
    return clean, flagged


def _parts_saving(d: Deal) -> str:
    """'N% under ~$X parts cost' when the target has a parts benchmark."""
    if d.pct_below_parts is None or d.target.parts_cost is None:
        return ""
    cost = f"~${d.target.parts_cost:,.0f} parts cost"
    if d.pct_below_parts >= 0:
        return f"{d.pct_below_parts:.0%} under {cost}"
    return f"{-d.pct_below_parts:.0%} over {cost}"


def _deal_lines(d: Deal) -> list[str]:
    from rundown import _clean_title, _escape_md

    facts = [
        f"${d.total:,.0f} total",
        f"{d.pct_below_target:.0%} under ${d.target.great_price:,.0f} great price"
        if d.pct_below_target >= 0
        else f"{-d.pct_below_target:.0%} over ${d.target.great_price:,.0f} great price",
    ]
    if d.pct_below_median is not None:
        facts.append(
            f"{d.pct_below_median:.0%} under 30d median ({d.history_samples} seen)"
        )
    if saving := _parts_saving(d):
        facts.append(saving)
    facts.append(d.listing.source)
    if d.listing.condition:
        facts.append(d.listing.condition)
    what = d.target.name + (f" · {d.target.memory_gb} GB" if d.target.memory_gb else "")
    what += " · part" if d.form == "part" else " · whole system"
    if d.target.note:
        what += f" · {d.target.note}"
    tags = " ".join(f"[{t}]" for t in d.tags)
    lines = [
        f"• {d.ref} [{_clean_title(d.listing.title)}]({d.listing.url}) "
        f"{_escape_md(tags)}",
        f"  {_escape_md(' · '.join(facts))}",
        f"  {_escape_md(what)}",
    ]
    if d.flags:
        lines.append(f"  ⚠ {_escape_md('; '.join(d.flags))}")
    return lines


def render_report(
    found: list[Deal],
    misses: list[tuple[Listing, WatchTarget]],
    statuses: list,
    synthesis: str | None,
    now,
    leads: list[tuple[Listing, WatchTarget]] | None = None,
) -> str:
    """Render the daily deal rundown. Never returns an empty report.

    `leads` are matched listings whose price could not be parsed (forum
    posts often put it in a table); they are worth a click, not a verdict.
    """
    from zoneinfo import ZoneInfo

    from rundown import REPORT_TZ, _clean_title, _escape_md, link_refs

    if not all(d.ref for d in found):
        clean, flagged = order_and_number(found)
    else:
        clean = [d for d in found if not d.flags]
        flagged = [d for d in found if d.flags]
    local = now.astimezone(ZoneInfo(REPORT_TZ))
    lines = [
        f"*Homelab deal rundown — {local:%a %b %d}* ({local:%H:%M %Z})",
        "Goal: hardware that moves inference off remote tiers onto the homelab.",
    ]
    if statuses:
        lines.append(
            "Sources: "
            + " · ".join(
                f"{s.name} {'✓' if s.ok else '✗'} {_escape_md(s.detail)}"
                for s in statuses
            )
        )
    lines.append("")

    if synthesis and (clean or flagged):
        by_ref = {d.ref: d.listing.url for d in clean + flagged}
        lines += ["*Buy read*", link_refs(synthesis, by_ref), ""]

    insane = [d for d in clean if d.tier == "insane"]
    great = [d for d in clean if d.tier == "great"]
    if insane:
        lines.append("*🔥 Insane deals*")
        for d in insane:
            lines += _deal_lines(d)
        lines.append("")
    if great:
        lines.append("*Great deals*")
        for d in great:
            lines += _deal_lines(d)
        lines.append("")
    if flagged:
        lines.append("*Look twice* (cheap, but red flags)")
        for d in flagged:
            lines += _deal_lines(d)
        lines.append("")

    if not clean and not flagged:
        lines.append("No listing beat a target today.")
        if misses:
            lines.append("Closest:")
            for listing, target in misses[:MAX_MISSES]:
                over = (listing.total or 0) / target.great_price - 1
                lines.append(
                    f"• [{_clean_title(listing.title)}]({listing.url}) — "
                    f"${listing.total:,.0f}, {over:.0%} over "
                    f"${target.great_price:,.0f} ({_escape_md(target.name)})"
                )
        lines.append("")

    if leads:
        lines.append("*Leads* (matched a target, no price parsed)")
        for listing, target in leads[:MAX_LEADS]:
            lines.append(
                f"• [{_clean_title(listing.title)}]({listing.url}) — "
                f"{_escape_md(target.name)} · {_escape_md(listing.source)}"
            )
        lines.append("")

    fixes = [s.fix for s in statuses if s.fix]
    if fixes:
        lines.append("*Setup needed*")
        lines += [f"{i}. {_escape_md(f)}" for i, f in enumerate(fixes, 1)]
    return "\n".join(lines).strip()


# ─── Synthesis ───────────────────────────────────────────────────────


async def synthesize(
    found: list[Deal], current_setup: str, stack_gaps: dict[str, str] | None = None
) -> str | None:
    """LLM 'buy read' over numbered deals. Returns raw text or None.

    The model may cite deals only as [D#]; render_report links known refs
    and strips everything else. Performance claims must be hedged because
    tokens/sec figures in the model's memory are often stale.
    """
    if not found:
        return None
    import llm_proxy

    lines = []
    for d in found:
        mem = f"{d.target.memory_gb} GB" if d.target.memory_gb else "memory n/a"
        flags = f" FLAGS: {'; '.join(d.flags)}" if d.flags else ""
        saving = f", {_parts_saving(d)}" if _parts_saving(d) else ""
        lines.append(
            f"[{d.ref}] {d.target.name} ({d.target.category}, {mem}, "
            f"{'part' if d.form == 'part' else 'whole system'}) "
            f"${d.total:,.0f} total, {d.pct_below_target:.0%} under the "
            f"${d.target.great_price:,.0f} great price{saving}, "
            f"via {d.listing.source}, tags: {', '.join(d.tags)}. "
            f"Title: {d.listing.title}{flags}"
        )
    prompt = (
        "You advise a homelab owner who wants to replace part of their "
        "remote LLM inference (OpenRouter free/paid tiers and Z.AI GLM, used "
        "for summarization, classification and short tool-using tasks) with "
        "open-weight models served locally behind an OpenAI-compatible proxy.\n"
        f"Current setup: {current_setup or 'not described'}\n"
        + (
            "Stack gaps: "
            + "; ".join(f"{k}: {v}" for k, v in stack_gaps.items())
            + "\n"
            if stack_gaps
            else ""
        )
        + "\n"
        "Today's deals (only these exist):\n" + "\n".join(lines) + "\n\n"
        "Write a plain-text BUY READ, no Markdown, no URLs, at most 6 "
        "bullets: which deal (cite [D#]) moves the most inference local per "
        "dollar and why; what class of model each worthwhile deal could "
        "serve (mark tokens/sec estimates 'needs verification'); fit with "
        "the current setup (power, noise, networking, extra parts needed); "
        "and which to skip, including any flagged listing. Judge each whole "
        "system against its parts: a bundle priced under the cost of buying "
        "its parts separately is a strong buy, one over it rarely is. Use the "
        "stated parts cost, today's part deals by [D#], or an estimate marked "
        "'needs verification'. Weigh tags: 'need' and 'fills <gap>' items "
        "matter more to this stack than 'watch' items at the same price. "
        "Unified-memory boxes (Strix Halo, Mac) have soldered "
        "memory, so their only parts route is a bare mainboard. If nothing "
        "is worth buying, say so in one line."
    )
    try:
        return await llm_proxy._llm_call(prompt, max_tokens=1200)
    except Exception:
        return None
