"""Sub2API subscription and group-rate monitor plugin."""

from .client import Sub2APIClient, Sub2APIError
from .data import PollResult, normalize_group_rates, normalize_subscriptions
from .plugin import Sub2APIMonitorPlugin
from .sources import (
    SourceConfig,
    parse_sources,
    parse_sources_partial,
    source_by_selector,
)

__all__ = [
    "PollResult",
    "SourceConfig",
    "Sub2APIClient",
    "Sub2APIError",
    "Sub2APIMonitorPlugin",
    "normalize_group_rates",
    "normalize_subscriptions",
    "parse_sources",
    "parse_sources_partial",
    "source_by_selector",
]
