# Income Loop: $50 Crypto Testbed, Strategy v0

Week of 2026-07-06. Reevaluated every Sunday via the weekly review task.

## Honest expectations: read this first

- **No legitimate strategy produces a reliably high weekly return.**
  Claims of "1% a day" or guaranteed weekly percentages are scams,
  survivorship bias, or hidden leverage. Treat them as disqualifying.
- Realistic anchors: USDC rewards run ~4–5% APY (≈ $0.04/week on $50).
  BTC/ETH exposure has historically strong but violent returns:
  +60–100% in good years, −50% or worse in bad ones.
- **Fees dominate small accounts.** At 0.25–0.6% per side, one round
  trip on $50 costs $0.25–0.60. A strategy that trades daily gives up
  20%+ per month to fees before it earns anything. Most retail active
  traders underperform simple buy-and-hold after fees.
- Even a *great* year (+100%) turns $50 into $100. This account will
  not generate income. Its job is different:

**The $50 account is a testbed.** It exists to prove the loop
(research → propose → approve → execute → weekly review) at stakes
where mistakes cost lunch money. Capital only scales after the loop
has a multi-week track record you trust.

## Strategy v0 (initial allocation)

Venue: Kraken Pro (0.16/0.26% maker/taker) or Coinbase Advanced
(0.40/0.60%). Use **limit orders** on the pro interface. Never the
instant-buy widget (~1.5–2% effective fee).

| Sleeve | Amount | What | Why |
|--------|--------|------|-----|
| Core | $20 | BTC | The only realistic return driver at this size is market beta |
| Core | $10 | ETH | Diversifies the beta slightly |
| Reserve | $20 | USDC, opt into exchange rewards (~4% APY) | Dry powder and the "boring baseline" every review compares against |

**Active sleeve: paper only for the first 4 weeks.** One decision per
week: if BTC's Friday close is below its 20-week SMA, paper-rotate the
core to USDC. If above, hold. Log every paper decision to the brain as
`income:paper: <decision + prices>`. After 4 weeks, the review decides
whether the paper sleeve earned real execution.

## Hard risk rules (non-negotiable until a review changes them)

1. No leverage, margin, futures, or options. Ever, at this size.
2. Exchange API keys get **query and trade permissions only, never
   withdrawal**. Keys live in `.env` (gitignored), nowhere else.
3. Maximum one real order per week (fee control).
4. Universe is BTC, ETH, USDC only. Anything else needs an approved
   proposal first. No memecoins.
5. **The system never trades on its own.** Every strategy change or
   trade proposal ships as a `[DRAFT]` task and requires explicit
   approval on Telegram, same convention as marketing drafts.

## Setup steps (operator, ~30 minutes, one time)

1. Create a Kraken account, complete KYC, enable 2FA.
2. Deposit $50 (ACH is free. Card deposits eat ~2%).
3. On Kraken Pro, place limit orders: ~$20 BTC, ~$10 ETH. Convert the
   remaining ~$20 to USDC and opt into rewards.
4. Create an API key with **Query Funds and Create/Modify Orders** only.
   Store as `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` in `.env`.
5. Tell John-117 the entry prices on Telegram (or add a brain note
   prefixed `income: entry`) so the weekly review has a baseline.

## Weekly review protocol (Sundays)

The `income_review` generator enqueues a `[DRAFT] income: weekly
strategy review` task and pings Telegram. The review must cover:

- Current balances and P&L versus two benchmarks: (a) the original
  $50 held as USDC, (b) $50 put 100% into BTC on day one.
- The paper sleeve's decision and hypothetical result.
- Fees paid this week.
- Proposed adjustments, each with a cited source per the project's
  citation discipline (single-source claims flagged).

The operator approves, adjusts, or rejects. Rejected proposals are
logged so the same idea doesn't resurface without new evidence.

## Telegram interaction surface

- **Describe a technique**: just ask John-117 ("explain funding-rate
  arbitrage"), routed through the LLM proxy, citation rules apply.
- **Research a technique**: ask for deep research. John-117 queues a
  `research:tome: <topic>` task, and the local tome bridge runs it
  through `/tome:research`.
- **Weekly review**: arrives as a `[DRAFT]`. Reply to approve/adjust.
- Planned (not built): `!income status` proxy command family.

## Reevaluation triggers (outside the weekly cadence)

- Portfolio down >25% from baseline → immediate review task.
- Any rule above was violated → halt, review, fix the loop first.
- 4 consecutive weeks of paper-sleeve outperformance → consider
  promoting it to real execution (still one order/week, still
  approval-gated).
