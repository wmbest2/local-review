---
name: address-review
description: Address review comments the user left in the local-review tool (a local GitHub-style diff reviewer). Use when the user says "address my review", "I left comments", "check local-review", "/address-review", or a hook reports open local-review comments.
---

# Address local-review comments

The user reviews your changes in the `local-review` browser UI and submits line comments in batches. You read them and answer them with the `local-review` CLI, run from the repo/worktree being reviewed.

## Steps

1. Run `local-review list` to see every open comment, with its id, current `file:line`, a code excerpt and the full thread. If nothing is open, say so and stop.
2. Read all the comments before you edit anything. Comments in one review often relate to each other, and the general (file-less) comments explain the reviewer's overall intent.
3. Handle each comment:
   - **Requested change:** make it, then reply saying briefly what you changed, and pass `--addressed`.
   - **Question:** answer it. Change code only if the answer shows a problem. Reply without `--addressed`, which sets the status to "replied".
   - **You disagree, or it's ambiguous:** don't guess and don't comply silently. Give your reasoning or ask a question, without `--addressed`. Pushing back is fine; the reviewer decides.
   - **OUTDATED comments:** the line has changed since the reviewer wrote the comment. Use the original context in the excerpt to work out what they meant.
4. Reply with `local-review reply <id> [--addressed] -m "..."`. Id prefixes work. Keep replies short and specific (what changed, where). Use backticks for identifiers. For multi-line replies, pipe a heredoc on stdin instead of passing `-m`.
5. Never resolve a comment. Only the reviewer resolves.
6. Validate the way the repo expects (tests, lint), following its CLAUDE.md or AGENTS.md. Don't commit unless the user or the repo conventions say to.
7. Finish with a short summary for the user: how many comments you addressed, which ones you replied to or pushed back on, and anything still unresolved. Point them back to the UI (`local-review`, then **Since last review**) to check your changes.

## Watching for submissions

If the user asks you to watch for reviews, run `local-review watch --once` in the background (Bash with `run_in_background`) from the reviewed worktree. It exits when a review is submitted. When it exits, follow the steps above, then start it again so the next review is picked up too. Stop re-arming when the user says to.

## Cleaning up

Snapshots from earlier reviews in the session are kept on purpose, so never clean up in the middle of a review. Run `local-review clean` once the work is finished: the user approves it, says it's merged or done, or asks you to wrap up. It archives resolved threads and drops every snapshot except the latest. Use `clean --all` only when the user wants the whole review thrown away; it refuses if draft or open comments would be lost, unless `--force` is passed. Report what the command removed.

## Notes

- `local-review list --all` also shows replied, addressed and resolved threads, if you need history.
- `local-review status` prints counts per status.
- Draft comments (not yet submitted) are invisible to you on purpose. If the user says they left comments but `list` shows none, tell them to click **Submit review**.
