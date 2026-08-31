# Contributing to SMTP-Switch

Thanks for taking a look. The project is alpha and feedback from people running
outbound email is especially valuable right now — bug reports and "this broke in
production" issues are as useful as code.

## Local development

Requires Python 3.11 or newer.

```bash
git clone https://github.com/saderi/SMTP-Switch.git
cd SMTP-Switch

python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Run the full check set before pushing:

```bash
pytest -q          # unit + end-to-end; uses in-process fake SMTP providers, no network
ruff check .
mypy smtp_switch
```

CI runs the same three on every push and pull request, then builds the Docker image
and smoke-tests that the container starts and serves `/healthz`.

### Running it locally

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml                       # at least one provider; set web.session_secret

smtp-switch-admin user add admin --generate
smtp-switch-admin account add app1 --generate

smtp-switch -c config.yaml
```

Then submit test mail to `localhost:2525` (for example with `swaks`) and watch the
dashboard on `http://localhost:8080`. `smtp-switch --no-ingress` and
`smtp-switch --no-web` are useful for narrowing things down.

## Project layout

| Path | Responsibility |
|---|---|
| `smtp_switch/ingress/` | `aiosmtpd` server, source-IP allowlist, SMTP-AUTH account store |
| `smtp_switch/dispatch/` | dispatcher/queue (`worker.py`), `router.py`, `rate_limiter.py`, circuit breaker (`health.py`), outbound relay (`sender.py`) |
| `smtp_switch/providers/` | effective provider list = static config + live DB overrides |
| `smtp_switch/db/` | SQLAlchemy 2.0 models and async session handling |
| `smtp_switch/web/` | FastAPI app: dashboard views, JSON API, `/metrics`, `/healthz` |
| `smtp_switch/config.py` | Pydantic settings models + YAML/env loader |
| `smtp_switch/main.py` | process entrypoint wiring the subsystems into one event loop |
| `smtp_switch/migrations/` | Alembic migrations (only used when `database.auto_create: false`) |
| `tests/` | `pytest`; `tests/fakeprovider.py` is the in-process SMTP sink |

## Pull requests

- Keep each PR focused on one change.
- Add or update tests for any behavior change. The end-to-end tests in
  `tests/test_integration_delivery.py` are the place for routing/queue behavior.
- Run `ruff check .` and `mypy smtp_switch` locally; both must be clean.
- In the PR description, say what the operational effect is — what an operator would
  see differently.
- If a change alters the database schema, include an Alembic revision
  (`alembic revision --autogenerate -m "..."`) and check the generated SQL.
- For anything large or structural, open an issue first so the approach can be
  discussed before you spend time on it.

## Reporting bugs

Use the issue templates. For a bug, the deployment method, a redacted copy of the
relevant config, and the log lines around the failure make it actionable. See
[SECURITY.md](SECURITY.md) for anything security-sensitive — do not open a public
issue for that.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
