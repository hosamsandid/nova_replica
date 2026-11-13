#!/usr/bin/env python3
"""Tests for `aee-wait-ci` (#2796) — the bounded in-turn wait for a PR's CI and
merge status.

The verdict rules are checked against the SAME case table the orchestrator's
pr-readiness tests run (orchestrator/pr-readiness-cases.json), and the rules
file against its canonical copy, so the CLI and the platform cannot drift. The
wait loop runs against a scripted fake GitHub and a fake clock; the exit-code
contract runs the real script against a fake `gh` (`AEE_WAIT_CI_GH`).

    python -m pytest agent-image/scripts/test_aee_wait_ci.py -q
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "aee-wait-ci"
REPO_ROOT = SCRIPTS.parent.parent
CANONICAL_RULES = REPO_ROOT / "orchestrator" / "pr-readiness-rules.json"
SHARED_CASES = REPO_ROOT / "orchestrator" / "pr-readiness-cases.json"


def _load():
    loader = importlib.machinery.SourceFileLoader("aee_wait_ci", str(SCRIPT))
    spec = importlib.util.spec_from_loader("aee_wait_ci", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


W = _load()

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
REPO = ("acme", "widgets", "github.com")


def run_(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


class SharedRules(unittest.TestCase):
    """AC2: one rule definition, shared with orchestrator/pr-readiness.js."""

    def test_rules_file_is_byte_identical_to_the_orchestrator_copy(self):
        if not CANONICAL_RULES.exists():
            self.skipTest("orchestrator/ not in this checkout")
        self.assertEqual((SCRIPTS / "pr-readiness-rules.json").read_bytes(), CANONICAL_RULES.read_bytes(),
                         "cp orchestrator/pr-readiness-rules.json agent-image/scripts/")

    def test_shared_verdict_cases(self):
        if not SHARED_CASES.exists():
            self.skipTest("orchestrator/ not in this checkout")
        cases = json.loads(SHARED_CASES.read_text(encoding="utf-8"))
        now = 10_000_000
        for c in cases["ci"]:
            with self.subTest(c["name"]):
                got = W.ci_verdict(c["checks"], c.get("truncated", False), now - c["elapsedMs"], now,
                                   c.get("graceMs", W.DEFAULT_CI_GRACE_MS))
                self.assertEqual(got, c["expect"])
        for c in cases["merge"]:
            with self.subTest(mergeable=c["mergeable"]):
                self.assertEqual(W.merge_verdict(c["mergeable"]), c["expect"])

    def test_grace_override_matches_the_orchestrator_env(self):
        self.assertEqual(W.ci_grace_ms({}), 180000)
        self.assertEqual(W.ci_grace_ms({"AEE_PR_READINESS_CI_GRACE_MS": "5000"}), 5000)
        self.assertEqual(W.ci_grace_ms({"AEE_PR_READINESS_CI_GRACE_MS": "junk"}), 180000)


class FakeGitHub:
    """Scripted GitHub: `steps` is a list of (pr, checks) states or GhError
    instances; each PR read advances one step (the last one repeats)."""

    def __init__(self, steps):
        self.steps = steps
        self.i = -1
        self.calls = []

    def _cur(self):
        return self.steps[min(self.i, len(self.steps) - 1)]

    def __call__(self, query, variables, host=None):
        self.calls.append(("pr" if "pullRequest(" in query else "checks", variables))
        if "pullRequest(" in query:
            self.i += 1
            step = self._cur()
            if isinstance(step, W.GhError):
                raise step
            return {"repository": {"pullRequest": step[0]}}
        step = self._cur()
        pr, checks = step
        assert variables["oid"] == pr["headRefOid"], "checks must be read by the head SHA"
        nodes, truncated = (checks if isinstance(checks, tuple) else (checks, False))
        return {"repository": {"object": {"oid": variables["oid"], "statusCheckRollup": {
            "contexts": {"pageInfo": {"hasNextPage": truncated}, "nodes": nodes}}}}}


def pr_(sha=SHA_A, mergeable="MERGEABLE", state="OPEN", merged=False, branch="fix/x"):
    return {"number": 7, "url": "u", "state": state, "merged": merged, "headRefName": branch,
            "headRefOid": sha, "mergeable": mergeable}


class FakeLocal:
    """A LocalClone on `branch` at `head`, whose history holds `ancestors`."""

    def __init__(self, head, branch="fix/x", ancestors=()):
        self.head, self.branch, self.ancestors = head, branch, set(ancestors)

    def expected_head(self, pr_branch):
        return self.head if pr_branch == self.branch else None

    def is_ancestor(self, sha, of):
        return of == self.head and sha in self.ancestors


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def wait(steps, timeout=600, **kw):
    gh = FakeGitHub(steps)
    clock = Clock()
    res = W.wait_for_ci(REPO, 7, timeout, gh=gh, clock=clock, sleep=clock.sleep,
                        interval_sec=kw.pop("interval_sec", 20), **kw)
    return res, gh, clock


class WaitLoop(unittest.TestCase):
    def test_passed_once_every_check_is_green(self):
        res, _gh, clock = wait([(pr_(), [run_("build", "IN_PROGRESS", None)]),
                                (pr_(), [run_("build")])])
        self.assertEqual(res["status"], "passed")
        self.assertTrue(res["ok"])
        self.assertEqual((res["headSha"], res["ci"], res["merge"]), (SHA_A, "passed", "clean"))
        self.assertEqual(clock.sleeps, [20])

    def test_failing_check_settles_early_naming_it(self):
        res, _gh, _c = wait([(pr_(), [run_("CodeQL", conclusion="FAILURE"), run_("e2e", "QUEUED", None)])])
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["failingChecks"], ["CodeQL"])
        self.assertEqual(res["pendingChecks"], ["e2e"])
        self.assertFalse(res["ok"])

    def test_no_checks_after_the_grace_means_no_ci(self):
        res, _gh, clock = wait([(pr_(), [])], grace_ms=60_000)
        self.assertEqual((res["status"], res["ci"]), ("no-ci", "none"))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60)

    def test_conflict_is_reported_at_once(self):
        res, gh, _c = wait([(pr_(mergeable="CONFLICTING"), [run_("build", "IN_PROGRESS", None)])])
        self.assertEqual((res["status"], res["merge"]), ("conflict", "conflict"))
        self.assertEqual([c[0] for c in gh.calls], ["pr"])

    def test_unknown_mergeable_is_re_read(self):
        res, _gh, clock = wait([(pr_(mergeable="UNKNOWN"), [run_("build")]),
                                (pr_(mergeable="UNKNOWN"), [run_("build")]),
                                (pr_(mergeable="MERGEABLE"), [run_("build")])])
        self.assertEqual(res["status"], "passed")
        self.assertEqual(len(clock.sleeps), 2)

    def test_no_conflict_check_ignores_merge_state(self):
        res, _gh, _c = wait([(pr_(mergeable="CONFLICTING"), [run_("build")])], conflict_check=False)
        self.assertEqual((res["status"], res["merge"]), ("passed", "skipped"))

    def test_a_push_during_the_wait_evaluates_the_new_head(self):
        # AC5: red on the old head is never reported once the head moved.
        res, _gh, _c = wait([(pr_(SHA_A), [run_("build", "IN_PROGRESS", None)]),
                             (pr_(SHA_B), [run_("build")])])
        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["headSha"], SHA_B)
        self.assertEqual(res["supersededHeads"], [SHA_A])

    def test_new_head_restarts_the_no_ci_grace(self):
        res, _gh, clock = wait([(pr_(SHA_A), [])] * 3 + [(pr_(SHA_B), [])], grace_ms=70_000)
        self.assertEqual(res["status"], "no-ci")
        self.assertEqual(res["headSha"], SHA_B)  # grace measured from B, not A
        self.assertGreaterEqual(clock.t - 1_000_000.0, 60 + 70)

    def test_timeout_says_how_to_continue(self):
        res, _gh, clock = wait([(pr_(), [run_("build", "IN_PROGRESS", None)])], timeout=90)
        self.assertEqual(res["status"], "timeout")
        self.assertIn("readinessGate", res["message"])
        self.assertIn("pull_request.checks_passed", res["message"])
        self.assertIn("background", res["message"])
        self.assertEqual(res["pendingChecks"], ["build"])
        self.assertLessEqual(clock.t - 1_000_000.0, 90)

    def test_zero_budget_still_reads_once(self):
        res, gh, _c = wait([(pr_(), [run_("build")])], timeout=0)
        self.assertEqual(res["status"], "passed")
        res, gh, _c = wait([(pr_(), [run_("build", "IN_PROGRESS", None)])], timeout=0)
        self.assertEqual(res["status"], "timeout")

    def test_closed_or_merged_pr_is_an_error(self):
        res, _gh, _c = wait([(pr_(state="MERGED", merged=True), [])])
        self.assertEqual((res["status"], res["prState"]), ("error", "MERGED"))
        self.assertIn("merged", res["message"])
        res, _gh, _c = wait([(pr_(state="CLOSED"), [])])
        self.assertIn("closed", res["message"])

    def test_rate_limit_backs_off_and_does_not_fail(self):
        rl = W.GhError("rate-limit", "API rate limit exceeded")
        res, _gh, clock = wait([rl, rl, rl, rl, rl, rl, (pr_(), [run_("build")])], timeout=3600)
        self.assertEqual(res["status"], "passed")
        self.assertEqual(clock.sleeps, [60, 120, 240, 300, 300, 300])

    def test_repeated_network_failures_end_as_unreachable(self):
        res, _gh, _c = wait([W.GhError("transient", "dial tcp: i/o timeout")], timeout=3600)
        self.assertEqual(res["status"], "error")
        self.assertTrue(res["unreachable"])

    def test_a_transient_blip_is_retried(self):
        res, _gh, _c = wait([W.GhError("transient", "EOF"), (pr_(), [run_("build")])])
        self.assertEqual(res["status"], "passed")

    def test_auth_failure_is_an_immediate_error_with_a_token_hint(self):
        saved = {k: os.environ.pop(k, None) for k in ("GH_TOKEN", "GITHUB_TOKEN")}
        try:
            res, gh, _c = wait([W.GhError("auth", "HTTP 401: Bad credentials")])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(res["status"], "error")
        self.assertIn("no GitHub token", res["message"])
        self.assertEqual(len(gh.calls), 1)

    def test_truncated_check_list_never_passes(self):
        res, _gh, _c = wait([(pr_(), ([run_("build")], True))], timeout=60)
        self.assertEqual(res["status"], "timeout")


class WaitAnnouncement(unittest.TestCase):
    """#2841: the runtime's chat status note fires once per wait, and only when
    there is really something to wait for."""

    def test_announced_once_before_the_first_pause(self):
        calls = []
        res, _gh, clock = wait([(pr_(), [run_("build", "IN_PROGRESS", None)])] * 3 + [(pr_(), [run_("build")])],
                               on_waiting=lambda: calls.append(1))
        self.assertEqual(res["status"], "passed")
        self.assertEqual(len(calls), 1)

    def test_not_announced_when_settled_on_the_first_look(self):
        for steps in ([(pr_(), [run_("build")])],
                      [(pr_(mergeable="CONFLICTING"), [])],
                      [(pr_(state="CLOSED"), [])],
                      [(pr_(), [run_("lint", conclusion="FAILURE")])]):
            calls = []
            wait(steps, on_waiting=lambda: calls.append(1))
            self.assertEqual(calls, [], steps)

    def test_not_announced_while_github_is_unreachable(self):
        calls = []
        wait([W.GhError("transient", "EOF")], timeout=3600, on_waiting=lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_a_failing_callback_never_breaks_the_wait(self):
        def boom():
            raise RuntimeError("room gone")
        res, _gh, _c = wait([(pr_(), [run_("build", "IN_PROGRESS", None)]), (pr_(), [run_("build")])],
                            on_waiting=boom)
        self.assertEqual(res["status"], "passed")

    def test_announce_wait_appends_one_json_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "status.jsonl")
            self.assertTrue(W.announce_wait(("acme", "widgets", "github.com"), 7, 1200, env={"AEE_STATUS_FILE": path}))
            self.assertTrue(W.announce_wait(("acme", "widgets", "github.com"), 7, 25, env={"AEE_STATUS_FILE": path}))
            lines = [json.loads(l) for l in Path(path).read_text().splitlines()]
        self.assertEqual(lines[0], {"kind": "ci-wait", "repo": "acme/widgets", "host": "github.com", "pr": 7,
                                    "timeoutMinutes": 20})
        self.assertEqual(lines[1]["timeoutMinutes"], 1)

    def test_announce_wait_without_a_runtime_does_nothing(self):
        self.assertFalse(W.announce_wait(("a", "b", "github.com"), 1, 60, env={}))
        self.assertFalse(W.announce_wait(("a", "b", "github.com"), 1, 60,
                                         env={"AEE_STATUS_FILE": "/nonexistent-dir/x/status"}))


class ExpectedHead(unittest.TestCase):
    """#2818: right after a push GitHub may still show the old head; its verdict
    must not be reported as the pushed commit's."""

    RED = [run_("pytest", conclusion="FAILURE")]

    def test_old_red_head_is_waited_out_until_the_pushed_head_shows(self):
        res, gh, clock = wait([(pr_(SHA_A), self.RED), (pr_(SHA_A), self.RED),
                               (pr_(SHA_B), [run_("pytest")])],
                              local=FakeLocal(SHA_B, ancestors=[SHA_A]))
        self.assertEqual((res["status"], res["headSha"], res["expectedHead"]), ("passed", SHA_B, SHA_B))
        self.assertEqual(res["supersededHeads"], [SHA_A])
        # The old head's checks are never even read, and it is re-read quickly.
        self.assertEqual([c[0] for c in gh.calls], ["pr", "pr", "pr", "checks"])
        self.assertEqual(clock.sleeps, [W.HEAD_SYNC_INTERVAL_SEC] * 2)

    def test_old_head_conflict_is_not_reported_either(self):
        res, _gh, _c = wait([(pr_(SHA_A, mergeable="CONFLICTING"), []), (pr_(SHA_B), [run_("pytest")])],
                            local=FakeLocal(SHA_B, ancestors=[SHA_A]))
        self.assertEqual(res["status"], "passed")

    def test_explicit_head_waits_out_any_other_head(self):
        # --head: even a commit the clone does not know is not the one asked for.
        res, _gh, _c = wait([(pr_(SHA_A), self.RED), (pr_(SHA_B), [run_("pytest")])], expected_head=SHA_B.upper())
        self.assertEqual((res["status"], res["headSha"], res["expectedHead"]), ("passed", SHA_B, SHA_B))

    def test_red_on_the_expected_head_is_still_reported(self):
        res, _gh, _c = wait([(pr_(SHA_B), self.RED)], local=FakeLocal(SHA_B, ancestors=[SHA_A]))
        self.assertEqual((res["status"], res["headSha"]), ("failed", SHA_B))

    def test_a_head_that_is_not_older_than_local_is_evaluated(self):
        # A teammate's push the clone has not fetched: not ours to wait out.
        res, _gh, _c = wait([(pr_(SHA_C), self.RED)], local=FakeLocal(SHA_B, ancestors=[SHA_A]))
        self.assertEqual((res["status"], res["headSha"], res["expectedHead"]), ("failed", SHA_C, SHA_B))

    def test_clone_on_another_branch_expects_nothing(self):
        res, _gh, _c = wait([(pr_(SHA_A), self.RED)], local=FakeLocal(SHA_B, branch="main", ancestors=[SHA_A]))
        self.assertEqual((res["status"], res["expectedHead"]), ("failed", None))

    def test_after_the_sync_the_wait_follows_later_pushes_as_before(self):
        res, _gh, _c = wait([(pr_(SHA_B), [run_("pytest", "IN_PROGRESS", None)]),
                             (pr_(SHA_C), [run_("pytest")])], expected_head=SHA_B)
        self.assertEqual((res["status"], res["headSha"], res["supersededHeads"]), ("passed", SHA_C, [SHA_B]))

    def test_head_never_shown_ends_as_a_stale_head_error(self):
        res, _gh, clock = wait([(pr_(SHA_A), self.RED)], timeout=1200, local=FakeLocal(SHA_B, ancestors=[SHA_A]))
        self.assertEqual(res["status"], "error")
        self.assertTrue(res["staleHead"])
        self.assertIn("pushed", res["message"])
        self.assertEqual((res["ci"], res["failingChecks"]), ("pending", []))
        self.assertLess(clock.t - 1_000_000.0, W.HEAD_SYNC_LIMIT_SEC + 2 * W.HEAD_SYNC_INTERVAL_SEC)

    def test_short_timeout_while_waiting_for_the_head_is_a_timeout(self):
        res, _gh, _c = wait([(pr_(SHA_A), self.RED)], timeout=30, expected_head=SHA_B)
        self.assertEqual((res["status"], res["headSha"]), ("timeout", SHA_A))
        self.assertIn(SHA_B[:7], res["message"])

    def test_no_expected_head_keeps_the_old_behaviour(self):
        res, _gh, _c = wait([(pr_(SHA_A), self.RED)])
        self.assertEqual((res["status"], res["expectedHead"]), ("failed", None))


