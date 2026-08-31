# Security Policy

## Supported versions

SMTP-Switch is alpha (`0.1.x`). Security fixes are made on `main`. There is no
backport process yet.

## Reporting a vulnerability

Please report privately. Do **not** open a public issue for a suspected
vulnerability.

- Preferred: open a private advisory via GitHub — the repository's **Security** tab →
  **Report a vulnerability**.
- Alternatively, email the maintainer at `saderi@gmail.com` with "SMTP-Switch
  security" in the subject.

Include what you were running (version/commit, deployment method), what you observed,
and a minimal reproduction if you have one. You will get an acknowledgement within a
few days. Because this is a volunteer alpha project, please allow reasonable time for
a fix before any public disclosure.

When describing a potential issue, a description of the class of problem is enough —
please do not include a working exploit or a step-by-step extraction path in the
initial report.

## Running SMTP-Switch safely

SMTP-Switch relays mail for your applications. The important operational risks and
mitigations:

- **Never expose it as an unauthenticated open relay.** Keep
  `ingress.require_auth: true` and keep the source-IP allowlist tight. Do not set
  `allowed_ips` to `0.0.0.0/0` unless another layer restricts access.
- **Do not expose port `2525` to the public internet.** Restrict it to the subnets
  your applications send from.
- **Enable TLS on the ingress listener** (`ingress.tls` + `require_starttls: true`).
  Without it, SMTP AUTH credentials are transmitted base64-encoded, not encrypted.
  The example config ships with TLS off only so the quick start works without
  certificates.
- **Protect the dashboard/API (port `8080`).** They share a single session cookie
  and there is no API-token mechanism. Bind to localhost or a management network, or
  place an authenticating TLS reverse proxy in front. Change the bootstrap `admin`
  password immediately — it is printed to the log once on first start.
- **Keep secrets out of `config.yaml`.** Inject `web.session_secret` and provider
  passwords through `SMTP_SWITCH_*` environment variables. `web.session_secret` has
  an insecure built-in default; always set your own. `config.yaml` is in
  `.gitignore` — keep it there.
- **Authorize every sending domain on every provider** it could fail over to, or a
  failover will bounce.

See the [Security considerations](README.md#security-considerations) section of the
README for the full checklist.
