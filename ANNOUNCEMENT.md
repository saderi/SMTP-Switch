# Announcement material

Preparation notes for posting SMTP-Switch to a DevOps / platform-engineering
audience. Not intended to live in the repo permanently — delete after the launch.

Replace `GITHUB_LINK` with `https://github.com/saderi/SMTP-Switch`.

---

## Short version (chat / forum post)

> I built a small self-hosted thing for outbound email reliability and would like
> people to poke holes in it.
>
> **SMTP-Switch** sits between your apps and your SMTP providers. Apps submit over
> normal authenticated SMTP; it spools the message, then relays it through the
> best eligible provider. If the preferred provider is down (circuit breaker) or
> you've hit a configured rate/quota limit, it fails over to the next one. If
> nothing has capacity right now, the message waits in a durable queue and is
> retried instead of dropped. Prometheus metrics, a small dashboard, dead-letter
> queue with the full per-attempt log.
>
> Roughly "HAProxy for outbound SMTP providers". Single Python process, SQLite for
> state, Docker image provided.
>
> It's alpha — component-tested (28 tests, failover / rate-cap spillover /
> queue-hold / dead-letter all covered) but not yet run at scale. Single-instance
> only for now. I'd really like feedback from people who actually operate
> transactional email: what would stop you using this, what's missing, what
> would you expect it to expose.
>
> GITHUB_LINK

---

## Longer version (blog / mailing list / GitHub Discussions)

**The problem.** If you send transactional email (receipts, password resets,
alerts) you eventually want more than one provider — for outages, for quota
headroom, for a migration path. The usual way to get that is per-provider code in
every service: SDKs, failover logic, retry logic, and rate-limit handling
duplicated across the fleet, each implementation slightly different. Provider
credentials end up scattered across application secrets. When a provider has a bad
hour, you find out from your users.

**What SMTP-Switch does.** It is a submission-side gateway. Applications point their
SMTP client at one endpoint and keep speaking ordinary authenticated SMTP — no SDK,
no proprietary API. SMTP-Switch accepts the message, writes it to a spool file and a
SQLite row before acknowledging, and then a dispatcher relays it. Providers are
tried in priority order. A provider is skipped if its circuit breaker is open or if
a reservation against its configured limits (`per_second` / `per_minute` /
`per_hour` sliding windows, `per_day` / `per_month` counters, `max_concurrent`)
fails. A single message can try several providers in one dispatch cycle. If every
provider is down or capped, the message stays queued and is retried with backoff —
it is not dead-lettered just for lack of capacity. Messages that exhaust retries,
age out, or are rejected `5xx` everywhere land in a dead-letter state that keeps the
body and the full per-attempt history, from where you can requeue or download them.

**Architecture.** One asyncio process: `aiosmtpd` for ingress, `aiosmtplib` for the
outbound relay, SQLite (WAL) for queue and bookkeeping, message bodies as files on
disk. Rate-limit reservations are taken before the outbound connection so parallel
workers can't collectively overshoot a provider's limit; the ambiguous-timeout
policy deliberately favors "never exceed a provider's limit" over "never
duplicate". Circuit-breaker state is persisted, so an outage in progress survives a
restart. Observability is Prometheus metrics at `/metrics` (queue depth,
per-provider health, remaining headroom, send/fail/dead-letter counters, relay
latency), a `/healthz` endpoint, structured JSON logs, and a small server-rendered
dashboard.

**Current status.** Alpha, version 0.1.0, no tagged release yet. 28 tests pass:
unit tests for the router, rate limiter, and breaker, plus end-to-end tests that run
the real ingress and dispatcher against in-process fake SMTP providers and exercise
failover, rate-cap spillover, queue-hold when everything is down, and
dead-lettering. What is *not* proven yet: sustained real throughput, large spool
directories, month-boundary quota accounting, the full range of real provider
quirks, upgrade-under-load. It is single-instance only right now — SQLite plus
in-process locks — so two replicas would double-count against provider limits.
There is no idempotency key, so an ambiguous timeout can produce a duplicate.

**What I'm asking for.** Try it in staging against your real providers and tell me
what breaks. I am particularly interested in whether the routing model matches how
people actually want to spread load, what metrics operators expect, and whether the
single-instance limitation is a dealbreaker or a "fine for now". Issues and blunt
feedback welcome.

GITHUB_LINK

---

## Suggested discussion questions

1. How do you handle SMTP provider outages today — in-app failover, a smarthost,
   manual DNS/config changes, or just accept the downtime?
2. Strict priority + failover is the only routing mode right now. Would weighted or
   round-robin distribution across healthy providers actually change how you'd
   deploy this, or is "primary with fallbacks" what you want anyway?
3. For a stateful single-process service like this, what would make Kubernetes/Helm
   support worth it versus running it as a plain container with a volume?
4. What metrics and alerts would you expect out of the box? Is the current set
   (queue depth, per-provider health, quota headroom, dead-letter rate, relay
   latency) missing something you'd want on day one?
5. The ambiguous-timeout policy chooses "count the send, risk a duplicate" over
   "don't count it, risk exceeding a provider limit". Is that the right default for
   transactional mail, or should it be configurable per provider?
