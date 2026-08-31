"""Sub2API subscription and group-rate monitor plugin."""

from .client import Sub2APIClient, Sub2APIError
from .data import PollResult, normalize_group_rates, normalize_subscriptions
from .plugin import Sub2APIMonitorPlugin

__all__ = [
    "PollResult",
    "Sub2APIClient",
    "Sub2APIError",
    "Sub2APIMonitorPlugin",
    "normalize_group_rates",
    "normalize_subscriptions",
]
