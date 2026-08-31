# smtp-switch

An SMTP switch that sits in front of multiple email providers (SMTP2GO, Mailgun,
Resend, …). Your internal services submit mail to it over authenticated SMTP; it
stores each message durably, then relays it through the best available provider —
skipping any provider that is unhealthy or would exceed one of its configured
rate/quota limits. If nothing has capacity right now, the message waits in the
queue and is retried until a provider frees up (or it ages out to a dead-letter
store).

```
  internal services ──SMTP(AUTH)──▶  smtp-switch  ──SMTP──▶  SMTP2GO
                                     │  queue    │  ──SMTP──▶  Mailgun
                                     │  router   │  ──SMTP──▶  Resend
                                     └────┬──────┘
                                     dashboard + /metrics
```

## How it works

| Stage | Behaviour |
|---|---|
| **Ingress** | `aiosmtpd` server. Enforces a source-IP allowlist, STARTTLS, and SMTP AUTH (per-service credentials, argon2-hashed). Accepted mail is written to a spool file + a `messages` row, then `250`. |
| **Queue** | SQLite. A single producer task claims due messages (`status=queued → sending`); a pool of workers relays them. |
| **Routing** | Providers are tried in ascending `priority`. A provider is skipped if its circuit breaker is open or if a reservation against its limits fails. First one that accepts a reservation wins. |
| **Rate limiting** | Per provider, per window: `per_second / per_minute / per_hour` (sliding window over a send-log) and `per_day / per_month` (period counters), plus `max_concurrent`. The reservation is taken **before** the outbound connection, so concurrent workers can't overshoot a limit. |
| **Health** | Circuit breaker per provider: `failure_threshold` consecutive failures → open for `cooldown_seconds` → half-open probe → close on success. Persisted, so a restart remembers an outage. |
| **Retry** | Exponential backoff with jitter between `base_delay_seconds` and `max_delay_seconds`, up to `max_attempts` or `max_age_hours`. Then the message is dead-lettered (keeps its body + full attempt history; requeue/download from the dashboard). "No provider has capacity" uses a short fixed backoff and does **not** burn an attempt. |
| **Observability** | Structured JSON logs, Prometheus `/metrics`, a REST API under `/api`, and a small web dashboard. |

## Requirements

- Python 3.11+
- Every sending (From) domain must already be verified/authorised on **each**
  provider it could be routed through — otherwise a failover will bounce.
- DKIM is signed by the providers themselves; the switch does not sign.
- A TLS cert/key if `ingress.tls.require_starttls` is `true` (recommended).

## Quick start (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml
# edit config.yaml: provider credentials, allowed_ips, web.session_secret, TLS certs

# create a login for the dashboard and a credential for a sending service
smtp-switch-admin user add admin --generate
smtp-switch-admin account add billing-service --generate

smtp-switch -c config.yaml
```

- SMTP submission: `localhost:2525`
- Dashboard: `http://localhost:8080`
- Metrics: `http://localhost:8080/metrics`, health: `/healthz`

On first run, if no dashboard user exists, an `admin` account is created and its
password is printed to the log once.

### Send a test message

```bash
swaks --server localhost:2525 --tls \
  --auth LOGIN --auth-user billing-service --auth-password '<password>' \
  --from noreply@yourdomain.com --to you@example.com \
  --header 'Subject: smtp-switch test'
```

Watch it move `queued → sending → sent` on the dashboard, and see which provider
it went through under **Messages**.

## Quick start (Docker)

```bash
mkdir -p data/certs
cp config.example.yaml data/config.yaml   # edit it
export SMTP_SWITCH_WEB__SESSION_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')

docker compose up -d --build
docker compose exec smtp-switch smtp-switch-admin user add admin --generate
docker compose exec smtp-switch smtp-switch-admin account add billing-service --generate
```

The container keeps `config.yaml`, the SQLite DB, the spool and TLS certs under
the `./data` volume.

## Configuration

