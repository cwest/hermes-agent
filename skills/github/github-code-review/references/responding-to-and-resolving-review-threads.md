# Responding to and resolving review threads (the AUTHOR side of the loop)

A review is not "done" when the reviewer posts comments. The loop closes only when
the **author** has replied to each comment thread and **resolved** it. Posting the
review (reviewer side) and responding to it (author side) are two distinct halves;
this file covers the author half — the GitHub mechanics for threaded replies and
thread resolution.

Use this when you are operating as the implementer/author (e.g. Eckert) responding
to a reviewer's (e.g. Lamport's) comments, or any time you need to close out open
review threads on a PR.

## Who resolves: the AUTHOR, not the reviewer (decision, Casey 2026-06-24)

The author who addressed the finding is the one who resolves its thread — not the
reviewer on a second pass. This matches GitHub's own model (reviewers *request
changes*; authors *resolve* what they fixed) and avoids spawning a second reviewer
run just to click resolve. It loses no safety: the reviewer's re-review re-reads
the actual diff before PASS, and `required_conversation_resolution` still gates the
merge, so a dishonest resolve is caught by the re-review + the gate — not by making
the reviewer do the resolving. So: author responds AND resolves, every thread,
before the loop is declared closed.

## Why threads, not comments

A PR review comment lives on a **review thread**. The thread carries the
`isResolved` state — that is the thing that shows "resolved" / collapses in the
GitHub UI. An individual comment does not have a resolved state; the *thread* does.
So you always work at thread granularity: reply on the thread, then resolve the
thread.

Two IDs, and you need both:

- **`thread_id`** — a GraphQL node id, looks like `PRRT_kwDO…`. Used to *resolve*
  the thread (GraphQL mutation only).
- **first comment `databaseId`** — an integer REST id. Used to post a *threaded
  reply* via REST (`/pulls/{n}/comments/{id}/replies`).

## Step 1 — enumerate OPEN threads

Filter on `isResolved`, not on comment presence. A thread can hold comments and
still be open; that's exactly the case you must act on. `isOutdated` (true after a
force-push moves the anchored line) does NOT mean resolved — outdated threads
still need a reply + resolve.

```bash
gh api graphql -f query='
{
  repository(owner:"OWNER", name:"REPO") {
    pullRequest(number: N) {
      reviewThreads(first: 50) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            nodes { databaseId author { login } path line body }
          }
        }
      }
    }
  }
}'
```

For each node where `isResolved == false`, capture `id` (thread_id) and
`comments.nodes[0].databaseId` (the comment to reply to).

## Step 2 — reply on the thread (THREADED, not a new top-level comment)

A threaded reply keeps the conversation anchored under the reviewer's comment. Do
NOT use `gh pr comment` here — that posts a detached top-level issue comment that
isn't part of the thread.

```bash
gh api repos/OWNER/REPO/pulls/N/comments/COMMENT_DB_ID/replies \
  --method POST -f body='Documented the fail-open behaviour inline at the guard. …'
```

The reply text is human-facing writing under a real identity — apply the
humanizer/de-claude pass (see the main SKILL.md gate): acknowledge the finding,
say what changed (point at the commit/line that addresses it), no attribution, no
system/status codes, no "Great catch!". If the fix already landed, acknowledge and
point at it — do NOT re-implement.

## Step 3 — resolve the thread (GraphQL mutation)

```bash
gh api graphql -f query='
mutation {
  resolveReviewThread(input: {threadId: "PRRT_…"}) {
    thread { isResolved }
  }
}'
```

(`unresolveReviewThread` reopens it if you resolved one in error.)

## Step 4 — VERIFY

Re-run the Step 1 query and confirm every thread shows `isResolved: true`.
Self-report ("I resolved them") is not proof — read the state back.

## Batch recipe

When there are several threads, drive replies + resolves in one pass keyed by
`(thread_id, comment_db_id, reply_body)`, then run the verify query once at the
end and assert all `True`. A short Python script over `gh api` (reply via REST,
resolve via GraphQL, collect results) is the clean way; print the final
`[isResolved …]` vector so the close-out is auditable.

## Credential gotcha when the author is a separate Hermes profile

Running the author under its own profile (`hermes chat --profile eckert`) does NOT
expose that profile's `.env` GitHub token to the agent's terminal `gh`. The
teammate will draft correct replies and then refuse to post (no resolvable
credential) — that refusal is the trust wall working, not a bug. Post the
teammate's drafted text yourself with the token supplied explicitly (set
`GH_TOKEN` in the environment of the `gh` call), or fix credential reachability.
Either way the teammate's words go up verbatim; you are the courier, not the
author. (Same gap applies to the reviewer profile posting its review.)
