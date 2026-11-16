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

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, get_args

logger = logging.getLogger("clawrange.deals")

# A listing at or under this share of max_price_great is "insane".
INSANE_OF_TARGET = 0.85
# History qualifies a deal when the total is this far under the median.
GREAT_UNDER_MEDIAN = 0.20
INSANE_UNDER_MEDIAN = 0.40
# Medians from fewer observations are too noisy to act on.
MIN_HISTORY_SAMPLES = 8
# Default share of max_price_great under which a listing is assumed to be
# a scam, typo or accessory; profiles override it with `too_good_share`.
# max_price_great is already a great price, so half of it is rarely real.
TOO_GOOD_OF_TARGET = 0.50

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
# Category-specific red flags (e.g. barebone servers) are profile data:
# `red_flags: {<flag label>: [phrases]}`, matched case-insensitively at a
# word start, so "0 bay" does not fire on "10 bay".

# "<accessory> for <target>": the target's name appears only after "for"
# (cases, adapters, SSD kits, coolers). "R740 for AI" keeps its match
# because the name comes first.
_FOR_RE = re.compile(r"\bfor\b", re.IGNORECASE)
# GPU memory sizes a title states ("6GB", "24 GB"); RAM lots run larger.
_GB_RE = re.compile(r"\b(\d{1,3})\s?gb\b", re.IGNORECASE)

_PRICE_RE = re.compile(r"(?:\$|\bUSD\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")


# How much the stack wants a target, apart from price. Orders deals inside
# a price tier; never moves one across tiers.
Need = Literal["need", "want", "watch"]
NEED_LEVELS: tuple[Need, ...] = get_args(Need)
Tier = Literal["insane", "great"]

# Default target categories bought as parts (profile: `part_categories`).
# Everything else is a whole system, and so is a part listing whose title
# reads as a prebuilt PC.
COMPONENT_CATEGORIES = frozenset({"gpu", "mainboard"})
# Default categories whose titles must not state less memory than the
# target (profile: `memory_check_categories`).
MEMORY_CHECK_CATEGORIES = frozenset({"gpu"})
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
    need: Need = "want"
    fills: list[str] = field(default_factory=list)  # stack_gaps keys
    # Policy applied by load_targets from the profile block; the defaults
    # keep a hand-built target usable on its own.
    too_good_share: float = TOO_GOOD_OF_TARGET
    red_flags: dict[str, list[str]] = field(default_factory=dict)
    part: bool | None = None  # None: decide from COMPONENT_CATEGORIES
    check_memory: bool | None = None  # None: MEMORY_CHECK_CATEGORIES
    ebay_category: str = ""  # eBay category id scoping the search

    def __post_init__(self) -> None:
        # great_price divides every discount; zero or negative is meaningless.
        if not self.great_price > 0:
            raise ValueError(
                f"target '{self.name}' great_price must be positive, "
                f"got {self.great_price!r}"
            )

    @property
    def too_good_floor(self) -> float:
        return self.too_good_share * self.great_price


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
    # The source quotes shipping apart from price (eBay), so shipping=None
    # means unknown rather than included. Feed posts state one price.
    shipping_itemized: bool = False

    @property
    def shipping_unknown(self) -> bool:
        return self.shipping_itemized and self.shipping is None

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
    tier: Tier
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
        part = self.target.part
        if part is None:
            part = self.target.category in COMPONENT_CATEGORIES
        if part and not _PREBUILT_RE.search(self.listing.title):
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


def _has_words(text: str, words: list[str]) -> bool:
    squashed = _squash(text)
    return bool(words) and all(w in squashed for w in words)


def _vram_contradicts(text: str, target: WatchTarget) -> bool:
    """A title that states only smaller memory sizes than the target
    (a 6 GB or 16 GB laptop A5000 module against the 24 GB card)."""
    check = target.check_memory
    if check is None:
        check = target.category in MEMORY_CHECK_CATEGORIES
    if not check or not target.memory_gb:
        return False
    sizes = [int(g) for g in _GB_RE.findall(text) if 2 <= int(g) <= 192]
    return bool(sizes) and max(sizes) < target.memory_gb


