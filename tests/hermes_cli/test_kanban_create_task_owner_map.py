"""Owner-map guarantee at the ``create_task`` chokepoint (card t_0c8744a1).

The defect class this closes: a card minted via a bare ``kanban_db.create_task``
(NOT the skill-side ``submit_card`` gate) carried NO owner map, so
``resolve_card_kind`` fell back to ``code`` and every such card routed to the
code reviewer — a research card MIS-routed, and a code card was "correct by
coincidence" (the more dangerous shape). The fix moves the owner-map guarantee
into the one chokepoint every filing path funnels through.

These are BEHAVIOR CONTRACTS: they assert the round-trip relationship
(filed kind == resolved kind == owner-map reviewer) via ``materialize_owner_map``
rather than freezing teammate names, so a roster change does not redden them.

Run:
  scripts/run_tests.sh tests/hermes_cli/test_kanban_create_task_owner_map.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ── Templates: single source of truth in kanban_db ───────────────────────────

def test_owner_map_templates_live_in_kanban_db():
    """The routing table + materializer moved INTO kanban_db (one place)."""
    assert hasattr(kb, "_OWNER_MAPS")
    assert hasattr(kb, "materialize_owner_map")
    assert hasattr(kb, "default_assignee")
    assert hasattr(kb, "default_team")
    # The three real kinds exist and every map carries a review lane (the kind
    # signature the readers key on).
    for kind in ("code", "research", "writing"):
        m = kb.materialize_owner_map(kind)
        assert m.get("review"), f"{kind} map must carry a review lane"


def test_materialize_owner_map_rejects_unknown_kind():
    with pytest.raises(Exception):
        kb.materialize_owner_map("bogus")


def test_kind_review_owners_are_distinct_per_kind():
    """The kind signature (review-lane owner) is unique per kind — this is what
    lets a reader reverse-map a stamped card to its kind."""
    reviewers = {
        kb.materialize_owner_map(k)["review"]
        for k in ("code", "research", "writing")
    }
    assert len(reviewers) == 3, "each kind must have a distinct review owner"


# ── Raw create_task stamps a kind-correct, reader-resolvable map ──────────────

def test_raw_create_task_research_resolves_to_research_reviewer(kanban_home):
    """Card body constraint: a RAW create_task(kind='research') — NOT submit_card
    — must still resolve. filed kind == resolved kind == owner-map reviewer."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="research card", kind="research")
        want_reviewer = kb.materialize_owner_map("research")["review"]
        # Reader agreement: the stamped map resolves the review lane.
        assert kb._review_owner_from_owner_map(conn, tid) == want_reviewer
        # Round-trip: resolved kind matches filed kind.
        assert kb.resolve_card_kind(conn, tid) == "research"
        # And it must NOT resolve to the code reviewer (the live mis-route).
        assert kb._review_owner_from_owner_map(conn, tid) != (
            kb.materialize_owner_map("code")["review"]
        )
    finally:
        conn.close()


def test_raw_create_task_code_is_stamped_not_fallback(kanban_home):
    """The coincidentally-correct shape: a code card must resolve to 'code'
    AND be provably STAMPED (declares a map), not the un-stamped fallback."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="code card", kind="code")
        assert kb.resolve_card_kind(conn, tid) == "code"
        assert kb._card_declares_owner_map(conn, tid) is True
        assert kb._review_owner_from_owner_map(conn, tid) == (
            kb.materialize_owner_map("code")["review"]
        )
    finally:
        conn.close()


def test_raw_create_task_no_kind_defaults_visibly_to_code(kanban_home):
    """Posture (b): kind omitted -> map STILL written, defaulted to code, and
    the default is RECORDED (kind_source=defaulted) so the fallback is visible.
    A post-change card therefore never reaches the silent legacy fallback."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="unkinded card")
        # Map is written even with no kind -> declares a map (not un-stamped).
        assert kb._card_declares_owner_map(conn, tid) is True
        assert kb.resolve_card_kind(conn, tid) == "code"
        # The defaulted-ness is auditable.
        comments = kb.list_comments(conn, tid)
        joined = "\n".join(c.body for c in comments)
        assert "kind_source=defaulted" in joined, (
            "a defaulted kind must be recorded so the fallback is visible, "
            f"got comments: {joined!r}"
        )
    finally:
        conn.close()


def test_explicit_kind_records_explicit_source(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="explicit", kind="writing")
        joined = "\n".join(c.body for c in kb.list_comments(conn, tid))
        assert "kind_source=explicit" in joined
    finally:
        conn.close()


