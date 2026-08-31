# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches a
tagged release.

## [Unreleased]

Nothing yet.

## [0.1.0] — unreleased

Initial public alpha. Core functionality is in place and covered by tests; not yet
proven at scale. See [Current project status](README.md#current-project-status).

### Added

- SMTP ingress server (`aiosmtpd`) on port 2525: source-IP allowlist (CIDR), SMTP
  AUTH with per-sending-service accounts (argon2id), optional STARTTLS with an
  option to require it before AUTH, configurable max message size, `SMTPUTF8`.
- Store-and-forward pipeline: every message is spooled to disk and written to SQLite
  before it is acknowledged.
- Durable retry queue: single-producer claim, worker pool, roughly-exponential
  jittered backoff bounded by `max_attempts` and `max_age_hours`, orphan recovery on
  startup.
- Priority-ordered provider routing with in-cycle failover (`failover_per_tick`).
- Per-provider rate limiting: sliding windows (`per_second` / `per_minute` /
  `per_hour`), fixed period counters (`per_day` / `per_month` with configurable
  `month_reset_day`), and `max_concurrent`. Reservations are taken before the
  outbound connection.
- Per-provider circuit breaker (closed / open / half-open), persisted to SQLite.
- Dead-letter state that retains the message body and full per-attempt log; requeue
  and `.eml` download from the dashboard/API.
- Retention sweep for old `sent` and `deadletter` messages and their spool files.
- Prometheus metrics at `/metrics`; `/healthz` endpoint.
- Server-rendered dashboard and a JSON REST API under `/api` (message inspection,
  requeue/delete, provider enable/disable, breaker reset, sending-account
  management). Live provider enable/disable persisted to the database.
- `smtp-switch-admin` CLI for headless bootstrap of dashboard logins and sending
  accounts.
- Structured JSON logging (`structlog`).
- Docker image (multi-stage, non-root, pinned base) and a single-node Docker Compose
  example.
- GitHub Actions CI: `ruff`, `mypy`, `pytest`, image build, and a container smoke
  test.

[Unreleased]: https://github.com/saderi/SMTP-Switch/compare/main...main
[0.1.0]: https://github.com/saderi/SMTP-Switch/commits/main
