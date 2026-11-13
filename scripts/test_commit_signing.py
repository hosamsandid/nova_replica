#!/usr/bin/env python3
"""Tests for SSH commit signing inside an agent container (#2938).

End-to-end: a fake orchestrator (local HTTP server) signs with a throwaway
ed25519 key exactly as the real one does (`ssh-keygen -Y sign -n git`); git in a
temp repo is configured the way the runtime configures a run
(`_signing_config_pairs` + `_with_git_config_env`, `aee-ssh-sign` as
`gpg.ssh.program`). Every commit-creating path — `gh-as-agent commit`, the
`knowledge` CLI's commit args, plain commit, amend, cherry-pick, rebase and
merge — must produce a valid SSH signature (`git verify-commit` against an
allowed-signers file), keep the `<id>@agents.local` author, and fail loudly
(never commit unsigned) when signing is configured but broken.

    python -m pytest agent-image/scripts/test_commit_signing.py -q
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIGNER = HERE / "aee-ssh-sign"
GH_AS_AGENT = HERE / "gh-as-agent"
SSH_KEYGEN = shutil.which("ssh-keygen")
AGENT = "team-x-developer"
SIGNER_EMAIL = "bot@example.com"

_spec = importlib.util.spec_from_file_location("agent_entrypoint", HERE / "agent-entrypoint.py")
entrypoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entrypoint)

_kloader = importlib.machinery.SourceFileLoader("knowledge_cli", str(HERE / "knowledge"))
_kspec = importlib.util.spec_from_loader("knowledge_cli", _kloader)
knowledge = importlib.util.module_from_spec(_kspec)
_kloader.exec_module(knowledge)


class SigningConfigPairs(unittest.TestCase):
    def test_no_signing_means_no_config(self):
        self.assertEqual(entrypoint._signing_config_pairs(None), ([], {}))
        self.assertEqual(entrypoint._signing_config_pairs({"publicKey": "ssh-ed25519 AAA"}), ([], {}))

    def test_signing_sets_committer_not_user(self):
        pairs, extra = entrypoint._signing_config_pairs({
            "label": "team SSH key 'k'", "publicKey": "ssh-ed25519 AAAA", "name": "Bot", "email": SIGNER_EMAIL,
        })
        m = dict(pairs)
        self.assertEqual(m["gpg.format"], "ssh")
        self.assertEqual(m["user.signingkey"], "key::ssh-ed25519 AAAA")
        self.assertEqual(m["commit.gpgsign"], "true")
        self.assertEqual(m["gpg.ssh.program"], entrypoint.SSH_SIGN_PROGRAM)
        self.assertEqual(m["committer.email"], SIGNER_EMAIL)
        self.assertNotIn("user.email", m)
        self.assertEqual(extra, {"AEE_SIGNING_LABEL": "team SSH key 'k'"})

    def test_unusable_key_still_turns_signing_on(self):
        pairs, extra = entrypoint._signing_config_pairs({"label": "L", "error": "cannot read"})
        self.assertEqual(dict(pairs)["commit.gpgsign"], "true")
        self.assertEqual(extra["AEE_SIGNING_ERROR"], "cannot read")

    def test_git_config_env_appends(self):
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
        entrypoint._with_git_config_env(env, [("x.y", "z")])
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "x.y")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "a.b")


class _ConfigServer:
    """A one-route fake orchestrator answering /git/signing-config with a fixed
    status and raw body."""

    def __init__(self, status, body, delay=0.0):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                time.sleep(delay)
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SigningConfigFetch(unittest.TestCase):
    """B2/AC9: a run whose signing config cannot be read must not commit
    unsigned — only a 404 (older orchestrator) keeps the old unsigned path."""

    def setUp(self):
        self.old = {k: os.environ.get(k) for k in ("ORCHESTRATOR_INTERNAL_URL", "AGENT_ID")}
        os.environ["AGENT_ID"] = AGENT

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def apply(self, url):
        os.environ["ORCHESTRATOR_INTERNAL_URL"] = url
        env = {"AEE_ACTING_SENDER": ""}
        entrypoint._apply_commit_signing(env)
        return env

    def assert_fails_closed(self, env, detail):
        self.assertEqual(env["GIT_CONFIG_COUNT"], "4")
        self.assertIn(("commit.gpgsign", "true"),
                      [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(4)])
        self.assertIn("could not read this run's commit-signing config", env["AEE_SIGNING_ERROR"])
        self.assertIn("start a new run", env["AEE_SIGNING_ERROR"])
        self.assertIn(detail, env["AEE_SIGNING_ERROR"])

    def with_server(self, status, body):
        srv = _ConfigServer(status, body)
        self.addCleanup(srv.close)
        return self.apply(srv.url)

    def test_unreachable_orchestrator_fails_closed(self):
        self.assert_fails_closed(self.apply("http://127.0.0.1:9"), "refused")

    def test_server_error_fails_closed(self):
        self.assert_fails_closed(self.with_server(500, '{"ok": false}'), "HTTP 500")

    def test_non_json_answer_fails_closed(self):
        self.assert_fails_closed(self.with_server(200, "<html>proxy error</html>"), "")

    def test_timeout_fails_closed(self):
        srv = _ConfigServer(200, '{"signing": null}', delay=1.0)
        self.addCleanup(srv.close)
        os.environ["ORCHESTRATOR_INTERNAL_URL"] = srv.url
        signing = entrypoint._fetch_signing_config("", timeout=0.2)
        self.assertIn("could not read this run's commit-signing config", signing["error"])
        self.assertIn("timed out", signing["error"])

    def test_404_from_an_older_orchestrator_stays_unsigned(self):
        self.assertNotIn("GIT_CONFIG_COUNT", self.with_server(404, "not found"))

    def test_no_key_configured_stays_unsigned(self):
        env = self.with_server(200, '{"signing": null}')
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn("AEE_SIGNING_ERROR", env)

    def test_no_agent_id_stays_unsigned(self):
        os.environ.pop("AGENT_ID", None)
        self.assertNotIn("GIT_CONFIG_COUNT", self.apply("http://127.0.0.1:9"))


class SignerArgs(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("aee_ssh_sign", str(SIGNER))
        spec = importlib.util.spec_from_loader("aee_ssh_sign", loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)

    def test_parses_git_sign_invocation(self):
        self.assertEqual(
            self.mod._parse_sign_args(["-Y", "sign", "-n", "git", "-f", "/tmp/k", "-U", "/tmp/buf"]),
            ("git", "/tmp/k", "/tmp/buf"),
        )
        self.assertIsNone(self.mod._parse_sign_args(["-Y", "verify", "-n", "git"]))
        self.assertIsNone(self.mod._parse_sign_args(["-l", "-f", "x"]))


@unittest.skipUnless(SSH_KEYGEN and shutil.which("git"), "needs git + ssh-keygen")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aee-sign-test-"))
        cls.key = cls.tmp / "signing_key"
        subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(cls.key)], check=True)
        cls.pub = " ".join((cls.tmp / "signing_key.pub").read_text().split()[:2])
        (cls.tmp / "allowed_signers").write_text(f"{SIGNER_EMAIL} {cls.pub}\n")
        cls.calls = []
        cls.fail_with = None
        outer = cls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                outer.calls.append((self.path, body))
                if outer.fail_with:
                    return self._send(422, {"ok": False, "error": outer.fail_with})
                payload = base64.b64decode(body["payload"])
                d = Path(tempfile.mkdtemp(dir=outer.tmp))
                (d / "p").write_bytes(payload)
                subprocess.run([SSH_KEYGEN, "-Y", "sign", "-n", "git", "-f", str(outer.key), str(d / "p")],
                               check=True, capture_output=True)
                self._send(200, {"ok": True, "signature": (d / "p.sig").read_text()})

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        type(self).fail_with = None
        self.repo = Path(tempfile.mkdtemp(dir=self.tmp))
        pairs, extra = entrypoint._signing_config_pairs({
            "label": "team SSH key 'k'", "publicKey": self.pub, "name": "Signing Bot", "email": SIGNER_EMAIL,
        })
        pairs = [(k, str(SIGNER) if k == "gpg.ssh.program" else v) for k, v in pairs]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({
            "HOME": str(self.repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "AGENT_ID": AGENT,
            "ORCHESTRATOR_INTERNAL_URL": f"http://127.0.0.1:{self.server.server_port}",
            "AEE_REAL_SSH_KEYGEN": SSH_KEYGEN,
            "AEE_ACTING_SENDER": "",
            # A non-interactive git identity for the plain (non-wrapper) commands.
            "GIT_AUTHOR_NAME": AGENT, "GIT_AUTHOR_EMAIL": f"{AGENT}@agents.local",
        })
        entrypoint._with_git_config_env(env, pairs + [("gpg.ssh.allowedSignersFile", str(self.tmp / "allowed_signers")),
                                                      ("init.defaultBranch", "main")])
        env.update(extra)
        self.env = env
        self.git("init", "-q")
        self.commit_file("base.txt", "base\n", "base")

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True, check=check)

    def commit_file(self, name, text, msg):
        (self.repo / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", msg)

    def assert_signed(self, rev="HEAD"):
        r = self.git("verify-commit", rev, check=False)
        self.assertEqual(r.returncode, 0, f"{rev} not validly signed: {r.stderr}")
        self.assertIn(f'Good "git" signature for {SIGNER_EMAIL}', r.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%ce", rev).stdout.strip(), SIGNER_EMAIL)

    def test_plain_commit(self):
        self.assert_signed()
        self.assertEqual(self.git("log", "-1", "--format=%ae").stdout.strip(), f"{AGENT}@agents.local")

    def test_gh_as_agent_commit_keeps_agent_author(self):
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        r = subprocess.run(["bash", str(GH_AS_AGENT), "commit", "-q", "-m", "via wrapper"],
                           cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_signed()
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae>").stdout.strip(), f"{AGENT} <{AGENT}@agents.local>")

    def test_knowledge_cli_commit_args(self):
        (self.repo / "k.jsonl").write_text("{}\n")
        self.git("add", "k.jsonl")
        self.git(*knowledge.build_commit_args(AGENT, "fact", "abc123"))
        self.assert_signed()
        self.assertEqual(self.git("log", "-1", "--format=%ae").stdout.strip(), f"{AGENT}@agents.local")

    def test_amend(self):
        self.git("commit", "--amend", "-q", "-m", "amended")
        self.assert_signed()

    def test_cherry_pick(self):
        self.git("checkout", "-q", "-b", "side")
        self.commit_file("s.txt", "s\n", "side work")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", "main")
        self.git("cherry-pick", sha)
        self.assert_signed()

    def test_rebase(self):
        self.git("checkout", "-q", "-b", "feat")
        self.commit_file("f1.txt", "1\n", "f1")
        self.commit_file("f2.txt", "2\n", "f2")
        self.git("checkout", "-q", "main")
        self.commit_file("m.txt", "m\n", "main moves")
        self.git("checkout", "-q", "feat")
        self.git("rebase", "-q", "main")
        self.assert_signed("HEAD")
        self.assert_signed("HEAD~1")

    def test_merge(self):
        self.git("checkout", "-q", "-b", "feat")
        self.commit_file("f.txt", "f\n", "f")
        self.git("checkout", "-q", "main")
        self.commit_file("m.txt", "m\n", "m")
        self.git("merge", "-q", "--no-edit", "feat")
        self.assert_signed()
        self.assertEqual(len(self.git("log", "-1", "--format=%P").stdout.split()), 2)

    def test_signing_failure_fails_the_commit(self):
        type(self).fail_with = "signing with the team SSH key 'k' failed: key gone"
        before = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "x.txt").write_text("x\n")
        self.git("add", "x.txt")
        r = self.git("commit", "-q", "-m", "should fail", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("key gone", r.stderr)
        self.assertIn("/ssh-keys", r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)

    def test_unusable_key_fails_without_calling_the_orchestrator(self):
        self.env["AEE_SIGNING_ERROR"] = "the team SSH key 'k' cannot be read: invalid format"
        n = len(self.calls)
        r = self.git("commit", "--allow-empty", "-q", "-m", "x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid format", r.stderr)
        self.assertEqual(len(self.calls), n)

    def test_unreadable_signing_config_fails_the_commit(self):
        # B2/AC9: the run-start config fetch failed → every commit fails loudly.
        _, extra = entrypoint._signing_config_pairs(
            {"label": "run's SSH signing key",
             "error": entrypoint.SIGNING_CONFIG_UNREADABLE.format(detail="HTTP 503")})
        self.env.update(extra)
        r = self.git("commit", "--allow-empty", "-q", "-m", "x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not read this run's commit-signing config from the orchestrator (HTTP 503)", r.stderr)
        self.assertIn("start a new run", r.stderr)

    def test_signer_sends_acting_sender_and_public_key(self):
        self.env["AEE_ACTING_SENDER"] = "@u-abc:local"
        self.git("commit", "--allow-empty", "-q", "-m", "x")
        path, body = self.calls[-1]
        self.assertEqual(path, f"/internal/agents/{AGENT}/git/sign")
        self.assertEqual(body["actingSender"], "@u-abc:local")
        self.assertEqual(body["publicKey"], self.pub)
        self.assertEqual(body["namespace"], "git")


if __name__ == "__main__":
    unittest.main()
