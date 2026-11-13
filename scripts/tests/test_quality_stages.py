"""Tests for the quality stages: aee-quality-scan (pre-stage), aee-quality-audit (read-only) and their wrappers.

Run: python3 -I -m unittest discover -s scripts/tests -k Quality -v
The linters (ruff, pylint, shellcheck) and lizard are replaced by stub executables that print fixture output, so the
suite needs no tool installation. hadolint, pmd and checkstyle are deliberately absent (reported as MISSING).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SCAN = SCRIPTS / "aee-quality-scan.py"
AUDIT = SCRIPTS / "aee-quality-audit.py"
PRE = SCRIPTS / "quality_pre.sh"
AUDIT_SH = SCRIPTS / "quality_audit.sh"

PAY_PY = ("import os\n"                                       # 1  unused import
          "\n"                                                # 2
          "def charge(a, b, c, d, e, f, g, h):\n"             # 3  8 parameters
          "    try:\n"                                        # 4
          "        pass\n"                                    # 5
          "    except:\n"                                     # 6  bare except
          "        pass\n")                                   # 7
RUN_SH = "#!/bin/bash\ncd /tmp\ncat x | grep y\n"
DOCKERFILE = "FROM python:latest\nRUN apt-get install curl\n"
APP_JS = "function route(x) {\n  return x;\n}\n"
FILES = {"src/app/pay.py": PAY_PY, "src/ops/run.sh": RUN_SH, "src/Dockerfile": DOCKERFILE, "src/web/app.js": APP_JS,
         "src/third_party/v.py": "x = 1\n", "src/gen/api.py": "# DO NOT EDIT\nx = 1\n",
         "src/tests/fixtures/f.py": "x = 1\n", "src/notes.txt": "not code\n"}

STUB = """#!{python}
import os, sys
if sys.argv[1:2] == ["--version"]:
    print({version!r}); sys.exit(0)
