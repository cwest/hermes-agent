# Webhook-triggered code reviews — operational notes

When a code review is spawned by a GitHub webhook (PR opened → gateway → `github-code-review`)
rather than by a human at a terminal, several failure modes appear that don't exist in
interactive review. These are the durable lessons; the SKILL.md body carries the hard rules.

## The core hazard: the PR comment thread is the agent's only output channel

A webhook-spawned agent has no human and no TTY. Its delivery sink is `gh pr comment`. So
*anything* the agent would normally surface interactively leaks onto the public PR:

- `clarify` questions → posted publicly ("How should I proceed?", "Should I check out the branch?")
- progress heartbeats → posted publicly ("⏳ Working — 9 min — iteration 3/150")
- busy-acks / interim assistant messages → posted publicly

To a reader this looks like a poorly-trained bot working in public. The fixes are two-layered:

1. **Skill layer (live immediately, read per run):** the SKILL.md "Automated / non-interactive
   context" rules — never `clarify`, review remote-only, post exactly one finished comment.
2. **Gateway display layer (see below):** suppress progress/interim/heartbeat output for the
   webhook platform so even a misbehaving agent can't emit chatter.

## The FIRST thing to check: does the webhook agent even have `terminal`?

Before chasing credentials, prompts, or plumbing, verify the webhook-spawned agent has the
toolset it needs to do a review. **The `webhook` platform defaults to a deliberately minimal
toolset** — on a stock setup it resolves to roughly `[clarify, gemini-search, gravatar,
vision, web]` with **NO `terminal`, NO `file`, NO `skills`**. A code-review agent with no
`terminal` has no `gh` to run `gh pr diff`/`gh api`, so it cannot read the diff or post a
review. It makes one API call and returns a near-empty stub.

**Diagnostic signature of the missing-toolset bug** (memorise this — it's distinct from a
credential failure):

- `agent.log`: `conversation turn ... history=0` then `API call #1: ... out=<tiny, e.g. 74>`
  and then **nothing** — no tool execution, no API call #2, no `end_reason`.
- session row in `state.db`: `message_count=0, tool_call_count=0, api_call_count=1`.
- the posted PR comment is a near-empty stub (e.g. `❓ placeholder`, `❓ noop`) or, on a run
  where the model *did* engage, it reaches for `web_search` (the only research-ish tool it
  has), can't get the diff, and honestly bails.

This is NOT a credential bug. A Bitwarden/`gh`-token failure looks different (auth errors,
401 on POST). The stub-with-zero-tools signature means **the model had no actionable tool to
call**, which on the webhook platform means the toolset, not the secret. Confirm the resolved
toolset directly instead of guessing:

```python
# in <hermes_repo>, venv python
import yaml, pathlib, sys; sys.path.insert(0, ".")
from hermes_cli.tools_config import _get_platform_tools
cfg = yaml.safe_load(pathlib.Path("<hermes_home>/config.yaml").read_text())
print(sorted(_get_platform_tools(cfg, "webhook")))   # is 'terminal' in here?
```

**Fix — grant the webhook platform the tools a review needs (config lever, no core edit):**
set `platform_toolsets.webhook` in `config.yaml`. `terminal`, `file`, and `skills` are all
allowed for the webhook platform (`_toolset_allowed_for_platform(<ts>, "webhook")` is `True`;
the only platform restrictions are discord-specific). Keep the existing minimal tools and add
the review essentials:

```yaml
platform_toolsets:
  webhook:
    - terminal        # run gh pr diff / gh api — the core need
    - file            # read files when a finding needs file context
    - skills          # load humanizer / de-claude that github-code-review chains
    - web
    - gemini-search
    - vision
    - gravatar
    - clarify
```

**This is NOT hot-reloaded.** Unlike `webhook_subscriptions.json` (mtime-reloaded per request)
and `display.*` (re-read per message), the platform toolset is bound when the gateway builds
the platform's agent — **it requires a gateway restart** to take effect. If the user's rule is
"I restart the gateway", make the config change, then ask them to restart before re-testing.

Add this to the "prefer config levers" mental list: the webhook review story has now needed
FOUR independent config knobs — `display.platforms.webhook.*` (chatter), `platforms.webhook.
gateway_restart_notification` (shutdown-notice leak), `webhook_subscriptions.json` (the route +
its prompt), and **`platform_toolsets.webhook` (the toolset)**. The toolset is the one that
makes the review *work at all*; the others make it *quiet and safe*.

## The webhook route prompt must be imperative, not bare metadata

