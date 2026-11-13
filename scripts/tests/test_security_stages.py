"""Tests for the security stages: aee-security-scan (pre-stage), aee-security-audit (read-only) and their wrappers.

Run: python3 -I -m unittest discover -s scripts/tests -k Security -v
The scanners (semgrep, bandit, gitleaks) are replaced by small stub executables that write fixture
output, so the suite needs no network and no scanner installation.
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
SCAN = SCRIPTS / "aee-security-scan.py"
AUDIT = SCRIPTS / "aee-security-audit.py"
PRE = SCRIPTS / "security_pre.sh"
AUDIT_SH = SCRIPTS / "security_audit.sh"

SECRET = "AKIAABCDEFGHIJKLMNOP"
A_PY = ('import subprocess, pickle\n'            # 1
        'from flask import request\n'            # 2
        '\n'                                     # 3
        '@app.route("/ping")\n'                  # 4
        'def ping():\n'                          # 5
        '    host = request.form["host"]\n'      # 6
        '    subprocess.run(host, shell=True)\n'  # 7
        '    return "ok"\n'                      # 8
        '\n\n\n'                                 # 9-11
        'def load(blob):\n'                      # 12
        '    return pickle.loads(blob)\n')       # 13
B_PY = f'API_KEY = "{SECRET}"\n'
D_PY = f'OLD_KEY = "{SECRET}"\n'
FILES = {"src/a.py": A_PY, "src/b.py": B_PY, "src/d.py": D_PY}

STUB = """#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
d = os.environ["STUB_DIR"]
if args[:1] == ["--version"] or args[:1] == ["version"]:
    print({version!r}); sys.exit(0)
