"""Outbound relay: hand one message to one upstream provider over SMTP."""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiosmtplib
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPConnectTimeoutError,
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
    SMTPTimeoutError,
)

from smtp_switch.config import ProviderConfig
from smtp_switch.logging_setup import get_logger
from smtp_switch.metrics import PROVIDER_RELAY_LATENCY

log = get_logger("dispatch.sender")

# Outcome classifications
SENT = "sent"
TRANSIENT = "transient"       # retry later / try another provider
PERMANENT = "permanent"      # the message itself was rejected (5xx)
CONNECT_ERROR = "connect_error"  # never reached the provider -> release reservation


@dataclass(slots=True)
class RelayResult:
    classification: str
    smtp_code: int | None
    detail: str
    reached_provider: bool
    refused_recipients: dict[str, str] | None = None

    @property
    def ok(self) -> bool:
        return self.classification == SENT


def _classify_code(code: int | None) -> str:
    if code is None:
        return TRANSIENT
    if 200 <= code < 300:
        return SENT
    if 400 <= code < 500:
        return TRANSIENT
    return PERMANENT


async def relay(
    provider: ProviderConfig,
    *,
    mail_from: str,
    rcpt_tos: list[str],
    raw_message: bytes,
) -> RelayResult:
    smtp = provider.smtp
    kwargs: dict = {
        "hostname": smtp.host,
        "port": smtp.port,
        "timeout": smtp.timeout_seconds,
        "validate_certs": smtp.verify_cert,
    }
    if smtp.tls:
        kwargs["use_tls"] = True
    else:
        kwargs["start_tls"] = bool(smtp.starttls)
    if smtp.username:
        kwargs["username"] = smtp.username
        kwargs["password"] = smtp.password or ""

    started = time.monotonic()
    try:
        errors, response = await aiosmtplib.send(
            raw_message,
            sender=mail_from,
            recipients=rcpt_tos,
            **kwargs,
        )
    except (SMTPConnectError, SMTPConnectTimeoutError, SMTPServerDisconnected) as exc:
        log.warning("relay_connect_error", provider=provider.name, error=str(exc))
        return RelayResult(CONNECT_ERROR, None, str(exc), reached_provider=False)
    except SMTPTimeoutError as exc:
        # Timed out mid-conversation: the provider may have accepted the message,
        # so keep the reservation (favour never exceeding a provider's quota).
        log.warning("relay_timeout", provider=provider.name, error=str(exc))
        return RelayResult(TRANSIENT, None, str(exc), reached_provider=True)
    except SMTPAuthenticationError as exc:
        # Our credentials are wrong: retrying this provider won't help, but the
        # message is fine — let the worker fail over.
        log.error("relay_auth_error", provider=provider.name, code=exc.code, error=str(exc))
        return RelayResult(TRANSIENT, exc.code, str(exc), reached_provider=True)
    except SMTPRecipientsRefused as exc:
        refused_map = {
            r.recipient: f"{r.code} {r.message}".strip() for r in exc.recipients
        }
        codes = [r.code for r in exc.recipients]
        worst = max(codes) if codes else 550
        cls = _classify_code(worst)
        log.warning("relay_recipients_refused", provider=provider.name, code=worst,
                    refused=refused_map)
        return RelayResult(cls, worst, "all recipients refused", reached_provider=True,
                           refused_recipients=refused_map)
    except SMTPResponseException as exc:
        cls = _classify_code(exc.code)
        log.warning("relay_response_error", provider=provider.name, code=exc.code,
                    classification=cls, error=str(exc))
        return RelayResult(cls, exc.code, str(exc), reached_provider=True)
    except SMTPException as exc:
        log.warning("relay_smtp_error", provider=provider.name, error=str(exc))
        return RelayResult(TRANSIENT, None, str(exc), reached_provider=True)
    finally:
        PROVIDER_RELAY_LATENCY.labels(provider=provider.name).observe(
            time.monotonic() - started
        )

    refused: dict[str, str] | None = _format_refused(errors) if errors else None
    if refused:
        log.info("relay_partial", provider=provider.name, refused=refused)
    log.info("relay_ok", provider=provider.name, response=str(response)[:200],
             partial=bool(refused))
    return RelayResult(SENT, 250, str(response)[:500], reached_provider=True,
                       refused_recipients=refused)


def _format_refused(mapping: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for addr, resp in (mapping or {}).items():
        code = getattr(resp, "code", None)
        text = getattr(resp, "message", None) or str(resp)
        out[addr] = f"{code} {text}".strip()
    return out