A secondary contributor to stub reviews: the route's `prompt` template in
`webhook_subscriptions.json` interpolating only PR metadata (title, author, branch, body) with
no instruction. Even with the right toolset, handing the agent "here is a PR" without "review
it" invites a minimal response. The prompt should be an explicit imperative that defers the
*how* to the loaded skill — e.g. "Review PR #{number} in {repo} following the github-code-review
skill: fetch the diff with `gh pr diff {number}`, then post ONE formal review (inline comments +
summary body)." The `skills: [github-code-review]` on the route loads the skill; the prompt's
job is to say *do it now*. (On its own this was not the root cause in the live test — the
toolset was — but a weak prompt compounds it, so fix both.)

## Gateway display config: why heartbeats leaked despite a "silent" default

Hermes maps the `webhook` platform to a minimal display tier by default (no
`long_running_notifications`, no `interim_assistant_messages`, `tool_progress: off`). But the
display-setting resolver checks settings in this precedence order:

1. `display.platforms.<platform>.<key>`  (per-platform override) ← highest
2. `display.<key>`                        (global user setting)
3. built-in platform default (the "silent" tier for webhook)
4. built-in global default

A global `display.long_running_notifications: true` (step 2) therefore **overrides** the
webhook silent default (step 3). That's the trap: the framework intends webhook silence, but a
broad global setting defeats it.

**Fix — pin a per-platform override (step 1 beats step 2):**

```bash
hermes config set display.platforms.webhook.long_running_notifications false
hermes config set display.platforms.webhook.interim_assistant_messages false
hermes config set display.platforms.webhook.busy_ack_detail false
hermes config set display.platforms.webhook.tool_progress off   # stored as bool false; resolves to "off"
```

Note: `tool_progress off` lands as YAML `false`; the resolver's `_normalise` maps
`tool_progress is False → "off"`, so it's correct. The main `~/.hermes/config.yaml` is
agent-write-protected — you must use `hermes config set`, not a direct file write.

**Verify with the real resolver before declaring it fixed** (resolution, not file contents):

```python
import yaml, sys
sys.path.insert(0, "<hermes_repo>")
from gateway.display_config import resolve_display_setting
cfg = yaml.safe_load(open("<hermes_home>/config.yaml"))
for k in ["long_running_notifications","interim_assistant_messages","busy_ack_detail","tool_progress"]:
    print(k, "webhook=", resolve_display_setting(cfg, "webhook", k, "MISSING"),
             "discord=", resolve_display_setting(cfg, "discord", k, "MISSING"))
# expect webhook all-silent, discord unchanged (proves you didn't break interactive platforms)
```

## Zombie agents: config reload is per-message, not retroactive

`_load_gateway_config()` is mtime-cached and re-read **per incoming message / per new agent
run** (e.g. `gateway/run.py` reloads it inside the message handler). So a config change takes
effect on the *next* webhook delivery without a restart.

BUT an agent run that is **already in flight** holds its startup config snapshot for its whole
lifetime. A long-lived stalled reviewer (e.g. one parked on a clarify loop, iterating
1/150 → 3/150) will keep posting chatter under the *old* config until it finishes or the
gateway restarts. You cannot kill an in-gateway async task without restarting the gateway.
If the user's rule is "I restart the gateway, not you", the only clean stop for a zombie is to
ask them to restart; meanwhile delete its noise comments as cleanup.

## Cleaning up leaked chatter

Delete the noise comments (keep legit CI/lint bot comments and any real review):

```bash
IDS=$(gh api repos/$OWNER/$REPO/issues/$PR/comments \
  -q '.[] | select((.body|startswith("⏳")) or (.body|startswith("❓"))) | .id')
for id in $IDS; do gh api -X DELETE repos/$OWNER/$REPO/issues/comments/$id; done
```

## When deleting can't keep up: disable the route at the source

Deleting leaked comments is whack-a-mole if the spawn source is still live. The decisive move
is to stop new reviewers from spawning at all — disable the webhook route.

**Why deletions never get ahead of it:** the `actions` allow-list filter (which drops noisy
`pull_request` actions like `closed`/`ready_for_review`) lives in the gateway *code*. If that
fix is itself sitting in an unmerged PR, the **running** gateway doesn't have it yet — so every
push (`synchronize`), reopen, etc. still spawns a reviewer that posts via `deliver:
github_comment`. You cannot out-delete a source that fires on every PR event.

**Disable the route (with a clean, restorable stash):**

```bash
cd "${HERMES_HOME:-$HOME/.hermes}"
cp webhook_subscriptions.json "webhook_subscriptions.json.bak.$(date +%Y%m%d-%H%M%S)"
# Stash the route to a clearly-named sidecar, then empty the live file:
python3 -c "import json; d=json.load(open('webhook_subscriptions.json')); \
  json.dump(d, open('webhook_subscriptions.<route>.disabled.json','w'), indent=2); \
  json.dump({}, open('webhook_subscriptions.json','w'), indent=2)"
```