def test_writing_card_resolves_to_writing_reviewer(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="writing card", kind="writing")
        assert kb.resolve_card_kind(conn, tid) == "writing"
        assert kb._review_owner_from_owner_map(conn, tid) == (
            kb.materialize_owner_map("writing")["review"]
        )
    finally:
        conn.close()


def test_create_task_rejects_unknown_kind(kanban_home):
    conn = kb.connect()
    try:
        with pytest.raises(Exception):
            kb.create_task(conn, title="bad", kind="nonsense")
    finally:
        conn.close()


# ── The stamped map round-trips through parse_owner_map_from_notes ────────────

def test_stamped_map_parses_back_to_the_materialized_map(kanban_home):
    """The canonical unquoted state_owners fragment written by the chokepoint
    parses back (via parse_owner_map_from_notes) to the materialized map."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="rt", kind="research")
        want = kb.materialize_owner_map("research")
        # Find the submit-stage audit comment and parse its notes.
        got = {}
        for c in kb.list_comments(conn, tid):
            parsed = kb.parse_owner_map_from_notes(c.body)
            if parsed:
                got = parsed
                break
        # Every lane in the materialized map is present and equal.
        for lane, owner in want.items():
            assert got.get(lane) == owner, f"lane {lane}: {got!r} vs {want!r}"
    finally:
        conn.close()


# ── Notify-sub: the worker auto-subscribe path stamps the DELIVERING profile ──
#
# The same root as the un-stamped owner map (card t_0c8744a1, constraint 5): the
# bypass that skipped the owner map also skipped sub registration, and a
# worker-registered sub landed with notifier_profile=<worker>, which the
# notifier's owner-profile gate silently drops. The fix is at the WORKER paths
# (_maybe_auto_subscribe stamps notifier_delivery_profile()), NOT a hard-fail in
# add_notify_sub — a sub legitimately CAN be owned by a secondary profile (the
# multi-gateway ownership model), so add_notify_sub stays permissive.

def test_worker_auto_subscribe_stamps_delivering_profile(kanban_home, monkeypatch):
    """A worker's kanban_create auto-subscribe must own the sub with the
    DELIVERING profile, never the worker's own profile (which the notifier drops).
    """
    from tools import kanban_tools as kt

    # Simulate a worker whose own profile differs from the delivering profile,
    # inside a gateway session (platform + chat_id present).
    monkeypatch.setenv("HERMES_PROFILE", "reddy-worker")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-1")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-1")

    out = kt._handle_create({"title": "auto-sub", "assignee": "peer", "kind": "code"})
    d = json.loads(out)
    assert d["ok"] is True
    tid = d["task_id"]

    conn = kb.connect()
    try:
        subs = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert subs, "worker create must register an auto-subscribe sub"
        delivering = kb.notifier_delivery_profile()
        for s in subs:
            assert s["notifier_profile"] == delivering, (
                "the auto-subscribe sub must be owned by the DELIVERING profile, "
                f"not the worker profile; got {s['notifier_profile']!r}"
            )
            assert s["notifier_profile"] != "reddy-worker"
    finally:
        conn.close()


# ── Intentional map supersedes the defaulted chokepoint stamp ─────────────────
#
# The exact failure shape the five prior recurrences were about (card t_0c8744a1,
# Lamport review): every card now carries a chokepoint-written DEFAULTED-code
# submit stamp. A card that ALSO declares an INTENTIONAL map (explicit kind /
# prose Routing line / submit-gated stamp) must have that intentional map win —
# and when the intentional map names one lane but OMITS another, the reader must
# return None for the omitted lane rather than resurrecting the co-present
# defaulted stamp's owner. Leaking to the defaulted stamp would re-route past a
# lane the intentional map deliberately dropped — the recurrence this card kills.


def _add_intentional_strict_stamp(conn, tid, owner_map_body):
    """File an INTENTIONAL submit-stage strict stamp (no kind_source=defaulted).

    This is the shape ``submit_card`` / ``create_task(kind=<explicit>)`` write:
    a ``stage=submit`` audit comment carrying ``state_owners={…}`` WITHOUT the
    ``kind_source=defaulted`` marker that flags the chokepoint's automatic map.
    """
    body = (
        "[audit] actor=hollis stage=submit ts=2026-08-17T00:00:00Z\n"
        f"notes: state_owners={{{owner_map_body}}} kind_source=explicit"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)


def test_intentional_strict_map_omitting_review_returns_none_not_defaulted(kanban_home):
    """An intentional strict map that names ``ready`` but OMITS ``review`` must
    resolve the review owner to None — never fall through to the co-present
    defaulted-code stamp's reviewer (the exact live mis-route this card ends)."""
    conn = kb.connect()
    try:
        # create_task with no kind stamps the DEFAULTED code map (carries a
        # real review-lane owner: the code reviewer).
        tid = kb.create_task(conn, title="intentional strict omit review")
        defaulted_reviewer = kb.materialize_owner_map("code")["review"]
        author = kb.materialize_owner_map("research")["ready"]
        # Now stamp an INTENTIONAL strict map naming only ready.
        _add_intentional_strict_stamp(conn, tid, f"ready: {author}")

        # ready resolves from the intentional map.
        assert kb._owner_from_owner_map(conn, tid, "ready") == author
        # review is OMITTED in the intentional map -> None, NOT the defaulted
        # stamp's code reviewer.
        assert kb._review_owner_from_owner_map(conn, tid) is None
        assert kb._review_owner_from_owner_map(conn, tid) != defaulted_reviewer
    finally:
        conn.close()