def match_target(listing: Listing, targets: list[WatchTarget]) -> WatchTarget | None:
    """First target whose query words all appear in the offered title.

    Matching ignores case and punctuation ("MS01" matches "ms-01");
    any exclusion phrase vetoes the target, as does an accessory title
    ("Case for RTX 3090") or a GPU title whose stated memory is smaller
    than the target's.
    """
    offered = _offered_text(listing.title)
    if offered is None:
        return None
    title = _squash(offered)
    before_for = _FOR_RE.split(offered, maxsplit=1)[0]
    for t in targets:
        words = [_squash(w) for w in t.query.split() if _squash(w)]
        if not _has_words(offered, words):
            continue
        if any(_squash(x) and _squash(x) in title for x in t.exclude):
            continue
        if before_for != offered and not _has_words(before_for, words):
            continue
        if _vram_contradicts(offered, t):
            continue
        return t
    return None


def red_flags(listing: Listing, target: WatchTarget) -> list[str]:
    haystack = f"{listing.title} {listing.condition}".lower()
    flags = [p for p in RED_FLAG_PHRASES if p in haystack]
    for label, phrases in target.red_flags.items():
        if any(re.search(rf"(?<!\w){re.escape(p.lower())}", haystack) for p in phrases):
            flags.append(label)
    total = listing.total
    if total is not None and total < target.too_good_floor:
        flags.append(
            f"too good to be true (under {target.too_good_share:.0%} of great price)"
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

    # Unknown shipping is counted as $0 in total, so it can't prove an
    # insane price; such a listing can still rank as great.
    insane = not listing.shipping_unknown and (
        total <= INSANE_OF_TARGET * target.great_price
        or (below_median is not None and below_median >= INSANE_UNDER_MEDIAN)
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

    Block-level policy is applied to every target: the shared `exclude`
    list is appended to each target's own, and `too_good_share`,
    `red_flags`, `part_categories`, `memory_check_categories` and
    `sources.ebay.categories` (target category -> eBay category id) set
    the per-target fields. Every key is optional.
    """
    shared = _phrases(block.get("exclude"), "homelab_deals.exclude")
    share = float(block.get("too_good_share") or TOO_GOOD_OF_TARGET)
    flags = {str(k): list(v or []) for k, v in (block.get("red_flags") or {}).items()}
    # An explicit empty list means "none", so test for None, not falsiness.
    parts = block.get("part_categories")
    part_cats = COMPONENT_CATEGORIES if parts is None else frozenset(parts)
    memory = block.get("memory_check_categories")
    memory_cats = MEMORY_CHECK_CATEGORIES if memory is None else frozenset(memory)
    ebay_cats = ((block.get("sources") or {}).get("ebay") or {}).get("categories") or {}
    out = []
    for t in block.get("targets") or []:
        category = t.get("category", "other")
        name = t["name"]
        out.append(
            WatchTarget(
                name=name,
                query=t["query"],
                category=category,
                great_price=float(t["max_price_great"]),
                exclude=_phrases(t.get("exclude"), f"target '{name}' exclude") + shared,
                memory_gb=t.get("min_vram_gb") or t.get("memory_gb"),
                note=t.get("note", ""),
                parts_cost=float(t["parts_cost"]) if t.get("parts_cost") else None,
                need=t.get("need", "want"),
                fills=list(t.get("fills") or []),
                too_good_share=share,
                # Each target gets its own copy: one shared dict would let a
                # change to one target's flags leak into every other.
                red_flags={k: list(v) for k, v in flags.items()},
                part=category in part_cats,
                check_memory=category in memory_cats,
                ebay_category=str(ebay_cats.get(category) or ""),
            )
        )
    return out


def _phrases(value, where: str) -> list[str]:
    """A list of phrases; a bare string would be split into characters."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where} must be a list of phrases, got {value!r}")
    return list(value)


def stale_observation(row: dict, targets: list[WatchTarget]) -> bool:
    """Whether a stored observation no longer belongs in a target's history.

    Stale when its target is gone, its price is under the too-good floor,
    or its title no longer matches that target under today's rules. Rows
    recorded before titles were stored are judged on price alone.
    """
    by_name = {t.name: t for t in targets}
    target = by_name.get(row["target"])
    if target is None:
        return True
    if row["price"] < target.too_good_floor:
        return True
    title = row.get("title") or ""
    if not title:
        return False
    listing = Listing(row["source"], row["listing_id"], title, "", row["price"])
    matched = match_target(listing, targets)
    return matched is None or matched.name != target.name


def history_stats(
    rows: list[dict], targets: list[WatchTarget], days: int = 30
) -> dict[str, PriceStats | None]:
    """Per-target median over the window, skipping stale observations.

    Filtering at read time, never deleting: tightening a match rule cleans
    the medians at once, and loosening it, renaming a target or an
    over-broad exclude loses nothing.
    """
    from datetime import UTC, datetime, timedelta
    from statistics import median

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    prices: dict[str, list[float]] = {t.name: [] for t in targets}
    for r in rows:
        if r["target"] in prices and r["observed_at"] >= cutoff:
            if not stale_observation(r, targets):
                prices[r["target"]].append(float(r["price"]))
    return {
        name: PriceStats(median=float(median(ps)), samples=len(ps)) if ps else None
        for name, ps in prices.items()
    }


# ─── Report ──────────────────────────────────────────────────────────

MAX_CLEAN = 12
MAX_FLAGGED = 5
MAX_MISSES = 3
MAX_LEADS = 5
# Listings shown per target in each section. The cheap end of a common
# model's market is crowded with near-identical boxes (nine R730s on
# 2026-11-14); the rest become a "+N more" line.
MAX_PER_TARGET = 2


def _cap_per_target(ranked: list[Deal], max_per_target: int) -> list[Deal]:
    seen: dict[str, int] = {}
    kept = []
    for d in ranked:
        seen[d.target.name] = seen.get(d.target.name, 0) + 1
        if seen[d.target.name] <= max_per_target:
            kept.append(d)
    return kept


def overflow_counts(found: list[Deal], shown: list[Deal]) -> dict[str, int]:
    """Per target, how many found deals the report does not show."""
    shown_ids = {id(d) for d in shown}
    more: dict[str, int] = {}
    for d in found:
        if id(d) not in shown_ids:
            more[d.target.name] = more.get(d.target.name, 0) + 1
    return more


def order_and_number(
    found: list[Deal], max_per_target: int = MAX_PER_TARGET
) -> tuple[list[Deal], list[Deal]]:
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
    clean = _cap_per_target(clean, max_per_target)[:MAX_CLEAN]
    flagged = _cap_per_target(flagged, max_per_target)[:MAX_FLAGGED]
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
    more: dict[str, int] | None = None,
    goal: str = "",
    title: str = "Deal rundown",
) -> str:
    """Render the daily deal rundown. Never returns an empty report.

    `leads` are matched listings whose price could not be parsed (forum
    posts often put it in a table); they are worth a click, not a verdict.
    """

    from rundown import _clean_title, _escape_md, link_refs, report_header

    if not all(d.ref for d in found):
        clean, flagged = order_and_number(found)
    else:
        clean = [d for d in found if not d.flags]
        flagged = [d for d in found if d.flags]
    lines = [report_header(_escape_md(title), now)]
    if goal:
        lines.append(f"Goal: {_escape_md(goal)}")
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
        body = re.sub(r"^\s*BUY READ:?\s*\n+", "", synthesis, flags=re.IGNORECASE)
        lines += ["*Buy read*", link_refs(one_verdict_per_deal(body), by_ref), ""]

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

    if more:
        lines.append(
            "Also matched: "
            + _escape_md(", ".join(f"+{n} more {name}" for name, n in more.items()))
        )
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