sys.stdout.write(open(os.path.join(os.environ["STUB_DIR"], {name!r})).read())
"""
STUBS = {"ruff": ("ruff 9.9.9", "ruff.json"), "pylint": ("pylint 9.9.9", "pylint.json"),
         "shellcheck": ("version: 9.9.9", "shellcheck.json"), "lizard": ("1.17.10", "lizard.csv")}

SECRET = "AKIAABCDEFGHIJKLMNOP"


def lr(a, b=None):
    return {"start": a, "end": a if b is None else b}


class Base(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.bin, self.stubs = base / "repo", base / "bin", base / "stubs"
        for d in (self.root, self.bin, self.stubs):
            d.mkdir()
        for name, (version, fixture) in STUBS.items():
            f = self.bin / name
            f.write_text(STUB.format(python=sys.executable, version=version, name=fixture))
            f.chmod(0o755)
        ruff = [{"code": "F401", "message": "`os` imported but unused", "filename": str(self.root / "src/app/pay.py"),
                 "location": {"row": 1}, "end_location": {"row": 1}},
                {"code": "E722", "message": "Do not use bare `except`", "filename": "src/app/pay.py",
                 "location": {"row": 6}, "end_location": {"row": 6}},
                {"code": "S603", "message": "subprocess call", "filename": "src/app/pay.py",
                 "location": {"row": 3}, "end_location": {"row": 3}},
                {"code": "F401", "message": "unused", "filename": "src/gen/api.py",
                 "location": {"row": 2}, "end_location": {"row": 2}}]
        pylint = {"messages": [
            {"type": "warning", "messageId": "W0611", "path": "src/app/pay.py", "line": 1, "endLine": 1, "message": "Unused import os"},
            {"type": "warning", "messageId": "W0702", "path": "src/app/pay.py", "line": 6, "endLine": 6, "message": "No exception type(s) specified"},
            {"type": "error", "messageId": "E0401", "path": "src/app/pay.py", "line": 1, "message": "Unable to import"},
            {"type": "warning", "messageId": "W0123", "path": "src/app/pay.py", "line": 3, "message": "Use of eval"}]}
        shell = [{"file": "src/ops/run.sh", "line": 2, "endLine": 2, "code": 2164, "level": "warning", "message": "Use cd ... || exit"},
                 {"file": "src/ops/run.sh", "line": 3, "endLine": 3, "code": 2002, "level": "style", "message": "Useless cat"}]
        lizard = ('3,2,20,8,5,"charge@3-7@src/app/pay.py","src/app/pay.py","charge","charge( a )",3,7,1\n'
                  '25,20,100,2,30,"route@1-3@src/web/app.js","src/web/app.js","route","route( x )",1,3,2\n')
        (self.stubs / "ruff.json").write_text(json.dumps(ruff))
        (self.stubs / "pylint.json").write_text(json.dumps(pylint))
        (self.stubs / "shellcheck.json").write_text(json.dumps(shell))
        (self.stubs / "lizard.csv").write_text(lizard)
        for rel, text in FILES.items():
            self.write(rel, text)
        rows = [{"file": f, "hash": "x", "language": "x", "component": "c", "batch": "b1", "skipped": False,
                 "loc": {"code": 5, "comments": 0, "blanks": 0}} for f in FILES]
        self.write(".aee/intake.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        self.write(".aee/changed-files.json", json.dumps({"sourceFolder": "src/"}))
        self.work = self.root / ".aee/quality-work"

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def env(self, path=None):
        return {**os.environ, "PATH": path or f"{self.bin}:/usr/bin:/bin", "STUB_DIR": str(self.stubs),
                "PYTHON": sys.executable, "AGENT_ID": "test", **self.extra_env}

    def py(self, script, *args, path=None):
        p = subprocess.run([sys.executable, "-I", str(script), *args], cwd=self.root, capture_output=True, text=True,
                           env=self.env(path))
        return p.returncode, p.stdout.strip()

    def scan(self, *args, **kw):
        return self.py(SCAN, "--raw-dir", str(Path(self.tmp.name) / "raw"), *args, **kw)

    def audit(self, *args):
        return self.py(AUDIT, *args)

    def j(self, rel):
        return json.loads((self.work / rel).read_text())

    def put(self, rel, data):
        (self.work / rel).write_text(json.dumps(data))

    def cdoc(self, rel):
        return self.j(f"candidates/{rel}.json")

    def findings_path(self, rel):
        return self.root / f".aee/quality/{rel}.findings.json"

    def log_rows(self):
        return [json.loads(l) for l in (self.root / ".aee/quality-log.jsonl").read_text().splitlines()]

    def set_log(self, counts):
        rows = self.log_rows()
        for r in rows:
            n, high, med = counts.get(r["file"], (0, 0, 0))
            r["findingCount"], r["highCount"], r["mediumCount"] = n, high, med
        (self.root / ".aee/quality-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def write_good_output(self):
        """What the analyzer agent writes after the pre-stage: findings, log counts and the triage."""
        pay, run, app = (self.cdoc(f"src/{n}") for n in ("app/pay.py", "ops/run.sh", "web/app.js"))
        t = {c["canonical"]: c for c in pay["toolCandidates"]}
        outs = {
            "src/app/pay.py": [
                {"file": "src/app/pay.py", "tool": "ruff,pylint", "ruleId": "F401", "lineRange": lr(1),
                 "title": "Unused import os", "detail": "The import is never used and adds noise; remove the import line.",
                 "category": "dead-code", "severity": "low", "confidence": "confirmed", "evidenceQuote": "import os"},
                {"file": "src/app/pay.py", "tool": "lizard", "ruleId": "params", "lineRange": lr(3, 7),
                 "title": "charge takes too many parameters",
                 "detail": "charge has 8 parameters (threshold 6); group them into one object.",
                 "category": "complexity", "severity": "medium", "confidence": "confirmed"},
                {"file": "src/app/pay.py", "tool": "ruff,pylint", "ruleId": "E722", "lineRange": lr(6),
                 "title": "Bare except hides failures", "detail": "A bare except swallows every error; catch the specific exception.",
                 "category": "error-handling", "severity": "medium", "confidence": "confirmed", "evidenceQuote": "except:"}],
            "src/ops/run.sh": [
                {"file": "src/ops/run.sh", "tool": "shellcheck", "ruleId": "SC2164", "lineRange": lr(2),
                 "title": "cd without error handling", "detail": "A failed cd continues in the wrong directory; use cd ... || exit.",
                 "category": "shell-hygiene", "severity": "low", "confidence": "confirmed"}],
            "src/web/app.js": [
                {"file": "src/web/app.js", "tool": "lizard", "ruleId": "ccn", "lineRange": lr(1, 3),
                 "title": "route is too complex", "detail": "route has CCN 20 (threshold 15); split the branches into helpers.",
                 "category": "complexity", "severity": "medium", "confidence": "confirmed"}],
            "src/Dockerfile": [
                {"file": "src/Dockerfile", "tool": "manual", "ruleId": None, "lineRange": lr(1),
                 "title": "Base image uses the latest tag", "detail": "The latest tag makes builds irreproducible; pin an exact version.",
                 "category": "container-hygiene", "severity": "low", "confidence": "confirmed", "evidenceQuote": "FROM python:latest"}]}
        for rel, items in outs.items():
            self.findings_path(rel).write_text(json.dumps(items))
        self.set_log({"src/app/pay.py": (3, 0, 2), "src/ops/run.sh": (1, 0, 0), "src/web/app.js": (1, 0, 1),
                      "src/Dockerfile": (1, 0, 0)})
        sc = {c["ruleIds"][0]: c for c in run["toolCandidates"]}
        triage = {"addressedVerdictRound": 0, "files": {
            "src/app/pay.py": {"candidates": [
                {"id": t["unused-import"]["id"], "action": "keep", "line": 1, "ruleId": "F401"},
                {"id": t["bare-except"]["id"], "action": "keep", "line": 6, "ruleId": "E722"},
                {"id": pay["metricCandidates"][0]["id"], "action": "keep", "line": 3, "ruleId": "params"}]},
            "src/ops/run.sh": {"candidates": [
                {"id": sc["SC2164"]["id"], "action": "keep", "line": 2, "ruleId": "SC2164"},
                {"id": sc["SC2002"]["id"], "action": "drop", "reason": "the useless cat is harmless in this one-off script"}]},
            "src/web/app.js": {"candidates": [{"id": app["metricCandidates"][0]["id"], "action": "keep", "line": 1, "ruleId": "ccn"}]}},
            "reviewedWithoutCandidates": ["src/Dockerfile"]}
        self.put("triage.json", triage)
        return outs, triage


class QualityScanTest(Base):
    def test_files_roles_and_skips(self):
        code, out = self.scan()
        self.assertEqual(code, 0, out)
        files = {f["file"]: f for f in self.j("files.json")["files"]}
        self.assertNotIn("src/notes.txt", files)                              # not a quality target
        self.assertEqual({f for f, x in files.items() if x["analyze"]},
                         {"src/app/pay.py", "src/ops/run.sh", "src/Dockerfile", "src/web/app.js"})
        self.assertEqual(files["src/third_party/v.py"]["skipReason"], "role vendored")
        self.assertEqual(files["src/gen/api.py"]["skipReason"], "role generated")
        self.assertEqual(files["src/tests/fixtures/f.py"]["skipReason"], "role fixture")
        self.assertEqual({k: v["language"] for k, v in files.items() if v["analyze"]},
                         {"src/app/pay.py": "python", "src/ops/run.sh": "shell", "src/Dockerfile": "dockerfile",
                          "src/web/app.js": "javascript"})

    def test_hits_are_merged_clustered_and_pre_triaged(self):
        self.scan()
        doc = self.cdoc("src/app/pay.py")
        cands = {c["canonical"]: c for c in doc["toolCandidates"]}
        self.assertEqual(sorted(cands), ["bare-except", "unused-import"])      # S603, W0123 and E0401 are gone
        self.assertEqual((cands["unused-import"]["tools"], cands["unused-import"]["ruleIds"]),
                         (["pylint", "ruff"], ["F401", "W0611"]))              # ruff and pylint merged into one candidate
        self.assertEqual(cands["bare-except"]["ruleIds"], ["E722", "W0702"])
        self.assertEqual(doc["dropped"], {"security_scope": 2, "env_noise": 1})
        self.assertEqual([c["id"] for c in doc["toolCandidates"]], ["T1", "T2"])
        self.assertEqual(doc["toolsRun"], ["ruff", "pylint", "lizard"])
        self.assertEqual(doc["toolsMissing"], [])

    def test_repeated_hits_of_one_rule_become_one_cluster(self):
        ruff = json.loads((self.stubs / "ruff.json").read_text())
        for row in (3, 4, 5):
            ruff.append({"code": "PLR2004", "message": "magic value", "filename": "src/app/pay.py",
                         "location": {"row": row}, "end_location": {"row": row}})
        (self.stubs / "ruff.json").write_text(json.dumps(ruff))
        self.scan()
        c = [x for x in self.cdoc("src/app/pay.py")["toolCandidates"] if x["canonical"] == "magic-value"][0]
        self.assertEqual((c["count"], c["line"], [o["line"] for o in c["occurrences"]]), (3, 3, [3, 4, 5]))

    def test_metric_candidates_come_from_lizard_and_the_thresholds(self):
        self.scan()
        m = self.cdoc("src/app/pay.py")["metricCandidates"]
        self.assertEqual([(x["id"], x["metric"], x["value"], x["threshold"], x["function"]) for x in m],
                         [("M1", "params", 8, 6, "charge")])
        js = self.cdoc("src/web/app.js")
        self.assertEqual([(x["metric"], x["value"]) for x in js["metricCandidates"]], [("ccn", 20)])
        self.assertEqual(self.j("files.json")["thresholds"]["ccn"], 15)
        self.write(".aee/quality-config.json", json.dumps({"thresholds": {"params": 10, "ccn": 25}}))
        self.scan()
        self.assertEqual(self.cdoc("src/app/pay.py")["metricCandidates"], [])
        self.assertEqual(self.cdoc("src/web/app.js")["metricCandidates"], [])

    def test_shell_findings_and_missing_tools_are_reported_never_clean(self):
        self.scan()
        sc = self.cdoc("src/ops/run.sh")
        self.assertEqual(sorted(r for c in sc["toolCandidates"] for r in c["ruleIds"]), ["SC2002", "SC2164"])
        status = self.j("tool_status.json")["tools"]
        self.assertEqual({k: v["status"] for k, v in status.items()},
                         {"ruff": "OK", "pylint": "OK", "shellcheck": "OK", "hadolint": "MISSING", "lizard": "OK"})
        self.assertEqual(self.cdoc("src/Dockerfile")["toolsMissing"], ["hadolint"])
        warnings = (self.root / ".aee/quality-warnings.md").read_text()
        self.assertIn("Tool `hadolint` is MISSING", warnings)
        self.assertIn("javascript: lizard metrics and the manual pass only", warnings)
        self.assertIn("src/gen/api.py: role generated", warnings)

    def test_a_failing_tool_is_reported_failed(self):
        (self.stubs / "shellcheck.json").write_text("not json at all")
        self.scan()
        self.assertEqual(self.j("tool_status.json")["tools"]["shellcheck"]["status"], "FAILED")
        self.assertEqual(self.cdoc("src/ops/run.sh")["toolCandidates"], [])
        self.assertEqual(self.cdoc("src/ops/run.sh")["toolsMissing"], ["shellcheck"])

    def test_skeleton_findings_and_log(self):
        self.scan()
        for rel in ("src/app/pay.py", "src/ops/run.sh", "src/Dockerfile", "src/web/app.js"):
            self.assertEqual(json.loads(self.findings_path(rel).read_text()), [])
        self.assertFalse(self.findings_path("src/gen/api.py").exists())       # skipped files get no skeleton
        rows = {r["file"]: r for r in self.log_rows()}
        self.assertEqual(sorted(rows), ["src/Dockerfile", "src/app/pay.py", "src/ops/run.sh", "src/web/app.js"])
        self.assertEqual((rows["src/app/pay.py"]["maxComplexity"], rows["src/app/pay.py"]["toolsRun"],
                          rows["src/app/pay.py"]["lines"], rows["src/app/pay.py"]["findingCount"]),
                         (2, ["ruff", "pylint", "lizard"], 5, 0))
        self.assertEqual(rows["src/Dockerfile"]["maxComplexity"], None)
        self.assertEqual(rows["src/Dockerfile"]["toolsRun"], [])

    def test_a_rule_that_fires_in_many_files_becomes_a_rule_group_but_errors_do_not(self):
        ruff = json.loads((self.stubs / "ruff.json").read_text())
        for n in ("a", "b", "c"):
            self.write(f"src/m/{n}.py", "import x\n")
            ruff.append({"code": "PLR2004", "message": "magic value", "filename": f"src/m/{n}.py",
                         "location": {"row": 1}, "end_location": {"row": 1}})
            ruff.append({"code": "E999", "message": "syntax error", "filename": f"src/m/{n}.py",
                         "location": {"row": 1}, "end_location": {"row": 1}})
        (self.stubs / "ruff.json").write_text(json.dumps(ruff))
        rows = (self.root / ".aee/intake.jsonl").read_text()
        self.write(".aee/intake.jsonl", rows + "".join(json.dumps({"file": f"src/m/{n}.py", "batch": "b1", "skipped": False}) + "\n"
                                                      for n in ("a", "b", "c")))
        self.extra_env = {"GROUP_MIN": "3"}
        self.scan()
        groups = self.j("files.json")["ruleGroups"]
        self.assertEqual([(g["id"], g["rule"], g["count"], g["files"]) for g in groups], [("R01", "magic-value", 3, 3)])
        self.assertEqual(sorted(groups[0]["ids"]), [f"src/m/{n}.py#T1" for n in ("a", "b", "c")])
        member = [c for c in self.cdoc("src/m/a.py")["toolCandidates"] if c["canonical"] == "magic-value"][0]
        self.assertEqual(member["ruleGroup"], "R01")
        syntax = [c for c in self.cdoc("src/m/a.py")["toolCandidates"] if c["canonical"] == "ruff:E999"][0]
        self.assertNotIn("ruleGroup", syntax)                                 # error-severity candidates stay individual
        self.extra_env = {}
        self.scan()
        self.assertEqual(self.j("files.json")["ruleGroups"], [])

    def test_only_the_last_batch_and_skipped_lines_are_analysed_and_explicit_files_work(self):
        rows = [json.loads(l) for l in (self.root / ".aee/intake.jsonl").read_text().splitlines()]
        for r in rows:
            r["batch"] = "old"
            r["skipped"] = False
        rows.append({"file": "src/app/pay.py", "batch": "b2", "skipped": False})
        rows.append({"file": "src/web/app.js", "batch": "b2", "skipped": True})
        self.write(".aee/intake.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        self.scan()
        self.assertEqual([f["file"] for f in self.j("files.json")["files"]], ["src/app/pay.py"])
        self.scan("src/ops/run.sh")
        self.assertEqual([f["file"] for f in self.j("files.json")["files"]], ["src/ops/run.sh"])

    def test_pass_without_ledger_and_a_new_scan_clears_triage_and_verdict(self):
        (self.root / ".aee/intake.jsonl").unlink()
        self.assertEqual(self.scan(), (10, "[PASS]"))
        self.write(".aee/intake.jsonl", json.dumps({"file": "src/app/pay.py", "batch": "b", "skipped": False}) + "\n")
        self.scan()
        self.write(".aee/quality-work/triage.json", "{}")
        self.write(".aee/quality-work/verdict.json", "{}")
        self.write(".aee/quality/src/app/pay.py.findings.json", '[{"x": 1}]')
        self.scan()
        self.assertFalse((self.work / "triage.json").exists())
        self.assertFalse((self.work / "verdict.json").exists())
        self.assertEqual(json.loads(self.findings_path("src/app/pay.py").read_text()), [])


class QualityAuditTest(Base):
    def setUp(self):
        super().setUp()
        self.scan()

    def issues(self, severity="error"):
        rep = json.loads(self.audit("--json")[1])
        return [i for i in rep["issues"] if i["severity"] == severity]

    def codes(self, severity="error"):
        return sorted({i["code"] for i in self.issues(severity)})

    def messages(self, code, severity="error"):
        return " | ".join(i["message"] for i in self.issues(severity) if i["code"] == code)

    def test_good_output_passes_and_the_audit_changes_nothing(self):
        self.write_good_output()
        before = {str(p): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}
        code, out = self.audit()
        self.assertEqual(code, 0, out)
        self.assertIn("AUDIT: PASS", out)
        self.assertEqual({str(p): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}, before)

    def test_the_untouched_skeleton_fails_because_nothing_is_triaged(self):
        code, _ = self.audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.codes(), ["Q05"])
        self.assertIn("triage.json is missing", self.messages("Q05"))

    def test_every_candidate_and_every_file_must_be_accounted_for(self):
        outs, triage = self.write_good_output()
        triage["files"]["src/ops/run.sh"]["candidates"] = triage["files"]["src/ops/run.sh"]["candidates"][:1]
        triage["reviewedWithoutCandidates"] = []
        self.put("triage.json", triage)
        msgs = self.messages("Q05")
        self.assertIn("run.sh#T2 has no decision", msgs)
        self.assertIn("src/Dockerfile", " ".join(i["file"] or "" for i in self.issues()))
        self.assertIn("not listed in `reviewedWithoutCandidates`", msgs)

    def test_drops_need_a_reason_and_keeps_must_point_at_a_finding(self):
        outs, triage = self.write_good_output()
        triage["files"]["src/ops/run.sh"]["candidates"][1]["reason"] = "ok"
        triage["files"]["src/app/pay.py"]["candidates"][0]["line"] = 99
        self.put("triage.json", triage)
        msgs = self.messages("Q05")
        self.assertIn("dropped without an adequate reason", msgs)
        self.assertIn("does not point at a finding", msgs)
        self.assertIn("not the target of any keep", msgs)                     # the F401 finding lost its keep

    def test_tool_backed_findings_must_match_the_candidates_and_the_tools_that_ran(self):
        outs, _ = self.write_good_output()
        base = outs["src/app/pay.py"][0]
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(base, ruleId="E999")]))
        self.assertIn("no pre-stage candidate reports rule 'E999'", self.messages("Q03"))
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(base, tool="hadolint")]))
        self.assertIn("did not complete", self.messages("Q03"))
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(base, tool="ruff,nosuchtool")]))
        self.assertIn("unknown tool", self.messages("Q03"))
        metric = outs["src/app/pay.py"][1]
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(metric, ruleId="ccn")]))
        self.assertIn("no metric candidate 'ccn'", self.messages("Q03"))

    def test_manual_findings_need_an_exact_evidence_quote_and_no_rule(self):
        outs, _ = self.write_good_output()
        manual = outs["src/Dockerfile"][0]
        for change, text in [({"evidenceQuote": "FROM python:3.12"}, "does not appear in the cited lines"),
                             ({"evidenceQuote": ""}, "empty evidenceQuote"),
                             ({"ruleId": "DL3007"}, "ruleId null"),
                             ({"tool": "manual,ruff"}, "tool exactly `manual`")]:
            self.findings_path("src/Dockerfile").write_text(json.dumps([dict(manual, **change)]))
            self.assertIn(text, self.messages("Q03"), change)
        spaced = dict(manual, evidenceQuote="FROM   python:latest")           # whitespace-insensitive
        self.findings_path("src/Dockerfile").write_text(json.dumps([spaced]))
        self.assertEqual(self.messages("Q03"), "")

    def test_complexity_numbers_must_equal_lizard_and_the_severity_follows_the_definition(self):
        outs, _ = self.write_good_output()
        metric = outs["src/app/pay.py"][1]
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(metric, detail="charge has 12 parameters (threshold 6); split it.")]))
        self.assertIn("params 12 does not match the lizard metrics", self.messages("Q04"))
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(metric, severity="low")]))
        self.assertIn("should be medium by the severity definitions", self.messages("Q04", "warning"))
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(metric, detail="charge is far too complex and should be split.")]))
        self.assertIn("does not quote lizard numbers", self.messages("Q04", "warning"))

    def test_structure_values_security_scope_secrets_and_suppressions(self):
        outs, _ = self.write_good_output()
        base = outs["src/app/pay.py"][0]
        cases = [({"lineRange": lr(99)}, "Q02", "invalid for a"), ({"title": "x" * 121}, "Q02", "1-120"),
                 ({"severity": "urgent"}, "Q02", "severity"), ({"category": "security"}, "Q02", "no security category"),
                 ({"ruleId": "S603"}, "Q02", "Security Analyzer"),
                 ({"detail": "The api_key = abcd1234efgh is used here and should not be."}, "Q02", "secret-like"),
                 ({"detail": f"A token {SECRET} appears in this unused import line, remove it."}, "Q02", "secret-like")]
        for change, code, text in cases:
            self.findings_path("src/app/pay.py").write_text(json.dumps([dict(base, **change)]))
            self.assertIn(text, self.messages(code), change)
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(base, extra="no")]))
        self.assertIn("nine contract keys", self.messages("Q01"))
        self.findings_path("src/app/pay.py").write_text("{nope")
        self.assertIn("missing or not valid JSON", self.messages("Q01"))
        self.findings_path("src/app/pay.py").write_text(json.dumps([dict(base, detail="The import is unused; add # noqa to silence it quickly.")]))
        self.assertIn("suppressing", self.messages("Q02", "warning"))

    def test_log_rows_must_match_findings_lizard_tools_and_lines(self):
        self.write_good_output()
        rows = self.log_rows()
        rows[0]["findingCount"] = 9
        rows[1]["maxComplexity"] = 99
        rows[2]["toolsRun"] = ["ruff"]
        rows[3]["lines"] = 1
        (self.root / ".aee/quality-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows[:4]))
        msgs = self.messages("Q06")
        for expected in ("do not match the findings", "maxComplexity 99 differs", "toolsRun", "lines 1 differs"):
            self.assertIn(expected, msgs)

    def test_skipped_files_must_not_have_findings_and_noise_is_an_error(self):
        outs, _ = self.write_good_output()
        self.findings_path("src/gen/api.py").parent.mkdir(parents=True, exist_ok=True)
        self.findings_path("src/gen/api.py").write_text("[]")
        self.assertIn("skipped file (role generated) has a findings file", self.messages("Q01"))
        base = outs["src/app/pay.py"][0]
        self.findings_path("src/app/pay.py").write_text(json.dumps([base, dict(base, title="again")]))
        self.assertIn("overlapping findings", self.messages("Q07"))
        many = [dict(base, lineRange=lr(1 + i % 7), ruleId=f"X{i}", category="style") for i in range(26)]
        self.findings_path("src/app/pay.py").write_text(json.dumps(many))
        self.assertIn("noise", self.messages("Q07"))

    def grouped_output(self):
        """Three files with the same non-error rule, GROUP_MIN=3: the group is dropped in one decision."""
        ruff = json.loads((self.stubs / "ruff.json").read_text())
        for n in ("a", "b", "c"):
            self.write(f"src/m/{n}.py", "import x\n")
            ruff.append({"code": "PLR2004", "message": "magic value", "filename": f"src/m/{n}.py",
                         "location": {"row": 1}, "end_location": {"row": 1}})
        (self.stubs / "ruff.json").write_text(json.dumps(ruff))
        rows = (self.root / ".aee/intake.jsonl").read_text()
        self.write(".aee/intake.jsonl", rows + "".join(json.dumps({"file": f"src/m/{n}.py", "batch": "b1", "skipped": False}) + "\n"
                                                      for n in ("a", "b", "c")))
        self.extra_env = {"GROUP_MIN": "3"}
        self.scan()
        outs, triage = self.write_good_output()
        group = self.j("files.json")["ruleGroups"][0]
        triage["groups"] = [{"group": group["id"], "action": "drop", "sampled": group["ids"],
                             "reason": "formatting-only rule with no effect on maintainability, as in src/m/a.py:L1"}]
        triage["reviewedWithoutCandidates"] = ["src/Dockerfile"]
        self.put("triage.json", triage)
        return triage, group

    def test_a_rule_group_can_be_dropped_in_one_decision_and_is_checked(self):
        triage, group = self.grouped_output()
        self.assertEqual(self.codes(), [])
        self.assertEqual(self.audit()[0], 0)
        entry = triage["groups"][0]
        cases = [({"sampled": group["ids"][:1]}, "must list at least 3"), ({"reason": "noise"}, "need 40"),
                 ({"action": "keep"}, "only a drop can cover a group"), ({"group": "R99"}, "not a rule group"),
                 ({"except": ["src/zzz.py#T1"]}, "not in the group")]
        for change, text in cases:
            self.put("triage.json", dict(triage, groups=[dict(entry, **change)]))
            self.assertIn(text, self.messages("Q05"), change)
        self.put("triage.json", dict(triage, groups=[dict(entry, **{"except": [group["ids"][0]]})]))
        self.assertIn(f"{group['ids'][0]} has no decision", self.messages("Q05"))   # an excepted member needs its own decision

    def test_approval_is_required_on_request(self):
        self.write_good_output()
        self.assertEqual(self.audit("--require-approval")[0], 1)
        self.put("verdict.json", {"round": 1, "verdict": "revise", "reviewed": {},
                                  "issues": [{"type": "severity", "file": "src/app/pay.py", "line": 1, "message": "m" * 20, "fix": "f"}]})
        self.assertEqual(self.audit("--require-approval")[0], 1)
        self.put("verdict.json", {"round": 1, "verdict": "approve", "reviewed": {}, "issues": []})
        self.assertEqual(self.audit("--require-approval")[0], 0)

    def test_exit_code_2_when_the_pre_stage_did_not_complete(self):
        (self.work / "files.json").unlink()
        code, out = self.audit()
        self.assertEqual(code, 2)
        self.assertIn("pre-stage did not complete", out)


@unittest.skipUnless(shutil.which("git"), "git is required")
class QualityWrapperTest(Base):
    """quality_pre.sh (last script before the agents) and quality_audit.sh (read-only, for the next stage)."""

    def setUp(self):
        super().setUp()
        self.remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "agent_workspace", str(self.remote)], check=True)
        self.git("init", "-q", "-b", "agent_workspace")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "intake")
        self.git("push", "-q", "origin", "agent_workspace")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def sh(self, script, *args, path=None):
        env = self.env(path or f"{self.bin}:{SCRIPTS}:/usr/bin:/bin:/opt/homebrew/bin")
        p = subprocess.run(["bash", str(script), *args], cwd=self.root, capture_output=True, text=True, env=env)
        out = p.stdout.strip().splitlines()
        return p.returncode, out[-1] if out else "", p.stdout

    def remote_files(self):
        return subprocess.run(["git", "-C", str(self.remote), "show", "--name-only", "--format=%s", "agent_workspace"],
                              capture_output=True, text=True).stdout

    def agent_commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        self.git("push", "-q", "origin", "agent_workspace")

    def test_the_pre_stage_pushes_candidates_and_skeleton_and_registers_only_the_index(self):
        code, last, out = self.sh(PRE)
        self.assertEqual(code, 0, out)
        files = self.remote_files()
        self.assertIn("chore(quality): scan results, candidates and output skeleton", files)
        for expected in (".aee/quality-work/files.json", ".aee/quality-work/candidates/src/app/pay.py.json",
                         ".aee/quality/src/app/pay.py.findings.json", ".aee/quality-log.jsonl", ".aee/quality-warnings.md"):
            self.assertIn(expected, files)
        names = [json.loads(l)["artifact"] for l in (self.root / ".agent-studio/artifacts.jsonl").read_text().splitlines()]
        self.assertEqual(names, ["Quality Candidates"])                       # Quality:<file> is registered by the reviewer

    def test_the_audit_is_read_only_and_follows_the_agents_through_git(self):
        self.assertEqual(self.sh(PRE)[0], 0)
        code, last, _ = self.sh(AUDIT_SH, "--no-sync")
        self.assertEqual((code, last), (1, "[FAIL] the quality output has errors; the problems above go back to the analyzer or reviewer"))
        self.write_good_output()
        self.agent_commit("analyzer: findings, log and triage")
        self.assertEqual(self.sh(AUDIT_SH)[:2], (0, "[PASS] the quality output is valid"))
        self.assertEqual(self.sh(AUDIT_SH, "--require-approval")[0], 1)
        self.put("verdict.json", {"round": 1, "verdict": "approve", "reviewed": {}, "issues": []})
        self.agent_commit("reviewer: verdict")
        before = self.git("rev-parse", "HEAD")
        self.assertEqual(self.sh(AUDIT_SH, "--require-approval")[:2], (0, "[PASS] the quality output is valid"))
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("status", "--short").strip(), "")

    def test_audit_reports_a_missing_pre_stage_and_bad_options(self):
        shutil.rmtree(self.root / ".aee/quality-work", ignore_errors=True)
        self.assertEqual(self.sh(AUDIT_SH, "--no-sync")[:2], (2, "[ERROR] the pre-stage did not complete"))
        self.assertEqual(self.sh(AUDIT_SH, "--bogus")[0], 2)

    def test_local_pre_stage_without_publish_does_not_touch_git(self):
        head = self.git("rev-parse", "HEAD")
        code, last, out = self.sh(PRE, "--no-sync", "--no-publish")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertTrue((self.work / "files.json").is_file())
        self.assertEqual(self.sh(PRE, "--no-sync", "--no-publish", "--bogus")[0], 1)
        (self.root / ".aee/intake.jsonl").unlink()
        self.assertEqual(self.sh(PRE, "--no-sync", "--no-publish")[:2], (0, "[PASS] No intake ledger (.aee/intake.jsonl), nothing to analyse"))


if __name__ == "__main__":
    unittest.main()
