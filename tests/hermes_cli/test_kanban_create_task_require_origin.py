"""Origin guarantee at the ``create_task`` chokepoint (card t_b76d0836).

The defect class this closes: ``kanban_db.create_task`` accepted
``session_id=None`` and filed a card *silently*. A card with no origin produces
transitions that can never wake anyone. Origin was advisory at write time when it
should be structurally required; the enforcing ``submit_card`` gate was one path
among several rather than the only door.

The fix makes an origin-less card **impossible to create** at the one chokepoint
every filing path funnels through: a call that resolves no origin and carries no
explicit ``detached=True`` marker raises rather than filing. This is the
"impossible by construction" half; PR #127 only cut off the poisoned supply at
the worker-spawn boundary.

These are BEHAVIOR CONTRACTS: they assert the relationship (origin present =>
files with origin; origin absent + unmarked => raises + writes NO row) with a
witnessed negative control, not a frozen name.

Run:
  scripts/run_tests.sh tests/hermes_cli/test_kanban_create_task_require_origin.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import gateway.session_context as sc
from hermes_cli import kanban_db as kb

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
_SESSION_VARS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_ID",
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _isolate_origin(monkeypatch):
    """Reset the origin ContextVar + env mirror and clear session vars per test.

    The refusal tests below assert that a no-``session_id`` create RAISES. Since
    the guard also resolves an ambient origin (inherited ``HERMES_KANBAN_ORIGIN``
    or a live ``HERMES_SESSION_*`` capture), a leaked origin in the runner env
    would let a "no origin" create silently succeed. Clearing both surfaces makes
    the refusal deterministic — the guard is exercised in a genuinely origin-less
    context, which is exactly what these tests claim to prove.
    """
    saved_ctx = sc._KANBAN_ORIGIN.get()
    saved_env = os.environ.get(_ORIGIN_ENV)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    for v in _SESSION_VARS:
        monkeypatch.delenv(v, raising=False)
    try:
        yield
    finally:
        sc._KANBAN_ORIGIN.set(saved_ctx)
        if saved_env is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_env


def _count(conn) -> int:
    return len(kb.list_tasks(conn, include_archived=True))


# ── Refusal: no origin, no marker => raise + NO row (negative control) ────────

def test_create_task_without_origin_or_marker_raises(kanban_home):
    """A create with no resolvable origin and no detached marker must raise."""
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="origin"):
            kb.create_task(conn, title="orphan", assignee="w")
    finally:
        conn.close()


def test_refused_create_writes_no_row(kanban_home):
    """Witnessed negative control: the refused create files NOTHING."""
    conn = kb.connect()
    try:
        before = _count(conn)
        with pytest.raises(ValueError):
            kb.create_task(conn, title="orphan", assignee="w")
        after = _count(conn)
        assert after == before, "a refused create must not leave a partial row"
    finally:
        conn.close()


def test_blank_session_id_is_not_an_origin(kanban_home):
    """A whitespace-only session_id must not launder past the guard."""
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="origin"):
            kb.create_task(conn, title="blank", assignee="w", session_id="   ")
    finally:
        conn.close()


# ── Detached opt-out: explicit marker files with a NULL origin ────────────────

def test_detached_marker_files_with_null_origin(kanban_home):
    """detached=True is the explicit opt-out for a genuinely origin-less card."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="detached", assignee="w", detached=True)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.session_id in (None, ""), "detached card carries no origin"
    finally:
        conn.close()


# ── Origin supplied: files and round-trips to an origin sub ───────────────────

def test_origin_supplied_files_and_round_trips(kanban_home):
    """A real session_id files the card and stamps the origin on the row."""
    conn = kb.connect()
    try:
        sid = "discord:123456:789"
        tid = kb.create_task(conn, title="origined", assignee="w", session_id=sid)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.session_id == sid
        # The stamped origin is parseable back to notify-sub columns — i.e. it is
        # a real, wake-able origin, not an inert string.
        assert kb.parse_origin_session(sid) is not None
    finally:
        conn.close()


# ── Ambient origin satisfies the guard (reconciliation with #128) ─────────────

def test_inherited_origin_satisfies_guard_without_session_id(kanban_home):
    """A card filed with NO session_id kwarg but an inherited origin is allowed.

    The guard resolves origin the same way #128's created-event fragment does:
    explicit kwarg OR ambient surface (inherited HERMES_KANBAN_ORIGIN across a
    spawn boundary, or a live session capture). A card created inside a live /
    inherited gateway session HAS a routable origin — refusing it would be a
    regression against #128. This proves the guard does not refuse it.
    """
    sc.set_kanban_origin(
        platform="discord", chat_id="HUMAN_ORIGIN", thread_id="HUMAN_THREAD",
        user_id="HUMAN_USER", session_id="sess-human",
    )
    conn = kb.connect()
    try:
        # No session_id, no detached — only the inherited ambient origin.
        tid = kb.create_task(conn, title="inherited-origin", assignee="w")
        task = kb.get_task(conn, tid)
        assert task is not None, "an inherited origin must satisfy the guard"
    finally:
        conn.close()


def test_live_session_origin_satisfies_guard_without_session_id(
    kanban_home, monkeypatch
):
    """A live HERMES_SESSION_* capture also satisfies the guard (no kwarg)."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "ROOTCHAT")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-root")
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="live-origin", assignee="w")
        task = kb.get_task(conn, tid)
        assert task is not None, "a live session origin must satisfy the guard"
    finally:
        conn.close()