_VERDICT_RE = re.compile(
    r"^(?P<lead>[\s*•\-\d.)]*)(?P<verdict>BUY|CONDITIONAL|SKIP|WATCH)\s+"
    r"(?P<refs>(?:\[?D\d+\]?(?:\s*(?:,|&|\band\b)\s*)?)+)",
)


def one_verdict_per_deal(text: str) -> str:
    """Keep each deal in the first verdict bullet that names it.

    The prompt asks for "VERDICT D#: reason" bullets, one per deal, but the
    model sometimes files a deal under two verdicts (2026-11-14: D6 under
    both "barebone" and "drive caddy" SKIPs). Later verdict heads lose any
    ref already given a verdict; a bullet left with none is dropped. Refs
    in a bullet's body ("pair with D1") are cross-references and stay.
    """
    claimed: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        m = _VERDICT_RE.match(line)
        if not m:
            out.append(line)
            continue
        refs = re.findall(r"D\d+", m.group("refs"))
        fresh = [r for r in dict.fromkeys(refs) if r not in claimed]
        if not fresh:
            continue
        claimed.update(fresh)
        rest = line[m.end() :]
        head = f"{m.group('lead')}{m.group('verdict')} {', '.join(fresh)}"
        out.append(head + ("" if rest[:1] in (":", "") else " ") + rest)
    return "\n".join(out)


