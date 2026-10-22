---
name: address-review
description: Start a local-review session and address the review comments and chat messages the user leaves in it (local-review is a local GitHub-style diff reviewer with a chat panel). Use when the user asks to spin up, open or start local-review (or a review of your changes), says "address my review", "I left comments", "check local-review", "/address-review", or a watcher or hook reports local-review activity.
---

# Address local-review comments

The user reviews your changes in the `local-review` browser UI and submits line comments in batches, and can chat with you in a side panel (messages arrive one at a time, straight away). You read and answer both with the `local-review` CLI, run from the repo/worktree being reviewed.

## Starting a review

When the user asks for a review (or you offer one and they accept):

1. Work out what to review: the repo or worktree with your changes, and the base. Use `--base <branch>` for the branch it will merge into (or the branch below it in a stack); without a stack.conf or an obvious base, ask the user rather than guessing.
2. Start the server in the background from that worktree: `local-review serve --base <base> --no-open --port <port>`. Each worktree needs its own port; pick a free one if another review is running. Give the user the URL.
3. **Start the watcher straight away, without being asked**: `local-review watch --once`, in the background from the same worktree, so you're told when the user submits a review, replies in a thread or sends a chat message (see Watching below). The chat panel shows the user whether a watcher is running. Tell the user you're watching.
4. If you can't run background commands that notify you when they exit, say so, and ask the user to tell you ("check local-review") when they've submitted.

## Addressing a review


1. Run `local-review list` to see every open comment (it also says when chat messages are waiting; see Answering the chat), with its id, current `file:line`, a code excerpt and the full thread. If nothing is open, say so, re-arm the watcher and stop.
2. Read all the comments before you edit anything. Comments in one review often relate to each other, and general (file-less) comments from older reviews explain the reviewer's overall intent.
3. Handle each comment:
   - **Requested change:** make it, then reply saying briefly what you changed, and pass `--addressed`.
   - **Question:** answer it. Change code only if the answer shows a problem. Reply without `--addressed`, which sets the status to "replied".
   - **You disagree, or it's ambiguous:** don't guess and don't comply silently. Give your reasoning or ask a question, without `--addressed`. Pushing back is fine; the reviewer decides.
   - **OUTDATED comments:** the line has changed since the reviewer wrote the comment. Use the original context in the excerpt to work out what they meant.
   - **Stack comments** (location starts with `[branch]`): the reviewer left them on that branch of a stacked-PR series. Make the change *on that branch*: check it out, fix it, and fold the fix into the commit it belongs to (the stack keeps production code and tests in separate commits, so amend the matching one rather than adding a stray commit). Then rebase each branch above it in stack order (`git rebase --onto <fixed> <old-tip> <next>`), and re-verify every rebased branch builds. `local-review list --branch <b>` narrows the list to one branch; batch all comments for a branch before rebasing so the stack is rebased once.
4. Reply with `local-review reply <id> [--addressed] -m "..."`. Id prefixes work. Keep replies short and specific (what changed, where). Use backticks for identifiers. For multi-line replies, pipe a heredoc on stdin instead of passing `-m`.
5. Never resolve a comment. Only the reviewer resolves.
6. Validate the way the repo expects (tests, lint), following its CLAUDE.md or AGENTS.md. Don't commit unless the user or the repo conventions say to.
7. Re-arm the watcher (see Watching).
8. Finish with a short summary for the user: how many comments you addressed, which ones you replied to or pushed back on, and anything still unresolved. Point them back to the UI (`local-review`, then **Since last review**) to check your changes.

## Answering the chat

When the watcher reports a chat message (or `list`/the hook says chat messages are unanswered):

1. Run `local-review chat` to read the conversation. Unanswered messages are marked `UNANSWERED`; a message `about` a file and line range comes with a code excerpt, already re-anchored to where that code is now.
2. Answer them in **one** message: `local-review chat -m "..."`, or pipe a longer answer on stdin with `-m -`. Markdown renders (code blocks, tables, lists). Keep it conversational and short; it's a chat, not a report.
3. When there are natural next steps, offer them with `--suggest "..."` (repeatable, 2–3 at most, each a short imperative the user could have typed, like "Add a KDoc note on why amounts are in cents"). The user sends one with a click, and it arrives as an ordinary chat message.
4. If the user asks for a change in chat, make it, as you would for a comment, then say in your answer what changed and where. Chat has no `--addressed` and nothing to resolve.
5. Always answer, even just to say you're on it or can't do something: answering clears the "Agent working" indicator the watcher set when it picked the message up. For long work, answer first with what you're about to do, then again when it's done.
6. Re-arm the watcher.

## Watching

Keep a watcher running for as long as a review server you started is up; that's the default, not something the user has to ask for.

- Run `local-review watch --once` in the background (e.g. Bash with `run_in_background`) from the reviewed worktree. It exits when a review is submitted, the reviewer replies in a thread, or they send a chat message; its output says whether to run `list` or `chat`.
- When it exits, address the review or answer the chat (steps above), then **start it again** before you finish your turn, so the next round is picked up too. Re-arm after addressing comments you found any other way as well (the user asked you to check, or a hook reported them).
- One watcher per reviewed worktree. If you're reviewing two worktrees at once, watch both.
- If a watcher stops without an event (the session restarted, it was killed), start it again the next time you notice, and check `local-review list` in case you missed something.
- Stop re-arming only when the user says to, or the review is finished (see Cleaning up).

## Cleaning up

Snapshots from earlier reviews in the session are kept on purpose, so never clean up in the middle of a review. Run `local-review clean` once the work is finished: the user approves it, says it's merged or done, or asks you to wrap up. It archives resolved threads and drops every snapshot except the latest. Use `clean --all` only when the user wants the whole review thrown away; it refuses if draft or open comments would be lost, unless `--force` is passed. Report what the command removed.

## Notes

- `local-review list --all` also shows replied, addressed and resolved threads, if you need history.
- `local-review status` prints counts per status.
- Draft comments (not yet submitted) are invisible to you on purpose. If the user says they left comments but `list` shows none, tell them to click **Submit review**.
