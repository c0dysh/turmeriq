# Part B — Reading and sending across whatever tool the brand already uses

The three brands differ only in **how much their tool will tell us and let us do**:

| | Brand A (Klaviyo) | Brand B (Indian tool) | Brand C (in-house) |
|---|---|---|---|
| read history | full, per-recipient | campaign totals only | dashboard only |
| send | full API | undocumented/unreliable | none |

The job is the same for all three: **read what they've sent and earned**, and
**send new campaigns**. Only the plumbing differs. So the design is one seam, two
capabilities, and a capability map that the rest of the system reads instead of
brand names.

## 1. The shape: one interface, per-tool adapters

Two capabilities, each a narrow interface every adapter implements as far as its
tool allows:

```
ReadConnector   -> list_campaigns(since) -> [Campaign{ id, sent_at, name,
                                                       sends, opens, revenue }]
                -> list_recipient_events(campaign_id) -> [Event{contact, type}]  # optional
SendConnector   -> send_campaign(audience, message) -> SendHandle
                -> capabilities() -> {per_recipient: bool, send: bool, ...}
```

Adapters: `KlaviyoAdapter`, `BrandBAdapter`, `BrandCAdapter`. Everything above the
adapter — audience selection, scheduling, reporting, the intelligence layer —
talks to `ReadConnector` / `SendConnector`, never to a brand's SDK.

Brand four and five are then a new adapter file, not a rewrite: implement the two
interfaces against the new tool, declare its capabilities, register it. The seam
is deliberately thin — everything tool-specific (auth, pagination, rate limits,
retries, the data-cleaning from Part A) lives inside the adapter; nothing leaks
up.

## 2. What the rest of the system should and shouldn't know

**Should not know:** which brand is on which tool. No `if klaviyo` anywhere above
the adapter. The product should not be able to tell Klaviyo from the in-house
tool by reading its own code.

**Should know:** a **capability descriptor** per connection — a small, honest
declaration of what this tool can do:

```
{ read_totals: true, read_per_recipient: false, send: false, send_reliable: false }
```

The system branches on *capabilities*, never on tool identity. "Show the
per-contact view **if** `read_per_recipient`" is fine and reusable; "show it if
Klaviyo" is a leak that breaks the moment brand four also does per-recipient. This
is the one thing that keeps the abstraction honest: the leak is *named and
typed*, not hidden in conditionals.

## 3. What to build first, and what not to

**First:** the `ReadConnector` seam and the **Klaviyo adapter, read path only**.
Reading is lower-risk than sending (you can't spam anyone by reading), it's what
the intelligence layer needs to produce recommendations at all, and Klaviyo's
full API lets us prove the interface against the richest case before it has to
survive the poor ones. Ship: connect Brand A, pull campaign + per-recipient
history, populate the store, show it back.

**Second:** the Brand B and C **read** adapters — totals-only and
scrape/CSV-import respectively — to prove the interface degrades gracefully to a
tool that tells us almost nothing.

**Deliberately not yet:**
- **Sending on Brand B/C.** B's send API is undocumented and unreliable; C has
  none. Building either now buys unreliable delivery and on-call pain for little
  learning. Send first only where it's a real API (Klaviyo), and for B/C fall
  back to **generate-the-campaign-and-hand-it-back** (export the audience + copy,
  the brand sends it in their own tool) until demand justifies more.
- **A generic connector framework / plugin registry / config DSL.** Three
  adapters don't justify it. Two interfaces and a dict of capabilities are
  enough; extract a framework at adapter five if the duplication is real.
- **Real-time sync.** Batch/polling is fine until a brand needs faster.

## 4. Where the abstraction leaks — and what to do about it

Brand A can say exactly who received which message; Brand C can say nothing per
person. The product wants to show a brand **which contacts are worth messaging** —
which is inherently per-contact. That is the real leak, and pretending otherwise
is how you ship a lie.

Options, worst to best:
- **Fabricate** per-contact data for C by guessing — no; a wrong number quietly
  makes every recommendation stupid, exactly what Part A is about.
- **Hide** the per-contact view for C — honest but the product loses its point
  for that brand.
- **Model the resolution explicitly and show it.** Carry a per-connection
  `granularity` (`per_recipient` | `campaign_totals` | `none`) on every metric.
  The UI renders what's available and *says what it can't know*: for A, "these 40
  contacts opened but didn't buy"; for C, "we can't see individual sends here —
  here's what we infer from **your own order data** (from Part A): who lapsed, who
  repeat-buys, who's high-value." The order data is first-party and complete for
  every brand regardless of their messaging tool, so the contact-worth view
  degrades from "based on message engagement" (A) to "based on purchase behaviour"
  (C) rather than disappearing.

So the leak is handled by making granularity a first-class, visible property, and
by **leaning on the order history** (which we own end-to-end from Part A) to
answer the contact-worth question even when the messaging tool is blind.

## 5. What breaks first in production, six months in

- **Brand B's undocumented send API changes or silently drops messages**, and
  because it gives no per-recipient feedback we can't tell delivery failed until
  the brand asks why a campaign underperformed. Mitigation: treat B's send as
  best-effort with reconciliation against *order* lift, and alert on totals that
  don't move.
- **Brand C's dashboard scrape breaks** on a UI change — the most brittle read
  path, and the one with no contract. It needs a human-in-the-loop CSV fallback
  from day one, not a promise that the scraper holds.
- **The capability map rots.** Someone adds a Klaviyo-only feature behind an
  `if brand == A` because it was faster, and now brand four regresses. Guard it:
  capability checks in code review, and no brand identity above the adapter seam.
- **Token/credential expiry across tools** — the Part A lesson, multiplied by N
  tools each with its own auth quirks. Centralise refresh in the adapter base so
  it's solved once, not per tool.

The through-line from Part A: the tools on the other side are unreliable and
uneven, so the system's correctness has to come from **what we own** — the
resolved order history — with the messaging tools treated as best-effort, their
limits declared honestly rather than papered over.