An **empty `{}`** live file = zero routes = no spawns; the reload is mtime-triggered so it takes
effect on the next delivery. Restore by copying the sidecar back.

### webhook_subscriptions.json quirks

- **It's a credential store.** Because routes carry a `secret`, `read_file` refuses to open it
  ("Hermes credential store"). Inspect/edit it via the `terminal` tool (python/jq), redacting
  the secret in any output.
- **Every top-level key is treated as a route** and validated to require a `secret`. Do NOT
  stash a disabled route under a fake key like `_disabled_routes` inside the live file — the
  loader iterates it as a route, finds no secret, and logs a skip warning each reload. Keep the
  live file to real routes only (or `{}`); stash disabled routes in a separate sidecar file.
- A route with a missing/empty effective secret is **skipped with a warning, not a crash** —
  but don't rely on that; keep the file clean.

## Gateway restart/shutdown notices leak infrastructure to public PRs (security)

Distinct from chatter: when the gateway restarts or shuts down, it broadcasts a notice
(`⚠️ Gateway restarting — Your current task will be interrupted…`) to **every active session**.
A webhook session whose delivery sink is `github_comment` is treated like any chat session, so
that notice posts **publicly on the PR**. That leaks internal infrastructure — that you run a
gateway, that it restarts, and the timing — to anyone watching the repo. Treat it as a security
issue, not cosmetic.

Root cause (Hermes core, `gateway/run.py` `_notify_active_sessions_of_shutdown`): the broadcast
loops over all active sessions with no platform/delivery filter. There IS a per-platform opt-out
(`gateway_restart_notification`, `gateway/config.py`), but its default is `True`, so webhook is
opted IN by default.

**Fix — config lever, no core edit:**

```bash
hermes config set platforms.webhook.gateway_restart_notification false
```

This lands under top-level `platforms.webhook` (NOT `display.platforms.webhook` — different
tree). Verify it parsed to the right place:

```bash
<hermes_repo>/venv/bin/python -c "import yaml; d=yaml.safe_load(open('<hermes_home>/config.yaml')); \
  print((d.get('platforms') or {}).get('webhook'))"   # expect ...'gateway_restart_notification': False
```