class LocalCloneGit(unittest.TestCase):
    """LocalClone against a real git repository."""

    def test_branch_head_ancestry_and_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(["git", "init", "-q", "-b", "fix/x", tmp], check=True)
            subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "one"], check=True)
            old = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "two"], check=True)
            new = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            lc = W.LocalClone(tmp)
            self.assertEqual(lc.expected_head("fix/x"), new)
            self.assertIsNone(lc.expected_head("main"))
            self.assertIsNone(lc.expected_head(None))
            self.assertTrue(lc.is_ancestor(old, new))
            self.assertFalse(lc.is_ancestor(new, old))
            self.assertFalse(lc.is_ancestor(SHA_C, new))  # unknown commit
            self.assertEqual(lc.resolve("HEAD~1"), old)
            self.assertIsNone(lc.resolve("no-such-rev"))

    def test_clone_dir_only_when_its_origin_is_the_pr_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "my-clone"
            subprocess.run(["git", "init", "-q", str(clone)], check=True)
            subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", "git@github.com:acme/widgets.git"],
                           check=True)
            cwd = os.getcwd()
            os.chdir(clone)
            try:
                self.assertEqual(os.path.realpath(W.local_clone_dir(REPO, None)), os.path.realpath(clone))
                self.assertIsNone(W.local_clone_dir(("acme", "other", "github.com"), None))
            finally:
                os.chdir(cwd)
            self.assertEqual(os.path.realpath(W.local_clone_dir(REPO, str(clone))), os.path.realpath(clone))