DEFAULT_AUDIENCE = "a buyer watching for hardware deals against the targets below"


async def synthesize(
    found: list[Deal],
    current_setup: str,
    stack_gaps: dict[str, str] | None = None,
    advisor: dict | None = None,
) -> str | None:
    """LLM 'buy read' over numbered deals. Returns raw text or None.

    The model may cite deals only as [D#]; render_report links known refs
    and strips everything else. The rules here hold for any deal list;
    what the buyer wants and domain rules (form factors, soldered memory,
    what a part needs to run) come from the profile's `advisor` block:
    `audience` (who is advised, and for what) and `rules` (list).
    """
    if not found:
        return None
    import llm_proxy

    advisor = advisor or {}
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
    rules = [str(r).strip() for r in advisor.get("rules") or [] if str(r).strip()]
    prompt = (
        f"You advise {advisor.get('audience') or DEFAULT_AUDIENCE}.\n"
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
        "bullets: which deal (cite [D#]) best serves the stack gaps per "
        "dollar and why; fit with the current setup and any extra parts the "
        "item itself requires; and which to skip, including any flagged "
        "listing. Mark any performance or cost estimate 'needs "
        "verification'. Judge each whole system against its parts: a bundle "
        "priced under the cost of buying its parts separately is a strong "
        "buy, one over it rarely is. Use the stated parts cost, today's part "
        "deals by [D#], or an estimate marked 'needs verification'. Weigh "
        "tags: 'need' and 'fills <gap>' items matter more to this stack than "
        "'watch' items at the same price. "
        "A deal's title is what the item actually is; the target name is "
        "only what it was searched for. If a title describes a different "
        "product (a camera or soundbar sharing a model number, an adapter "
        "board), recommend skipping it. Say a listing lacks parts only when "
        "its FLAGS or its own title say so; judge each listing on its own "
        "title, not on others of the same model. If nothing is worth buying, say so "
        "in one line. Cite each deal in one bullet only: start every deal "
        "bullet with BUY, CONDITIONAL or SKIP and its refs, e.g. 'SKIP D4, "
        "D6: reason'. Start with the first bullet, no heading."
        + (
            "\n\nRules for this setup:\n" + "\n".join(f"- {r}" for r in rules)
            if rules
            else ""
        )
    )
    try:
        return await llm_proxy._llm_call(prompt, max_tokens=1200)
    except Exception:
        # The report degrades to "synthesis off"; the cause must stay visible.
        logger.exception("deals: synthesis failed")
        return None