def test_intentional_prose_map_omitting_review_returns_none_not_defaulted(kanban_home):
    """The prose-form equivalent: a body ``Routing (owner map): {ready: X}`` that
    omits the review lane resolves the review owner to None, not the defaulted
    chokepoint stamp's reviewer."""
    conn = kb.connect()
    try:
        author = kb.materialize_owner_map("research")["ready"]
        # Body carries an intentional prose routing line omitting review; the
        # chokepoint still stamps a defaulted-code map on create.
        tid = kb.create_task(
            conn,
            title="intentional prose omit review",
            body=f"Routing (owner map): {{ready: {author}}}",
        )
        defaulted_reviewer = kb.materialize_owner_map("code")["review"]
        assert kb._owner_from_owner_map(conn, tid, "ready") == author
        assert kb._review_owner_from_owner_map(conn, tid) is None
        assert kb._review_owner_from_owner_map(conn, tid) != defaulted_reviewer
    finally:
        conn.close()


def test_intentional_strict_map_with_review_supersedes_defaulted(kanban_home):
    """When the intentional map DOES name review, its reviewer wins over the
    co-present defaulted-code stamp — the supersedes relationship, both ways."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="intentional strict with review")
        research_map = kb.materialize_owner_map("research")
        intentional_reviewer = research_map["review"]
        defaulted_reviewer = kb.materialize_owner_map("code")["review"]
        assert intentional_reviewer != defaulted_reviewer  # precondition
        _add_intentional_strict_stamp(
            conn,
            tid,
            f"ready: {research_map['ready']}, review: {intentional_reviewer}",
        )
        assert kb._review_owner_from_owner_map(conn, tid) == intentional_reviewer
    finally:
        conn.close()


# ── _card_declares_intentional_owner_map discriminator ───────────────────────
#
# The discriminator all six review-lifecycle gates depend on: True for a card
# that INTENTIONALLY declared its routing (explicit kind / prose / submit-gated),
# False for a card carrying only the chokepoint's automatic DEFAULTED stamp — so
# a generic bookkeeping card still completes to done while a real work-item still
# routes to review (preserves the t_baaa247f no-self-complete fix).


def test_declares_intentional_false_for_defaulted_only(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="defaulted only")
        # It DOES declare a map (routing always resolves)...
        assert kb._card_declares_owner_map(conn, tid) is True
        # ...but NOT an intentional one — the chokepoint default is routing-only.
        assert kb._card_declares_intentional_owner_map(conn, tid) is False
    finally:
        conn.close()


def test_declares_intentional_true_for_explicit_kind(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="explicit kind", kind="research")
        assert kb._card_declares_intentional_owner_map(conn, tid) is True
    finally:
        conn.close()


def test_declares_intentional_true_for_prose_routing_line(kanban_home):
    conn = kb.connect()
    try:
        author = kb.materialize_owner_map("writing")["ready"]
        tid = kb.create_task(
            conn,
            title="prose routing",
            body=f"Routing (owner map): {{ready: {author}, review: "
            f"{kb.materialize_owner_map('writing')['review']}}}",
        )
        assert kb._card_declares_intentional_owner_map(conn, tid) is True
    finally:
        conn.close()


def test_declares_intentional_true_for_submit_gated_stamp(kanban_home):
    """A submit-gated card (submit_card writes a stage=submit state_owners stamp
    with NO kind_source field) is intentional even without an explicit kind."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="submit gated")
        wmap = kb.materialize_owner_map("writing")
        # submit_card's stamp shape: no kind_source marker at all.
        body = (
            "[audit] actor=hollis stage=submit ts=2026-08-17T00:00:00Z\n"
            f"notes: state_owners={{ready: {wmap['ready']}, review: {wmap['review']}}}"
        )
        kb.add_comment(conn, tid, author="hollis", body=body)
        assert kb._card_declares_intentional_owner_map(conn, tid) is True
    finally:
        conn.close()
