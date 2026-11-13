#!/usr/bin/env python3
"""Read-only audit of the quality analyzer's and reviewer's output (no model, writes nothing).

Usage: aee-quality-audit [--aee DIR] [--require-approval] [--json]

For the pre-step of the next stage (Quality Reporter) or for a human. It validates what the two agents pushed against
the evidence the pre-stage wrote; it changes nothing and runs nothing inside the agents' loop. A port of the
quality-pipeline pre-work's precheck, extended by triage and log consistency.

Checks (errors fail the audit):
  Q01  every analysed file has a findings file: a JSON array of objects with the nine contract keys (and optionally
       `evidenceQuote`); skipped (generated, vendored, fixture) files have none
  Q02  line range inside the file; title 1-120 characters; detail present; valid severity, category, confidence; no
       security category or ruff `S*` rule; no secret-like content in title or detail
  Q03  evidence: tool-backed findings cite a tool that completed for the file and a rule a pre-stage candidate reports
       near those lines (a metric name for `lizard`); manual findings have tool `manual`, ruleId null and an
       `evidenceQuote` that appears verbatim in the cited lines
  Q04  complexity numbers in title or detail equal the lizard metrics of the functions in range
  Q05  triage.json accounts for every candidate exactly once (own entry, or a `groups` entry: drop only, sampled
       members, reason of 40+ characters, `except` for the others); a keep points at a finding of that file by line
       and ruleId; every tool- or metric-backed finding is the target of a keep; every analysed file either has
       candidates in `files` or is listed in `reviewedWithoutCandidates`
  Q06  quality-log.jsonl has one row per analysed file; counts, maxComplexity, lines and toolsRun are consistent
  Q07  at most 25 findings per file; no overlapping findings for one rule
  Q08  no secret-like content in the triage or the verdict
  Q09  with --require-approval: verdict.json says `approve` with no open issue
Warnings (never fail): suggested suppressions, evidence quote mismatch on a tool-backed finding, complexity without
numbers, a metric finding whose severity differs from the definition (high at twice the threshold, else medium),
mass drops and copied drop reasons.

Exit code: 0 passes (warnings allowed), 1 errors found, 2 input missing.
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aee_quality_lib import *  # noqa: E402,F401,F403

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)\b\s*[:=]\s*['\"]?[^\s'\",;]{4,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
SUPPRESSION = re.compile(r"(?i)#\s*noqa|pylint:\s*disable|\bNOPMD\b|shellcheck\s+disable|hadolint\s+ignore|@SuppressWarnings")
SECURITY_RULE = re.compile(r"^S\d{3}$")
METRIC_PATTERNS = {
    "ccn": re.compile(r"(?i)\bCCN\s+(?:of\s+)?(\d+)"),
    "lines": re.compile(r"(?i)\b(\d+)\s+(?:lines|nloc|loc)\b"),
    "params": re.compile(r"(?i)\b(\d+)\s+(?:parameters|params|arguments)\b"),
    "nesting": re.compile(r"(?i)\bnesting(?:\s+depth)?\s+(?:of\s+)?(\d+)"),
}
MAX_PER_FILE = 25


class Report:
    def __init__(self):
        self.issues = []

    def add(self, code, severity, message, file=None, line=None):
        self.issues.append({"code": code, "severity": severity, "message": message, "file": file, "line": line})

    @property
    def errors(self):
        return [i for i in self.issues if i["severity"] == "error"]


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


def overlaps(a1, a2, b1, b2):
    return a1 <= b2 and b1 <= a2


def norm_reason(s):
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", (s or "").lower())).strip()


def metric_allowed(funcs, thresholds, file_lines):
    allowed = {"ccn": {thresholds["ccn"]}, "params": {thresholds["params"]}, "nesting": {thresholds["nesting"]},
               "lines": {thresholds["function_lines"], thresholds["file_lines"], file_lines}}
    for f in funcs:
        allowed["ccn"].add(f["ccn"])
        allowed["params"].add(f["params"])
        allowed["nesting"].add(f["maxNesting"])
        allowed["lines"] |= {f["endLine"] - f["startLine"] + 1, f["nloc"]}
    return allowed


def check_finding(R, rel, f, lines, cdoc, thresholds):
    """Validate one finding; returns True if it is well-formed enough to take part in the later checks."""
    if not isinstance(f, dict) or not set(FINDING_KEYS) <= set(f) or not set(f) <= set(FINDING_KEYS + OPTIONAL_KEYS):
        R.add("Q01", "error", f"finding keys must be the nine contract keys {FINDING_KEYS} (optionally evidenceQuote)", rel)
        return False
    lr = f["lineRange"] if isinstance(f["lineRange"], dict) else {}
    s, e = lr.get("start"), lr.get("end")
    if not (isinstance(s, int) and isinstance(e, int) and 1 <= s <= e <= max(len(lines), 1)):
        R.add("Q02", "error", f"lineRange {s}-{e} is invalid for a {len(lines)}-line file", rel)
        return False
    if f["file"] != rel:
        R.add("Q01", "error", f"finding.file {f['file']!r} differs from its findings path {rel!r}", rel, s)
    title, detail = f["title"], f["detail"]
    if not isinstance(title, str) or not (1 <= len(title) <= 120):
        R.add("Q02", "error", "title must be 1-120 characters", rel, s)
        title = title if isinstance(title, str) else ""
    if not isinstance(detail, str) or len(detail.strip()) < 30:
        R.add("Q02", "error", "detail is missing or too short", rel, s)
        detail = detail if isinstance(detail, str) else ""
    if f["severity"] not in SEVERITIES:
        R.add("Q02", "error", f"severity {f['severity']!r} is invalid", rel, s)
    if f["category"] not in CATEGORIES:
        R.add("Q02", "error", f"category {f['category']!r} is invalid (there is no security category)", rel, s)
    if f["confidence"] not in CONFIDENCES:
        R.add("Q02", "error", f"confidence {f['confidence']!r} is invalid", rel, s)
    if "security" in str(f["category"]).lower() or SECURITY_RULE.match(str(f["ruleId"] or "")):
        R.add("Q02", "error", "security findings belong to the Security Analyzer", rel, s)
    text = f"{title}\n{detail}"
    if any(p.search(text) for p in SECRET_PATTERNS):
        R.add("Q02", "error", "title or detail contains secret-like content", rel, s)
    if SUPPRESSION.search(text):
        R.add("Q02", "warning", "detail suggests suppressing the warning", rel, s)

    cited = squash("\n".join(lines[s - 1:e]))
    quote = squash(f.get("evidenceQuote") or "")
    tools = f["tool"].split(",") if isinstance(f["tool"], str) else []
    unknown = [t for t in tools if t not in TOOLS]
    if unknown or not tools:
        R.add("Q03", "error", f"unknown tool(s) {unknown or f['tool']!r}", rel, s)
        return True
    if "manual" in tools:
        if tools != ["manual"] or f["ruleId"] is not None:
            R.add("Q03", "error", "manual findings must have tool exactly `manual` and ruleId null", rel, s)
        if not quote:
            R.add("Q03", "error", "manual finding has an empty evidenceQuote", rel, s)
        elif quote not in cited:
            R.add("Q03", "error", "evidenceQuote does not appear in the cited lines", rel, s)
    else:
        if quote and quote not in cited:
            R.add("Q03", "warning", "evidenceQuote does not appear in the cited lines", rel, s)
        completed = set(cdoc.get("toolsRun", []))
        for t in tools:
            if t != "lizard" and t not in completed:
                R.add("Q03", "error", f"tool '{t}' did not complete for this file but is cited", rel, s)
        real = [t for t in tools if t != "lizard"]
        if real:
            near = [c for c in cdoc.get("toolCandidates", []) if any(
                overlaps(o["line"], o["endLine"], s, e) for o in c.get("occurrences", [{"line": c["line"], "endLine": c["endLine"]}]))]
            if not (near and set(real) <= {t for c in near for t in c["tools"]}
                    and f["ruleId"] in {r for c in near for r in c["ruleIds"]}):
                R.add("Q03", "error", f"no pre-stage candidate reports rule {f['ruleId']!r} near these lines (tool "
                      f"{f['tool']}): copy tool and ruleId from the candidates", rel, s)
        if "lizard" in tools:
            near = [m for m in cdoc.get("metricCandidates", []) if m["metric"] == f["ruleId"] and overlaps(m["line"], m["endLine"], s, e)]
            if f["ruleId"] not in METRICS or not near:
                R.add("Q03", "error", f"no metric candidate {f['ruleId']!r} overlaps these lines: a lizard finding needs one", rel, s)
            else:
                m = near[0]
                want = "high" if m["value"] >= 2 * m["threshold"] else "medium"
                if f["severity"] in SEVERITIES and f["severity"] != want:
                    R.add("Q04", "warning", f"{f['ruleId']}={m['value']} (threshold {m['threshold']}) should be {want} by the "
                          f"severity definitions, the finding says {f['severity']}", rel, s)
    # complexity numbers must equal lizard
    funcs = [fn for fn in cdoc.get("functions", []) if overlaps(fn["startLine"], fn["endLine"], s, e)]
    if f["category"] == "complexity" or "lizard" in tools:
        allowed = metric_allowed(funcs, thresholds, len(lines))
        claimed = [(kind, int(m.group(1))) for kind, pat in METRIC_PATTERNS.items() for m in pat.finditer(text)]
        if not claimed:
            R.add("Q04", "warning", "complexity finding does not quote lizard numbers", rel, s)
        elif not funcs and "lines" not in {k for k, _ in claimed}:
            R.add("Q04", "error", "numbers claimed but no lizard function metrics overlap the range", rel, s)
        for kind, value in claimed:
            if value not in allowed[kind]:
                R.add("Q04", "error", f"{kind} {value} does not match the lizard metrics {sorted(allowed[kind])}", rel, s)
    return True


def keep_target(items, d):
    """Index of the finding a `keep` points at: the line lies in its range and, if given, the ruleId matches."""
    for i, x in enumerate(items):
        lr = x["lineRange"]
        if lr["start"] <= (d.get("line") or -1) <= lr["end"] and ("ruleId" not in d or d.get("ruleId") == x["ruleId"]):
            return i
    return None


def expand_groups(R, triage, groups_doc, decided):
    groups = {g["id"]: g for g in groups_doc}
    for d in triage.get("groups", []):
        g = groups.get(d.get("group"))
        if g is None:
            R.add("Q05", "error", f"group {d.get('group')!r} is not a rule group of files.json")
            continue
        ids = set(g["ids"])
        if d.get("action") != "drop":
            R.add("Q05", "error", f"group {g['id']}: only a drop can cover a group; decide keeps per candidate")
            continue
        reason = (d.get("reason") or "").strip()
        if len(reason) < 40:
            R.add("Q05", "error", f"group {g['id']} dropped without an adequate reason ({len(reason)} chars, need 40)")
        sampled = set(d.get("sampled") or [])
        if not sampled <= ids or len(sampled) < min(3, len(ids)):
            R.add("Q05", "error", f"group {g['id']}: `sampled` must list at least {min(3, len(ids))} member ids "
                                  "(`file#T1`) of the group that you read")
        exceptions = set(d.get("except") or [])
        if not exceptions <= ids:
            R.add("Q05", "error", f"group {g['id']}: `except` lists ids that are not in the group")
        for cid in sorted(ids - exceptions):
            decided.setdefault(cid, {"action": "drop", "reason": reason, "bulk": True})


def run_audit(aee, require_approval):
    R = Report()
    work = aee / "quality-work"
    idx = load_json(work / "files.json")
    status = (load_json(work / "tool_status.json") or {}).get("tools")
    triage = load_json(work / "triage.json")
    verdict = load_json(work / "verdict.json")
    if not (idx and status is not None):
        R.add("Q00", "error", "the pre-stage did not complete (files.json or tool_status.json missing)")
        return R
    thresholds = idx["thresholds"]
    analysed = [f for f in idx["files"] if f["analyze"]]
    skipped = [f for f in idx["files"] if not f["analyze"]]
    cdocs = {f["file"]: (load_json(work / "candidates" / f"{f['file']}.json") or {}) for f in analysed}

    findings = {}
    for f in analysed:
        rel = f["file"]
        items = load_json(aee / "quality" / f"{rel}.findings.json")
        if items is None:
            R.add("Q01", "error", "findings file is missing or not valid JSON", rel)
            continue
        if not isinstance(items, list):
            R.add("Q01", "error", "findings file must be a JSON array", rel)
            continue
        lines = read_lines(rel)
        ok = [x for x in items if check_finding(R, rel, x, lines, cdocs[rel], thresholds)]
        findings[rel] = ok
        if len(items) > MAX_PER_FILE:
            R.add("Q07", "error", f"{len(items)} findings in one file - noise, not coverage", rel)
        for i, x in enumerate(ok):
            for y in ok[i + 1:]:
                if x["ruleId"] == y["ruleId"] and x["tool"] == y["tool"] and x["category"] == y["category"] \
                        and overlaps(x["lineRange"]["start"], x["lineRange"]["end"], y["lineRange"]["start"], y["lineRange"]["end"]):
                    R.add("Q07", "error", f"overlapping findings for {x['ruleId'] or x['category']} at lines "
                          f"{x['lineRange']['start']}-{x['lineRange']['end']} and {y['lineRange']['start']}-{y['lineRange']['end']}",
                          rel, x["lineRange"]["start"])
    for f in skipped:
        if (aee / "quality" / f"{f['file']}.findings.json").exists():
            R.add("Q01", "error", f"skipped file ({f['skipReason']}) has a findings file", f["file"])

    # triage
    if triage is None:
        R.add("Q05", "error", "triage.json is missing: the analyzer has not delivered")
    else:
        decided = {}
        tfiles = triage.get("files", {})
        for rel, entry in tfiles.items():
            for d in entry.get("candidates", []):
                key = f"{rel}#{d.get('id')}"
                if key in decided:
                    R.add("Q05", "error", f"candidate {key} is decided twice", rel)
                decided[key] = d
        expand_groups(R, triage, idx.get("ruleGroups", []), decided)
        reviewed = set(triage.get("reviewedWithoutCandidates", []))
        targeted = {rel: set() for rel in findings}
        for f in analysed:
            rel = f["file"]
            cdoc = cdocs[rel]
            cands = [("T", c) for c in cdoc.get("toolCandidates", [])] + [("M", c) for c in cdoc.get("metricCandidates", [])]
            if not cands and rel not in reviewed and rel not in tfiles:
                R.add("Q05", "error", "file has no candidates and is not listed in `reviewedWithoutCandidates`", rel)
            for kind, c in cands:
                key = f"{rel}#{c['id']}"
                d = decided.get(key)
                line = c["line"]
                if not d:
                    R.add("Q05", "error", f"candidate {key} has no decision in triage.json", rel, line)
                elif d.get("action") == "drop":
                    if len((d.get("reason") or "").strip()) < 15:
                        R.add("Q05", "error", f"candidate {key} dropped without an adequate reason (need 15 characters)", rel, line)
                elif d.get("action") == "keep":
                    hit = keep_target(findings.get(rel, []), d)
                    if hit is None:
                        R.add("Q05", "error", f"keep of {key} does not point at a finding of {rel} (line + ruleId)", rel, line)
                    else:
                        targeted[rel].add(hit)
                else:
                    R.add("Q05", "error", f"candidate {key} action must be 'keep' or 'drop'", rel, line)
        for rel, items in findings.items():
            for i, x in enumerate(items):
                if x["tool"] != "manual" and i not in targeted.get(rel, set()):
                    R.add("Q05", "error", "tool- or metric-backed finding is not the target of any keep in triage.json",
                          rel, x["lineRange"]["start"])
        dropped = [d for d in decided.values() if d.get("action") == "drop"]
        total = sum(len(c.get("toolCandidates", [])) + len(c.get("metricCandidates", [])) for c in cdocs.values())
        if total >= 10 and len(dropped) / total > 0.7:
            R.add("Q05", "warning", f"{len(dropped)} of {total} candidates dropped - read the drop reasons")
        for reason, n in Counter(norm_reason(d.get("reason")) for d in dropped if not d.get("bulk")).most_common():
            if n >= 3:
                R.add("Q05", "warning", f"the same drop reason is used {n}x: '{reason[:80]}' - copy-pasted triage?")

    # log
    rows = jsonl(aee / "quality-log.jsonl")
    if None in rows:
        R.add("Q06", "error", "quality-log.jsonl contains a line that is not valid JSON")
    by_file = {}
    for r in rows:
        if r:
            by_file.setdefault(r.get("file"), []).append(r)
    for f in analysed:
        rel = f["file"]
        got = by_file.get(rel, [])
        if len(got) != 1:
            R.add("Q06", "error", f"expected exactly 1 quality-log row, found {len(got)}", rel)
            continue
        r, items = got[0], findings.get(rel, [])
        if any(k not in r for k in ("findingCount", "highCount", "mediumCount", "maxComplexity", "lines", "toolsRun", "analyzedAt")):
            R.add("Q06", "error", "log row is missing fields", rel)
            continue
        exp = (len(items), sum(x["severity"] == "high" for x in items), sum(x["severity"] == "medium" for x in items))
        if (r["findingCount"], r["highCount"], r["mediumCount"]) != exp:
            R.add("Q06", "error", f"log counts {(r['findingCount'], r['highCount'], r['mediumCount'])} do not match the findings {exp}", rel)
        if r["maxComplexity"] != f.get("maxCcn"):
            R.add("Q06", "error", f"log maxComplexity {r['maxComplexity']} differs from lizard ({f.get('maxCcn')})", rel)
        if set(r["toolsRun"]) != set(f.get("toolsRun", [])):
            R.add("Q06", "error", f"log toolsRun {r['toolsRun']} differs from the tools that completed", rel)
        if r["lines"] != f["lines"]:
            R.add("Q06", "error", f"log lines {r['lines']} differs from the pre-stage value {f['lines']}", rel)

    # secrets
    texts = []
    if triage:
        for rel, entry in triage.get("files", {}).items():
            texts += [("triage", str(d.get("reason", ""))) for d in entry.get("candidates", [])]
        texts += [("triage", str(g.get("reason", ""))) for g in triage.get("groups", [])]
    if verdict:
        texts += [("verdict", f"{i.get('message', '')} {i.get('fix', '')}") for i in verdict.get("issues", [])]
    for where, text in texts:
        if any(p.search(text) for p in SECRET_PATTERNS):
            R.add("Q08", "error", f"{where} contains secret-like content")
    if require_approval:
        if verdict is None:
            R.add("Q09", "error", "verdict.json is missing: the reviewer has not delivered")
        elif verdict.get("verdict") != "approve" or verdict.get("issues"):
            R.add("Q09", "error", f"the reviewer's verdict is {verdict.get('verdict')!r}"
                  + (f" with {len(verdict.get('issues'))} open issue(s)" if verdict.get("issues") else ""))
    return R


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--require-approval", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the issues as JSON")
    a = ap.parse_args(argv)
    R = run_audit(Path(a.aee), a.require_approval)
    if a.json:
        print(json.dumps({"status": "FAIL" if R.errors else "PASS", "issues": R.issues}, indent=2))
    else:
        print(f"AUDIT: {'FAIL' if R.errors else 'PASS'}  errors={len(R.errors)} "
              f"warnings={sum(i['severity'] == 'warning' for i in R.issues)}")
        for i in R.issues:
            loc = (i["file"] or "-") + (f":L{i['line']}" if i["line"] else "")
            print(f"  [{i['severity'].upper():7}] {i['code']} {loc} {i['message']}")
    return 2 if any(i["code"] == "Q00" for i in R.issues) else (1 if R.errors else 0)


if __name__ == "__main__":
    sys.exit(main())