class Helpers(unittest.TestCase):
    def test_timeout_default_and_caps(self):
        self.assertEqual(W.DEFAULT_TIMEOUT_MIN, 20)
        self.assertEqual(W.effective_timeout_sec(20, env={}, now=0), 1200)
        self.assertEqual(W.effective_timeout_sec(500, env={}, now=0), W.MAX_TIMEOUT_MIN * 60)
        # AC4: never past the run's own deadline (minus the reserve to act).
        env = {"AEE_RUN_DEADLINE": "1000"}
        self.assertEqual(W.effective_timeout_sec(20, env=env, now=0), 1000 - W.DEADLINE_RESERVE_SEC)
        self.assertEqual(W.effective_timeout_sec(20, env=env, now=990), 0)
        self.assertEqual(W.effective_timeout_sec(20, env={"AEE_RUN_DEADLINE": "x"}, now=0), 1200)

    def test_gh_failure_classification(self):
        c = W.classify_gh_failure
        self.assertEqual(c("API rate limit exceeded for installation"), "rate-limit")
        self.assertEqual(c("You have exceeded a secondary rate limit"), "rate-limit")
        self.assertEqual(c("HTTP 401: Bad credentials"), "auth")
        self.assertEqual(c("Could not resolve to a Repository with the name 'x'"), "not-found")
        self.assertEqual(c("HTTP 404: Not Found (https://api.github.com/graphql)"), "not-found")
        self.assertEqual(c("Requires authentication"), "auth")
        # Network noise that merely contains the words is retried (review S1).
        self.assertEqual(c("dial tcp: lookup api.github.com: host not found"), "transient")
        self.assertEqual(c("tls: authentication handshake failed"), "transient")
        self.assertEqual(c("dial tcp: lookup api.github.com: no such host"), "transient")

    def test_pr_argument(self):
        self.assertEqual(W.parse_pr_arg("12"), (12, None))
        self.assertEqual(W.parse_pr_arg("#12"), (12, None))
        self.assertEqual(W.parse_pr_arg("https://github.com/o/n/pull/12/files"), (12, ("o", "n", "github.com")))
        for bad in ("0", "abc", "", "https://github.com/o/n/issues/1"):
            with self.assertRaises(ValueError):
                W.parse_pr_arg(bad)

    def test_remote_parsing(self):
        self.assertEqual(W.parse_remote("git@github.com:o/n.git"), ("o", "n", "github.com"))
        self.assertEqual(W.parse_remote("https://github.com/o/n"), ("o", "n", "github.com"))
        self.assertEqual(W.parse_remote("ssh://git@ghe.corp:22/o/n.git"), ("o", "n", "ghe.corp"))

    def test_repo_resolution_from_owner_name_and_clone_dir(self):
        self.assertEqual(W.resolve_repo("o/n"), ("o", "n", "github.com"))
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "my-clone"
            subprocess.run(["git", "init", "-q", str(clone)], check=True)
            subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", "git@github.com:acme/other.git"],
                           check=True)
            cwd, home = os.getcwd(), os.environ.get("HOME")
            os.chdir(tmp)
            os.environ["HOME"] = tmp
            try:
                self.assertEqual(W.resolve_repo("my-clone"), ("acme", "other", "github.com"))
                # No --repo outside a clone: the sole clone in the workspace.
                self.assertEqual(W.resolve_repo(None), ("acme", "other", "github.com"))
                with self.assertRaises(ValueError):
                    W.resolve_repo("missing-clone")
            finally:
                os.chdir(cwd)
                if home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = home


