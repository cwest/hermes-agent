# Consolidating duplicate reviews on a PR

When more than one review event from your identity lands on the same PR (interrupted
runs, gateway restarts, or a consent-gate timeout that reported `BLOCKED` while the
`gh api` POST actually succeeded — see SKILL.md rule 4), the goal is to get back to
the skill's mandated end state: **one review event with visible inline comments**,
everything else collapsed.

You **cannot** delete a `COMMENTED` review via the API, and you should **not** delete
its inline comments — that erases real review content. Use GitHub's **minimize**
mutation, which is *reversible* (it collapses a comment as "marked as duplicate", not
gone).

## 1. Enumerate every review and its comments

```bash
gh api graphql -f query='query {
  repository(owner:"OWNER",name:"REPO"){ pullRequest(number:N){
    reviews(first:20){ nodes {
      databaseId state createdAt
      comments(first:20){ nodes { databaseId path isMinimized } }
    } } } } }' \
  --jq '.data.repository.pullRequest.reviews.nodes[] |
        "review \(.databaseId) [\(.state)]: " +
        ([.comments.nodes[] | "\(.path)\(if .isMinimized then " (hidden)" else " VISIBLE" end)"] | join(", "))'
```

Also fetch each review's `body` (REST: `gh api repos/OWNER/REPO/pulls/N/reviews/<id> --jq '.body'`)
so you can pick which review to keep as canonical.

## 2. Pick the canonical review

Keep the one with the **strongest body** and **valid diff-anchored inline comments**.
It does not have to be the newest — in the session that produced this reference, the
*earliest* review had the most complete body, while a later duplicate had sharper
inline notes. You can keep the earliest as canonical and `PUT` an improved body onto
it (`gh api repos/OWNER/REPO/pulls/N/reviews/<id> --method PUT -f body="$(cat body.md)"`),
salvaging the best wording from the duplicates before collapsing them.

## 3. Minimize the redundant inline comments (reversible)

`minimizeComment` takes the comment's **GraphQL node id** (`PRRC_…`), not the REST
integer. Get the node id, then minimize:

```bash
# REST comment id -> node id
NODE=$(gh api repos/OWNER/REPO/pulls/comments/<REST_COMMENT_ID> --jq '.node_id')

gh api graphql -f query='mutation($id:ID!){
  minimizeComment(input:{classifier:DUPLICATE, subjectId:$id}){
    minimizedComment { isMinimized } } }' -f id="$NODE" \
  --jq '.data.minimizeComment.minimizedComment.isMinimized'   # -> true
```

Loop over every redundant comment (all comments belonging to the duplicate reviews,
plus any empty threaded-reply stragglers from an earlier consolidation attempt):

```bash
for cid in 3439653446 3439653448 3439653450 ...; do
  nid=$(gh api repos/OWNER/REPO/pulls/comments/$cid --jq '.node_id')
  gh api graphql -f query='mutation($id:ID!){ minimizeComment(input:{classifier:DUPLICATE, subjectId:$id}){ minimizedComment { isMinimized } } }' \
     -f id="$nid" --jq '"'"$cid"' -> "+( .data.minimizeComment.minimizedComment.isMinimized|tostring)'
done
```

`classifier` values: `DUPLICATE` (the right one here), `OUTDATED`, `RESOLVED`,
`OFF_TOPIC`, `SPAM`, `ABUSE`. To undo, use the `unminimizeComment` mutation with the
same node id.

## 4. Fix any now-stale comment on the canonical review

A comment written by an earlier run can be wrong by the time you consolidate (e.g. it
asks for a test the PR already added). Edit it in place rather than leaving a wrong
nit: `gh api repos/OWNER/REPO/pulls/comments/<id> --method PATCH -f body="..."`.

## 5. Verify the end state

```bash
gh api graphql -f query='query { repository(owner:"OWNER",name:"REPO"){ pullRequest(number:N){
  reviews(first:20){ nodes { databaseId comments(first:20){ nodes { path isMinimized } } } } } } }' \
  --jq '[.data.repository.pullRequest.reviews.nodes[].comments.nodes[] | select(.isMinimized==false) | .path]'
```

Expect exactly the canonical review's comments to show as visible; everything else
hidden. Then report the duplication (and its cause) to the operator out-of-band — the
published review must not mention it.

## Gotchas

- The mutation rejects a placeholder/truncated node id with
  `Could not resolve to a node with the global id of '...'`. Always fetch the real
  `.node_id`; never hand-type it.
- Minimizing only *collapses* a comment in the UI; the review event itself remains in
  the list. That's fine — the skill's rule is "one review with visible content", which
  minimize achieves without destroying anything.
