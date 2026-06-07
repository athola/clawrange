# John-117 Research and Marketing Upgrade — Plan

This document is the planning artifact for the overnight upgrade
that teaches John-117 to do multi-source research and disciplined
top-of-funnel marketing for `claude-night-market`, `skrills`,
`simple-resume`, and Alex's AI-systems expertise. Companion code
lives under `workflows/research.py`, `workflows/generators.py`,
and the `/research` endpoints.

## Context

John-117 already runs a marketing orchestrator with scheduled
Reddit and GitHub scans (`morning_scan`, `weekly_traffic`,
`awesome_lists_watch`). What he is missing:

1. **Research depth**: he can call `/scan/web` (GLM server-side
   web search) but cannot triangulate findings across channels
   or persist a research session for recall.
2. **Personal-brand surfaces**: the orchestrator knows about
   three products but not Alex's AI-systems expertise or the
   adjacent topics (agent platforms, plugin ecosystems, MSP
   tooling) where his voice belongs.
3. **Content production**: scans surface relevant posts but no
   workflow drafts an actual reply or content idea, so Alex
   has to compose every piece from scratch.

## Research findings (May 2026)

### Research-agent design
- **Citation discipline is non-negotiable**: every claim links
  to a source URL or DOI; single-source claims get flagged as
  "needs verification". General-purpose LLMs fabricate citations,
  so research tools must be grounded in real fetches. [^research-tools]
- **Multi-source synthesis beats summarization**: useful agents
  compare findings, surface contradictions, and produce a
  ranked, themed report. [^synthesis]
- **Anthropic's lead/subagent pattern** beat single-agent Claude
  Opus by 90.2% on Anthropic's research eval, at ~15x token cost.
  Subagents return *findings*, not raw context — compression at
  the edges. [^anthropic-multi-agent]
- **Most "multi-agent" tasks** can be handled by a single ReAct
  agent with good tools and a well-structured prompt. The
  sophistication is in the tool catalog and termination logic,
  not in the topology. [^react]
- **Triangulation bonus** (cross-channel corroboration) and
  **authority bonuses** (GitHub stars, HN score, citation count,
  Reddit upvotes) are how `tome` scores findings — port them.

### Marketing for developer tools
- **Be a community member, not a marketer**: lurk for 1-2 weeks
  on each target subreddit, comment on others' posts first, then
  share your work as a natural solution to a real problem. [^reddit-playbook]
- **r/SideProject post format**: `[Launch] Name — one-liner`
  ≤100 chars + opening (problem you faced) + journey (stack,
  hardest part) + 3-5 specific features + ending question. [^sideproject]
- **Honest "rough around edges" disclaimers** outperform polished
  pitches. Members can smell a low-effort pitch. [^sideproject]
- **r/SideProject best posting time**: weekend mornings 8-10am ET. [^sideproject]
- **HN converts ~3x better than Reddit** but reaches 3-5x fewer
  developers. Use HN for depth, Reddit for breadth. [^reddit-vs-hn]
- **X in 2026 boosts external article links** (Medium, dev.to,
  Substack). The 70/30 reply strategy (mostly engage, sometimes
  post) outperforms posting-only. AI-drafted content is fine;
  automated replies and follow loops are not. [^x-strategy]
- **E-E-A-T**: Experience is the most heavily weighted signal —
  first-hand insights and proprietary data win. [^content-strategy]

### Implications for John-117
- He runs in OpenClaw (single agent), so the **tool-catalog**
  approach fits: give him richer endpoints, not subagents.
- The **research orchestrator** lives server-side in
  `workflows/research.py` and fans out across channels.
  John-117 calls it as a single tool.
- **Never auto-post**. Comment drafts and content ideas land
  in the task queue; Alex approves before anything is sent.
- **Triangulation + citations** are the discipline that makes
  research output trustworthy.

## Implementation plan

