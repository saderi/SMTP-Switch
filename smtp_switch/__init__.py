"""smtp_switch — an SMTP switch that fronts multiple email providers.

Internal services submit mail over authenticated SMTP; the switch stores each
message durably, then relays it through the best available upstream provider
based on provider health and remaining rate/quota headroom.
"""

__version__ = "0.1.0"