`config.yaml` (see `config.example.yaml` for a complete annotated sample). Any
leaf can be overridden by an environment variable
`SMTP_SWITCH_<SECTION>__<KEY>` (double underscore = nesting), e.g.
`SMTP_SWITCH_DATABASE__URL=...` or `SMTP_SWITCH_WEB__SESSION_SECRET=...`. The
provider *list* must come from the file.

### Adding a provider

Append to `providers:` and restart:

```yaml
  - name: postmark            # [a-z0-9_-]
    enabled: true
    priority: 40              # lower number = tried earlier
    smtp:
      host: smtp.postmarkapp.com
      port: 587
      starttls: true
      username: <token>
      password: <token>
    limits:
      per_second: 10
      per_month: 100000
      month_reset_day: 1      # your plan's billing anchor day (1..28)
```

Every `limits` field is optional; omit or set `null` for "no limit on this
window". `month_reset_day` shifts the monthly window so the counter rolls over on
your provider's billing day, not the calendar 1st.

### Routing model

Priority list + failover. The switch always prefers the lowest `priority` number
that is healthy and has headroom. Put your cheapest/preferred provider first and
your fallback last. Disable a provider live from the **Providers** page (survives
restart) without editing the file.

## Operational runbook

| Situation | What happens / what to do |
|---|---|
| A provider goes down | Breaker opens after `failure_threshold` failures; traffic fails over. It self-heals via a half-open probe after `cooldown_seconds`. Force it with **Reset breaker** on the Providers page. |
| A provider hits its monthly quota | It stops being selected; traffic fails over. When every provider is capped, messages hold in the queue (short backoff) — they are **not** lost or dead-lettered for lack of capacity. |
| Message dead-lettered | Open it under **Messages** (filter `deadletter`): see the per-provider attempt log, download the `.eml`, or **Requeue** it. |
| Restart mid-flight | Any `sending` rows are reset to `queued` on startup. Single-send is best-effort — see limitations below. |
| Rotate a sending credential | `smtp-switch-admin account passwd <name>` or the **Accounts** page. The ingress refreshes its credential cache within 30s (immediately on a dashboard change). |
| Disk growth | The housekeeping task deletes `sent` messages after `dispatch.sent_retention_hours` (default 7d) and dead letters after `deadletter_retention_hours` (default 30d), spool files included. |

### Metrics worth alerting on

- `smtp_switch_queue_depth{status="queued"}` climbing steadily → all providers capped/down.
- `smtp_switch_provider_healthy{provider=...} == 0` → breaker open.
- `smtp_switch_messages_deadlettered_total` increasing.
- `smtp_switch_provider_headroom{provider,window}` near 0 for `per_day`/`per_month`.

## Database migrations

Tables are auto-created on startup (`database.auto_create: true`) — fine for the
single-file SQLite deployment. To manage the schema with Alembic instead, set
`auto_create: false` and run:

```bash
alembic upgrade head              # URL comes from your config.yaml / env
alembic revision --autogenerate -m "describe change"
```

## Development

```bash
pip install -e ".[dev]"
pytest            # unit + end-to-end (uses in-process fake providers)
ruff check .
```

`tests/fakeprovider.py` is an in-process SMTP sink that can accept, reject with a
chosen code, or drop the connection — the integration tests use two of them to
exercise failover, rate-cap routing, queue-hold, and dead-lettering.

## Known limitations (v1)

- **No idempotency key.** If a sending service retries after a `4xx` from the
  switch, or a provider times out *after* accepting `DATA`, a duplicate is
  possible. When a timeout is ambiguous the switch keeps the reservation (favours
  "never exceed a provider's limit" over "never duplicate").
- **No bounce/webhook ingestion.** Envelope-from is relayed unchanged; bounces go
  wherever the original sender set them. Provider webhooks are not consumed.
- **Single instance only.** State (SQLite, in-process locks, concurrency
  counters) is not shared. Running two instances would double-count against
  provider limits.
- Provider limit values are operator-supplied; the switch does not read live
  limit headers from providers.
