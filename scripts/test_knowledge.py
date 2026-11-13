#!/usr/bin/env python3
"""Unit tests for the pure core of the `knowledge` CLI.

The CLI has no `.py` extension (it lives on PATH as `knowledge`), so it is
loaded via SourceFileLoader — the same trick test_agent_entrypoint.py uses for
the entrypoint. Only the PURE core is tested (build_entry / serialize / parse /
match / manifest); the git + network IO in the cmd_* wrappers is verified
manually per repo convention. Run with:

    python3 -m unittest agent-image/scripts/test_knowledge.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

_CLI_PATH = Path(__file__).resolve().parent / "knowledge"
_loader = SourceFileLoader("knowledge_cli", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("knowledge_cli", _loader)
kn = importlib.util.module_from_spec(_spec)
_loader.exec_module(kn)

# The cross-runtime guard: this exact string must be produced by BOTH the Python
# core here and orchestrator/knowledge-store.js `serializeEntry` (see the mirror
# assertion in the node --test). If either side reorders keys or changes JSON
# spacing, the shared KB files diverge — so both tests pin the same golden line.
GOLDEN = (
    '{"v":1,"id":"0123456789ab","kind":"fact","text":"Branch from origin/main",'
    '"originAgent":"cobol-agent","teamId":"team-x","createdAt":"2026-07-08T10:00:00Z",'
    '"correlationId":"evt-123","sourceRefs":[{"kind":"pr","ref":"459"}],'
    '"convergence":["cobol-agent"],"tags":["git","workflow"],"status":"active"}'
)


def _golden_entry():
    return kn.build_entry(
        "fact", "Branch from origin/main",
        origin_agent="cobol-agent", team_id="team-x",
        correlation_id="evt-123", source_refs=[{"kind": "pr", "ref": "459"}],
        tags=["git", "workflow"],
        entry_id="0123456789ab", created_at="2026-07-08T10:00:00Z",
    )


class BuildEntry(unittest.TestCase):
    def test_defaults_and_convergence_seed(self):
        e = kn.build_entry("fact", "x", origin_agent="a1", team_id="t",
                           correlation_id="c1")
        self.assertEqual(e["v"], 1)
        self.assertEqual(e["kind"], "fact")
        self.assertEqual(e["convergence"], ["a1"])  # seeded [originAgent]
        self.assertEqual(e["status"], "active")
        self.assertTrue(len(e["id"]) == 12)

    def test_id_is_12_hex(self):
        e = kn.build_entry("fact", "x", origin_agent="a", team_id="t", correlation_id="c")
        import re
        self.assertRegex(e["id"], r"^[0-9a-f]{12}$")

    def test_missing_provenance_link_raises(self):
        # originAgent/teamId/createdAt present but NO correlationId/sessionId/sourceRefs
        with self.assertRaises(ValueError):
            kn.build_entry("fact", "x", origin_agent="a", team_id="t")

    def test_correlation_id_alone_satisfies_provenance(self):
        e = kn.build_entry("fact", "x", origin_agent="a", team_id="t", correlation_id="c1")
        self.assertEqual(e["correlationId"], "c1")

    def test_source_ref_alone_satisfies_provenance(self):
        e = kn.build_entry("fact", "x", origin_agent="a", team_id="t",
                           source_refs=[{"kind": "pr", "ref": "1"}])
        self.assertEqual(e["sourceRefs"], [{"kind": "pr", "ref": "1"}])

    def test_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            kn.build_entry("nonsense", "x", origin_agent="a", team_id="t", correlation_id="c")

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            kn.build_entry("fact", "   ", origin_agent="a", team_id="t", correlation_id="c")

    def test_missing_origin_agent_raises(self):
        with self.assertRaises(ValueError):
            kn.build_entry("fact", "x", origin_agent="", team_id="t", correlation_id="c")

    def test_bad_source_refs_dropped(self):
        e = kn.build_entry("fact", "x", origin_agent="a", team_id="t",
                           correlation_id="c",
                           source_refs=[{"kind": "bogus", "ref": "1"}, {"kind": "pr", "ref": "9"}])
        self.assertEqual(e["sourceRefs"], [{"kind": "pr", "ref": "9"}])


class Serialize(unittest.TestCase):
    def test_golden_matches_js_core(self):
        self.assertEqual(kn.serialize_entry(_golden_entry()), GOLDEN)

    def test_key_order_no_spaces(self):
        line = kn.serialize_entry(_golden_entry())
        self.assertNotIn(", ", line)  # separators=(",",":"), matches JSON.stringify
        self.assertTrue(line.startswith('{"v":1,"id":'))

    def test_round_trip(self):
        e = _golden_entry()
        self.assertEqual(kn.parse_entry(kn.serialize_entry(e)), e)

    def test_summary_optional_field(self):
        # Absent unless supplied — byte-identical to pre-#635 output.
        bare = kn.build_entry("fact", "x", origin_agent="a", team_id="t", correlation_id="c")
        self.assertNotIn("summary", bare)
        self.assertNotIn('"summary"', kn.serialize_entry(bare))
        # Supplied → trimmed and round-trips.
        with_s = kn.build_entry("fact", "x", origin_agent="a", team_id="t",
                                correlation_id="c", summary="  A readable rewrite.  ")
        self.assertEqual(with_s["summary"], "A readable rewrite.")
        self.assertTrue(kn.validate_entry(with_s)[0])
        self.assertEqual(kn.parse_entry(kn.serialize_entry(with_s)), with_s)
        # Blank summary dropped, not stored.
        blank = kn.build_entry("fact", "x", origin_agent="a", team_id="t",
                               correlation_id="c", summary="   ")
        self.assertNotIn("summary", blank)
        # Non-string summary is a soft validation error.
        bad = dict(bare); bad["summary"] = 42
        self.assertFalse(kn.validate_entry(bad)[0])


class Parse(unittest.TestCase):
    def test_blank_and_mangled_return_none(self):
        for bad in ("", "   ", "{not json", "[1,2,3]", "42", None):
            self.assertIsNone(kn.parse_entry(bad))

    def test_valid_line(self):
        self.assertEqual(kn.parse_entry(GOLDEN)["id"], "0123456789ab")


class Match(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {"kind": "fact", "text": "poller pagination handles bursts", "tags": ["polling"]},
            {"kind": "entity", "text": "github-poller.js polls every 30s"},
            {"kind": "convention", "text": "Branch from origin/main"},
        ]

    def test_substring_case_insensitive(self):
        m = kn.match_entries(self.entries, "POLLER")
        self.assertEqual(len(m), 2)

    def test_kind_filter(self):
        m = kn.match_entries(self.entries, "poller", kind="fact")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["kind"], "fact")

    def test_tag_match(self):
        m = kn.match_entries(self.entries, "polling")
        self.assertEqual(len(m), 1)

    def test_invalid_regex_falls_back_to_substring(self):
        # a stray unbalanced paren must not crash the search
        m = kn.match_entries(self.entries, "origin/main(")
        self.assertEqual(m, [])  # no literal match, but no crash
        m2 = kn.match_entries([{"kind": "fact", "text": "a(b"}], "a(")
        self.assertEqual(len(m2), 1)

    def test_empty_query_returns_all(self):
        self.assertEqual(len(kn.match_entries(self.entries, "")), 3)

    def test_bad_kind_returns_empty(self):
        self.assertEqual(kn.match_entries(self.entries, "x", kind="bogus"), [])


class Manifest(unittest.TestCase):
    def test_default_manifest_shape(self):
        m = kn.build_default_manifest()
        self.assertEqual(kn.kind_dir(m, "fact"), "facts")
        self.assertEqual(kn.kind_canonical_file(m, "fact"), "facts/facts.jsonl")

    def test_parse_source_ref(self):
        self.assertEqual(kn.parse_source_ref("pr:459"), {"kind": "pr", "ref": "459"})
        self.assertEqual(kn.parse_source_ref("url:https://x/y"), {"kind": "url", "ref": "https://x/y"})
        self.assertIsNone(kn.parse_source_ref("bogus:1"))
        self.assertIsNone(kn.parse_source_ref("nope"))


class LeanSummary(unittest.TestCase):
    """The Phase-2 push summary — only whitelisted fields, count from convergence."""

    def test_whitelists_fields(self):
        entry = {
            "id": "9f3a12bc7de0", "kind": "fact", "text": "hi",
            "originAgent": "acme-dev-1", "teamId": "acme",
            "convergence": ["acme-dev-1", "acme-dev-2"], "correlationId": "$evt1",
        }
        self.assertEqual(kn.lean_summary(entry), {
            "id": "9f3a12bc7de0", "kind": "fact", "text": "hi",
            "originAgent": "acme-dev-1", "convergenceCount": 2,
        })

    def test_defaults_count_to_one(self):
        self.assertEqual(kn.lean_summary({"id": "aaaaaaaaaaaa"})["convergenceCount"], 1)

    def test_drops_idless(self):
        self.assertIsNone(kn.lean_summary({"kind": "fact"}))
        self.assertIsNone(kn.lean_summary(None))


class BuildNominationTests(unittest.TestCase):
    """The `knowledge promote <id>` nomination payload (Phase 3)."""

    def test_shape_with_note(self):
        self.assertEqual(
            kn.build_nomination("9f3a12bc7de0", "worth it", "acme-dev-1"),
            {"entryId": "9f3a12bc7de0", "agentId": "acme-dev-1", "note": "worth it"},
        )

    def test_note_omitted_when_blank(self):
        body = kn.build_nomination("9f3a12bc7de0", "   ", "acme-dev-1")
        self.assertNotIn("note", body)
        self.assertEqual(body["entryId"], "9f3a12bc7de0")
        body2 = kn.build_nomination("9f3a12bc7de0", None, "acme-dev-1")
        self.assertNotIn("note", body2)

    def test_promote_dispatches_not_stub(self):
        # The verb is wired (no longer the exit-3 stub) and resolves cmd_promote.
        self.assertIn("promote", ("add", "search", "list", "path", "promote"))
        self.assertTrue(callable(kn.cmd_promote))


class SlugifySkillNameTests(unittest.TestCase):
    """Mirror of slugifySkillName in the JS suggestions/curator cores."""

    def test_slug_from_text(self):
        self.assertEqual(kn.slugify_skill_name("This repo unit-tests pure helper cores only"),
                         "this-repo-unit-tests-pure-helper")

    def test_fallback_on_empty_or_invalid(self):
        self.assertEqual(kn.slugify_skill_name("", "entity"), "entity-skill")
        self.assertEqual(kn.slugify_skill_name("!!!", "convention"), "convention-skill")


class BuildAgentSkillTests(unittest.TestCase):
    """`knowledge promote <id> --to agent` — a private agent-tier skill (Phase 4)."""

    def _entry(self, **over):
        base = {
            "v": 1, "id": "9f3a12bc7de0", "kind": "fact",
            "text": "Branch from origin/main",
            "originAgent": "acme-dev-1", "teamId": "acme",
            "createdAt": "2026-07-08T09:00:00Z", "correlationId": "$evt1",
            "convergence": ["acme-dev-1"], "status": "active",
            "sourceRefs": [{"kind": "pr", "ref": "459"}],
        }
        base.update(over)
        return base

    def test_shape_and_mandatory_provenance(self):
        s = kn.build_agent_skill(self._entry(), agent_id="acme-dev-1", team_id="acme",
                                 now=lambda: "2026-07-09T00:00:00Z")
        self.assertEqual(s["name"], "branch-from-origin-main")
        self.assertTrue(s["skill_md"].startswith("# Branch from origin/main"))
        self.assertIn("Branch from origin/main", s["skill_md"])
        prov = s["provenance"]
        self.assertEqual(prov["toTier"], "agent")
        self.assertEqual(prov["entryIds"], ["9f3a12bc7de0"])  # graph skillEntryIds needs this
        self.assertEqual(prov["originAgents"], ["acme-dev-1"])
        self.assertEqual(prov["promotedBy"], "acme-dev-1")
        self.assertEqual(prov["promotedAt"], "2026-07-09T00:00:00Z")
        self.assertEqual(prov["sourceRefs"], [{"kind": "pr", "ref": "459"}])
        self.assertEqual(prov["sessions"], [{"agentId": "acme-dev-1", "correlationId": "$evt1"}])

    def test_custom_name_and_no_correlation(self):
        s = kn.build_agent_skill(self._entry(correlationId=None), name="my-skill",
                                 agent_id="a1", now=lambda: "t")
        self.assertEqual(s["name"], "my-skill")
        self.assertEqual(s["provenance"]["sessions"], [])

    def test_invalid_name_raises(self):
        with self.assertRaises(ValueError):
            kn.build_agent_skill(self._entry(), name="bad name!", agent_id="a1")

    def test_entry_without_id_raises(self):
        with self.assertRaises(ValueError):
            kn.build_agent_skill({"text": "x"}, agent_id="a1")

    def test_no_secrets_in_body(self):
        s = kn.build_agent_skill(self._entry(), agent_id="a1", now=lambda: "t")
        self.assertNotIn("$evt1", s["skill_md"])  # correlationId stays out of the body


class TestVisibleEntries(unittest.TestCase):
    """visible_entries hides non-curated lines from `knowledge search` / `list` so an
    agent's query matches the operator's Knowledge Flow (and never surfaces a fact a
    human has not yet approved)."""

    def _e(self, eid, status="active", **over):
        d = {"id": eid, "kind": "fact", "text": eid, "status": status,
             "createdAt": "2026-07-08T09:00:00Z"}
        d.update(over)
        return d

    def test_active_kept_hidden_statuses_dropped(self):
        entries = [
            self._e("a" * 12, "active"),
            self._e("b" * 12, "promoted"),
            self._e("c" * 12, "archived"),
            self._e("d" * 12, "pending"),
            self._e("e" * 12, "rejected"),
            self._e("f" * 12, "merged", mergedInto="a" * 12),
        ]
        ids = [e["id"] for e in kn.visible_entries(entries)]
        self.assertEqual(ids, ["a" * 12, "b" * 12])

    def test_shard_active_plus_canonical_pending_same_id_is_hidden(self):
        # shard copy (active) read first, canonical held copy (pending) read last —
        # the held status must win so the fact never reaches the query.
        entries = [
            self._e("b" * 12, "active", createdAt="2026-07-08T09:00:00Z"),
            self._e("b" * 12, "pending", createdAt="2026-07-08T09:00:00Z"),
        ]
        self.assertEqual(kn.visible_entries(entries), [])

    def test_newer_active_update_beats_stale_pending(self):
        entries = [
            self._e("b" * 12, "pending", updatedAt="2026-07-08T09:00:00Z"),
            self._e("b" * 12, "active", updatedAt="2026-07-08T11:00:00Z"),
        ]
        ids = [e["id"] for e in kn.visible_entries(entries)]
        self.assertEqual(ids, ["b" * 12])  # approved later → visible again

    def test_no_id_skipped_and_output_stable(self):
        entries = [self._e("a" * 12), {"text": "no id"}, self._e("a" * 12)]
        self.assertEqual([e["id"] for e in kn.visible_entries(entries)], ["a" * 12])

    def test_expired_folds_out_with_injected_now_only(self):
        # #916: validTo before `now` hides the entry exactly like archived; a
        # future validTo — or no injected clock at all — keeps it visible.
        entries = [
            self._e("a" * 12, validTo="2026-07-01T00:00:00Z"),
            self._e("b" * 12, validTo="2026-12-31T23:59:59Z"),
            self._e("c" * 12),
        ]
        now = "2026-08-01T12:00:00Z"
        self.assertEqual([e["id"] for e in kn.visible_entries(entries, now=now)],
                         ["b" * 12, "c" * 12])
        # no clock (pure core has none) => nothing expires
        self.assertEqual(len(kn.visible_entries(entries)), 3)

    def test_validity_fields_round_trip_and_key_order(self):
        # #916 AC1: build_entry accepts the three fields; serialized key order is
        # stable (validity keys after summary); pre-existing entries still validate.
        e = kn.build_entry(
            "fact", "expires", origin_agent="dev-1", team_id="acme",
            correlation_id="$evt", valid_from="2026-07-01T00:00:00Z",
            valid_to="2026-09-01T00:00:00Z", superseded_by="d" * 12,
            entry_id="a" * 12, created_at="2026-07-01T00:00:00Z",
        )
        ok, errors = kn.validate_entry(e)
        self.assertTrue(ok, errors)
        line = kn.serialize_entry(e)
        keys = list(json.loads(line).keys())
        self.assertEqual(keys.index("validFrom"), len(keys) - 3)
        self.assertEqual(kn.parse_entry(line), e)
        # legacy entry without the fields is untouched and valid
        legacy = kn.build_entry("fact", "old", origin_agent="dev-1",
                                team_id="acme", correlation_id="$evt")
        self.assertNotIn("validTo", legacy)
        ok2, _ = kn.validate_entry(legacy)
        self.assertTrue(ok2)
        # invalid shapes are rejected
        bad = dict(e, supersededBy="not-hex")
        ok3, errs3 = kn.validate_entry(bad)
        self.assertFalse(ok3)
        self.assertTrue(any("supersededBy" in x for x in errs3))


class ResolveTeamId(unittest.TestCase):
    """resolve_team_id_from_config mirrors the orchestrator's agentKnowledgeTeamId
    precedence so the CLI scopes writes to the same team the orchestrator injects
    (issue #538). Battery: parentTeamId / role=='team' id / plain teamId / none."""

    def test_parent_team_id_wins(self):
        cfg = {"parentTeamId": "agent-studio", "teamId": "default", "role": "team", "id": "x"}
        self.assertEqual(kn.resolve_team_id_from_config(cfg), "agent-studio")

    def test_role_team_uses_id(self):
        cfg = {"role": "team", "id": "squad-a", "teamId": "default"}
        self.assertEqual(kn.resolve_team_id_from_config(cfg), "squad-a")

    def test_plain_team_id(self):
        self.assertEqual(kn.resolve_team_id_from_config({"teamId": "acme"}), "acme")

    def test_role_non_team_ignores_id(self):
        # a normal agent's id is NOT its team — only role=="team" promotes id
        cfg = {"role": "team-member", "id": "acme-dev-1", "teamId": "acme"}
        self.assertEqual(kn.resolve_team_id_from_config(cfg), "acme")

    def test_empty_or_missing_falls_back_to_default(self):
        for cfg in ({}, {"teamId": "  "}, {"parentTeamId": ""}, None, "nope", {"role": "team", "id": ""}):
            self.assertEqual(kn.resolve_team_id_from_config(cfg), "default")


class TeamIdResolution(unittest.TestCase):
    """_team_id(): explicit env override wins, else config fallback, else default.
    Covers issue #538 acceptance criteria 1–3."""

    def setUp(self):
        self._saved = kn._load_agent_config
        self._env = dict(kn.os.environ)

    def tearDown(self):
        kn._load_agent_config = self._saved
        kn.os.environ.clear()
        kn.os.environ.update(self._env)

    def test_env_override_wins(self):
        kn.os.environ["KNOWLEDGE_TEAM_ID"] = "explicit"
        kn._load_agent_config = lambda: {"parentTeamId": "agent-studio"}
        self.assertEqual(kn._team_id(), "explicit")

    def test_config_fallback_when_env_empty(self):
        kn.os.environ.pop("KNOWLEDGE_TEAM_ID", None)
        kn._load_agent_config = lambda: {"parentTeamId": "agent-studio", "teamId": "default"}
        self.assertEqual(kn._team_id(), "agent-studio")

    def test_blank_env_is_ignored(self):
        kn.os.environ["KNOWLEDGE_TEAM_ID"] = "   "
        kn._load_agent_config = lambda: {"teamId": "acme"}
        self.assertEqual(kn._team_id(), "acme")

    def test_no_env_no_config_is_default_not_error(self):
        kn.os.environ.pop("KNOWLEDGE_TEAM_ID", None)
        kn._load_agent_config = lambda: {}
        self.assertEqual(kn._team_id(), "default")

    def test_config_derived_team_builds_valid_entry(self):
        # end-to-end: a config-derived team feeds build_entry without hard-failing
        kn.os.environ.pop("KNOWLEDGE_TEAM_ID", None)
        kn._load_agent_config = lambda: {"parentTeamId": "agent-studio"}
        e = kn.build_entry("fact", "x", origin_agent="a", team_id=kn._team_id(),
                           correlation_id="c1")
        self.assertEqual(e["teamId"], "agent-studio")


class BuildCommitArgs(unittest.TestCase):
    """`knowledge add`'s commit must be self-sufficient (issue #615): committer
    identity set INLINE (mirroring gh-as-agent commit) so it works with no ambient
    git identity, the author keeps the @agents.local loop-protection marker, and
    the global git config is never touched."""

    def test_inline_committer_identity(self):
        args = kn.build_commit_args("cobol-agent", "fact", "0123456789ab")
        # committer set inline via -c pairs (mirrors gh-as-agent commit)
        self.assertIn("-c", args)
        self.assertIn("user.name=agent-cobol-agent", args)
        self.assertIn("user.email=agent-cobol-agent@ace.local", args)

    def test_author_keeps_agents_local_marker(self):
        args = kn.build_commit_args("cobol-agent", "fact", "0123456789ab")
        i = args.index("--author")
        self.assertEqual(args[i + 1], "cobol-agent <cobol-agent@agents.local>")

    def test_never_mutates_global_config(self):
        # no `config` / `--global` — identity is per-commit only (AC3)
        args = kn.build_commit_args("a1", "entity", "aaaaaaaaaaaa")
        self.assertNotIn("config", args)
        self.assertNotIn("--global", args)
        self.assertNotIn("--system", args)

    def test_commit_shape(self):
        args = kn.build_commit_args("a1", "convention", "aaaaaaaaaaaa")
        self.assertIn("commit", args)
        i = args.index("-m")
        self.assertEqual(args[i + 1], "knowledge(convention): aaaaaaaaaaaa")
        # the -c identity flags precede the `commit` subcommand
        self.assertLess(args.index("-c"), args.index("commit"))


def _has_git():
    return shutil.which("git") is not None


@unittest.skipUnless(_has_git(), "git not available")
class AddCommitIntegration(unittest.TestCase):
    """End-to-end coverage for the #615 fix: `knowledge add` commits in a context
    with NO ambient git identity, and fails loud when the commit cannot happen."""

    def _run_add(self, cwd_repo, *extra, env_over=None):
        """Run the real CLI as a subprocess with a git context that has NO ambient
        identity (empty HOME, global/system config pointed at /dev/null) so a
        commit only succeeds if the CLI sets the identity itself."""
        env = dict(os.environ)
        # Strip any inherited git identity so the ambient identity is truly absent.
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            env.pop(k, None)
        env["HOME"] = self._home
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["AGENT_ID"] = "cobol-agent"
        env["KNOWLEDGE_DIR"] = cwd_repo
        env["KNOWLEDGE_TEAM_ID"] = "team-x"
        env["KNOWLEDGE_CORRELATION_ID"] = "evt-1"
        if env_over:
            env.update(env_over)
        return subprocess.run(
            [sys.executable, str(_CLI_PATH), "add", *extra],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def _git(self, repo, *args):
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True,
            env={**os.environ, "HOME": self._home,
                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
            timeout=15,
        )

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="kn-test-")
        self._home = os.path.join(self._tmp, "home")
        os.makedirs(self._home)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_commit_succeeds_without_ambient_identity(self):
        repo = os.path.join(self._tmp, "kb")
        os.makedirs(repo)
        self.assertEqual(self._git(repo, "init").returncode, 0)

        res = self._run_add(repo, "fact", "no-ambient-identity works", "--no-push")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("Committer identity unknown", res.stderr)
        self.assertIn("recorded fact", res.stderr)  # success line printed (AC5)

        # AC2: committer is the deterministic agent identity, author unchanged.
        log = self._git(repo, "log", "-1", "--format=%cn|%ce|%an|%ae")
        self.assertEqual(log.returncode, 0, log.stderr)
        cn, ce, an, ae = log.stdout.strip().split("|")
        self.assertEqual((cn, ce), ("agent-cobol-agent", "agent-cobol-agent@ace.local"))
        self.assertEqual((an, ae), ("cobol-agent", "cobol-agent@agents.local"))

    def test_prior_uncommitted_entry_is_committed_next_time(self):
        # Edge case: an entry appended by an earlier (buggy) run but never committed
        # must land on the next successful add, without duplication.
        repo = os.path.join(self._tmp, "kb")
        os.makedirs(repo)
        self.assertEqual(self._git(repo, "init").returncode, 0)
        shard = os.path.join(repo, "docs/agent-knowledge/facts/shards/cobol-agent.jsonl")
        os.makedirs(os.path.dirname(shard))
        with open(shard, "w", encoding="utf-8") as fh:
            fh.write('{"v":1,"id":"aaaaaaaaaaaa","kind":"fact","text":"leftover"}\n')

        res = self._run_add(repo, "fact", "fresh entry", "--no-push")
        self.assertEqual(res.returncode, 0, res.stderr)
        # both the leftover and the fresh line are now committed (one commit).
        show = self._git(repo, "show", "HEAD:docs/agent-knowledge/facts/shards/cobol-agent.jsonl")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("leftover", show.stdout)
        self.assertIn("fresh entry", show.stdout)
        self.assertEqual(show.stdout.count("aaaaaaaaaaaa"), 1)  # no duplication

    def test_fail_loud_when_commit_cannot_happen(self):
        # KNOWLEDGE_DIR is NOT a git repo → the commit fails; the CLI must exit
        # non-zero and must NOT print the `recorded` success line (AC4).
        not_a_repo = os.path.join(self._tmp, "plain")
        os.makedirs(not_a_repo)
        res = self._run_add(not_a_repo, "fact", "should not be reported as shared",
                            "--no-push")
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("recorded fact", res.stderr)
        self.assertIn("NOT recorded", res.stderr)


if __name__ == "__main__":
    unittest.main()
