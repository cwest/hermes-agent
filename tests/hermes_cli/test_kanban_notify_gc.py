"""Tests for notify-subscription garbage collection.

Covers the lifecycle cleanup added to keep ``kanban_notify_subs`` from
growing unbounded:

* ``archive_task`` prunes a task's subscriptions in the same txn as the
  status write (archived events are silent, so there is nothing to
  deliver — pruning immediately is safe).
* ``gc_terminal_notify_subs`` sweeps subscription rows whose task is
  already terminal (``done`` / ``archived``), deduplicates legacy
  duplicate rows, supports a non-mutating dry-run with a per-status
  breakdown, and is idempotent.
* Live-card subscriptions are never touched.
* Prune-on-terminal is not a one-way door: a card that re-subscribes
  after being swept can be woken again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _insert_raw_sub(conn, task_id, platform="telegram", chat_id="chat1",
                    thread_id="", last_event_id=0):
    """Insert a subscription row bypassing ``add_notify_sub``'s idempotency.

    Used to fabricate the *legacy duplicate* rows that predate the
    ``(task_id, platform, chat_id, thread_id)`` primary key. Since the
    current schema enforces that PK, genuine duplicates can only exist on
    a legacy-shaped table — so the dedup tests recreate the table without
    the PK to model the wild-board state faithfully.

    Column-agnostic: only the four target columns + ``created_at`` /
    ``last_event_id`` are set explicitly; any other columns the schema
    grows over time take their declared defaults. This keeps the helper
    from breaking when a new column is added to ``kanban_notify_subs``.
    """
    import time
    conn.execute(
        "INSERT INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, created_at, last_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, platform, chat_id, thread_id, int(time.time()), last_event_id),
    )


def _drop_notify_pk(conn):
    """Recreate ``kanban_notify_subs`` WITHOUT its primary key.

    Models a legacy board where duplicate ``(task, platform, chat,
    thread)`` rows were able to accumulate before the PK was added.

    Schema-agnostic: the replacement table copies whatever columns the
    live schema currently declares (minus the PK constraint), so it keeps
    working as columns are added to ``kanban_notify_subs`` over time.
    """
    cols = [row[1] for row in conn.execute(
        "PRAGMA table_info(kanban_notify_subs)"
    ).fetchall()]
    col_list = ", ".join(cols)
    conn.execute("ALTER TABLE kanban_notify_subs RENAME TO _subs_old")
    # Rebuild from the renamed table's definition but strip the PK: create
    # a bare table with the same columns (all TEXT/INTEGER affinity is
    # irrelevant for the dedup test) and no constraints.
    typed_cols = ", ".join(
        f"{row[1]} {row[2] or 'TEXT'}"
        for row in conn.execute("PRAGMA table_info(_subs_old)").fetchall()
    )
    conn.execute(f"CREATE TABLE kanban_notify_subs ({typed_cols})")
    conn.execute(
        f"INSERT INTO kanban_notify_subs ({col_list}) "
        f"SELECT {col_list} FROM _subs_old"
    )
    conn.execute("DROP TABLE _subs_old")
    conn.commit()


# ---------------------------------------------------------------------------
# Prune-on-terminal: archive
# ---------------------------------------------------------------------------

def test_archive_prunes_notify_subs(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="archive me", assignee="w1", detached=True)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="c1")
        assert len(kb.list_notify_subs(conn, tid)) == 1

        assert kb.archive_task(conn, tid) is True

        # Readback: a card moved to archived has zero subscription rows.
        assert kb.list_notify_subs(conn, tid) == []


def test_archive_prune_leaves_other_tasks_subs(kanban_home):
    with kb.connect() as conn:
        keep = kb.create_task(conn, title="live", assignee="w1", detached=True)
        drop = kb.create_task(conn, title="archive me", assignee="w1", detached=True)
        kb.add_notify_sub(conn, task_id=keep, platform="telegram", chat_id="c1")
        kb.add_notify_sub(conn, task_id=drop, platform="telegram", chat_id="c1")

        kb.archive_task(conn, drop)

        assert kb.list_notify_subs(conn, drop) == []
        assert len(kb.list_notify_subs(conn, keep)) == 1


# ---------------------------------------------------------------------------
# Backlog sweep: gc_terminal_notify_subs
# ---------------------------------------------------------------------------

def test_gc_dry_run_reports_without_mutating(kanban_home):
    with kb.connect() as conn:
        done = kb.create_task(conn, title="done card", assignee="w1", detached=True)
        arch = kb.create_task(conn, title="arch card", assignee="w1", detached=True)
        live = kb.create_task(conn, title="live card", assignee="w1", detached=True)
        for t in (done, arch, live):
            kb.add_notify_sub(conn, task_id=t, platform="telegram", chat_id="c1")

        kb.complete_task(conn, done, result="ok")
        # archive_task prunes on its own, so fabricate an archived-with-sub
        # row directly to exercise the sweep's archived branch too.
        _insert_raw_sub(conn, arch)  # arch already has one; add a second target
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?", (arch,)
        )
        conn.commit()

        # complete_task does NOT prune synchronously (would drop the wake);
        # so the done card's sub is still present for the sweep to find.
        report = kb.gc_terminal_notify_subs(conn, dry_run=True)

        assert report["removed"] == 0, "dry-run must not delete"
        assert report["would_remove"] >= 2
        # Per-status breakdown present.
        assert "by_status" in report
        assert report["by_status"].get("done", 0) >= 1
        assert report["by_status"].get("archived", 0) >= 1

        # Nothing was mutated.
        assert len(kb.list_notify_subs(conn, done)) == 1
        assert len(kb.list_notify_subs(conn, arch)) >= 1
        # Live card untouched in dry-run.
        assert len(kb.list_notify_subs(conn, live)) == 1


def test_gc_real_sweep_then_idempotent(kanban_home):
    with kb.connect() as conn:
        done = kb.create_task(conn, title="done card", assignee="w1", detached=True)
        live = kb.create_task(conn, title="live card", assignee="w1", detached=True)
        kb.add_notify_sub(conn, task_id=done, platform="telegram", chat_id="c1")
        kb.add_notify_sub(conn, task_id=live, platform="telegram", chat_id="c1")
        kb.complete_task(conn, done, result="ok")

        report = kb.gc_terminal_notify_subs(conn, dry_run=False)
        assert report["removed"] >= 1

        # done card swept clean; live card survives.
        assert kb.list_notify_subs(conn, done) == []
        assert len(kb.list_notify_subs(conn, live)) == 1

        # Running it again reports zero — idempotent.
        again = kb.gc_terminal_notify_subs(conn, dry_run=True)
        assert again["would_remove"] == 0
        again_real = kb.gc_terminal_notify_subs(conn, dry_run=False)
        assert again_real["removed"] == 0


def test_gc_preserves_live_card_subs(kanban_home):
    """The 10-live-rows invariant: non-terminal cards keep their subs."""
    with kb.connect() as conn:
        statuses_live = []
        for st in ("ready", "running", "blocked", "todo"):
            t = kb.create_task(conn, title=f"{st} card", assignee="w1", detached=True)
            kb.add_notify_sub(conn, task_id=t, platform="telegram", chat_id="c1")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (st, t))
            statuses_live.append(t)
        conn.commit()

        kb.gc_terminal_notify_subs(conn, dry_run=False)

        for t in statuses_live:
            assert len(kb.list_notify_subs(conn, t)) == 1, (
                "live-card subscription must survive the sweep"
            )


def test_gc_deduplicates_legacy_duplicate_rows(kanban_home):
    with kb.connect() as conn:
        live = kb.create_task(conn, title="live card", assignee="w1", detached=True)
        conn.commit()
        _drop_notify_pk(conn)
        # Three identical rows for the SAME live target (legacy dupes).
        _insert_raw_sub(conn, live, thread_id="t9")
        _insert_raw_sub(conn, live, thread_id="t9")
        _insert_raw_sub(conn, live, thread_id="t9")
        conn.commit()
        assert len(kb.list_notify_subs(conn, live)) == 3

        report = kb.gc_terminal_notify_subs(conn, dry_run=False)

        # Duplicates collapse to exactly one row for the live target.
        remaining = kb.list_notify_subs(conn, live)
        assert len(remaining) == 1, f"dupes should collapse to 1, got {remaining!r}"
        assert report["deduplicated"] >= 2


def test_gc_dry_run_counts_duplicates_without_mutating(kanban_home):
    with kb.connect() as conn:
        live = kb.create_task(conn, title="live card", assignee="w1", detached=True)
        conn.commit()
        _drop_notify_pk(conn)
        _insert_raw_sub(conn, live, thread_id="t9")
        _insert_raw_sub(conn, live, thread_id="t9")
        conn.commit()

        report = kb.gc_terminal_notify_subs(conn, dry_run=True)
        assert report["would_deduplicate"] >= 1
        # Not mutated.
        assert len(kb.list_notify_subs(conn, live)) == 2


# ---------------------------------------------------------------------------
# Not a one-way door: a swept card can re-subscribe and be woken again.
# ---------------------------------------------------------------------------

def test_reopened_card_can_resubscribe_and_be_woken(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="cycle", assignee="w1", detached=True)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="c1")
        kb.complete_task(conn, tid, result="done")

        # Sweep removes the terminal sub.
        kb.gc_terminal_notify_subs(conn, dry_run=False)
        assert kb.list_notify_subs(conn, tid) == []

        # Card is reopened (moved back to an active status) and re-subscribes
        # through the normal add path.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="c1")
        assert len(kb.list_notify_subs(conn, tid)) == 1

        # A fresh terminal event is now claimable again — the wake works.
        kb.block_task(conn, tid, reason="need input", kind="needs_input")
        old, new, events = kb.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="c1",
            kinds=("blocked",),
        )
        assert any(e.kind == "blocked" for e in events), (
            "re-subscribed card must be able to claim a new terminal event"
        )


# ---------------------------------------------------------------------------
# CLI surface: hermes kanban gc [--dry-run]
# ---------------------------------------------------------------------------

def _run_kanban_cli(argv):
    """Drive the real argparse surface exactly like `hermes kanban …`."""
    import argparse

    from hermes_cli import kanban as kc

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def test_cli_gc_dry_run_reports_and_does_not_mutate(kanban_home, capsys):
    with kb.connect() as conn:
        done = kb.create_task(conn, title="done card", assignee="w1", detached=True)
        live = kb.create_task(conn, title="live card", assignee="w1", detached=True)
        kb.add_notify_sub(conn, task_id=done, platform="telegram", chat_id="c1")
        kb.add_notify_sub(conn, task_id=live, platform="telegram", chat_id="c1")
        kb.complete_task(conn, done, result="ok")

    rc = _run_kanban_cli(["gc", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    assert "done=1" in out

    # Nothing deleted by the dry-run.
    with kb.connect() as conn:
        assert len(kb.list_notify_subs(conn, done)) == 1
        assert len(kb.list_notify_subs(conn, live)) == 1


def test_cli_gc_sweeps_then_dry_run_reports_zero(kanban_home, capsys):
    with kb.connect() as conn:
        done = kb.create_task(conn, title="done card", assignee="w1", detached=True)
        live = kb.create_task(conn, title="live card", assignee="w1", detached=True)
        kb.add_notify_sub(conn, task_id=done, platform="telegram", chat_id="c1")
        kb.add_notify_sub(conn, task_id=live, platform="telegram", chat_id="c1")
        kb.complete_task(conn, done, result="ok")

    rc = _run_kanban_cli(["gc"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "notify-sub" in out

    with kb.connect() as conn:
        assert kb.list_notify_subs(conn, done) == []
        assert len(kb.list_notify_subs(conn, live)) == 1

    # After a real gc, the dry-run reports zero to remove — idempotent.
    rc = _run_kanban_cli(["gc", "--dry-run"])
    assert rc == 0
    out2 = capsys.readouterr().out
    assert "would remove 0" in out2.lower()