### Commit 1 — `feat(research): multi-source orchestrator`
- Add `workflows/research.py` with `Finding` dataclass,
  `merge_findings`, `deduplicate`, `compute_relevance_score`,
  `rank_findings`, `compute_triangulation_bonus`, and
  `orchestrate_research(topic, channels)`.
- Add `POST /research` to `workflows/app.py`.
- Tests in `workflows/tests/test_research.py`.

### Commit 2 — `feat(brain): persist research sessions`
- Add `research_sessions` and `research_findings` tables to
  `brain_db.py`.
- Methods: `create_research_session`, `add_research_finding`,
  `complete_research_session`, `get_research_session`,
  `list_research_sessions`.
- Endpoints: `GET /research/sessions`, `GET /research/sessions/{id}`.
- Tests in `workflows/tests/test_brain_db_research.py`.

### Commit 3 — `feat(marketing): personal brand + content ideas`
- Seed a `personal-brand` project (owner: athola) tracking
  AI-agents, plugins, MSP tooling.
- Add `content_idea_generator` to the GENERATORS registry — pulls
  recent research findings and proposes 3 content angles
  (technical post, Reddit comment, X thread).
- Tests in `workflows/tests/test_marketing.py`.

### Commit 4 — `feat(marketing): comment-draft generator`
- Add `comment_draft_generator` to GENERATORS — drafts a useful,
  non-promotional comment for a given URL with optional product
  mention discipline (mention only if directly relevant).
- Drafts queue as tasks tagged `comment-draft` for Alex review.
- Tests in `workflows/tests/test_marketing.py`.

### Commit 5 — `feat(persona): research and marketing posture`
- Update `openclaw/soul.md` with Research Mode (multi-source,
  citations, triangulation) and Marketing Mode (useful-comments
  first, never auto-post).
- Update `HEARTBEAT.md` with daily content-idea pulse.
- Reflect new tools and product surfaces.

### Commit 6 — `docs: surface new capabilities`
- New `docs/research-and-marketing.md` (operator guide).
- README: new Research section.
- CLAUDE.md: research/marketing conventions.

## Acceptance criteria

- All new tests pass under `make test-unit` (pytest).
- `ruff` and `ruff-format` clean on all touched Python files.
- `validate_stack.py` passes (pre-push).
- README diff stays under 200 lines.
- No new top-level dependencies; everything uses existing httpx
  and SQLite.

[^research-tools]: PapersFlow, "12 Best AI Research Tools in 2026",
    https://papersflow.ai/blog/best-ai-research-tools-2026
[^synthesis]: Jenova.ai, "AI Research Assistant Guide 2026",
    https://www.jenova.ai/en/resources/ai-research-assistant
[^anthropic-multi-agent]: Anthropic Engineering, "How we built our
    multi-agent research system",
    https://www.anthropic.com/engineering/multi-agent-research-system
[^react]: IBM, "What is a ReAct Agent?",
    https://www.ibm.com/think/topics/react-agent ; Innovatrix,
    "Agentic AI Design Patterns 2026",
    https://www.innovatrixinfotech.com/blog/agentic-ai-design-patterns-react-reflection-tool-use
[^reddit-playbook]: Growth Tools, "Reddit Marketing for B2B SaaS in
    2026",
    https://gingiris.github.io/growth-tools/blog/2026/03/30/reddit-marketing-guide-how-to-promote-without-getting-banned/
[^sideproject]: MediaFa.st, "How to Market on r/SideProject",
    https://www.mediafa.st/marketing-on-rsideproject
[^reddit-vs-hn]: Teract.ai, "Reddit vs Hacker News 2026",
    https://www.teract.ai/resources/reddit-vs-hackernews-tech-marketing-2026
[^x-strategy]: Teract.ai, "Twitter Strategy for Indie Hackers 2026",
    https://www.teract.ai/resources/twitter-strategy-indie-hackers-2026
[^content-strategy]: Search Engine Land, "Content strategy in 2026",
    https://searchengineland.com/guide/content-strategy-in-2026
