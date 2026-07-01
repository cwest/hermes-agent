# Editing an already-posted review in place

You normally run the humanizer/de-claude pass *before* posting (see SKILL.md
"Humanizer / de-claude pass before posting"). But if a review already landed and
needs fixing — an AI-ism slipped through, a system/status code leaked into the
body, an anchor was wrong — you do **not** have to delete and re-post. GitHub
lets you edit a submitted review's body and each inline comment in place, so the
review keeps its single review-event identity and notification.

## The two endpoints (they differ)

- **Review summary body:** `PUT /repos/{owner}/{repo}/pulls/{pr}/reviews/{review_id}`
  with `{"body": "..."}`. (PUT, not PATCH.)
- **Each inline comment:** `PATCH /repos/{owner}/{repo}/pulls/comments/{comment_id}`
  with `{"body": "..."}`. Note the path is `/pulls/comments/{id}` — a review-
  comment endpoint, not `/pulls/{pr}/comments`.

## Matching comments to your rewrites

Inline comments don't carry your local labels, so match them by `(path, line)`:

```bash
gh api repos/$OWNER/$REPO/pulls/$PR/comments --paginate \
  -q '.[] | "\(.id)\t\(.path):\(.line)"'
```

Build a `{(path, line): comment_id}` map, then PATCH each by id.

## Minimal recipe (gh handles the token; no secrets in the command)

```bash
REVIEW_ID=$(gh api repos/$OWNER/$REPO/pulls/$PR/reviews \
  -q '.[] | select(.user.login=="<reviewer>") | .id' | tail -1)

# 1) summary body
gh api -X PUT repos/$OWNER/$REPO/pulls/$PR/reviews/$REVIEW_ID \
  -f body="<humanized summary>"

# 2) each inline comment, by id from the path:line map above
gh api -X PATCH repos/$OWNER/$REPO/pulls/comments/<comment_id> \
  -f body="<humanized comment>"
```

Verify the cleanup landed — grep the live review for the tells you removed; the
count should be zero:

```bash
{ gh api repos/$OWNER/$REPO/pulls/$PR/reviews/$REVIEW_ID -q '.body';
  gh api repos/$OWNER/$REPO/pulls/$PR/comments -q '.[].body'; } \
  | grep -cE 'Posted as COMMENT|Non-blocking\.|well-scoped|I hope this helps'
```

## Gotchas

- **You cannot APPROVE your own PR.** If the reviewer identity equals the PR
  author (common in a single-account test), `event: "APPROVE"` returns 422.
  Downgrade to `event: "COMMENT"`. In a real team the reviewer (e.g. Lamport)
  and author (e.g. Eckert) are distinct identities, so this only bites in
  single-account dry runs. Either way, the *reason* never goes in the review
  body — the human operator hears it out-of-band (see the humanizer gate's
  no-system-codes rule).
- **A bad anchor fails the whole review POST.** If the initial post 422'd on
  "Line could not be resolved", fix the anchor with `scripts/commentable_lines.py`
  and re-post — don't spray separate comments. Once posted, editing is per the
  recipe above.
- **Profile reviewers and the token.** When a reviewer runs under a separate
  Hermes profile (`hermes chat --profile lamport`), its subprocess may not
  inherit the profile's `.env`, so `gh` can't see the token and the post fails
  auth. The reviewer should read the token from its own profile `.env`
  (`~/.hermes/profiles/<name>/.env`) and pass it explicitly as `GH_TOKEN` in the
  posting subprocess env — not assume the ambient shell has it. See
  `webhook-triggered-reviews.md` for the profile-reviewer setup.