After this, restart notices are suppressed for webhook (logged "Shutdown notification
suppressed … gateway_restart_notification=false"). Note: this and the display silence and the
route-disable are three independent holes — close all three; with the route disabled the others
are belt-and-suspenders but make a future re-enable safe.

## Prefer config levers over modifying built-in Hermes (workflow rule)

When a webhook/gateway misbehaves, the reflex to "fix the skill" or edit gateway code is usually
wrong. Hermes almost always already exposes a **config setting** for the behavior — find it
before touching code or bundled skills. This session's three fixes were ALL config:
`display.platforms.webhook.*`, `platforms.webhook.gateway_restart_notification`, and emptying
`webhook_subscriptions.json`. Zero core edits were actually needed.

- **Never edit the bundled/built-in skill copy** (`<hermes_repo>/skills/...`). It ships with
  Hermes; editing it is modifying the product. If you catch yourself having done so, revert with
  `git checkout -- <path>` in the repo and confirm `git status` is clean. Customizations belong
  in the user skills dir (`~/.hermes/skills/`), and even there, prefer encoding review discipline
  in the task prompt over forking a duplicated skill that then silently drifts from upstream.
- **Find the setting first.** `grep -rn "<behavior_key>" <hermes_repo>/gateway/config.py` and
  the display/resolver modules usually surface the exact knob and its default. Set it with
  `hermes config set`, then verify by reading resolved values — not file contents.
- A drive-by core code edit to fix a one-off is the wrong default. If core genuinely lacks a
  lever (e.g. the shutdown broadcast having no concept of "non-human delivery channel" as a safe
  default), that's a legitimate upstream ISSUE to file deliberately — not an undocumented local
  patch.

## Multi-profile team reviews (separate reviewer identity)

When a reviewer runs under its own Hermes profile (e.g. a dedicated reviewer persona):

- **Skills aren't shared across profiles by default.** A profile sees only its own
  `skills/` plus `skills.external_dirs`. To let a reviewer profile load a shared skill by
  name, point its config at the main skills dir:

  ```bash
  # external_dirs must be a real YAML list, not a quoted JSON string.
  # `hermes config set ... '["..."]'` may store the literal string — verify and fix to:
  #   skills:
  #     external_dirs:
  #       - /path/to/main/.hermes/skills
  ```

  If you pass `--skills <name>` for a skill the profile can't resolve, the run aborts with
  "Unknown skill(s)". Either wire `external_dirs` first, or bake the discipline into the task
  prompt and drop the `--skills` flag.

- **Each profile needs its own valid auth.** A reviewer profile with a stale/expired
  `GH_TOKEN` can read public PRs (anonymous API) but gets 401 on posting. Symptom: the review
  is composed and returned to the caller but never lands on the PR. Copy a working token into
  the profile `.env` (chmod 600); never echo token values.

## Double-posting: a webhook reviewer with no human watching posts twice

A webhook run has no human to notice a slow or unconfirmed POST, so the model is
tempted to "try again" and you end up with TWO review events on the same head SHA
(seen live: two `COMMENTED` reviews 77s apart — the first with 1 inline comment,
the second a fuller pass with 3). This is a quality bug, not a plumbing one: the
agent did real work both times.

**The guard (now in the main SKILL.md, "post exactly once" rule):** before
POSTing, check whether the reviewing identity already has a review on the *exact
head SHA*, and treat the POST's returned review `id` as the single proof of
success — never re-POST on a slow response or unconfirmed timeout. Re-running a
call that already succeeded is precisely what creates the duplicate.

```bash
HEAD_SHA=$(gh pr view $PR --repo $OWNER/$REPO --json headRefOid --jq '.headRefOid')
ME=$(gh api user --jq '.login')
# Pass shell vars into jq with --arg — do NOT backslash-escape $ inside a
# double-quoted --jq string (that SUPPRESSES shell expansion and the filter
# silently matches nothing). The portable form pipes to jq:
EXISTING=$(gh api repos/$OWNER/$REPO/pulls/$PR/reviews --paginate \
  | jq --arg me "$ME" --arg sha "$HEAD_SHA" \
       '[.[] | select(.user.login==$me and .commit_id==$sha)] | length')
[ "${EXISTING:-0}" -gt 0 ] && { echo "already reviewed $HEAD_SHA — do NOT post again"; exit 0; }
```

**Verifying the guard works on the LIVE webhook** (don't trust the doc — fire it):
re-fire the event on the *same head SHA* (a PR with an existing review by your
identity), then confirm the agent still *engaged fully* but posted *nothing*:

- review count before == review count after (e.g. stays 6 → 6),
- zero `Posted comment` events in `gateway.log` for the run's window,
- yet `agent.log` shows the run did real work (multiple API calls, `terminal`
  tool calls) — proving it ran and *chose* to skip, not that it failed to start.

That "engaged fully, posted nothing" pair is the signature of a working
idempotency guard, and it's the opposite of the missing-toolset stub signature
above (which is "barely engaged, posted a stub").

**If a duplicate already slipped through:** you canNOT dismiss a `COMMENTED`
review via the API (only `APPROVED`/`CHANGES_REQUESTED` can be dismissed). Leave
both — deleting the inline comments would erase real review content — and report
it out-of-band rather than papering over it.

## Non-interactive reviewer invocation (one-shot, quiet)

```bash
hermes --profile <reviewer> chat -q "<self-contained review task>" \
  --toolsets terminal --yolo -Q --max-turns 40
```

The task prompt must itself carry the non-interactive rules (no clarify, remote-only, one
comment) so the behavior holds even if the skill isn't loaded into that profile.

## Running a reviewer as a separate profile — credential + self-PR gotchas

Two failure modes proven in a live end-to-end test of a profile-run reviewer:

1. **The reviewer subprocess may not inherit a GitHub token.** Writing
   `GITHUB_TOKEN`/`GH_TOKEN` into `~/.hermes/profiles/<reviewer>/.env` does not
   guarantee `gh`/`curl` inside the `hermes chat --profile <reviewer>` subprocess
   see it — profile `.env` loading into the child's *process environment* for
   shell tools is not automatic. Symptom: the reviewer composes a perfect review
   payload, then every POST returns `401 Requires authentication`. A correct
   reviewer will refuse to fabricate a review ID (good — never invent one). Fixes:
   pass the token explicitly into the run's environment, or have the orchestrator
   post the reviewer's pre-validated payload with the token supplied via env. Do
   not rely on the profile `.env` alone until you've verified `gh api user` works
   *inside* that profile's subprocess.

2. **You cannot APPROVE your own PR.** If the reviewer's token resolves to the
   same GitHub identity as the PR author, `event: APPROVE` returns
   `422 Unprocessable Entity`. In a real two-identity team (author ≠ reviewer)
   this never happens; in single-identity testing, downgrade the event to
   `COMMENT` and note why in the body. The inline-comments + summary-body shape is
   identical for `COMMENT` and `APPROVE` — only the event verb changes.

3. **`422 "Line could not be resolved"`** means an inline anchor points to a line
   outside the diff. See the main SKILL "Anchoring rules" — compute commentable
   lines from the patch hunks first. One bad anchor fails the whole review; fix it
   and resubmit the single review, never fall back to scattered comments.
