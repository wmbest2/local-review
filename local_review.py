#!/usr/bin/env python3
"""local-review: a local, GitHub-style diff review tool whose comments an LLM agent can act on.

Human side:  `local-review` opens a browser UI to review the branch (or uncommitted changes),
             leave line comments as drafts, and submit them as a batch.
Agent side:  `local-review list` / `local-review reply` read and answer submitted comments.

Stacks:      with a `stack.conf` (the push-stack format) at the repo root, the "stack" view reviews
             each branch of a stacked-PR series against the branch below it, like its PR will.

State lives in <git-dir>/local-review/state.json, so it is per-worktree and never committed.
"""
import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.realpath(__file__))
CONTEXT_LINES = 3  # lines of surrounding code stored with each comment to re-anchor it after edits
REVIEWER = "reviewer"
ORPHAN_MIN_AGE_S = 3600  # never prune snapshots this young: another worktree may be mid-submit
AGENT = "agent"


class UserError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(*args, cwd=None, env=None, check=True):
    p = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, env=env, capture_output=True)
    if check and p.returncode != 0:
        raise UserError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode("utf-8", errors="replace")


def split_lines(text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


# --------------------------------------------------------------------------------------------- git


class Repo:
    def __init__(self, path):
        self.root = git("rev-parse", "--show-toplevel", cwd=path).strip()
        git_dir = git("rev-parse", "--absolute-git-dir", cwd=self.root).strip()
        self.state_dir = os.path.join(git_dir, "local-review")
        os.makedirs(self.state_dir, exist_ok=True)
        self.state_path = os.path.join(self.state_dir, "state.json")
        self.lock_path = os.path.join(self.state_dir, "state.lock")

    def g(self, *args, **kw):
        return git(*args, cwd=self.root, **kw)

    def rev(self, ref):
        return self.g("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).strip()

    def head(self):
        return self.rev("HEAD")

    def branch(self):
        return self.g("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()

    def default_base(self):
        ref = self.g("symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).strip()
        if ref:
            return ref
        for cand in ("origin/develop", "origin/main", "origin/master", "develop", "main", "master"):
            if self.rev(cand):
                return cand
        return "HEAD"

    def snapshot_tree(self):
        """Tree object of the working tree as it is right now, including untracked (non-ignored) files.

        Uses a throwaway copy of the index so the real index, stash and refs are untouched.
        """
        index = self.g("rev-parse", "--git-path", "index").strip()
        if not os.path.isabs(index):
            index = os.path.join(self.root, index)
        fd, tmp = tempfile.mkstemp(prefix="local-review-index-")
        os.close(fd)
        try:
            if os.path.exists(index):
                shutil.copyfile(index, tmp)
            else:
                os.remove(tmp)
            env = dict(os.environ, GIT_INDEX_FILE=tmp)
            self.g("add", "-A", env=env)
            return self.g("write-tree", env=env).strip()
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def pin_snapshot(self, tree, name):
        """Wrap a snapshot tree in a commit under refs/local-review/ so gc keeps it."""
        head = self.head()
        parents = ["-p", head] if head else []
        commit = self.g("commit-tree", tree, *parents, "-m", "local-review snapshot").strip()
        self.g("update-ref", f"refs/local-review/{name}", commit)
        return commit

    def working_file_lines(self, path):
        full = os.path.join(self.root, path)
        try:
            with open(full, "rb") as f:
                return split_lines(f.read().decode("utf-8", errors="replace"))
        except OSError:
            return None

    def branch_file_lines(self, branch, path):
        """A file as a review of `branch` sees it: the working tree when that branch is checked out
        (so uncommitted edits count), otherwise the branch's committed content."""
        if not branch or branch == self.branch():
            return self.working_file_lines(path)
        return self.rev_file_lines(branch, path)

    def rev_file_lines(self, rev, path):
        p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=self.root, capture_output=True)
        if p.returncode != 0:
            return None
        return split_lines(p.stdout.decode("utf-8", errors="replace"))


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _strip_path(p):
    p = p.rstrip("\t")
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text):
    files = []
    cur = None
    raw = []
    orem = nrem = 0
    o = n = 0

    def finish():
        if cur is not None:
            cur["hash"] = hashlib.sha1("\n".join(raw).encode()).hexdigest()[:16]
            cur["path"] = cur["new_path"] or cur["old_path"]

    for line in text.split("\n"):
        if line.startswith("diff --git ") and orem <= 0 and nrem <= 0:
            finish()
            raw = []
            cur = {"old_path": None, "new_path": None, "status": "modified", "binary": False,
                   "hunks": [], "additions": 0, "deletions": 0}
            files.append(cur)
            rest = line[len("diff --git "):]
            # Unambiguous when both sides are the same path (the common case, even with spaces).
            plen = (len(rest) - 5) // 2
            if rest.startswith("a/") and rest[2:2 + plen] == rest[5 + plen:] and rest[2 + plen:5 + plen] == " b/":
                cur["old_path"] = cur["new_path"] = rest[2:2 + plen]
            raw.append(line)
            continue
        if cur is None:
            continue
        raw.append(line)
        if orem > 0 or nrem > 0:
            tag, body = line[:1], line[1:]
            hunk_lines = cur["hunks"][-1]["lines"]
            if tag == "+":
                hunk_lines.append({"t": "add", "o": None, "n": n, "s": body})
                n += 1
                nrem -= 1
                cur["additions"] += 1
            elif tag == "-":
                hunk_lines.append({"t": "del", "o": o, "n": None, "s": body})
                o += 1
                orem -= 1
                cur["deletions"] += 1
            elif tag == "\\":
                pass
            else:
                hunk_lines.append({"t": "ctx", "o": o, "n": n, "s": body})
                o += 1
                n += 1
                orem -= 1
                nrem -= 1
            continue
        m = HUNK_RE.match(line)
        if m:
            o = int(m.group(1))
            orem = int(m.group(2)) if m.group(2) is not None else 1
            n = int(m.group(3))
            nrem = int(m.group(4)) if m.group(4) is not None else 1
            cur["hunks"].append({"header": line, "section": m.group(5), "lines": []})
        elif line.startswith("--- "):
            cur["old_path"] = _strip_path(line[4:])
        elif line.startswith("+++ "):
            cur["new_path"] = _strip_path(line[4:])
        elif line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["status"] = "renamed"
            cur["old_path"] = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur["new_path"] = line[len("rename to "):]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur["binary"] = True
    finish()
    for f in files:
        if f["status"] == "added":
            f["old_path"] = None
        elif f["status"] == "deleted":
            f["new_path"] = None
            f["path"] = f["old_path"]
    return files


# ------------------------------------------------------------------------------------------- stack


def load_stack(repo, path=None):
    """Reads a push-stack `stack.conf`: `key = value` settings plus `tier | branch | title` rows in
    stack order. Each branch's parent is the row above it; the first row's parent is `trunk`.
    Returns {"trunk", "phases": [{"branch", "parent", "tier", "title"}]} or None without a file."""
    path = path or os.path.join(repo.root, "stack.conf")
    try:
        with open(path) as f:
            raw = f.read().split("\n")
    except FileNotFoundError:
        return None
    settings, rows = {}, []
    for line in raw:
        line = re.sub(r"\s+#.*$", "", line).strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            parts = [x.strip() for x in line.split("|", 2)]
            if len(parts) == 3 and parts[1]:
                rows.append({"tier": parts[0], "branch": parts[1], "title": parts[2]})
        elif "=" in line:
            k, v = line.split("=", 1)
            settings[k.strip()] = v.strip()
    if not rows:
        return None
    trunk = settings.get("trunk") or repo.default_base()
    parent = trunk
    for r in rows:
        r["parent"] = parent
        parent = r["branch"]
    return {"trunk": trunk, "phases": rows}


def stack_phase(stack, branch):
    for phase in (stack or {}).get("phases", []):
        if phase["branch"] == branch:
            return phase
    raise UserError(f"{branch} is not in the stack" if stack else "no stack.conf found")


def resolve_ref(repo, ref):
    """A branch that only exists on the remote (e.g. a merged-away trunk) still resolves."""
    if repo.rev(ref):
        return ref
    if repo.rev(f"origin/{ref}"):
        return f"origin/{ref}"
    raise UserError(f"unknown ref: {ref}")


def resolve_view(repo, mode, base, branch=None, stack=None):
    """Returns (old_rev, new_tree, description) for a diff mode, snapshotting the working tree."""
    if mode == "stack":
        phase = stack_phase(stack, branch)
        tip = resolve_ref(repo, branch)
        parent = resolve_ref(repo, phase["parent"])
        old = repo.g("merge-base", parent, tip).strip()
        if branch == repo.branch():
            new_tree, where = repo.snapshot_tree(), "working tree"
        else:
            new_tree, where = repo.g("rev-parse", f"{tip}^{{tree}}").strip(), "committed"
        return old, new_tree, f"{phase['parent']} → {branch} ({where})"
    new_tree = repo.snapshot_tree()
    if mode == "branch":
        if not repo.rev(base):
            raise UserError(f"unknown base ref: {base}")
        old = repo.g("merge-base", base, "HEAD").strip()
        label = f"{base} (merge-base {old[:8]}) → working tree"
    elif mode == "working":
        old = repo.head()
        label = "HEAD → working tree (uncommitted, incl. untracked)"
    elif mode == "since":
        st = load_state(repo)
        if not st["reviews"]:
            raise UserError("no submitted review yet")
        last = st["reviews"][-1]
        old = last["snapshot"]
        label = f"last review ({last['submitted_at']}) → working tree"
    else:
        raise UserError(f"unknown mode: {mode}")
    return old, new_tree, label


def compute_diff(repo, mode, base, context, branch=None, stack=None):
    old, new_tree, label = resolve_view(repo, mode, base, branch, stack)
    text = repo.g("diff", "--no-color", "--no-ext-diff", "-M", f"-U{context}",
                  "--src-prefix=a/", "--dst-prefix=b/", old, new_tree)
    commits = []
    if mode in ("branch", "stack"):
        tip = resolve_ref(repo, branch) if mode == "stack" else "HEAD"
        log = repo.g("log", "--format=%h %s", f"{old}..{tip}")
        commits = [ln for ln in log.split("\n") if ln]
    return {"mode": mode, "base": base, "branch": branch if mode == "stack" else None, "old_rev": old,
            "new_tree": new_tree, "label": label, "commits": commits, "files": parse_diff(text)}


# ------------------------------------------------------------------------------------------- state


def empty_state():
    return {"version": 1, "reviews": [], "comments": []}


def load_state(repo):
    try:
        with open(repo.state_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return empty_state()


@contextmanager
def state_txn(repo, write=False):
    with open(repo.lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        st = load_state(repo)
        yield st
        if write:
            tmp = repo.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f, indent=2)
            os.replace(tmp, repo.state_path)


def state_version(repo):
    try:
        return os.stat(repo.state_path).st_mtime_ns
    except FileNotFoundError:
        return 0


def new_id(st, prefix):
    taken = {c["id"] for c in st["comments"]} | {r["id"] for r in st["reviews"]}
    while True:
        cid = prefix + uuid.uuid4().hex[:6]
        if cid not in taken:
            return cid


def find_comment(st, cid):
    matches = [c for c in st["comments"] if c["id"] == cid or c["id"].startswith(cid)]
    if not matches:
        raise UserError(f"no comment with id {cid}")
    if len(matches) > 1:
        raise UserError(f"ambiguous comment id {cid}")
    return matches[0]


def locate(lines, anchor):
    """Find the anchored line in (possibly edited) file content. Returns 1-based line or None."""
    target = anchor["line_text"].rstrip()
    before, after = anchor["before"], anchor["after"]
    best = None
    for i, text in enumerate(lines):
        if text.rstrip() != target:
            continue
        score = 0
        for k, b in enumerate(reversed(before)):
            j = i - 1 - k
            if j >= 0 and lines[j].rstrip() == b.rstrip():
                score += 1
        for k, a in enumerate(after):
            j = i + 1 + k
            if j < len(lines) and lines[j].rstrip() == a.rstrip():
                score += 1
        key = (score, -abs(i + 1 - anchor["line"]))
        if best is None or key > best[0]:
            best = (key, i + 1)
    if best is None:
        return None
    # Short/blank lines like "}" occur everywhere; without any matching context we can't trust the match.
    if len(target.strip()) <= 3 and best[0][0] == 0 and (before or after):
        return None
    return best[1]


def annotate(repo, comments):
    """Adds current_line/outdated to each comment by re-anchoring against current file content."""
    cache = {}
    for c in comments:
        c["current_line"] = None
        c["outdated"] = False
        if not c.get("file"):
            continue
        if c["side"] == "old":
            c["current_line"] = c["anchor"]["line"]
            continue
        key = (c.get("branch"), c["file"])
        if key not in cache:
            cache[key] = repo.branch_file_lines(c.get("branch"), c["file"])
        lines = cache[key]
        found = locate(lines, c["anchor"]) if lines is not None else None
        c["current_line"] = found
        c["outdated"] = found is None
    return comments


def add_comment(repo, data):
    body = (data.get("body") or "").strip()
    if not body:
        raise UserError("comment body is empty")
    comment = {"status": "draft", "review_id": None, "created_at": now(), "author": REVIEWER,
               "body": body, "replies": [], "file": None, "side": None, "anchor": None,
               "old_rev": None, "head": repo.head(), "branch": data.get("branch") or None}
    path = data.get("file")
    if path:
        side = data.get("side")
        line = int(data.get("line"))
        if side == "new":
            lines = repo.branch_file_lines(comment["branch"], path)
        elif side == "old":
            if not data.get("old_rev"):
                raise UserError("old_rev required for old-side comments")
            lines = repo.rev_file_lines(data["old_rev"], path)
            comment["old_rev"] = data["old_rev"]
        else:
            raise UserError("side must be 'new' or 'old'")
        if lines is None or not 1 <= line <= len(lines):
            raise UserError(f"{path}:{line} does not exist")
        i = line - 1
        comment.update(file=path, side=side, anchor={
            "line": line, "line_text": lines[i],
            "before": lines[max(0, i - CONTEXT_LINES):i], "after": lines[i + 1:i + 1 + CONTEXT_LINES]})
    with state_txn(repo, write=True) as st:
        comment["id"] = new_id(st, "c")
        st["comments"].append(comment)
    return comment


def submit_review(repo, summary):
    snapshot = repo.pin_snapshot(repo.snapshot_tree(), "tmp")
    with state_txn(repo, write=True) as st:
        rid = new_id(st, "r")
        repo.g("update-ref", f"refs/local-review/{rid}", snapshot)
        review = {"id": rid, "submitted_at": now(), "snapshot": snapshot, "head": repo.head()}
        summary = (summary or "").strip()
        if summary:
            st["comments"].append({"id": new_id(st, "c"), "status": "draft", "created_at": now(),
                                   "author": REVIEWER, "body": summary, "replies": [], "file": None,
                                   "side": None, "anchor": None, "old_rev": None, "head": review["head"]})
        drafts = [c for c in st["comments"] if c["status"] == "draft"]
        if not drafts:
            raise UserError("nothing to submit: no draft comments and no summary")
        for c in drafts:
            c["status"] = "open"
            c["review_id"] = rid
            c["submitted_at"] = review["submitted_at"]
        review["comment_count"] = len(drafts)
        st["reviews"].append(review)
    repo.g("update-ref", "-d", "refs/local-review/tmp", check=False)
    return review


def update_comment(repo, cid, action, data):
    with state_txn(repo, write=True) as st:
        c = find_comment(st, cid)
        if action == "edit":
            if c["status"] != "draft":
                raise UserError("only drafts can be edited")
            body = (data.get("body") or "").strip()
            if not body:
                raise UserError("comment body is empty")
            c["body"] = body
        elif action == "delete":
            if c["status"] != "draft":
                raise UserError("only drafts can be deleted")
            st["comments"].remove(c)
        elif action == "reply":
            if c["status"] == "draft":
                raise UserError("submit the review before replying")
            body = (data.get("body") or "").strip()
            if not body:
                raise UserError("reply is empty")
            c["replies"].append({"author": REVIEWER, "body": body, "at": now()})
            c["status"] = "open"  # a reviewer reply always puts the ball back in the agent's court
        elif action == "status":
            status = data.get("status")
            if status not in ("open", "resolved") or c["status"] == "draft":
                raise UserError("invalid status change")
            c["status"] = status
        else:
            raise UserError(f"unknown action {action}")
        return c


# ------------------------------------------------------------------------------------------ server


class App:
    def __init__(self, repo, base, mode, stack=None):
        self.repo, self.base, self.mode, self.stack = repo, base, mode, stack


class Handler(BaseHTTPRequestHandler):
    server_version = "local-review"

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def _host_ok(self):
        # Guards against DNS-rebinding: only answer requests addressed to loopback names.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _comments_payload(self):
        repo = self.app.repo
        with state_txn(repo) as st:
            v = state_version(repo)
            comments = annotate(repo, st["comments"])
            return {"v": v, "comments": comments, "reviews": st["reviews"]}

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        repo = self.app.repo
        try:
            if u.path == "/":
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path.startswith("/vendor/"):
                name = u.path[len("/vendor/"):]
                vendor = os.path.join(HERE, "vendor")
                if name not in os.listdir(vendor):
                    return self._json({"error": "not found"}, 404)
                ctype = {".js": "text/javascript", ".css": "text/css"}.get(os.path.splitext(name)[1], "text/plain")
                with open(os.path.join(vendor, name), "rb") as f:
                    self._send(200, f.read(), ctype + "; charset=utf-8")
            elif u.path == "/api/meta":
                st = load_state(repo)
                self._json({"root": repo.root, "name": os.path.basename(repo.root), "branch": repo.branch(),
                            "base": self.app.base, "mode": self.app.mode, "has_review": bool(st["reviews"]),
                            "stack": self.app.stack["phases"] if self.app.stack else None})
            elif u.path == "/api/diff":
                context = max(0, min(int(q.get("context", 3)), 100000))
                self._json(compute_diff(repo, q.get("mode", self.app.mode), q.get("base") or self.app.base, context,
                                        q.get("branch"), self.app.stack))
            elif u.path == "/api/comments":
                self._json(self._comments_payload())
            elif u.path == "/api/version":
                self._json({"v": state_version(repo)})
            else:
                self._json({"error": "not found"}, 404)
        except (UserError, ValueError) as e:
            self._json({"error": str(e)}, 400)

    def do_POST(self):
        # The custom header forces a CORS preflight, so other websites can't post to this server.
        if not self._host_ok() or self.headers.get("X-Local-Review") != "1":
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            repo = self.app.repo
            m = re.match(r"^/api/comments/([\w-]+)/(edit|delete|reply|status)$", u.path)
            if u.path == "/api/comments":
                self._json(add_comment(repo, data))
            elif u.path == "/api/submit":
                self._json(submit_review(repo, data.get("summary")))
            elif m:
                self._json(update_comment(repo, m.group(1), m.group(2), data))
            else:
                self._json({"error": "not found"}, 404)
        except (UserError, ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)


# ----------------------------------------------------------------------------------------- cleanup


def snapshot_refs(repo):
    """(refname, review_id, commit_time) for every snapshot ref; refs are shared by all worktrees."""
    out = repo.g("for-each-ref", "--format=%(refname) %(committerdate:unix)", "refs/local-review/")
    refs = []
    for line in out.split("\n"):
        if line:
            ref, ts = line.rsplit(" ", 1)
            refs.append((ref, ref.rsplit("/", 1)[-1], int(ts or 0)))
    return refs


def referenced_review_ids(repo):
    """Review ids still recorded in any worktree's state (main checkout and linked worktrees)."""
    common = repo.g("rev-parse", "--git-common-dir").strip()
    common = common if os.path.isabs(common) else os.path.join(repo.root, common)
    paths = [os.path.join(common, "local-review", "state.json")]
    paths += glob.glob(os.path.join(common, "worktrees", "*", "local-review", "state.json"))
    ids = set()
    for path in paths:
        try:
            with open(path) as f:
                ids |= {r["id"] for r in json.load(f).get("reviews", [])}
        except (OSError, ValueError):
            pass
    return ids


def prune_orphans(repo, dry_run=False, min_age=ORPHAN_MIN_AGE_S):
    """Deletes snapshot refs no worktree refers to anymore (e.g. the worktree was removed)."""
    keep = referenced_review_ids(repo)
    cutoff = time.time() - min_age
    removed = [ref for ref, rid, ts in snapshot_refs(repo) if rid not in keep and ts <= cutoff]
    if not dry_run:
        for ref in removed:
            repo.g("update-ref", "-d", ref, check=False)
    return removed


def cmd_clean(repo, args):
    archive_path = os.path.join(repo.state_dir, "archive.json")
    with state_txn(repo, write=not args.dry_run) as st:
        if args.all:
            pending = [c for c in st["comments"] if c["status"] in ("draft", "open")]
            if pending and not args.force:
                raise UserError(f"{len(pending)} draft/open comment(s) would be discarded; pass --force to do it anyway")
            drop_ids = {c["id"] for c in st["comments"]}
            drop_reviews = list(st["reviews"])
        else:
            drop_ids = {c["id"] for c in st["comments"] if c["status"] == "resolved"}
            # The latest snapshot stays so "Since last review" still works next session.
            drop_reviews = st["reviews"][:-1]
        dropped = [c for c in st["comments"] if c["id"] in drop_ids]
        if not args.dry_run:
            if dropped:
                try:
                    with open(archive_path) as f:
                        archive = json.load(f)
                except (OSError, ValueError):
                    archive = []
                with open(archive_path, "w") as f:
                    json.dump(archive + [dict(c, archived_at=now()) for c in dropped], f, indent=2)
            for r in drop_reviews:
                repo.g("update-ref", "-d", f"refs/local-review/{r['id']}", check=False)
            st["comments"] = [c for c in st["comments"] if c["id"] not in drop_ids]
            dropped_review_ids = {r["id"] for r in drop_reviews}
            st["reviews"] = [r for r in st["reviews"] if r["id"] not in dropped_review_ids]
    orphans = prune_orphans(repo, dry_run=args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"{verb} {len(dropped)} thread(s) (archived to {archive_path}), {len(drop_reviews)} old snapshot(s), "
          f"{len(orphans)} orphaned snapshot(s).")


def probe(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=0.5) as r:
            return json.load(r)
    except Exception:
        return None


def cmd_serve(repo, args):
    base = args.base or repo.default_base()
    if not repo.rev(base):
        raise UserError(f"unknown base ref: {base}")
    stack = load_stack(repo, getattr(args, "stack", None))
    mode = args.mode
    if mode is None:
        # A stack that includes the checked-out branch is almost certainly what's under review.
        on_stack = bool(stack) and any(p["branch"] == repo.branch() for p in stack["phases"])
        mode = "stack" if on_stack else "branch"
    if mode == "stack" and not stack:
        raise UserError("stack view needs a stack.conf at the repo root (or --stack FILE)")
    port = args.port or 7400 + int(hashlib.sha1(repo.root.encode()).hexdigest(), 16) % 500
    try:
        prune_orphans(repo)
    except UserError:
        pass  # housekeeping only; never block opening the UI
    running = probe(port)
    if running and running.get("root") == repo.root:
        url = f"http://127.0.0.1:{port}/"
        print(f"local-review already running for {repo.root}: {url}")
        if not args.no_open:
            webbrowser.open(url)
        return
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.app = App(repo, base, mode, stack)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    where = f"stack: {len(stack['phases'])} branches on {stack['trunk']}" if mode == "stack" else f"base: {base}"
    print(f"local-review: {repo.root}\n  {where}\n  url:  {url}\n(Ctrl-C to stop)", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------------------------- cli


def excerpt(repo, c):
    """Code around a comment, with line numbers and a marker on the commented line."""
    a = c["anchor"]
    if c["side"] == "new" and c["current_line"]:
        lines = repo.branch_file_lines(c.get("branch"), c["file"]) or []
        at = c["current_line"]
        start = max(1, at - CONTEXT_LINES)
        numbered = [(i, lines[i - 1]) for i in range(start, min(len(lines), at + CONTEXT_LINES) + 1)]
    else:
        at = a["line"]
        seq = a["before"] + [a["line_text"]] + a["after"]
        first = at - len(a["before"])
        numbered = [(first + k, s) for k, s in enumerate(seq)]
    width = len(str(numbered[-1][0])) if numbered else 1
    return "\n".join(f"{'>' if i == at else ' '} {i:>{width}} | {s}" for i, s in numbered)


def location(c):
    prefix = f"[{c['branch']}] " if c.get("branch") else ""
    return prefix + _location(c)


def _location(c):
    if not c.get("file"):
        return "(general review comment)"
    if c["side"] == "old":
        return f"{c['file']}:{c['anchor']['line']} (deleted/old line, at {c['old_rev'][:8]})"
    if c["outdated"]:
        return f"{c['file']} (OUTDATED: originally line {c['anchor']['line']}; that line has since changed)"
    return f"{c['file']}:{c['current_line']}"


def cmd_list(repo, args):
    with state_txn(repo) as st:
        comments = annotate(repo, st["comments"])
    statuses = {"open", "replied", "addressed", "resolved"} if args.all else {"open"}
    sel = [c for c in comments if c["status"] in statuses]
    if getattr(args, "branch", None):
        sel = [c for c in sel if c.get("branch") == args.branch]
    sel.sort(key=lambda c: (c.get("branch") or "", c.get("file") is not None, c.get("file") or "", c.get("current_line") or 0))
    if args.json:
        print(json.dumps(sel, indent=2))
        return
    if not sel:
        print("No open review comments.")
        return
    print(f"# {len(sel)} review comment(s) in {repo.root}\n")
    print(f"Reply with: local-review reply <id> [--addressed] -m \"...\"\n")
    if any(c.get("branch") for c in sel):
        print("Comments marked [branch] belong to that stack branch: check it out to change it, then rebase\n"
              "the branches stacked above it.\n")
    for c in sel:
        print(f"## {c['id']} · {c['status']} · {location(c)}\n")
        if c.get("file"):
            print("```")
            print(excerpt(repo, c))
            print("```\n")
        print(f"**{c['author']}:** {c['body']}\n")
        for r in c["replies"]:
            print(f"**{r['author']}:** {r['body']}\n")


def cmd_reply(repo, args):
    msg = args.message if args.message is not None else sys.stdin.read()
    msg = msg.strip()
    if not msg:
        raise UserError("reply message is empty")
    with state_txn(repo, write=True) as st:
        c = find_comment(st, args.id)
        if c["status"] == "draft":
            raise UserError(f"{c['id']} is an unsubmitted draft")
        c["replies"].append({"author": args.author, "body": msg, "at": now()})
        if c["status"] != "resolved":
            c["status"] = "addressed" if args.addressed else "replied"
    print(f"{c['id']}: {c['status']}")


def cmd_status(repo, args):
    st = load_state(repo)
    counts = {}
    for c in st["comments"]:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    if args.hook:
        if counts.get("open"):
            print(f"[local-review] {counts['open']} open review comment(s) in this worktree. "
                  f"Run `local-review list` to see them.")
        return
    print(" ".join(f"{k}={counts.get(k, 0)}" for k in ("draft", "open", "replied", "addressed", "resolved")))


def cmd_watch(repo, args):
    """Prints one line per newly submitted review; with --once, exits after the first."""
    seen = {r["id"] for r in load_state(repo)["reviews"]}
    last = state_version(repo)
    while True:
        time.sleep(args.interval)
        v = state_version(repo)
        if v == last:
            continue
        last = v
        for r in load_state(repo)["reviews"]:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            print(f"local-review: review {r['id']} submitted with {r['comment_count']} comment(s) in {repo.root}. "
                  f"Run `local-review list`.", flush=True)
            if args.once:
                return


def main():
    p = argparse.ArgumentParser(prog="local-review", description=__doc__.split("\n")[0])
    p.add_argument("-C", dest="path", default=".", help="repository path (default: cwd)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="open the review UI (default command)")
    s.add_argument("--base", help="base ref for branch mode (default: origin/HEAD)")
    s.add_argument("--mode", choices=["branch", "working", "since", "stack"],
                   help="default: stack when stack.conf lists the checked-out branch, else branch")
    s.add_argument("--stack", help="stack.conf path (default: <repo>/stack.conf)")
    s.add_argument("--port", type=int)
    s.add_argument("--no-open", action="store_true", help="don't open a browser")

    s = sub.add_parser("list", help="print open review comments (for the agent)")
    s.add_argument("--all", action="store_true", help="include replied/addressed/resolved")
    s.add_argument("--json", action="store_true")
    s.add_argument("--branch", help="only comments left on this stack branch")

    s = sub.add_parser("reply", help="reply to a comment as the agent")
    s.add_argument("id")
    s.add_argument("-m", "--message", help="reply text (default: read stdin)")
    s.add_argument("--addressed", action="store_true", help="mark as addressed (code changed)")
    s.add_argument("--author", default=AGENT)

    s = sub.add_parser("status", help="print comment counts")
    s.add_argument("--hook", action="store_true", help="print a reminder only if comments are open")

    s = sub.add_parser("clean", help="archive resolved threads and drop old snapshots (run when the work is done)")
    s.add_argument("--all", action="store_true", help="discard this worktree's whole review, incl. latest snapshot")
    s.add_argument("--force", action="store_true", help="with --all: discard even draft/open comments")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("watch", help="print a line whenever a review is submitted (for agents)")
    s.add_argument("--once", action="store_true", help="exit after the first submitted review")
    s.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")

    args = p.parse_args()
    if args.cmd is None:
        args = p.parse_args(sys.argv[1:] + ["serve"])
    try:
        repo = Repo(args.path)
        {"serve": cmd_serve, "list": cmd_list, "reply": cmd_reply, "status": cmd_status,
         "watch": cmd_watch, "clean": cmd_clean}[args.cmd](repo, args)
    except UserError as e:
        if getattr(args, "hook", False):
            return
        print(f"local-review: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
