"""``/restart`` while a restart is already waiting, and ``/restart now``.

A second ``/restart`` used to answer "restart in progress" with nothing about what the wait holds
for or how to end it, so a restart stuck behind a busy board looked hung. It now names what is
live (count + kinds), how long the wait has run, and the way out: ``/restart now``, which takes
the same ``force_restart`` path ``hermes update --force`` uses.
"""
from __future__ import annotations

import time
from collections import Counter

FORCE_ARGS = frozenset({"now", "force", "--now", "--force"})


def wants_force(args: str) -> bool:
    return args.strip().lower() in FORCE_ARGS


def waiting_summary(runner) -> tuple[int, str, int]:
    """(count, "2 process, 1 chat", seconds waited) for a pending restart."""
    kinds = Counter(str(u.get("kind") or "work") for u in runner._describe_active_work() if isinstance(u, dict))
    started = getattr(runner, "_restart_wait_started_at", None)
    waited = int(time.time() - started) if started else 0
    return sum(kinds.values()), ", ".join(f"{n} {k}" for k, n in kinds.most_common()) or "nothing", waited


def pending_reply(runner) -> str:
    from agent.i18n import t
    count, kinds, waited = waiting_summary(runner)
    return t("gateway.restart.waiting", count=count, kinds=kinds, waited=_fmt_duration(waited))


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"
