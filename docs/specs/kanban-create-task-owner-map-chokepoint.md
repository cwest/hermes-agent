# Spec: Owner-map guarantee at the `create_task` chokepoint

Status: implemented (design gate approved via card body; TDD complete)
Base: `cwest/integration` @ `5d3461ec2`
Card: t_0c8744a1
Scope: make an un-routable card **impossible to create** by moving the
owner-map guarantee from the optional skill-side `submit_card` helper into the
one `kanban_db.create_task` chokepoint every filing path funnels through, and
fix the same-root notify-sub `notifier_profile` defect in the same motion.

## 1. Ground truth (verified against HEAD)

A kanban card's routing is driven by its **owner map** — a
`state_owners={ready: …, review: …, blocked-acceptance: …}` fragment that the
webhook lanes read to decide who owns each lane. Verified on the fork-local
`cwest/integration` base:

- The **readers** live in `hermes_cli/kanban_db.py`:
  `_owner_from_owner_map`, `_review_owner_from_owner_map`,
  `_ready_owner_from_owner_map`, `_acceptance_owner_from_owner_map`,
  `_card_declares_owner_map` (+ `_OWNER_MAP_RE`, `_SUBMIT_AUDIT_RE`,
  `_PROSE_OWNER_MAP_RE`). These are the FIVE prior reader hardenings.
- The **writer** — the owner-map TEMPLATES + materialization + kind derivation
  (`_OWNER_MAPS`, `materialize_owner_map`, `default_team`, `resolve_card_kind`,
  `parse_owner_map_from_notes`) — lives ONLY skill-side in
  `~/.hermes/skills/homestead/stage-pr-review/scripts/onecard_common.py`
  (`cwest/hermes-config`, edit-in-place).
- `kanban_db.create_task` has **no** concept of a card kind or an owner map. It
  is a pure `INSERT` + `created` event. The owner map is applied *afterward* by
  `onecard_common.submit_card` — which any caller can simply not use.
- The strict reader (`_owner_from_owner_map` step 1) is authoritative only when
  the map is stamped in a **submit-stage** §9.1 audit comment
  (`_SUBMIT_AUDIT_RE` → `[audit] … stage=submit … state_owners={…}`).

Consequence (observed live twice, both filed by a worker outside the gate):
a bare `create_task` produces a card with no owner map; `resolve_card_kind`
falls back to `code`; the `github-prs` webhook stages every such card to the
code reviewer. A research card mis-routes to the code reviewer; a code card is
"correct by coincidence" (the fallback happens to match) — the more dangerous
shape, because it looks healthy.

The notify-sub defect shares the root: the bypass that skips the owner map also
skips sub registration, and a worker-registered sub lands with
`notifier_profile=<worker>`, which the notifier's owner-profile gate silently
drops (only the delivering profile — `notifier_delivery_profile()` — delivers).

## 2. Design decisions (the four constraints)

### 2.1 Templates MOVE into `kanban_db` (single source of truth)

`_OWNER_MAPS`, `_ALT_READY_AUTHORS`, `_KIND_TEAMS`, `materialize_owner_map`,
`_accepted_ready_authors`, `default_assignee`, `default_team`, and a new
`format_owner_map` (canonical unquoted `state_owners={…}` renderer) move into
`hermes_cli/kanban_db.py`. `onecard_common` becomes an IMPORT of these (that
edit lands in place under `~/.hermes`, committed by the hourly backup cron —
**NOT in this PR**; see §4). Two copies of a routing table is the next defect,
so there is exactly one.

Verification of "one place": `grep -rn "_OWNER_MAPS\|materialize_owner_map"`
against the fork tree shows the table only in `kanban_db.py` (onecard's copy is
removed edit-in-place, out of this repo).

### 2.2 Persistence: keep the submit-stage §9.1 audit comment (compat)

The map is stamped as a canonical unquoted `state_owners={…}` fragment inside a
**submit-stage** §9.1 audit comment written INSIDE the same `create_task`
transaction. Rationale for NOT adding a first-class column:

- Every existing reader (`_owner_from_owner_map`, `resolve_card_kind`, the five
  hardenings) already reads the audit-comment representation. A column would
  require re-plumbing all of them and would leave legacy rows readable only via
  the comment anyway — two readers, the exact duplication we are removing.
- The repr-vs-bare formatting slip that wedged routing (`t_cbacdd82`) is already
  neutralized: `_lane_owner_from_map_body` strips quotes, and the chokepoint now
  emits the **canonical unquoted** form by construction (no caller-supplied
  string), so a formatting slip can no longer be introduced.

`parse_owner_map_from_notes` stays the reader. The stamp is written by
`kanban_db` itself, so it is uniform across every filing path.

