# Announcement assets

Visuals worth preparing before posting. Nothing here is fabricated — each item is a
screen or artifact the running project actually produces. Capture them from a local
`docker compose up` with two or three providers configured and a few test messages
pushed through.

## 1. Architecture diagram

Already in the README as a Mermaid `flowchart`. For the announcement, export it to
PNG/SVG (mermaid.live, or `mmdc`) so it renders on platforms that don't do Mermaid.
A simplified three-box version (`Applications → SMTP-Switch → Providers`) is also
useful as a lead image.

## 2. Overview page

`http://localhost:8080/` after logging in.

- Queue tiles (Queued / Sending / Sent 60m / Received 60m / Dead-lettered).
- The provider table with priority, breaker pill, in-flight, and the live limit
  bars.
- Best captured with one provider showing an `open` breaker and another showing
  `capped (per_minute)`, so the failover story is visible in one shot.

To produce that state: point one provider at a dead host (breaker opens after
`failure_threshold` failures) and give another a low `per_minute` limit, then send a
short burst.

## 3. Providers page

`http://localhost:8080/providers`.

- Per-provider cards: breaker state, limit bars with headroom numbers, consecutive
  failures, last success / last failure timestamps.
- The **Disable** / **Reset breaker** actions.

## 4. Messages list + a failover message detail

`http://localhost:8080/messages` filtered to `sent`, then open a message that failed
over.

- The list view: status pills, attempt count, "Provider used" column.
- The detail view: the **Delivery attempts** table showing e.g. `primary →
  transient 451`, then `secondary → sent 250`. This is the single most convincing
  screenshot — it shows the core behavior concretely.

## 5. Dead-letter queue

`http://localhost:8080/messages?status=deadletter`, plus one dead-letter message
detail showing the full attempt history and the **Requeue** / **Download .eml**
actions. Produce it by rejecting `5xx` from every provider.

## 6. Prometheus / metrics

- Raw `curl -s localhost:8080/metrics | grep smtp_switch` output (a dozen lines is
  enough to show the metric names).
- Optional: a small Grafana panel row — `smtp_switch_queue_depth{status="queued"}`,
  `smtp_switch_provider_healthy`, `rate(smtp_switch_messages_sent_total[5m])` by
  provider, `smtp_switch_provider_headroom{window="per_day"}`. There is no shipped
  dashboard JSON yet; a hand-built screenshot is fine, or leave it out.

## 7. Optional: a short terminal capture

An asciinema/GIF of: `swaks` submitting a message → the JSON log line
`message_accepted` → `message_sent provider=secondary` after the primary is
rejected. Ties the CLI, the logs, and the failover together in ~15 seconds.

## Capture notes

- Use placeholder domains (`app1`, `noreply@yourdomain.example`,
  `you@example.com`). No real recipients or credentials in any frame.
- Dashboard is bound to `127.0.0.1:8080` by the Compose file; screenshot from the
  same host.
- Light theme, default window width; the tables are designed for ~1100px.