def arg(flag):
    return args[args.index(flag) + 1]
{body}
"""
STUBS = {
    "semgrep": ("1.2.3", 'shutil.copy(os.path.join(d, "semgrep.json"), arg("-o"))'),
    "bandit": ("bandit 1.2.3", 'shutil.copy(os.path.join(d, "bandit.json"), arg("-o"))'),
    "gitleaks": ("8.0.0", '''stage = args[-1]
rows = json.load(open(os.path.join(d, "gitleaks.json")))
for r in rows: r["File"] = stage + "/" + r["File"]
json.dump(rows, open(arg("--report-path"), "w"))'''),
}
SEMGREP = {"results": [{"check_id": "python.subprocess-injection", "path": "src/a.py", "start": {"line": 7},
                        "end": {"line": 7}, "extra": {"severity": "ERROR", "message": "user input enters subprocess",
                        "metadata": {"cwe": ["CWE-78: OS Command Injection"], "owasp": ["A03:2021 - Injection"]}}}],
           "errors": []}
BANDIT = {"results": [{"filename": "src/a.py", "line_number": 7, "line_range": [7], "test_id": "B602",
                       "test_name": "subprocess_shell_true", "issue_severity": "HIGH",
                       "issue_text": "shell=True", "issue_cwe": {"id": 78}}]}
GITLEAKS = [{"File": "src/b.py", "StartLine": 1, "EndLine": 1, "RuleID": "aws-access-token", "Description": "AWS key"},
            {"File": "src/d.py", "StartLine": 1, "EndLine": 1, "RuleID": "aws-access-token", "Description": "AWS key"}]


def kb(root, file, language="Python", surface=(), warnings=()):
    import hashlib
    return {"file": file, "hash": hashlib.sha256((Path(root) / file).read_bytes()).hexdigest(), "language": language,
            "component": "app", "package": None, "symbols": [], "referencedSymbols": [], "dependencies": [],
            "sideEffects": [], "securitySurface": list(surface), "injection_flags": [], "warnings": list(warnings)}


def lr(a, b=None):
    return {"start": a, "end": a if b is None else b}


A_SURFACE = [{"kind": "entry-point", "category": "http-route", "description": "HTTP route handler ping", "lineRange": lr(4, 5)},
             {"kind": "entry-point", "category": "request-data", "description": "reads request.form", "lineRange": lr(6, 7)},
             {"kind": "sink", "category": "command-exec", "description": "call to subprocess.run", "lineRange": lr(7)},
             {"kind": "sink", "category": "deserialization", "description": "call to pickle.loads", "lineRange": lr(13)}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.stubs = Path(self.tmp.name) / "stubs"
        self.stubs.mkdir()
        for name, (version, body) in STUBS.items():
            f = self.bin / name
            f.write_text(STUB.format(python=sys.executable, version=version, body=body))
            f.chmod(0o755)
        self.fixture("semgrep", SEMGREP)
        self.fixture("bandit", BANDIT)
        self.fixture("gitleaks", GITLEAKS)
        for rel, text in FILES.items():
            self.write(rel, text)
        self.write(".aee/kb/src/a.py.json", json.dumps(kb(self.root, "src/a.py", surface=A_SURFACE)))
        self.write(".aee/kb/src/b.py.json", json.dumps(kb(self.root, "src/b.py")))
        self.write(".aee/kb/src/d.py.json", json.dumps(kb(self.root, "src/d.py")))
        self.write(".aee/extract-log.jsonl", "".join(
            json.dumps({"file": f, "batch": "b1", "hash": "x"}) + "\n" for f in FILES))
        self.work = self.root / ".aee/security-work"

    def fixture(self, name, data):
        (self.stubs / f"{name}.json").write_text(json.dumps(data))

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    extra_env = {}

    def env(self, path=None):
        return {**os.environ, "PATH": path or f"{self.bin}:/usr/bin:/bin", "STUB_DIR": str(self.stubs),
                "PYTHON": sys.executable, "AGENT_ID": "test", **self.extra_env}

    def py(self, script, *args, path=None):
        p = subprocess.run([sys.executable, "-I", str(script), *args], cwd=self.root, capture_output=True,
                           text=True, env=self.env(path))
        return p.returncode, p.stdout.strip()

    def scan(self, *args, **kw):
        return self.py(SCAN, "--raw-dir", str(Path(self.tmp.name) / "raw"), *args, **kw)

    def audit(self, *args):
        return self.py(AUDIT, *args)

    def j(self, rel):
        return json.loads((self.work / rel).read_text())

    def put(self, rel, data):
        (self.work / rel).write_text(json.dumps(data))

    def candidates(self):
        return {c["file"]: c for c in self.j("candidates.json")["candidates"]}

    def finding(self, rel, line, cwe, owasp, tool, rule, detail, severity="high", confidence="probable"):
        return {"file": rel, "tool": tool, "ruleId": rule, "lineRange": {"start": line, "end": line},
                "title": f"Finding in {rel}", "detail": detail, "owaspCategory": owasp, "cweId": cwe,
                "severity": severity, "confidence": confidence}

    def add_noisy_files(self, names=("e", "f", "g"), strong=("h",), rule="c.insecure-copy"):
        """Plain files that all trigger the same semgrep rule (medium), plus strong ones (high) with that rule."""
        results = json.loads((self.stubs / "semgrep.json").read_text())
        log = []
        for n in list(names) + list(strong):
            rel = f"src/{n}.py"
            self.write(rel, "x = 1\n")
            self.write(f".aee/kb/{rel}.json", json.dumps(kb(self.root, rel)))
            log.append(json.dumps({"file": rel, "batch": "b1", "hash": "x"}) + "\n")
            sev = "ERROR" if n in strong else "WARNING"
            results["results"].append({"check_id": rule, "path": rel, "start": {"line": 1}, "end": {"line": 1},
                                       "extra": {"severity": sev, "message": "generic copy rule",
                                                 "metadata": {"cwe": ["CWE-676: Dangerous Function"]}}})
        self.fixture("semgrep", results)
        with (self.root / ".aee/extract-log.jsonl").open("a") as fh:
            fh.write("".join(log))

    def write_good_output(self):
        """What the analyzer agent writes: final findings, log counts and the triage (after the pre-stage)."""
        c = self.candidates()
        outs = {"src/a.py": [self.finding("src/a.py", 7, "CWE-78", "A03 Injection", "semgrep,bandit",
                                          "python.subprocess-injection",
                                          "src/a.py:L7 passes the request value to subprocess.run with shell=True.",
                                          confidence="confirmed")],
                "src/b.py": [self.finding("src/b.py", 1, "CWE-798", "A07 Identification and Authentication Failures",
                                          "gitleaks", "aws-access-token",
                                          "src/b.py:L1 holds a credential; the same secret also appears in src/d.py.")],
                "src/d.py": [self.finding("src/d.py", 1, "CWE-798", "A07 Identification and Authentication Failures",
                                          "gitleaks", "aws-access-token",
                                          "src/d.py:L1 holds a credential; the same secret also appears in src/b.py.")]}
        for rel, items in outs.items():
            (self.root / f".aee/security/{rel}.findings.json").write_text(json.dumps(items))
        rows = [json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()]
        for r in rows:
            r["findingCount"] = r["highCount"] = len(outs.get(r["file"], []))
        (self.root / ".aee/security-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        cov = self.j("coverage.json")
        triage = {"addressedVerdictRound": 0,
                  "candidates": [{"id": c["src/a.py"]["id"], "action": "keep", "file": "src/a.py", "line": 7, "cweId": "CWE-78"},
                                 {"id": c["src/b.py"]["id"], "action": "keep", "file": "src/b.py", "line": 1, "cweId": "CWE-798"},
                                 {"id": c["src/d.py"]["id"], "action": "keep", "file": "src/d.py", "line": 1, "cweId": "CWE-798"}],
                  "gaps": [{"id": g["id"], "action": "benign", "reason": f"{g['file']}:L{g['lineRange']['start']} is not reachable in this batch"}
                           for g in cov["sinkGaps"] + cov["surfaceGaps"]]}
        self.put("triage.json", triage)
        return outs, triage

    def read_outputs(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}


class ScanTest(Base):
    def test_candidates_merge_tools_attach_surface_and_redact_secrets(self):
        code, out = self.scan()
        self.assertEqual(code, 0, out)
        c = self.candidates()
        a = c["src/a.py"]
        self.assertEqual((a["id"], a["tool"], a["cweId"], a["owaspCategory"]),
                         ("C001", "semgrep,bandit", "CWE-78", "A03 Injection"))
        self.assertEqual({s["tool"] for s in a["sources"]}, {"semgrep", "bandit"})
        self.assertEqual(a["suggestedSeverity"], "high")
        self.assertEqual([e["category"] for e in a["kbSurface"]], ["request-data", "command-exec"])
        b, d = c["src/b.py"], c["src/d.py"]
        self.assertEqual((b["cweId"], b["sameSecretInOtherFiles"], d["sameSecretInOtherFiles"]),
                         ("CWE-798", ["src/d.py"], ["src/b.py"]))
        for name in ("candidates.json", "coverage.json", "files.json", "warnings.json", "scanner_status.json"):
            self.assertNotIn(SECRET, (self.work / name).read_text())
        status = self.j("scanner_status.json")["scanners"]
        self.assertEqual({k: v["status"] for k, v in status.items()}, {"gitleaks": "ok", "semgrep": "ok", "bandit": "ok"})
        self.assertEqual(status["bandit"]["version"], "bandit 1.2.3")

    def test_coverage_gaps_list_uncovered_sinks_and_surface(self):
        self.scan()
        cov = self.j("coverage.json")
        self.assertEqual([(g["rule"], g["lineRange"]["start"]) for g in cov["sinkGaps"]], [("py-pickle", 13)])
        kinds = {(g["surfaceKind"], g["lineRange"]["start"]) for g in cov["surfaceGaps"]}
        self.assertIn(("entry-point", 4), kinds)   # the route handler: no candidate within two lines
        self.assertNotIn(("entry-point", 6), kinds)  # covered by the candidate on line 7
        self.assertNotIn(("sink", 13), kinds)      # already shown as a sink gap
        self.assertNotIn(("sink", 7), kinds)       # covered by the candidate

    def test_files_that_cannot_be_scanned_are_skipped_with_a_reason_never_clean(self):
        self.write(".aee/kb/src/b.py.json", json.dumps({**kb(self.root, "src/b.py"), "hash": "stale"}))
        (self.root / ".aee/kb/src/d.py.json").unlink()
        self.write("src/e.rs", "fn main() {}\n")
        self.write(".aee/kb/src/e.rs.json", json.dumps(kb(self.root, "src/e.rs", language="Rust")))
        self.write(".aee/extract-log.jsonl", "".join(json.dumps({"file": f, "batch": "b1", "hash": "x"}) + "\n"
                                                   for f in ("src/a.py", "src/b.py", "src/d.py", "src/e.rs")))
        self.scan()
        files = {f["file"]: f for f in self.j("files.json")["files"]}
        self.assertTrue(files["src/a.py"]["analyze"])
        self.assertIn("content changed", files["src/b.py"]["skipReason"])
        self.assertEqual(files["src/d.py"]["skipReason"], "no KB record")
        self.assertIn("not supported", files["src/e.rs"]["skipReason"])
        self.assertEqual(len(self.j("warnings.json")), 3)
        self.assertEqual([f["file"] for f in self.j("candidates.json")["files"]], ["src/a.py"])

    def test_only_the_last_batch_and_error_lines_are_scanned(self):
        self.write(".aee/extract-log.jsonl",
                   json.dumps({"file": "src/b.py", "batch": "old", "hash": "x"}) + "\n"
                   + json.dumps({"file": "src/a.py", "batch": "b2", "hash": "x"}) + "\n"
                   + json.dumps({"file": "src/d.py", "batch": "b2", "error": "unparseable"}) + "\n")
        self.scan()
        self.assertEqual([f["file"] for f in self.j("files.json")["files"]], ["src/a.py"])

    def test_a_missing_scanner_is_a_warning_and_its_files_are_not_scanned_clean(self):
        (self.bin / "bandit").unlink()
        self.scan()
        st = self.j("scanner_status.json")["scanners"]
        self.assertEqual(st["bandit"]["status"], "missing")
        self.assertEqual(st["bandit"]["files"], [])
        self.assertTrue(any("bandit MISSING" in w for w in self.j("warnings.json")))
        self.assertEqual(self.candidates()["src/a.py"]["tool"], "semgrep")
        meta = {f["file"]: f for f in self.j("candidates.json")["files"]}
        self.assertNotIn("bandit", meta["src/a.py"]["scannersCompleted"])

    def test_pass_without_an_extract_log_and_a_new_scan_clears_old_decisions(self):
        (self.root / ".aee/extract-log.jsonl").unlink()
        self.assertEqual(self.scan(), (10, "[PASS]"))
        self.write(".aee/extract-log.jsonl", json.dumps({"file": "src/a.py", "batch": "b", "hash": "x"}) + "\n")
        self.write(".aee/security-work/decisions.json", "{}")
        self.write(".aee/security-work/feedback.md", "old")
        self.scan()
        self.assertFalse((self.work / "decisions.json").exists())
        self.assertFalse((self.work / "feedback.md").exists())

    def test_the_pre_stage_creates_the_skeleton_the_analyzer_fills_in(self):
        self.scan()
        for rel in FILES:
            self.assertEqual(json.loads((self.root / f".aee/security/{rel}.findings.json").read_text()), [])
        rows = [json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()]
        self.assertEqual([r["file"] for r in rows], ["src/a.py", "src/b.py", "src/d.py"])
        self.assertEqual(rows[0]["toolsRun"], ["gitleaks", "semgrep", "bandit"])
        self.assertEqual((rows[0]["findingCount"], rows[0]["criticalCount"], rows[0]["highCount"]), (0, 0, 0))
        self.assertIn("- bandit: ok (bandit 1.2.3)", (self.root / ".aee/security-warnings.md").read_text())
        gaps = self.j("coverage.json")
        self.assertEqual([g["id"] for g in gaps["sinkGaps"] + gaps["surfaceGaps"]],
                         [f"G{i:03d}" for i in range(1, len(gaps["sinkGaps"] + gaps["surfaceGaps"]) + 1)])

    def test_a_pattern_that_fires_many_times_becomes_one_gap_with_occurrences(self):
        for n in ("e", "f"):
            self.write(f"src/{n}.py", "import pickle\n\n\n\n\n\ndef load(b):\n    return pickle.loads(b)\n")
            self.write(f".aee/kb/src/{n}.py.json", json.dumps(kb(self.root, f"src/{n}.py")))
        with (self.root / ".aee/extract-log.jsonl").open("a") as fh:
            fh.write("".join(json.dumps({"file": f"src/{n}.py", "batch": "b1", "hash": "x"}) + "\n" for n in ("e", "f")))
        self.scan()
        singles = [g for g in self.j("coverage.json")["sinkGaps"] if g["rule"] == "py-pickle"]
        self.assertEqual(len(singles), 3)                                    # below the default group size of 10
        self.assertTrue(all("count" not in g for g in singles))
        self.extra_env = {"GROUP_MIN": "3"}
        self.scan()
        cov = self.j("coverage.json")
        grouped = [g for g in cov["sinkGaps"] if g["rule"] == "py-pickle"]
        self.assertEqual(len(grouped), 1)
        self.assertEqual((grouped[0]["count"], grouped[0]["files"]), (3, 3))
        self.assertEqual(sorted(o["file"] for o in grouped[0]["occurrences"]), ["src/a.py", "src/e.py", "src/f.py"])
        ids = [g["id"] for g in cov["sinkGaps"] + cov["surfaceGaps"]]
        self.assertEqual(ids, [f"G{i:03d}" for i in range(1, len(ids) + 1)])

    def test_a_rule_that_fires_in_many_candidates_becomes_a_rule_group_without_strong_members(self):
        self.add_noisy_files()
        self.extra_env = {"GROUP_MIN": "3"}
        self.scan()
        doc = self.j("candidates.json")
        self.assertEqual([(g["id"], g["ruleId"], g["count"], g["files"], g["cweId"]) for g in doc["ruleGroups"]],
                         [("R01", "c.insecure-copy", 3, 3, "CWE-676")])
        members = [c for c in doc["candidates"] if c.get("ruleGroup") == "R01"]
        self.assertEqual(sorted(c["file"] for c in members), ["src/e.py", "src/f.py", "src/g.py"])
        self.assertEqual(sorted(doc["ruleGroups"][0]["ids"]), sorted(c["id"] for c in members))
        strong = [c for c in doc["candidates"] if c["file"] == "src/h.py"][0]
        self.assertEqual(strong["suggestedSeverity"], "high")
        self.assertNotIn("ruleGroup", strong)                                # high-severity candidates stay individual
        self.extra_env = {}
        self.scan()
        self.assertEqual(self.j("candidates.json")["ruleGroups"], [])        # below the default group size

    def test_skipped_files_get_no_skeleton_and_a_rescan_keeps_the_rows_of_other_files(self):
        self.write(".aee/security-log.jsonl", json.dumps({"file": "src/old.py", "findingCount": 2, "criticalCount": 0,
                                                          "highCount": 1, "toolsRun": ["semgrep"], "analyzedAt": "x"}) + "\n")
        (self.root / ".aee/kb/src/d.py.json").unlink()                     # d.py cannot be scanned
        self.scan()
        self.assertFalse((self.root / ".aee/security/src/d.py.findings.json").exists())
        rows = {json.loads(l)["file"]: json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()}
        self.assertEqual(sorted(rows), ["src/a.py", "src/b.py", "src/old.py"])
        self.assertEqual(rows["src/old.py"]["findingCount"], 2)

    def test_a_new_scan_resets_the_findings_of_rescanned_files_and_clears_triage_and_verdict(self):
        self.scan()
        self.write(".aee/security/src/a.py.findings.json", '[{"x": 1}]')
        self.write(".aee/security-work/triage.json", "{}")
        self.write(".aee/security-work/verdict.json", "{}")
        self.scan()
        self.assertEqual(json.loads((self.root / ".aee/security/src/a.py.findings.json").read_text()), [])
        self.assertFalse((self.work / "triage.json").exists())
        self.assertFalse((self.work / "verdict.json").exists())


class AuditTest(Base):
    """aee-security-audit: the read-only safety net for the next stage."""

    def setUp(self):
        super().setUp()
        self.scan()

    def codes(self, severity="error"):
        rep = json.loads(self.audit("--json")[1])
        return sorted({i["code"] for i in rep["issues"] if i["severity"] == severity})

    def messages(self, code):
        return [i["message"] for i in json.loads(self.audit("--json")[1])["issues"] if i["code"] == code]


    def findings_path(self, rel):
        return self.root / f".aee/security/{rel}.findings.json"

    def test_good_output_passes_with_calibration_warnings_and_changes_nothing(self):
        self.write_good_output()
        before = self.read_outputs()
        code, out = self.audit()
        self.assertEqual(code, 0, out)
        self.assertIn("AUDIT: PASS", out)
        self.assertIn("A09", self.codes("warning"))                # the confirmed a.py:L7 finding overlaps a KB entry-point
        self.assertEqual(self.read_outputs(), before)              # read-only

    def test_the_untouched_skeleton_fails_because_nothing_is_triaged(self):
        code, _ = self.audit()
        self.assertEqual(code, 1)
        self.assertEqual(self.codes(), ["A04"])
        self.assertIn("triage.json is missing", " ".join(self.messages("A04")))

    def test_every_candidate_and_every_gap_must_be_decided(self):
        outs, triage = self.write_good_output()
        triage["candidates"] = triage["candidates"][:2]
        triage["gaps"] = triage["gaps"][1:]
        self.put("triage.json", triage)
        msgs = " | ".join(self.messages("A04"))
        self.assertIn("has no decision in triage.json", msgs)
        self.assertIn("coverage gap", msgs)
        self.assertEqual(self.audit()[0], 1)

    def test_drops_need_a_reason_and_strong_drops_a_line_citation(self):
        outs, triage = self.write_good_output()
        c = self.candidates()
        triage["candidates"][1] = {"id": c["src/b.py"]["id"], "action": "drop", "reason": "fixture"}
        self.put("triage.json", triage)
        self.findings_path("src/b.py").write_text("[]")
        rows = [json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()]
        for r in rows:
            if r["file"] == "src/b.py":
                r["findingCount"] = r["highCount"] = 0
        (self.root / ".aee/security-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIn("dropped without an adequate reason", " ".join(self.messages("A04")))
        triage["candidates"][1]["reason"] = "src/b.py:L1 is a placeholder in a unit-test fixture that is never deployed"
        self.put("triage.json", triage)
        self.assertEqual(self.codes(), [])                          # the secret of d.py is still named, b.py is dropped

    def test_keeps_must_point_at_a_finding_and_every_scanner_finding_needs_a_keep(self):
        outs, triage = self.write_good_output()
        triage["candidates"][0]["line"] = 99
        self.put("triage.json", triage)
        msgs = " | ".join(self.messages("A04"))
        self.assertIn("does not point at a finding", msgs)
        self.assertIn("not the target of any keep", msgs)

    def test_two_findings_on_one_line_are_told_apart_by_cwe(self):
        sem = json.loads((self.stubs / "semgrep.json").read_text())
        second = json.loads(json.dumps(sem["results"][0]))
        second["check_id"] = "python.code-injection"
        second["extra"]["metadata"]["cwe"] = ["CWE-94: Code Injection"]
        sem["results"].append(second)
        self.fixture("semgrep", sem)
        self.scan()
        cands = [x for x in self.j("candidates.json")["candidates"] if x["file"] == "src/a.py"]
        self.assertEqual(sorted(x["cweId"] for x in cands), ["CWE-78", "CWE-94"])   # different CWEs are not merged
        outs, triage = self.write_good_output()
        a = outs["src/a.py"][0]
        f78 = dict(a, tool="semgrep,bandit", ruleId="python.subprocess-injection")
        f94 = dict(a, tool="semgrep", ruleId="python.code-injection", cweId="CWE-94", title="Code injection too")
        self.findings_path("src/a.py").write_text(json.dumps([f78, f94]))
        rows = [json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()]
        for r in rows:
            if r["file"] == "src/a.py":
                r["findingCount"] = r["highCount"] = 2
        (self.root / ".aee/security-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        keeps = [{"id": x["id"], "action": "keep", "file": "src/a.py", "line": 7, "cweId": x["cweId"]} for x in cands]
        others = [t for t in triage["candidates"] if t["file"] != "src/a.py"] if False else \
            [{"id": x["id"], "action": "keep", "file": x["file"], "line": 1, "cweId": "CWE-798"}
             for x in self.j("candidates.json")["candidates"] if x["file"] != "src/a.py"]
        triage["candidates"] = keeps + others
        cov = self.j("coverage.json")
        triage["gaps"] = [{"id": g["id"], "action": "benign", "reason": "src/a.py:L13 is not reachable in this batch"}
                          for g in cov["sinkGaps"] + cov["surfaceGaps"]]
        self.put("triage.json", triage)
        self.assertEqual(self.codes(), [])                                              # cweId tells the two keeps apart
        for k in keeps:
            k.pop("cweId")                                                              # without it both keeps hit the first
        self.put("triage.json", triage)
        self.assertIn("not the target of any keep", " ".join(self.messages("A04")))

    def test_a_keep_with_the_wrong_cwe_points_at_nothing(self):
        outs, triage = self.write_good_output()
        triage["candidates"][0]["cweId"] = "CWE-22"
        self.put("triage.json", triage)
        self.assertIn("does not point at a finding", " ".join(self.messages("A04")))

    def grouped_output(self):
        """A batch with a 3-member rule group plus the usual findings, everything decided; the group by one entry."""
        self.add_noisy_files()
        self.extra_env = {"GROUP_MIN": "3"}
        self.scan()
        outs, triage = self.write_good_output()
        doc = self.j("candidates.json")
        group = doc["ruleGroups"][0]
        strong = [c for c in doc["candidates"] if c["file"] == "src/h.py"][0]
        self.assertTrue(strong["id"] not in group["ids"])
        finding = self.finding("src/h.py", 1, "CWE-676", "A06 Vulnerable and Outdated Components", "semgrep",
                               "c.insecure-copy", "src/h.py:L1 uses the dangerous copy function without a bound.")
        self.findings_path("src/h.py").write_text(json.dumps([finding]))
        rows = [json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()]
        for r in rows:
            if r["file"] == "src/h.py":
                r["findingCount"] = r["highCount"] = 1
        (self.root / ".aee/security-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        triage["candidates"].append({"id": strong["id"], "action": "keep", "file": "src/h.py", "line": 1, "cweId": "CWE-676"})
        triage["groups"] = [{"group": group["id"], "action": "drop", "sampled": group["ids"][:3],
                             "reason": "each copy is bounded by the destination size, as in src/e.py:L1 and src/f.py:L1"}]
        self.put("triage.json", triage)
        return triage, group, strong

    def test_a_rule_group_can_be_dropped_in_one_decision(self):
        self.grouped_output()
        self.assertEqual(self.codes(), [])
        code, out = self.audit()
        self.assertEqual(code, 0, out)
        self.assertNotIn("same drop reason", " ".join(self.messages("A09")))      # a bulk reason is stated once

    def test_group_decisions_are_checked_for_samples_reason_action_and_members(self):
        triage, group, strong = self.grouped_output()
        entry = triage["groups"][0]
        cases = [({"sampled": group["ids"][:1]}, "must list at least 3"),
                 ({"sampled": ["C900", "C901", "C902"]}, "must list at least 3"),
                 ({"reason": "bounded copies"}, "need 40"),
                 ({"action": "keep"}, "only a drop can cover a group"),
                 ({"group": "R99"}, "not a rule group"),
                 ({"except": ["C900"]}, "not in the group")]
        for change, text in cases:
            self.put("triage.json", dict(triage, groups=[dict(entry, **change)]))
            self.assertIn(text, " ".join(self.messages("A04")), change)
        self.put("triage.json", triage)
        self.assertEqual(self.codes(), [])

    def test_excepted_members_must_be_decided_individually(self):
        triage, group, strong = self.grouped_output()
        odd = group["ids"][0]
        triage["groups"][0]["except"] = [odd]
        triage["groups"][0]["sampled"] = list(group["ids"])               # the odd member was read too
        self.put("triage.json", triage)
        self.assertIn(f"candidate {odd}", " ".join(self.messages("A04")))          # no decision for the excepted member
        cand = [c for c in self.j("candidates.json")["candidates"] if c["id"] == odd][0]
        triage["candidates"].append({"id": odd, "action": "drop",
                                     "reason": f"{cand['file']}:L1 is a plain assignment, so the rule does not apply here"})
        self.put("triage.json", triage)
        self.assertEqual(self.codes(), [])

    def test_a_group_never_covers_a_high_severity_candidate(self):
        triage, group, strong = self.grouped_output()
        triage["groups"][0]["except"] = []
        self.put("triage.json", dict(triage, candidates=[t for t in triage["candidates"] if t["id"] != strong["id"]]))
        self.assertIn(f"candidate {strong['id']}", " ".join(self.messages("A04")))   # the strong one needs its own decision

    def test_repeated_individual_drop_reasons_are_still_flagged_but_bulk_ones_are_not(self):
        triage, group, strong = self.grouped_output()
        cands = {c["id"]: c for c in self.j("candidates.json")["candidates"]}
        triage["groups"] = []
        triage["candidates"] += [{"id": i, "action": "drop", "reason": "the rule does not apply to this plain assignment"}
                                 for i in group["ids"]]
        self.put("triage.json", triage)
        self.assertIn("same drop reason", " ".join(self.messages("A09")))

    def test_scanner_backed_findings_must_match_the_evidence(self):
        outs, _ = self.write_good_output()
        bad = dict(outs["src/a.py"][0], ruleId="made.up.rule")
        self.findings_path("src/a.py").write_text(json.dumps([bad]))
        self.assertIn("no scanner candidate backs this finding", " ".join(self.messages("A03")))
        bad = dict(outs["src/a.py"][0], tool="spotbugs")
        self.findings_path("src/a.py").write_text(json.dumps([bad]))
        self.assertIn("did not complete", " ".join(self.messages("A03")))
        manual = dict(outs["src/a.py"][0], tool="manual", ruleId="x")
        self.findings_path("src/a.py").write_text(json.dumps([manual]))
        self.assertIn("ruleId null", " ".join(self.messages("A03")))

    def test_merged_candidates_are_matched_against_their_union(self):
        sem = json.loads((self.stubs / "semgrep.json").read_text())
        second = json.loads(json.dumps(sem["results"][0]))
        second["check_id"] = "python.shell-true"
        sem["results"].append(second)
        self.fixture("semgrep", sem)
        self.scan()
        c = [x for x in self.j("candidates.json")["candidates"] if x["file"] == "src/a.py"]
        self.assertEqual(len(c), 2)                                  # two semgrep rules on one line stay two candidates
        outs, triage = self.write_good_output()
        triage["candidates"] = [t for t in triage["candidates"] if not t["id"].startswith("C00") or t["file"] != "src/a.py"] \
            if False else triage["candidates"]
        ids = {x["id"] for x in self.j("candidates.json")["candidates"]}
        gitleaks = {x["id"]: x for x in self.j("candidates.json")["candidates"] if x["file"] != "src/a.py"}
        triage["candidates"] = [{"id": x["id"], "action": "keep", "file": "src/a.py", "line": 7} for x in c] + \
            [{"id": i, "action": "keep", "file": x["file"], "line": 1} for i, x in gitleaks.items()]
        self.put("triage.json", triage)
        cov = self.j("coverage.json")
        triage["gaps"] = [{"id": g["id"], "action": "benign", "reason": "src/a.py:L13 is not reachable in this batch"}
                          for g in cov["sinkGaps"] + cov["surfaceGaps"]]
        self.put("triage.json", triage)
        self.assertEqual(self.codes(), [])
        self.assertEqual(self.audit()[0], 0)

    def test_structure_ranges_and_enums_are_validated(self):
        outs, _ = self.write_good_output()
        base = outs["src/a.py"][0]
        for change, code, text in [({"lineRange": {"start": 99, "end": 99}}, "A02", "invalid for a"),
                                   ({"cweId": "CWE-xx"}, "A02", "CWE-<n>"),
                                   ({"owaspCategory": "A99 Nope"}, "A02", "A01-A10"),
                                   ({"severity": "urgent"}, "A02", "severity"),
                                   ({"title": "x" * 121}, "A01", "1-120")]:
            self.findings_path("src/a.py").write_text(json.dumps([dict(base, **change)]))
            self.assertIn(text, " ".join(self.messages(code)), change)
        extra = dict(base, candidateIds=["C001"])
        self.findings_path("src/a.py").write_text(json.dumps([extra]))
        self.assertIn("exactly", " ".join(self.messages("A01")))
        self.findings_path("src/a.py").write_text("{not json")
        self.assertIn("missing or not valid JSON", " ".join(self.messages("A01")))
        secret = dict(outs["src/b.py"][0], owaspCategory="A03 Injection")
        self.findings_path("src/a.py").write_text(json.dumps([base]))
        self.findings_path("src/b.py").write_text(json.dumps([secret]))
        self.assertIn("A07", " ".join(self.messages("A02")))

    def test_log_rows_must_match_the_findings_and_the_scanners(self):
        self.write_good_output()
        rows = [json.loads(l) for l in (self.root / ".aee/security-log.jsonl").read_text().splitlines()]
        rows[0]["findingCount"] = 5
        rows[1]["toolsRun"] = ["semgrep"]
        (self.root / ".aee/security-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows[:2]))
        msgs = " | ".join(self.messages("A05"))
        self.assertIn("do not match the findings", msgs)
        self.assertIn("toolsRun", msgs)
        self.assertIn("found 0", msgs)                                # the row of the third file is missing

    def test_secrets_never_appear_in_any_output_and_shared_secrets_are_named(self):
        outs, triage = self.write_good_output()
        leaked = dict(outs["src/a.py"][0], detail=outs["src/a.py"][0]["detail"] + f" The key is {SECRET}.")
        self.findings_path("src/a.py").write_text(json.dumps([leaked]))
        self.assertIn("secret", " ".join(self.messages("A06")))
        self.findings_path("src/a.py").write_text(json.dumps(outs["src/a.py"]))
        unnamed = dict(outs["src/b.py"][0], detail="src/b.py:L1 holds a credential in the source file.")
        self.findings_path("src/b.py").write_text(json.dumps([unnamed]))
        self.assertIn("the same secret appears in src/d.py", " ".join(self.messages("A07")))
        triage["gaps"][0]["reason"] = f"src/a.py:L1 contains {SECRET} so it is benign"
        self.put("triage.json", triage)
        self.assertIn("triage", " ".join(self.messages("A06")))

    def test_noise_and_overlapping_duplicates_are_errors(self):
        outs, _ = self.write_good_output()
        a = outs["src/a.py"][0]
        self.findings_path("src/a.py").write_text(json.dumps([a, dict(a, title="again")]))
        self.assertIn("overlapping findings", " ".join(self.messages("A08")))
        many = [dict(a, lineRange={"start": 1 + i, "end": 1 + i}, cweId=f"CWE-{100 + i}") for i in range(26)]
        self.findings_path("src/a.py").write_text(json.dumps(many))
        self.assertIn("noise", " ".join(self.messages("A08")))

    def test_approval_is_required_on_request(self):
        self.write_good_output()
        self.assertEqual(self.audit("--require-approval")[0], 1)
        rep = json.loads(self.audit("--require-approval", "--json")[1])
        self.assertTrue(any("verdict.json is missing" in i["message"] for i in rep["issues"]))
        self.put("verdict.json", {"round": 1, "verdict": "revise", "reviewed": {},
                                  "issues": [{"type": "integrity", "file": "src/a.py", "line": 7, "message": "m" * 20, "fix": "f"}]})
        self.assertEqual(self.audit("--require-approval")[0], 1)
        self.put("verdict.json", {"round": 1, "verdict": "approve", "reviewed": {}, "issues": []})
        self.assertEqual(self.audit("--require-approval")[0], 0)

    def test_exit_code_2_when_the_pre_stage_did_not_complete(self):
        (self.work / "candidates.json").unlink()
        code, out = self.audit()
        self.assertEqual(code, 2)
        self.assertIn("pre-stage did not complete", out)


@unittest.skipUnless(shutil.which("git"), "git is required")
class SecurityWrapperTest(Base):
    """security_pre.sh (last script before the agents) and security_audit.sh (read-only, for the next stage)."""

    def setUp(self):
        super().setUp()
        self.remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "agent_workspace", str(self.remote)], check=True)
        self.git("init", "-q", "-b", "agent_workspace")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "entry stages")
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

    def test_the_pre_stage_pushes_candidates_and_skeleton_and_registers_only_the_candidates(self):
        code, last, out = self.sh(PRE)
        self.assertEqual(code, 0, out)
        files = self.remote_files()
        self.assertIn("chore(security): scan results, candidates and output skeleton", files)
        for expected in (".aee/security-work/candidates.json", ".aee/security/src/a.py.findings.json",
                         ".aee/security-log.jsonl", ".aee/security-warnings.md"):
            self.assertIn(expected, files)
        names = [json.loads(l)["artifact"] for l in (self.root / ".agent-studio/artifacts.jsonl").read_text().splitlines()]
        self.assertEqual(names, ["Security Candidates"])            # Security:<file> is registered by the reviewer

    def test_the_audit_is_read_only_and_follows_the_agents_through_git(self):
        self.assertEqual(self.sh(PRE)[0], 0)
        code, last, _ = self.sh(AUDIT_SH, "--no-sync")
        self.assertEqual((code, last), (1, "[FAIL] the security output has errors; the problems above go back to the analyzer or reviewer"))
        self.write_good_output()                                      # the analyzer writes its files ...
        self.agent_commit("analyzer: findings, log and triage")        # ... and pushes them itself
        self.assertEqual(self.sh(AUDIT_SH)[:2], (0, "[PASS] the security output is valid"))
        self.assertEqual(self.sh(AUDIT_SH, "--require-approval")[0], 1)
        self.put("verdict.json", {"round": 1, "verdict": "approve", "reviewed": {}, "issues": []})
        self.agent_commit("reviewer: verdict")
        before = self.git("rev-parse", "HEAD")
        self.assertEqual(self.sh(AUDIT_SH, "--require-approval")[:2], (0, "[PASS] the security output is valid"))
        self.assertEqual(self.git("rev-parse", "HEAD"), before)       # nothing committed by the audit
        self.assertEqual(self.git("status", "--short").strip(), "")

    def test_audit_reports_a_missing_pre_stage_as_error_and_rejects_bad_options(self):
        shutil.rmtree(self.root / ".aee/security-work", ignore_errors=True)
        code, last, out = self.sh(AUDIT_SH, "--no-sync")
        self.assertEqual((code, last), (2, "[ERROR] the pre-stage did not complete"))
        self.assertEqual(self.sh(AUDIT_SH, "--bogus")[0], 2)

    def test_local_pre_stage_without_publish_does_not_touch_git(self):
        head = self.git("rev-parse", "HEAD")
        code, last, out = self.sh(PRE, "--no-sync", "--no-publish")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertTrue((self.work / "candidates.json").is_file())
        self.assertEqual(self.sh(PRE, "--no-sync", "--no-publish", "--bogus")[0], 1)
        no_log = self.sh(PRE, "--no-sync", "--no-publish", path=f"{self.bin}:/usr/bin:/bin")
        self.assertEqual(no_log[0], 0)


if __name__ == "__main__":
    unittest.main()
