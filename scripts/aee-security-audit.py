#!/usr/bin/env python3
"""Read-only audit of the security analyzer's and reviewer's output (no model, writes nothing).

Usage: aee-security-audit [--aee DIR] [--require-approval] [--json]

For the pre-step of the NEXT stage (Correlator, Reporter) or for a human: it validates what the two
agents pushed, against the scanner evidence the pre-stage wrote. It never changes a file and runs
nothing after the agents in their own loop.

Checks (errors fail the audit):
  A01  every analysed file has a findings file: a JSON array, items with exactly the 10 contract keys
  A02  line ranges lie inside the file; severity, confidence, CWE and OWASP are valid and consistent
  A03  scanner-backed findings match scanner evidence (an overlapping candidate with the same tools and
       ruleId, and every cited tool completed for the file); manual findings have tool `manual`, ruleId null
  A04  triage.json accounts for every candidate exactly once; drops are justified (15 characters, and for
       high-severity or secret candidates 40 characters and a line citation); every keep points at a finding
       of that file (file + line + cweId) and every scanner-backed finding is the target of a keep; every gap is decided.
       A rule group (`ruleGroups` of candidates.json) may be dropped in one `groups` entry: action `drop`, a
       reason of at least 40 characters and `sampled` candidate ids from the group (min(3, size) of them);
       members that differ are listed in `except` and decided individually
  A05  security-log.jsonl has one row per analysed file, with counts and toolsRun matching the findings
  A06  no secret value or secret-shaped string in any findings file, the triage or the verdict
  A07  the same secret in several files is named in each finding
  A08  not too many findings in one file; no overlapping findings for one CWE
  A09  with --require-approval: verdict.json says `approve` with no open issue
Warnings (never fail): detail quality, calibration (taint source, CWE changed from the scanner, severity
2+ levels apart), mass drops, copied drop reasons.

Exit code: 0 passes (warnings allowed), 1 errors found, 2 input missing.
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aee_security_lib import *  # noqa: E402,F401,F403

LINE_CITE = re.compile(r"(?:\bL\d+\b|:\d+)")
HEDGE = re.compile(r"\b(may|might|could|possibly|depends|potentially)\b", re.I)
MAX_PER_FILE = 25
WARN_PER_FILE = 10
REPEAT_REASON_MIN = 3


class Report:
    def __init__(self):
        self.issues = []

    def add(self, code, severity, message, file=None, line=None):
        self.issues.append({"code": code, "severity": severity, "message": message, "file": file, "line": line})

    @property
    def errors(self):
        return [i for i in self.issues if i["severity"] == "error"]


def is_strong(c):
    return c["suggestedSeverity"] in ("critical", "high") or "gitleaks" in c["tool"]


def jsonl(path):
    out = []
    if Path(path).is_file():
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    out.append(None)
    return out


def norm_reason(s):
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", (s or "").lower())).strip()


def check_finding(R, rel, f, lines, completed, cands_of_file):
    if not isinstance(f, dict) or set(f) != set(FINDING_KEYS):
        R.add("A01", "error", f"finding keys must be exactly {FINDING_KEYS}", rel)
        return False
    s = (f.get("lineRange") or {}).get("start")
    e = (f.get("lineRange") or {}).get("end")
    if not (isinstance(s, int) and isinstance(e, int) and 1 <= s <= e <= max(len(lines), 1)):
        R.add("A02", "error", f"lineRange {s}-{e} is invalid for a {len(lines)}-line file", rel)
        return False
    if f["file"] != rel:
        R.add("A01", "error", f"finding.file {f['file']!r} differs from its findings path {rel!r}", rel, s)
    if not isinstance(f["title"], str) or not (1 <= len(f["title"]) <= 120):
        R.add("A01", "error", "title must be 1-120 characters", rel, s)
    detail = f["detail"] if isinstance(f["detail"], str) else ""
    if len(detail.strip()) < 30:
        R.add("A01", "error", "detail is missing or too short", rel, s)
    if f["severity"] not in SEVERITIES:
        R.add("A02", "error", f"severity {f['severity']!r} is invalid", rel, s)
    if f["confidence"] not in CONFIDENCES:
        R.add("A02", "error", f"confidence {f['confidence']!r} is invalid", rel, s)
    if f["owaspCategory"] not in OWASP.values():
        R.add("A02", "error", f"owaspCategory {f['owaspCategory']!r} is not one of A01-A10", rel, s)
    cwe = f["cweId"]
    if not isinstance(cwe, str) or not re.fullmatch(r"CWE-\d+", cwe):
        R.add("A02", "error", f"cweId {cwe!r} is not 'CWE-<n>'", rel, s)
    else:
        if cwe in SECRET_CWES and f["owaspCategory"] != OWASP["A07"]:
            R.add("A02", "error", "hardcoded secrets (CWE-798) must use A07", rel, s)
        exp = CWE_TO_OWASP.get(int(cwe.split("-")[1]))
        if exp and exp != f["owaspCategory"]:
            R.add("A02", "warning", f"{cwe} usually maps to '{exp}', the finding says '{f['owaspCategory']}'", rel, s)
    # evidence
    tools = f["tool"].split(",") if isinstance(f["tool"], str) else []
    if f["tool"] == "manual":
        if f["ruleId"] is not None:
            R.add("A03", "error", "manual findings must have ruleId null", rel, s)
    else:
        for t in tools:
            if t not in TOOLS - {"manual"}:
                R.add("A03", "error", f"unknown tool {t!r}", rel, s)
            elif t not in completed:
                R.add("A03", "error", f"tool '{t}' did not complete for this file but is cited", rel, s)
        near = [c for c in cands_of_file if ranges_overlap(c["lineRange"], f["lineRange"])]  # merged candidates: use the union
        if not (near and set(tools) <= {x["tool"] for c in near for x in c["sources"]}
                and f["ruleId"] in {x["ruleId"] for c in near for x in c["sources"]}):
            R.add("A03", "error", f"no scanner candidate backs this finding (tools {f['tool']}, ruleId {f['ruleId']!r}) "
                                  "near these lines: copy tool and ruleId from candidates.json", rel, s)
    # quality warnings
    if "\n\n" in detail.strip():
        R.add("A02", "warning", "detail should be one paragraph", rel, s)
    if not re.search(re.escape(rel) + r":L?" + str(s) + r"\b", detail):
        R.add("A02", "warning", f"detail does not cite '{rel}:L{s}'", rel, s)
    if f["confidence"] == "confirmed" and HEDGE.search(detail):
        R.add("A02", "warning", "confidence=confirmed but the detail hedges (may / could / depends)", rel, s)
    return True


def keep_target(items, d):
    """Index of the finding a `keep` points at: the line lies in its range and, if given, the cweId matches."""
    for i, x in enumerate(items):
        if x["lineRange"]["start"] <= (d.get("line") or -1) <= x["lineRange"]["end"] \
                and d.get("cweId") in (None, x["cweId"]):
            return i
    return None


def expand_groups(R, triage, cands, decided):
    """Apply the bulk drops of triage `groups` to the per-candidate decisions (individual decisions win)."""
    groups = {g["id"]: g for g in cands.get("ruleGroups", [])}
    for d in triage.get("groups", []):
        g = groups.get(d.get("group"))
        if g is None:
            R.add("A04", "error", f"group {d.get('group')!r} is not a rule group of candidates.json")
            continue
        ids = set(g["ids"])
        if d.get("action") != "drop":
            R.add("A04", "error", f"group {g['id']}: only a drop can cover a group; decide keeps per candidate")
            continue
        reason = (d.get("reason") or "").strip()
        if len(reason) < 40:
            R.add("A04", "error", f"group {g['id']} dropped without an adequate reason ({len(reason)} chars, need 40)")
        sampled = set(d.get("sampled") or [])
        if not sampled <= ids or len(sampled) < min(3, len(ids)):
            R.add("A04", "error", f"group {g['id']}: `sampled` must list at least {min(3, len(ids))} candidate ids of the "
                                  "group that you read")
        exceptions = set(d.get("except") or [])
        if not exceptions <= ids:
            R.add("A04", "error", f"group {g['id']}: `except` lists ids that are not in the group")
        for cid in sorted(ids - exceptions):
            decided.setdefault(cid, {"id": cid, "action": "drop", "reason": reason, "bulk": True, "group": g["id"]})


def run_audit(aee, require_approval):
    R = Report()
    work = aee / "security-work"
    files = load_json(work / "files.json")
    cands = load_json(work / "candidates.json")
    status = (load_json(work / "scanner_status.json") or {}).get("scanners")
    cov = load_json(work / "coverage.json") or {"sinkGaps": [], "surfaceGaps": []}
    triage = load_json(work / "triage.json")
    verdict = load_json(work / "verdict.json")
    if not (files and cands and status is not None):
        R.add("A00", "error", "the pre-stage did not complete (files, candidates or scanner_status missing)")
        return R, 2
    assigned = [f for f in files["files"] if f["analyze"]]
    meta = {f["file"]: f for f in cands["files"]}
    cands_by_file = {}
    for c in cands["candidates"]:
        cands_by_file.setdefault(c["file"], []).append(c)
    kbs = {f["file"]: load_json(f["kbPath"]) for f in assigned}

    # A01-A03 findings
    findings = {}
    for f in assigned:
        rel = f["file"]
        p = aee / "security" / f"{rel}.findings.json"
        items = load_json(p)
        if items is None:
            R.add("A01", "error", "findings file is missing or not valid JSON", rel)
            continue
        if not isinstance(items, list):
            R.add("A01", "error", "findings file must be a JSON array", rel)
            continue
        lines = read_lines(rel)
        completed = set(meta.get(rel, {}).get("scannersCompleted", []))
        ok = [x for x in items if check_finding(R, rel, x, lines, completed, cands_by_file.get(rel, []))]
        findings[rel] = ok
        if len(items) > MAX_PER_FILE:
            R.add("A08", "error", f"{len(items)} findings in one file - noise, not coverage", rel)
        elif len(items) > WARN_PER_FILE:
            R.add("A08", "warning", f"{len(items)} findings in one file - double-check precision", rel)
        for i, x in enumerate(ok):
            for y in ok[i + 1:]:
                if x["cweId"] == y["cweId"] and ranges_overlap(x["lineRange"], y["lineRange"]):
                    R.add("A08", "error", f"overlapping findings for {x['cweId']} at lines {x['lineRange']['start']}-"
                          f"{x['lineRange']['end']} and {y['lineRange']['start']}-{y['lineRange']['end']}", rel,
                          x["lineRange"]["start"])
        for x in ok:
            for c in cands_by_file.get(rel, []):
                if "gitleaks" in x["tool"] and c.get("sameSecretInOtherFiles") and ranges_overlap(c["lineRange"], x["lineRange"]) \
                        and not all(o in x["detail"] for o in c["sameSecretInOtherFiles"]):
                    R.add("A07", "error", f"the same secret appears in {', '.join(c['sameSecretInOtherFiles'])} but the "
                          "finding does not say so", rel, x["lineRange"]["start"])

    # A04 triage
    if triage is None:
        R.add("A04", "error", "triage.json is missing: the analyzer has not delivered")
    else:
        decided = {}
        for d in triage.get("candidates", []):
            if d.get("id") in decided:
                R.add("A04", "error", f"candidate {d.get('id')} is decided twice")
            decided[d.get("id")] = d
        expand_groups(R, triage, cands, decided)
        targeted = {rel: set() for rel in findings}
        for c in cands["candidates"]:
            d = decided.get(c["id"])
            if not d:
                R.add("A04", "error", f"candidate {c['id']} ({c['tool']} {c['ruleId']}) has no decision in triage.json",
                      c["file"], c["lineRange"]["start"])
            elif d.get("action") == "drop":
                need, reason = (40 if is_strong(c) else 15), (d.get("reason") or "").strip()
                if len(reason) < need or (is_strong(c) and not LINE_CITE.search(reason)):
                    R.add("A04", "error", f"candidate {c['id']} dropped without an adequate reason ({len(reason)} chars, "
                          f"need {need}" + (" and a line citation" if is_strong(c) else "") + ")", c["file"], c["lineRange"]["start"])
            elif d.get("action") == "keep":
                hit = keep_target(findings.get(d.get("file"), []), d)
                if d.get("file") != c["file"] or hit is None:
                    R.add("A04", "error", f"keep of {c['id']} does not point at a finding of {c['file']} "
                          "(file + line + cweId)", c["file"], c["lineRange"]["start"])
                else:
                    targeted[c["file"]].add(hit)
            else:
                R.add("A04", "error", f"candidate {c['id']} action must be 'keep' or 'drop'", c["file"])
        for rel, items in findings.items():
            for i, x in enumerate(items):
                if x["tool"] != "manual" and i not in targeted.get(rel, set()):
                    R.add("A04", "error", "scanner-backed finding is not the target of any keep in triage.json", rel,
                          x["lineRange"]["start"])
        gap_ids = {g["id"] for g in cov["sinkGaps"] + cov["surfaceGaps"]}
        gdec = {g.get("id"): g for g in triage.get("gaps", [])}
        for gid in sorted(gap_ids - set(gdec)):
            R.add("A04", "error", f"coverage gap {gid} has no decision in triage.json (manual or benign)")
        for gid, g in gdec.items():
            if gid in gap_ids and g.get("action") not in ("manual", "benign"):
                R.add("A04", "error", f"gap {gid} action must be 'manual' or 'benign'")
            elif g.get("action") == "benign" and len((g.get("reason") or "").strip()) < 15:
                R.add("A04", "error", f"gap {gid} is called benign without a reason")
        # calibration warnings and triage statistics
        by_id = {c["id"]: c for c in cands["candidates"]}
        for cid, d in decided.items():
            c = by_id.get(cid)
            if not c or d.get("action") != "keep":
                continue
            hit = keep_target(findings.get(c["file"], []), d)
            if hit is None:
                continue
            x = findings[c["file"]][hit]
            if c["cweId"] and x["cweId"] != c["cweId"]:
                R.add("A09", "warning", f"CWE changed from the scanner's {c['cweId']} to {x['cweId']}", c["file"], x["lineRange"]["start"])
            if x["severity"] in SEVERITIES and c["suggestedSeverity"] in SEVERITIES \
                    and abs(SEVERITIES.index(x["severity"]) - SEVERITIES.index(c["suggestedSeverity"])) >= 2:
                R.add("A09", "warning", f"severity {x['severity']} vs scanner-suggested {c['suggestedSeverity']} (2+ levels apart)",
                      c["file"], x["lineRange"]["start"])
            surf = kb_surface(kbs.get(c["file"]), x["lineRange"]["start"], x["lineRange"]["end"])
            if x["confidence"] in ("confirmed", "probable") and any(e["kind"] == "entry-point" for e in surf):
                R.add("A09", "warning", "overlaps a KB entry-point: the Correlator will trace this as a TAINT SOURCE",
                      c["file"], x["lineRange"]["start"])
        dropped = [d for d in decided.values() if d.get("action") == "drop"]
        total = len(cands["candidates"])
        if total >= 10 and len(dropped) / total > 0.7:
            R.add("A09", "warning", f"{len(dropped)} of {total} candidates dropped - read the drop reasons")
        # a bulk drop states its reason once; only individually written reasons can be copy-pasted
        for reason, n in Counter(norm_reason(d.get("reason")) for d in dropped if not d.get("bulk")).most_common():
            if n >= REPEAT_REASON_MIN:
                R.add("A09", "warning", f"the same drop reason is used {n}x: '{reason[:80]}' - copy-pasted triage?")

    # A05 log
    rows = jsonl(aee / "security-log.jsonl")
    if None in rows:
        R.add("A05", "error", "security-log.jsonl contains a line that is not valid JSON")
    by_file = {}
    for r in rows:
        if r:
            by_file.setdefault(r.get("file"), []).append(r)
    for f in assigned:
        rel = f["file"]
        got = by_file.get(rel, [])
        if len(got) != 1:
            R.add("A05", "error", f"expected exactly 1 security-log row, found {len(got)}", rel)
            continue
        r = got[0]
        items = findings.get(rel, [])
        exp = (len(items), sum(x["severity"] == "critical" for x in items), sum(x["severity"] == "high" for x in items))
        if any(k not in r for k in ("findingCount", "criticalCount", "highCount", "toolsRun", "analyzedAt")):
            R.add("A05", "error", "log row is missing fields", rel)
        elif (r["findingCount"], r["criticalCount"], r["highCount"]) != exp:
            R.add("A05", "error", f"log counts {(r['findingCount'], r['criticalCount'], r['highCount'])} do not match the findings {exp}", rel)
        elif set(r["toolsRun"]) != set(meta.get(rel, {}).get("scannersCompleted", [])):
            R.add("A05", "error", f"log toolsRun {r['toolsRun']} differs from the scanners that completed", rel)

    # A06 secrets
    texts = []
    for rel, items in findings.items():
        texts += [(f"{rel} finding", f"{x.get('title', '')} {x.get('detail', '')}") for x in items]
    if triage:
        texts += [("triage", " ".join(str(d.get("reason", "")) for d in triage.get("candidates", []) + triage.get("gaps", [])))]
    if verdict:
        texts += [("verdict", " ".join(str(i.get("message", "")) + " " + str(i.get("fix", "")) for i in verdict.get("issues", [])))]
    lits = secret_literals(cands)
    for where, text in texts:
        if has_secret_pattern(text):
            R.add("A06", "error", f"{where} contains a value shaped like a secret (key / token pattern)")
            continue
        for lit, origin in lits.items():
            if lit in text:
                R.add("A06", "error", f"{where} repeats a literal from the secret flagged at {origin}")
                break

    # A09 approval
    if require_approval:
        if verdict is None:
            R.add("A09", "error", "verdict.json is missing: the reviewer has not delivered")
        elif verdict.get("verdict") != "approve" or verdict.get("issues"):
            R.add("A09", "error", f"the reviewer's verdict is {verdict.get('verdict')!r}"
                  + (f" with {len(verdict.get('issues'))} open issue(s)" if verdict.get("issues") else ""))
    return R, (1 if R.errors else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--require-approval", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the issues as JSON")
    a = ap.parse_args(argv)
    R, rc = run_audit(Path(a.aee), a.require_approval)
    if a.json:
        print(json.dumps({"status": "FAIL" if R.errors else "PASS", "issues": R.issues}, indent=2))
    else:
        print(f"AUDIT: {'FAIL' if R.errors else 'PASS'}  errors={len(R.errors)} "
              f"warnings={sum(i['severity'] == 'warning' for i in R.issues)}")
        for i in R.issues:
            loc = (i["file"] or "-") + (f":L{i['line']}" if i["line"] else "")
            print(f"  [{i['severity'].upper():7}] {i['code']} {loc} {i['message']}")
    return 2 if any(i["code"] == "A00" for i in R.issues) else rc


if __name__ == "__main__":
    sys.exit(main())
