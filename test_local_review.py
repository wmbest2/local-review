"""End-to-end tests for local-review. Run: python3 test_local_review.py"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import local_review as lr  # noqa: E402

FOO_V1 = "fun a() {\n    return 1\n}\n\nfun b() {\n    return 2\n}\n"
FOO_V2 = "fun a() {\n    return 1\n}\n\nfun b() {\n    return 42 // magic\n}\n"


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


class LocalReviewTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lr-test-")
        d = self.dir
        sh(d, "git", "init", "-q", "-b", "main")
        sh(d, "git", "config", "user.email", "t@t")
        sh(d, "git", "config", "user.name", "t")
        self.write("Foo.kt", FOO_V1)
        self.write("Gone.txt", "old stuff\n")
        sh(d, "git", "add", "-A")
        sh(d, "git", "commit", "-qm", "base")
        sh(d, "git", "checkout", "-qb", "feature")
        self.write("Foo.kt", FOO_V2)
        sh(d, "git", "rm", "-q", "Gone.txt")
        sh(d, "git", "commit", "-qam", "feat")
        self.write("New File.md", "untracked new\n")
        self.repo = lr.Repo(d)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def cli(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(self.repo, SimpleNamespace(**kw))
        return out.getvalue()

    def files(self, mode):
        return {f["path"]: f for f in lr.compute_diff(self.repo, mode, "main", 3)["files"]}

    def test_modes_and_untracked(self):
        branch = self.files("branch")
        self.assertEqual(branch["Foo.kt"]["status"], "modified")
        self.assertEqual(branch["Gone.txt"]["status"], "deleted")
        self.assertEqual(branch["New File.md"]["status"], "added")  # untracked + path with a space
        self.assertEqual(set(self.files("working")), {"New File.md"})
        # The real index must not pick up the untracked file.
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.dir, capture_output=True, text=True)
        self.assertEqual(staged.stdout, "")

    def test_review_round_trip(self):
        old_rev = lr.compute_diff(self.repo, "branch", "main", 3)["old_rev"]
        c1 = lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 6, "body": "Name this constant"})
        c2 = lr.add_comment(self.repo, {"file": "Gone.txt", "side": "old", "line": 1, "old_rev": old_rev,
                                        "body": "Why delete this?"})
        self.assertEqual(c1["anchor"]["line_text"], "    return 42 // magic")
        self.assertIn("No open", self.cli(lr.cmd_list, all=False, json=False))  # drafts aren't visible to the agent

        lr.submit_review(self.repo, "Looks close.")
        self.assertIn("open=3", self.cli(lr.cmd_status, hook=False))
        self.assertIn("3 open", self.cli(lr.cmd_status, hook=True))

        # Code shifts down two lines: the comment follows its line.
        self.write("Foo.kt", "// header\n\n" + FOO_V2)
        out = self.cli(lr.cmd_list, all=False, json=False)
        self.assertIn(f"{c1['id']} · open · Foo.kt:8", out)
        self.assertIn("> 8 |     return 42 // magic", out)
        self.assertIn("Gone.txt:1 (deleted/old line", out)
        self.assertIn("(general review comment)", out)

        self.cli(lr.cmd_reply, id=c1["id"][:4], message="Extracted to ANSWER.", addressed=True, author="agent")
        self.cli(lr.cmd_reply, id=c2["id"], message="It was unused.", addressed=False, author="agent")
        self.assertNotIn(c1["id"], self.cli(lr.cmd_list, all=False, json=False))

        # The commented line itself changes: comment becomes outdated but keeps its original context.
        self.write("Foo.kt", "// header\n\n" + FOO_V2.replace("42 // magic", "ANSWER"))
        comments = {c["id"]: c for c in lr.annotate(self.repo, lr.load_state(self.repo)["comments"])}
        self.assertTrue(comments[c1["id"]]["outdated"])
        self.assertEqual(comments[c1["id"]]["status"], "addressed")
        self.assertEqual(comments[c2["id"]]["status"], "replied")

        # A reviewer reply puts it back in the agent's queue.
        self.assertEqual(lr.update_comment(self.repo, c1["id"], "reply", {"body": "add a kdoc"})["status"], "open")
        self.assertIn("OUTDATED", self.cli(lr.cmd_list, all=False, json=False))

        # "Since last review" shows only what changed after submitting.
        self.assertEqual(set(self.files("since")), {"Foo.kt"})

    def test_multi_line_comment_follows_its_range(self):
        c = lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "start_line": 5, "line": 7, "body": "Inline b()"})
        self.assertEqual(c["anchor"]["span"], 3)
        self.assertEqual(c["anchor"]["range_text"], ["fun b() {", "    return 42 // magic", "}"])
        with self.assertRaises(lr.UserError):
            lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "start_line": 7, "line": 5, "body": "backwards"})
        lr.submit_review(self.repo, "")

        self.write("Foo.kt", "// header\n\n" + FOO_V2)
        out = self.cli(lr.cmd_list, all=False, json=False)
        self.assertIn(f"{c['id']} · open · Foo.kt:7-9", out)
        for n, text in [(7, "fun b() {"), (8, "    return 42 // magic"), (9, "}")]:
            self.assertIn(f"> {n} | {text}", out)
        self.assertIn("  6 | ", out)  # context before the range isn't marked

        self.write("Foo.kt", FOO_V2.replace("}\n\nfun b", "}\nfun b").replace("fun b() {\n    return 42 // magic\n}\n", "fun b() = 0\n"))
        self.assertIn("originally lines 5-7", self.cli(lr.cmd_list, all=False, json=False))

    def test_classifies_tests_by_path_with_overrides(self):
        is_test = lr.test_classifier(self.repo)
        for path in ["app/src/test/java/com/x/FooTest.kt", "app/src/androidTest/java/Foo.kt",
                     "app/src/sharedTest/java/Fakes.kt", "app/src/testProdDebug/Foo.kt", "lib/FooTests.swift",
                     "core/src/commonTest/kotlin/A.kt", "core/src/androidUnitTest/kotlin/B.kt", "core/src/iosTest/kotlin/C.kt",
                     "pkg/foo_test.go", "test_local_review.py", "web/src/a.spec.ts", "__tests__/x.js", "conftest.py"]:
            self.assertTrue(is_test(path), path)
        for path in ["app/src/main/java/com/x/Foo.kt", "app/src/main/java/com/x/Latest.kt", "README.md",
                     "app/src/main/java/com/x/testing.kt", "contest/Entry.kt", None]:
            self.assertFalse(is_test(path), path)

        sh(self.dir, "git", "config", "--add", "local-review.tests", "*PreviewData.kt")
        sh(self.dir, "git", "config", "--add", "local-review.notTests", "app/src/test/java/com/x/Real.kt")
        is_test = lr.test_classifier(self.repo)
        self.assertTrue(is_test("app/src/main/java/com/x/ActivityCenterPreviewData.kt"))
        self.assertFalse(is_test("app/src/test/java/com/x/Real.kt"))

        self.write("FooTest.kt", "class FooTest\n")
        files = self.files("branch")
        self.assertTrue(files["FooTest.kt"]["test"])
        self.assertFalse(files["Foo.kt"]["test"])

    def test_classifies_project_files_and_resources(self):
        kind = lr.kind_classifier(self.repo)
        expected = {
            "project": ["build.gradle.kts", "core/build.gradle.kts", "settings.gradle.kts", "gradle/libs.versions.toml",
                        "gradle.properties", "build-logic/convention/src/main/kotlin/kmp-lib.gradle.kts",
                        "app/src/main/AndroidManifest.xml", "app/proguard-rules.pro", "core/src/nativeInterop/cinterop/x.def",
                        "Package.swift", "App.xcodeproj/project.pbxproj", ".github/workflows/ci.yml", ".gitignore",
                        "pyproject.toml", "package.json"],
            "resources": ["app/src/main/res/values/strings.xml", "app/src/main/res/drawable/logo.png",
                          "core/src/main/resources/rules.tsv", "App/Assets.xcassets/a.imageset/Contents.json",
                          "App/en.lproj/Localizable.strings", "App/Main.storyboard", "web/public/logo.svg"],
            "tests": ["core/src/test/resources/vectors.tsv", "app/src/test/AndroidManifest.xml"],
            "code": ["app/src/main/java/com/x/resources/Strings.kt", "app/src/main/java/com/x/ui/layout/Row.kt",
                     "README.md", "core/src/commonMain/kotlin/Platform.kt", None],
        }
        for k, paths in expected.items():
            for path in paths:
                self.assertEqual(kind(path), k, path)

        sh(self.dir, "git", "config", "--add", "local-review.project", "scripts/*")
        sh(self.dir, "git", "config", "--add", "local-review.resources", "*.json")
        sh(self.dir, "git", "config", "--add", "local-review.code", "app/src/main/res/raw/rules.js")
        kind = lr.kind_classifier(self.repo)
        self.assertEqual(kind("scripts/release.sh"), "project")
        self.assertEqual(kind("config/flags.json"), "resources")
        self.assertEqual(kind("app/src/main/res/raw/rules.js"), "code")

        self.write("build.gradle.kts", "plugins {}\n")
        files = self.files("branch")
        self.assertEqual(files["build.gradle.kts"]["kind"], "project")
        self.assertEqual(files["Foo.kt"]["kind"], "code")

    def test_watch_once(self):
        lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 6, "body": "x"})
        out = io.StringIO()
        watcher = threading.Thread(target=lambda: self._watch(out))
        watcher.start()
        time.sleep(0.3)
        review = lr.submit_review(self.repo, "")
        watcher.join(timeout=5)
        self.assertFalse(watcher.is_alive())
        self.assertIn(f"review {review['id']} submitted with 1 comment(s)", out.getvalue())

    def test_watch_once_wakes_on_a_reviewer_reply(self):
        lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 6, "body": "why 42?"})
        [c] = [c for c in lr.load_state(self.repo)["comments"]]
        lr.submit_review(self.repo, "")
        lr.update_comment(self.repo, c["id"], "reply", {"body": "first"})  # before watching: not an event
        out = io.StringIO()
        watcher = threading.Thread(target=lambda: self._watch(out))
        watcher.start()
        time.sleep(0.3)
        lr.update_comment(self.repo, c["id"], "reply", {"body": "go ahead"})
        watcher.join(timeout=5)
        self.assertFalse(watcher.is_alive())
        self.assertIn(f"reviewer replied on {c['id']}", out.getvalue())
        self.assertNotIn("submitted", out.getvalue())

    def test_ignore_whitespace_and_single_file(self):
        # Re-indent Foo.kt in the working tree: a whitespace-only change on top of the branch.
        self.write("Foo.kt", FOO_V2.replace("    return 1", "        return 1"))
        full = {f["path"]: f for f in lr.compute_diff(self.repo, "working", "main", 3)["files"]}
        self.assertIn("Foo.kt", full)
        hidden = {f["path"]: f for f in lr.compute_diff(self.repo, "working", "main", 3, ignore_ws=True)["files"]}
        self.assertNotIn("Foo.kt", hidden, "a whitespace-only change disappears with -w")
        one = lr.compute_diff(self.repo, "branch", "main", 3, paths=["Foo.kt"])["files"]
        self.assertEqual([f["path"] for f in one], ["Foo.kt"])

        # Re-fetching one file against the view's own trees, without a new snapshot.
        view = lr.compute_diff(self.repo, "working", "main", 3)
        again = lr.compute_diff(self.repo, "working", "main", 3, paths=["Foo.kt"],
                                trees=(view["old_rev"], view["new_tree"]), ignore_ws=True)
        self.assertEqual(again["files"], [], "same trees, -w: the re-indent alone is hidden")

    def _watch(self, out):
        with redirect_stdout(out):
            lr.cmd_watch(self.repo, SimpleNamespace(once=True, interval=0.1))

    def test_clean(self):
        c1 = lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 6, "body": "one"})
        r1 = lr.submit_review(self.repo, "")
        lr.update_comment(self.repo, c1["id"], "status", {"status": "resolved"})
        c2 = lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 6, "body": "two"})
        r2 = lr.submit_review(self.repo, "")
        refs = lambda: {rid for _, rid, _ in lr.snapshot_refs(self.repo)}
        self.assertEqual(refs(), {r1["id"], r2["id"]})

        self.assertIn("Would remove 1 thread", self.cli(lr.cmd_clean, all=False, force=False, dry_run=True))
        self.assertEqual(refs(), {r1["id"], r2["id"]})

        self.cli(lr.cmd_clean, all=False, force=False, dry_run=False)
        st = lr.load_state(self.repo)
        self.assertEqual([c["id"] for c in st["comments"]], [c2["id"]])  # resolved thread archived
        self.assertEqual(refs(), {r2["id"]})  # latest snapshot kept for "since last review"
        self.assertEqual(set(self.files("since")), set())
        with open(os.path.join(self.repo.state_dir, "archive.json")) as f:
            self.assertEqual([c["id"] for c in json.load(f)], [c1["id"]])

        with self.assertRaises(lr.UserError):  # c2 is still open
            self.cli(lr.cmd_clean, all=True, force=False, dry_run=False)
        self.cli(lr.cmd_clean, all=True, force=True, dry_run=False)
        self.assertEqual(lr.load_state(self.repo)["comments"], [])
        self.assertEqual(refs(), set())

    def test_prune_orphans(self):
        lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 6, "body": "x"})
        review = lr.submit_review(self.repo, "")
        head = self.repo.head()
        self.repo.g("update-ref", "refs/local-review/rdeadbe", head)  # snapshot from a deleted worktree
        self.assertEqual(lr.prune_orphans(self.repo), [])  # too young to prune
        self.assertEqual(lr.prune_orphans(self.repo, min_age=0), ["refs/local-review/rdeadbe"])
        self.assertEqual({rid for _, rid, _ in lr.snapshot_refs(self.repo)}, {review["id"]})

    def test_generic_line_needs_context(self):
        self.assertIsNone(lr.locate(["x", "}", "y"], {"line": 2, "line_text": "}", "before": ["a"], "after": ["b"]}))
        self.assertEqual(lr.locate(["a", "}", "b"], {"line": 9, "line_text": "}", "before": ["a"], "after": ["b"]}), 2)

    def test_http_guards(self):
        httpd = lr.ThreadingHTTPServer(("127.0.0.1", 0), lr.Handler)
        httpd.app = lr.App(self.repo, "main", "branch")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(url + "/api/meta") as r:
                self.assertEqual(json.load(r)["root"], self.repo.root)
            with urllib.request.urlopen(url + "/") as r:
                self.assertIn(b"local-review", r.read())
            req = urllib.request.Request(url + "/api/submit", data=b"{}", method="POST")  # no custom header
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            e.exception.close()
            req = urllib.request.Request(url + "/api/meta", headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            e.exception.close()
            req = urllib.request.Request(url + "/api/comments", method="POST", headers={"X-Local-Review": "1"},
                                         data=json.dumps({"file": "Foo.kt", "side": "new", "line": 6, "body": "hi"}).encode())
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.load(r)["status"], "draft")
        finally:
            httpd.shutdown()
            httpd.server_close()


class StackTest(unittest.TestCase):
    """A two-branch stack on main: s1 adds a line to Foo.kt, s2 (checked out) changes that line."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lr-stack-")
        d = self.dir
        sh(d, "git", "init", "-q", "-b", "main")
        sh(d, "git", "config", "user.email", "t@t")
        sh(d, "git", "config", "user.name", "t")
        self.write("Foo.kt", FOO_V1)
        sh(d, "git", "add", "-A")
        sh(d, "git", "commit", "-qm", "base")
        sh(d, "git", "checkout", "-qb", "s1")
        self.write("Foo.kt", FOO_V1 + "\nfun c() = 3\n")
        sh(d, "git", "commit", "-qam", "s1")
        sh(d, "git", "checkout", "-qb", "s2")
        self.write("Foo.kt", FOO_V1 + "\nfun c() = 30 // s2\n")
        sh(d, "git", "commit", "-qam", "s2")
        self.write("stack.conf", "trunk = main   # inline comments are ignored\n"
                                 "A | s1 | First change\nB | s2 | Second change\n")
        self.repo = lr.Repo(d)
        self.stack = lr.load_stack(self.repo)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def diff(self, branch):
        return {f["path"]: f for f in lr.compute_diff(self.repo, "stack", "main", 3, branch, self.stack)["files"]}

    def test_parents_chain_from_trunk(self):
        self.assertEqual(self.stack["trunk"], "main")
        self.assertEqual([(p["branch"], p["parent"], p["tier"]) for p in self.stack["phases"]],
                         [("s1", "main", "A"), ("s2", "s1", "B")])

    def test_each_branch_diffs_against_the_one_below(self):
        s1 = self.diff("s1")
        self.assertEqual(list(s1), ["Foo.kt"], "s1 is not checked out, so stack.conf is not in its view")
        added = [ln["s"] for h in s1["Foo.kt"]["hunks"] for ln in h["lines"] if ln["t"] == "add"]
        self.assertIn("fun c() = 3", added)

        # s2 is checked out: its view is the working tree, so the uncommitted stack.conf shows here.
        s2 = self.diff("s2")
        changed = [ln["s"] for h in s2["Foo.kt"]["hunks"] for ln in h["lines"] if ln["t"] in ("add", "del")]
        self.assertEqual(changed, ["fun c() = 3", "fun c() = 30 // s2"])
        self.assertIn("stack.conf", s2)

    def test_comment_on_another_branch_anchors_to_that_branch(self):
        c = lr.add_comment(self.repo, {"file": "Foo.kt", "side": "new", "line": 9, "branch": "s1", "body": "why 3?"})
        self.assertEqual(c["anchor"]["line_text"], "fun c() = 3")
        # The working tree (s2) rewrote that line, but the comment belongs to s1 and still lines up.
        with lr.state_txn(self.repo) as st:
            [annotated] = lr.annotate(self.repo, st["comments"])
        self.assertEqual(annotated["current_line"], 9)
        self.assertFalse(annotated["outdated"])
        self.assertEqual(lr.location(annotated), "[s1] Foo.kt:9")

    def test_unknown_branch_is_a_user_error(self):
        with self.assertRaises(lr.UserError):
            lr.compute_diff(self.repo, "stack", "main", 3, "nope", self.stack)


if __name__ == "__main__":
    unittest.main()
