#!/usr/bin/env python3
"""Unit tests for the pure core of the `usage` CLI (#679).

The CLI has no `.py` extension (it lives on PATH as `usage`), so it is loaded
via SourceFileLoader — the same trick test_knowledge.py uses. Only the PURE core
is tested (URL assembly + rendering + the no-money guarantee); the network IO in
the cmd_* wrappers is verified manually per repo convention (concept §8). Run:

    python3 -m unittest agent-image/scripts/test_usage.py
"""
import importlib.util
import re
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_CLI_PATH = Path(__file__).resolve().parent / "usage"
_loader = SourceFileLoader("usage_cli", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("usage_cli", _loader)
uu = importlib.util.module_from_spec(_spec)
_loader.exec_module(uu)


class BuildUrl(unittest.TestCase):
    def test_defaults_to_self_scope_only(self):
        url = uu.build_summary_url("http://orchestrator:4002", "dev-1")
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/internal/agents/dev-1/usage/summary")
        self.assertEqual(parse_qs(parsed.query), {"scope": ["self"]})

    def test_all_params_threaded(self):
        url = uu.build_summary_url(
            "http://orchestrator:4002/", "dev-1",
            scope="team", since="2026-07-01", until="2026-07-16", group_by="day",
        )
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["scope"], ["team"])
        self.assertEqual(q["since"], ["2026-07-01"])
        self.assertEqual(q["until"], ["2026-07-16"])
        self.assertEqual(q["groupBy"], ["day"])

    def test_bad_scope_falls_closed_to_self(self):
        url = uu.build_summary_url("http://o:4002", "dev-1", scope="all")
        self.assertEqual(parse_qs(urlparse(url).query)["scope"], ["self"])

    def test_agent_id_is_path_quoted(self):
        url = uu.build_summary_url("http://o:4002", "team/../etc")
        self.assertIn("team%2F..%2Fetc", url)
        self.assertNotIn("team/../etc", url)

    def test_omitted_params_are_absent(self):
        q = parse_qs(urlparse(uu.build_summary_url("http://o:4002", "a")).query)
        self.assertNotIn("since", q)
        self.assertNotIn("until", q)
        self.assertNotIn("groupBy", q)


_SAMPLE = {
    "scope": "self",
    "agentId": "dev-1",
    "teamId": None,
    "since": "2026-07-01",
    "until": None,
    "groupBy": "agent",
    "tokenClasses": list(uu.TOKEN_CLASSES),
    "tokensOnly": True,
    "totals": {
        "input": 30, "output": 9, "cacheCreationInput": 1,
        "cacheReadInput": 2, "total": 42, "invocations": 2,
    },
    "groups": [
        {"key": "dev-1", "tokens": {
            "input": 30, "output": 9, "cacheCreationInput": 1,
            "cacheReadInput": 2, "total": 42,
        }},
    ],
}


class FormatSummary(unittest.TestCase):
    def test_renders_four_classes_total_and_invocations(self):
        out = uu.format_summary(_SAMPLE)
        for label in ("Input", "Output", "Cache write", "Cache read", "Total", "Invocations"):
            self.assertIn(label, out)
        self.assertIn("42", out)  # total
        self.assertIn("dev-1", out)  # group key

    def test_zeroed_result_renders_without_error(self):
        out = uu.format_summary({"scope": "self", "totals": {}, "groups": []})
        self.assertIn("Total", out)
        self.assertIn("0", out)

    def test_malformed_result_is_zeroed_not_raised(self):
        self.assertIn("Total", uu.format_summary(None))
        self.assertIn("Total", uu.format_summary("nonsense"))

    def test_no_money_figures_ever(self):
        # AC4: even if the endpoint smuggled a cost field, the renderer only reads
        # the four token classes + total + invocations, so no money can leak.
        dirty = dict(_SAMPLE)
        dirty["totals"] = dict(_SAMPLE["totals"], costUsd=1.23, totalCostUsd=4.56)
        dirty["groups"] = [dict(g, costUsd=9.9) for g in _SAMPLE["groups"]]
        out = uu.format_summary(dirty).lower()
        self.assertIsNone(re.search(r"cost|usd|dollar|price|\$", out))

    def test_team_scope_names_the_team(self):
        out = uu.format_summary(dict(_SAMPLE, scope="team", teamId="team-a"))
        self.assertIn("team team-a", out)


if __name__ == "__main__":
    unittest.main()


class GroupByAllowlist(unittest.TestCase):
    """#2137: which groupings the CLI offers.

    The tuple only shapes `--help` and the argument error — the rule is
    ENFORCED in orchestrator/usage-query.js (`AGENT_GROUP_BY`), because an agent
    can call the internal endpoint directly. What is asserted here is that the
    two agree, so the CLI never advertises something the server coerces away
    nor hides something it would happily answer."""

    def test_provider_tier_is_offered_and_user_is_not(self):
        self.assertIn("providerTier", uu.GROUP_BY)
        # The per-human breakdown is System-Admin-only (#801); a team-scoped
        # answer would hand one agent every colleague's spend.
        self.assertNotIn("user", uu.GROUP_BY)

    def test_every_pre_2137_grouping_still_works(self):
        for g in ("agent", "team", "model", "provider", "day"):
            self.assertIn(g, uu.GROUP_BY)

    def test_the_help_text_lists_exactly_the_offered_groupings(self):
        # The Options entry, not the usage examples (which also say --group-by).
        lines = [ln for ln in uu.__doc__.splitlines() if "--group-by <g>" in ln]
        self.assertEqual(len(lines), 1, "expected one --group-by <g> line in --help")
        for g in uu.GROUP_BY:
            self.assertIn(g, lines[0], f"{g} missing from --help")
        self.assertNotIn("user", lines[0])

    def test_the_cli_tuple_matches_the_servers_allowlist(self):
        # Read the enforcement side rather than restating it: a grouping added
        # on one side only is the drift this guards.
        src = (Path(__file__).resolve().parents[2]
               / "orchestrator" / "usage-query.js").read_text(encoding="utf-8")
        m = re.search(r"const AGENT_GROUP_BY = new Set\(\[([^\]]*)\]\)", src)
        self.assertIsNotNone(m, "could not read AGENT_GROUP_BY out of usage-query.js")
        server = [t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()]
        self.assertGreaterEqual(len(server), 5, "the regex has drifted")
        self.assertEqual(sorted(server), sorted(uu.GROUP_BY))