### 2.3 Enforcement posture: **(b) — always write a map, default visibly**

`create_task` gains an optional `kind` parameter (`code` / `research` /
`writing`). Posture chosen: **(b)** — `kind` optional, but a map is ALWAYS
written; when `kind` is omitted it defaults to `code` **explicitly** and records
that it was defaulted, so the fallback becomes VISIBLE rather than silent.

Trade-off, stated inline: posture (c) (hard-fail an unstamped create) is
"impossible-by-construction" but `create_task` is the **generic** board
primitive — 76+ call sites, 71 of them test fixtures and generic
swarm/decompose children that carry no card-kind semantics at all. Forcing
`kind` on the generic primitive is the wrong breaking change. Posture (b)
achieves the same *routing* guarantee (no post-change card is ever un-stamped)
without breaking the generic API, and it makes the previously-silent `code`
fallback **auditable**: the submit audit comment records
`notes: state_owners={…} kind=code kind_source=defaulted` when the kind was
defaulted vs `kind_source=explicit` when supplied. A post-change card therefore
never reaches the legacy `resolve_card_kind` silent fallback — it always carries
a real stamped map — and a `code` resolution is provably a stamped `code` card.

### 2.4 Notify-sub: stamp the delivering profile at the WORKER paths

Chosen: **(a)** — fix the source. The worker-side auto-subscribe
(`tools/kanban_tools.py::_maybe_auto_subscribe`, three `add_notify_sub` call
sites) previously stamped `os.environ["HERMES_PROFILE"]` / `HERMES_SESSION_PROFILE`
— the WORKER's own profile — which the notifier's owner-profile gate silently
drops. All three now stamp `notifier_delivery_profile()` (the delivering
gateway's profile) so the sub is actually deliverable.

Option (b) — a hard-fail in `add_notify_sub` for any non-delivering
`notifier_profile` — was implemented, then REJECTED: `add_notify_sub` is a shared
primitive, and a sub is legitimately owned by a SECONDARY profile in the
multi-gateway ownership model (a `beta`-owned telegram sub delivered only by
beta's notifier — `test_notifier_owning_profile_adapter_no_default_fallback`).
A blanket hard-fail there forbids that valid case, so the guard stays out and the
root-cause fix lives at the worker paths that actually produced the bad sub.

## 3. Behavior contracts (tests — assert relationships, not names)

- Raw `create_task(kind="research")` (NOT `submit_card`) → the card's stamped
  map resolves `review` to the research reviewer, and `resolve_card_kind` /
  `_review_owner_from_owner_map` agree: filed kind == resolved kind ==
  owner-map reviewer. Asserted against `materialize_owner_map("research")`, not
  a frozen name, so a roster change does not redden the suite.
- Raw `create_task()` with no kind → map written, `kind=code`,
  `kind_source=defaulted` recorded; `resolve_card_kind == "code"` AND the card
  DECLARES a map (`_card_declares_owner_map` True) — i.e. it is a stamped code
  card, not the un-stamped fallback.
- The research live shape: a research card's review owner != the code card's
  review owner (they must differ), reproducing the mis-route.
- The coincidental code shape: a code card resolves to `code` AND is stamped
  (declares a map), not merely resolving-right.
- The worker auto-subscribe path (`_maybe_auto_subscribe`) stamps the sub with
  `notifier_delivery_profile()`, never the worker's own profile — asserted by
  driving `kanban_create` under a worker profile inside a gateway session and
  checking the resulting sub's `notifier_profile`.

## 4. Cross-repo seam (which half goes where)

- **THIS PR (`cwest/hermes-agent` → `cwest/integration`):** the templates +
  `materialize_owner_map` + `format_owner_map` + kind logic move INTO
  `hermes_cli/kanban_db.py`; `create_task` gains `kind` and stamps the map;
  `add_notify_sub` gains the non-delivering-profile guard; call sites in
  `hermes_cli/kanban.py`, `hermes_cli/kanban_swarm.py`, `tools/kanban_tools.py`,
  `plugins/kanban/dashboard/plugin_api.py` pass a correct `kind`.
- **Edit-in-place (`cwest/hermes-config`, `~/.hermes`, NOT a PR):** after this
  merges + deploys, `onecard_common` DELETES its own `_OWNER_MAPS` /
  `materialize_owner_map` / `default_team` / `_ALT_READY_AUTHORS` /
  `_KIND_TEAMS` and imports them from `kanban_db`. That edit lands in place and
  is committed by the hourly backup cron. It is deliberately NOT in this PR
  because `~/.hermes` is `cwest/hermes-config` (edit-in-place, never a PR).