# A fake `gh` for the end-to-end exit-code contract: answers the two GraphQL
# reads from the JSON scenario in $FAKE_GH_SCENARIO.
_FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
sc = json.load(open(os.environ["FAKE_GH_SCENARIO"]))
q = next(a for a in sys.argv if a.startswith("query="))
if sc.get("fail"):
    sys.stderr.write(sc["fail"] + "\n"); sys.exit(1)
if "pullRequest(" in q:
    print(json.dumps({"data": {"repository": {"pullRequest": sc["pr"]}}}))
else:
    print(json.dumps({"data": {"repository": {"object": {"statusCheckRollup": {"contexts": {
        "pageInfo": {"hasNextPage": False}, "nodes": sc["checks"]}}}}}}))
'''


class ExitCodes(unittest.TestCase):
    """AC3: JSON on stdout plus a documented exit code per verdict."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        fake = self.tmp / "gh"
        fake.write_text(_FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1), encoding="utf-8")
        fake.chmod(0o755)
        self.fake = fake

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, scenario, *extra):
        sc = self.tmp / "scenario.json"
        sc.write_text(json.dumps(scenario), encoding="utf-8")
        env = {**os.environ, "AEE_WAIT_CI_GH": str(self.fake), "FAKE_GH_SCENARIO": str(sc),
               "AEE_WAIT_CI_INTERVAL_SEC": "0.05", "AEE_PR_READINESS_CI_GRACE_MS": "0"}
        env.pop("AEE_RUN_DEADLINE", None)
        env["AEE_STATUS_FILE"] = str(self.tmp / "status.jsonl")
        p = subprocess.run([sys.executable, str(SCRIPT), "7", "--repo", "acme/widgets", *extra],
                           capture_output=True, text=True, env=env, timeout=60)
        out = json.loads(p.stdout)
        self.assertEqual(out["exitCode"], p.returncode)
        return p.returncode, out

    def status_notes(self):
        f = self.tmp / "status.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_codes(self):
        cases = [
            ({"pr": pr_(), "checks": [run_("build")]}, 0, "passed"),
            ({"pr": pr_(), "checks": []}, 0, "no-ci"),
            ({"pr": pr_(), "checks": [run_("lint", conclusion="FAILURE")]}, 4, "failed"),
            ({"pr": pr_(mergeable="CONFLICTING"), "checks": []}, 5, "conflict"),
            ({"pr": pr_(state="MERGED", merged=True), "checks": []}, 1, "error"),
            ({"fail": "HTTP 401: Bad credentials (https://api.github.com/graphql)"}, 1, "error"),
        ]
        for scenario, code, status in cases:
            with self.subTest(status=status, scenario=scenario):
                got_code, out = self.run_cli(scenario)
                self.assertEqual((got_code, out["status"]), (code, status))
                self.assertEqual(out["repo"], "acme/widgets")

    def test_timeout_code(self):
        code, out = self.run_cli({"pr": pr_(), "checks": [run_("build", "IN_PROGRESS", None)]},
                                 "--timeout", "0.003")
        self.assertEqual((code, out["status"]), (6, "timeout"))
        # A real wait announced itself exactly once (#2841).
        notes = self.status_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["kind"], notes[0]["repo"], notes[0]["pr"]), ("ci-wait", "acme/widgets", 7))

    def test_settled_or_usage_error_posts_no_note(self):
        self.run_cli({"pr": pr_(), "checks": [run_("build")]})
        self.assertEqual(self.status_notes(), [])
        env = {**os.environ, "AEE_STATUS_FILE": str(self.tmp / "status.jsonl")}
        p = subprocess.run([sys.executable, str(SCRIPT), "7", "8", "--repo", "acme/widgets"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.status_notes(), [])

    def test_explicit_head_is_never_answered_with_another_heads_verdict(self):
        code, out = self.run_cli({"pr": pr_(SHA_A), "checks": [run_("pytest", conclusion="FAILURE")]},
                                 "--head", SHA_B, "--timeout", "0.003")
        self.assertEqual((code, out["status"], out["expectedHead"]), (6, "timeout", SHA_B))

    def test_usage_errors(self):
        env = {**os.environ}
        for args in (["7", "--timeout", "x"], ["7", "--timeout", "0"], ["7", "8"], ["nope"], ["7", "--bogus"],
                     ["7", "--head", "not-a-commit"], ["7", "--head"]):
            with self.subTest(args=args):
                p = subprocess.run([sys.executable, str(SCRIPT), *args, "--repo", "acme/widgets"],
                                   capture_output=True, text=True, env=env, timeout=30)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertFalse(json.loads(p.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
