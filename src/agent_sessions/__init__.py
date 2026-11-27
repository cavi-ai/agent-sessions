"""Local session discovery, messaging, partner coordination, and resource admission."""

from .cli import main
from .hooks import configure_hooks, hook, hook_status
from .hosts import describe, sessions, who
from .partners import (
    all_pairs,
    attach_partner,
    complete_partner,
    connect_partner,
    create_partner,
    launch_profile,
    read_pair,
)
from .paths import VERSION, locked, write_json
from .queue import claim, receive, send, status

__all__ = [
    "VERSION",
    "all_pairs",
    "attach_partner",
    "claim",
    "complete_partner",
    "configure_hooks",
    "connect_partner",
    "create_partner",
    "describe",
    "hook",
    "hook_status",
    "launch_profile",
    "locked",
    "main",
    "read_pair",
    "receive",
    "send",
    "sessions",
    "status",
    "who",
    "write_json",
]
