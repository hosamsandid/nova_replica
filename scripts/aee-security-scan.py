#!/usr/bin/env python3
"""Security pre-stage: run the static scanners and normalise their output (no model involved).

Usage: aee-security-scan [--aee DIR] [--raw-dir DIR] [FILE ...]

  FILE ...   scan exactly these files; otherwise the files of the last batch of the Extract Log
             (lines without `error`). A file is only scanned if it has a KB record and its
             content still matches the KB record's hash; skipped files are reported, never
             described as clean.

Scanners (never `--config auto`): gitleaks (assigned files only, redacted), semgrep
(p/security-audit + p/owasp-top-ten, or SEMGREP_RULES_DIR for an offline mirror; AIRGAPPED=1
refuses the registry), bandit -ll (Python), spotbugs (only if compiled classes exist).
A missing or failed scanner is a warning: its files are not reported as scanned clean.
Raw scanner output stays under --raw-dir (default /tmp/scan/security) and is never committed.

Writes <aee>/security-work/ (the analyzer's input):
  files.json           assigned files: language, KB path, analyze flag, skip reason
  scanner_status.json  per scanner: status, version, rules, files it completed
  candidates.json      one normalised candidate per merged scanner result (id Cnnn), with a redacted
                       snippet, the KB surface on those lines and the same secret in other files; a rule
                       that fires GROUP_MIN (10) times or more among non-strong candidates is a rule group
                       (`ruleGroups`, id Rnn, members carry `ruleGroup`) the analyzer can drop in one decision
  coverage.json        sink patterns and KB surface entries nothing covers (ids Gnnn): questions, not findings;
                       a pattern that fires GROUP_MIN times or more is ONE item with `count` and `occurrences`
  warnings.json        scanner problems and skipped files
and the skeleton the analyzer fills in (the template of its output; nothing is final yet):
  <aee>/security/<file>.findings.json   `[]` for every analysed file
  <aee>/security-log.jsonl              one row per analysed file with zero counts and the scanners that ran
  <aee>/security-warnings.md            scanner status and warnings
Secret values never reach any of these files.

Exit code and stdout: 0 done; 10 `[PASS]` (no file to scan); 1 `[ERROR] ...`.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aee_security_lib import *  # noqa: E402,F401,F403

PASS, ERROR = 10, 1
CHUNK = int(os.environ.get("SCAN_CHUNK", "500"))
SEMGREP_TIMEOUT = os.environ.get("SEMGREP_TIMEOUT", "30")
SEMGREP_RULES_DIR = os.environ.get("SEMGREP_RULES_DIR")
AIRGAPPED = os.environ.get("AIRGAPPED", "0") == "1"
SNIP = int(os.environ.get("SNIPPET_CONTEXT", "3"))
TOOL_PREF = ["semgrep", "gitleaks", "bandit", "spotbugs"]
SEV_MAP = {"semgrep": {"ERROR": "high", "WARNING": "medium", "INFO": "low"},
           "bandit": {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}}
ROOT = Path.cwd()


class StageError(Exception):
    pass


def run(cmd, timeout=900):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def tool_version(exe):
    try:
        r = run([exe, "--version"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or r.stderr).splitlines():
        m = re.search(r"\d+\.\d+(?:\.\d+)?", line)
        if m:
            return line.strip()[:80] if line.lower().startswith(exe.split("/")[-1].lower()) else m.group(0)
    return None


# ---------------------------------------------------------------- assigned files
def assigned_files(aee, explicit):
    """[{"file", "language", "kbPath", "analyze", "skipReason"?}] and the batch id."""
    batch, wanted = None, []
    if explicit:
        wanted = list(dict.fromkeys(explicit))
    else:
        log = aee / "extract-log.jsonl"
        lines = [load for load in (_json_lines(log))]
        if not lines:
            return [], None
        batch = lines[-1].get("batch")
        for e in lines:
            if e.get("batch") == batch and e.get("file") not in wanted and "error" not in e:
                wanted.append(e["file"])
    out = []
    for f in wanted:
        kb_path = aee / "kb" / (f + ".json")
        kb = load_json(kb_path)
        rec = {"file": f, "language": None, "kbPath": kb_path.as_posix(), "analyze": False}
        if not Path(f).is_file():
            rec["skipReason"] = "file missing on disk"
        elif kb is None:
            rec["skipReason"] = "no KB record"
        elif sha256_file(f) != kb.get("hash"):
            rec["skipReason"] = "content changed since extraction (a newer batch will bring it)"
        elif kb.get("language") not in LANG_KEY:
            rec["skipReason"] = f"language {kb.get('language')!r} is not supported by the scanners"
        else:
            rec["language"], rec["analyze"] = LANG_KEY[kb["language"]], True
        out.append(rec)
    return out, batch


def _json_lines(path):
    out = []
    if Path(path).is_file():
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
    return out


# ---------------------------------------------------------------- scanners
def run_scanners(assigned, raw, warnings):
    langs = {f["file"]: f["language"] for f in assigned}
    exe = {n: shutil.which(n) for n in ("gitleaks", "semgrep", "bandit", "spotbugs")}
    status = {}
    raw.mkdir(parents=True, exist_ok=True)

    def record(name, state, detail="", scanned=None, **extra):
        status[name] = {"status": state, "detail": detail, "files": scanned or [],
                        "version": tool_version(exe[name]) if state == "ok" and exe[name] else None, **extra}
        if state != "ok":
            warnings.append(f"{name} {state.upper()}: {detail} - affected files must not be described as scanned clean")

    if exe["gitleaks"] and assigned:
        stage = raw / "gl-input"
        shutil.rmtree(stage, ignore_errors=True)
        for f in assigned:
            dst = stage / f["file"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f["file"], dst)
        report = raw / "gitleaks.json"
        report.unlink(missing_ok=True)
        cmd = [exe["gitleaks"], "dir", "--no-banner", "--redact", "--report-format", "json",
               "--report-path", str(report), "--exit-code", "0"]
        if os.environ.get("GITLEAKS_CONFIG"):
            cmd += ["--config", os.environ["GITLEAKS_CONFIG"]]
        r = run([*cmd, str(stage)])
        if r.returncode == 0 and (report.exists() or "no leaks" in (r.stderr + r.stdout).lower()):
            if not report.exists():
                write_text(report, "[]")
            record("gitleaks", "ok", scanned=[f["file"] for f in assigned])
        else:
            record("gitleaks", "failed", (r.stderr or r.stdout).strip()[:300])
    elif not exe["gitleaks"]:
        record("gitleaks", "missing", "not installed")

    if not exe["semgrep"]:
        record("semgrep", "missing", "not installed")
    elif assigned:
        configs = None
        if SEMGREP_RULES_DIR:
            if Path(SEMGREP_RULES_DIR).is_dir():
                configs = ["--config", SEMGREP_RULES_DIR]
            else:
                record("semgrep", "failed", f"SEMGREP_RULES_DIR {SEMGREP_RULES_DIR!r} is not a directory")
        elif AIRGAPPED:
            record("semgrep", "failed", "AIRGAPPED=1 but SEMGREP_RULES_DIR is not set; the registry packs need network")
        else:
            configs = ["--config", "p/security-audit", "--config", "p/owasp-top-ten"]
        if configs:
            merged, ok, detail = {"results": [], "errors": []}, True, ""
            for i in range(0, len(assigned), CHUNK):
                part = raw / f"semgrep.part{i // CHUNK}.json"
                chunk = [f["file"] for f in assigned[i:i + CHUNK]]
                r = run([exe["semgrep"], "scan", *configs, "--metrics", "off", "--disable-version-check",
                         "--timeout", SEMGREP_TIMEOUT, "--json", "-o", str(part), *chunk])
                data = load_json(part)
                if r.returncode not in (0, 1) or data is None:
                    ok, detail = False, (r.stderr or r.stdout).strip()[-300:]
                    break
                merged["results"] += data.get("results", [])
                merged["errors"] += data.get("errors", [])
                part.unlink(missing_ok=True)
            if ok:
                dump_json(raw / "semgrep.json", merged)
                rules = (f"local:{_rules_fingerprint(SEMGREP_RULES_DIR)}" if SEMGREP_RULES_DIR
                         else "registry:p/security-audit+p/owasp-top-ten")
                record("semgrep", "ok", scanned=[f["file"] for f in assigned], rules=rules)
                if merged["errors"]:
                    warnings.append(f"semgrep reported {len(merged['errors'])} non-fatal error(s); see raw output")
            else:
                record("semgrep", "failed", detail or "semgrep returned an error (rules download / network?)")

    py = [f for f in assigned if langs[f["file"]] == "python"]
    if py:
        if exe["bandit"]:
            out = raw / "bandit.json"
            r = run([exe["bandit"], "-ll", "-f", "json", "-o", str(out), *[f["file"] for f in py]])
            if r.returncode in (0, 1) and out.exists():
                record("bandit", "ok", scanned=[f["file"] for f in py], rules="bandit -ll (medium and above)")
            else:
                record("bandit", "failed", (r.stderr or r.stdout).strip()[-300:])
        else:
            record("bandit", "missing", "not installed")

    if any(langs[f["file"]] == "java" for f in assigned):
        classes = sorted({str(p.parent) for p in ROOT.rglob("*.class") if ".git" not in p.parts})[:50]
        if not classes:
            warnings.append("spotbugs skipped: no compiled classes (Java files covered by semgrep and the manual pass only)")
            status["spotbugs"] = {"status": "skipped", "detail": "no compiled classes", "files": []}
        elif not exe["spotbugs"]:
            record("spotbugs", "missing", "not installed")
        else:
            out = raw / "spotbugs.xml"
            r = run([exe["spotbugs"], "-textui", "-xml:withMessages", "-output", str(out), *classes])
            if r.returncode == 0 and out.exists():
                record("spotbugs", "ok", scanned=[f["file"] for f in assigned if langs[f["file"]] == "java"])
            else:
                record("spotbugs", "failed", (r.stderr or r.stdout).strip()[-300:])
    shutil.rmtree(raw / "gl-input", ignore_errors=True)
    return status


def _rules_fingerprint(d):
    h = hashlib.sha256()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file() and p.suffix in (".yml", ".yaml"):
            h.update(p.relative_to(d).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- normalise
def snippet(rel, start, end, secret):
    lines = read_lines(rel)
    a, b = max(1, start - SNIP), min(len(lines), end + SNIP)
    fmt = redact_line if secret else redact_secrets
    return {"startLine": a, "lines": [f"{n}: {fmt(lines[n - 1])}" for n in range(a, b + 1)]}


def collect(assigned, status, raw, warnings):
    real = {os.path.realpath(ROOT / rel): rel for rel in assigned}
    stage = raw / "gl-input"

    def to_rel(p):
        p = p.replace("\\", "/")
        for root in {str(stage).replace("\\", "/"), os.path.realpath(stage).replace("\\", "/")}:
            if p.startswith(root + "/"):
                r = p[len(root) + 1:]
                return r if r in assigned else None
        for cand in ([Path(p)] if os.path.isabs(p) else []) + [ROOT / p]:
            rp = os.path.realpath(cand)
            if rp in real:
                return real[rp]
        p2 = p.lstrip("./")
        return p2 if p2 in assigned else None

    out = []
    ok = lambda s: status.get(s, {}).get("status") == "ok"

    if ok("gitleaks"):
        for r in load_json(raw / "gitleaks.json", []) or []:
            rel = to_rel(r.get("File", ""))
            if rel:
                s, e = r.get("StartLine", 1), r.get("EndLine", r.get("StartLine", 1))
                out.append(dict(file=rel, tool="gitleaks", ruleId=r.get("RuleID"), lineRange={"start": s, "end": e},
                                scannerSeverity="n/a", suggestedSeverity="high",
                                message=r.get("Description", "Secret detected"),  # never r["Secret"] / r["Match"]
                                cweId="CWE-798", owaspCategory=OWASP["A07"],
                                secretGroup=secret_group(literals_on_lines(rel, s, e))))
    if ok("semgrep"):
        for r in (load_json(raw / "semgrep.json", {}) or {}).get("results", []):
            rel = to_rel(r.get("path", ""))
            if not rel:
                continue
            ex, md = r.get("extra", {}), r.get("extra", {}).get("metadata", {})
            cwe, sev = norm_cwe(md.get("cwe")), ex.get("severity", "WARNING")
            out.append(dict(file=rel, tool="semgrep", ruleId=r.get("check_id"),
                            lineRange={"start": r["start"]["line"], "end": r["end"]["line"]},
                            scannerSeverity=sev, suggestedSeverity=SEV_MAP["semgrep"].get(sev, "medium"),
                            message=redact_secrets(ex.get("message") or "")[:300], cweId=cwe,
                            owaspCategory=owasp_from(md.get("owasp"), cwe)))
    if ok("bandit"):
        for r in (load_json(raw / "bandit.json", {}) or {}).get("results", []):
            rel = to_rel(r.get("filename", ""))
            if not rel:
                continue
            lr = r.get("line_range") or [r.get("line_number", 1)]
            cwe, sev = norm_cwe((r.get("issue_cwe") or {}).get("id")), r.get("issue_severity", "MEDIUM")
            out.append(dict(file=rel, tool="bandit", ruleId=r.get("test_id"),
                            lineRange={"start": min(lr), "end": max(lr)},
                            scannerSeverity=sev, suggestedSeverity=SEV_MAP["bandit"].get(sev, "medium"),
                            message=redact_secrets(f"{r.get('test_name', '')}: {r.get('issue_text', '')}")[:300],
                            cweId=cwe, owaspCategory=owasp_from(None, cwe)))
    if ok("spotbugs") and (raw / "spotbugs.xml").exists():
        try:
            for bug in ET.parse(raw / "spotbugs.xml").getroot().iter("BugInstance"):
                sl = bug.find("SourceLine")
                if sl is None or not sl.get("start"):
                    continue
                rel = to_rel(sl.get("sourcepath", "")) or next(
                    (a for a in assigned if a.endswith(sl.get("sourcepath", "#"))), None)
                if rel:
                    pri = bug.get("priority", "2")
                    out.append(dict(file=rel, tool="spotbugs", ruleId=bug.get("type"),
                                    lineRange={"start": int(sl.get("start")), "end": int(sl.get("end", sl.get("start")))},
                                    scannerSeverity=f"priority-{pri}",
                                    suggestedSeverity={"1": "high", "2": "medium"}.get(pri, "low"),
                                    message=redact_secrets(bug.findtext("LongMessage") or bug.get("type", ""))[:300],
                                    cweId=None, owaspCategory=None))
        except ET.ParseError:
            warnings.append("spotbugs.xml could not be parsed - spotbugs results ignored")
    return out


def merge(results):
    """Merge results from different tools on the same file / CWE / overlapping lines."""
    results.sort(key=lambda r: (r["file"], r["lineRange"]["start"], TOOL_PREF.index(r["tool"])))
    merged = []
    for r in results:
        target = next((m for m in merged if m["file"] == r["file"] and r["cweId"] and m["cweId"] == r["cweId"]
                       and ranges_overlap(m["lineRange"], r["lineRange"])
                       and r["tool"] not in {s["tool"] for s in m["sources"]}), None)
        src = {"tool": r["tool"], "ruleId": r["ruleId"], "scannerSeverity": r["scannerSeverity"]}
        if target:
            target["sources"].append(src)
            target["lineRange"] = {"start": min(target["lineRange"]["start"], r["lineRange"]["start"]),
                                   "end": max(target["lineRange"]["end"], r["lineRange"]["end"])}
            if SEVERITIES.index(r["suggestedSeverity"]) < SEVERITIES.index(target["suggestedSeverity"]):
                target["suggestedSeverity"] = r["suggestedSeverity"]
        else:
            r["sources"] = [src]
            merged.append(r)
    for m in merged:
        m["tool"] = ",".join(s["tool"] for s in m["sources"])
        m["ruleId"] = m["sources"][0]["ruleId"]
        m["fingerprint"] = candidate_fingerprint(m["file"], m["cweId"] or m["ruleId"] or "",
                                                 m["lineRange"]["start"], m["lineRange"]["end"])
    return merged


def finish(cands, kbs):
    for i, c in enumerate(cands, 1):
        c["id"] = f"C{i:03d}"
        secret = c["cweId"] in SECRET_CWES or "gitleaks" in c["tool"]
        c["snippet"] = snippet(c["file"], c["lineRange"]["start"], c["lineRange"]["end"], secret)
        c["needsCweFromAgent"] = c["cweId"] is None
        c["kbSurface"] = kb_surface(kbs.get(c["file"]), c["lineRange"]["start"], c["lineRange"]["end"])
    groups = {}
    for c in cands:
        if c.get("secretGroup"):
            groups.setdefault(c["secretGroup"], set()).add(c["file"])
    for c in cands:
        g = c.get("secretGroup")
        c["sameSecretInOtherFiles"] = sorted(groups.get(g, set()) - {c["file"]}) if g else []
    return rule_groups(cands)


def rule_groups(cands):
    """Rules that fire in many candidates (never high-severity or secret ones): the analyzer may drop such a
    group in one decision after sampling it. Each member carries `ruleGroup`."""
    by_rule = {}
    for c in cands:
        if not is_strong(c):
            by_rule.setdefault(c["ruleId"], []).append(c)
    groups = sorted(((rid, items) for rid, items in by_rule.items() if len(items) >= GROUP_MIN),
                    key=lambda t: (-len(t[1]), str(t[0])))
    out = []
    for i, (rid, items) in enumerate(groups, 1):
        gid = f"R{i:02d}"
        for c in items:
            c["ruleGroup"] = gid
        out.append({"id": gid, "ruleId": rid, "count": len(items), "files": len({c["file"] for c in items}),
                    "cweId": items[0]["cweId"], "suggestedSeverity": items[0]["suggestedSeverity"],
                    "ids": [c["id"] for c in items]})
    return out


# ---------------------------------------------------------------- stage
STALE = ("triage.json", "verdict.json", "escalation.md", "decisions.json", "judge-packet.json", "judge-verdict.json",
         "judge-prev.json", "judge-invalid.json", "verification-report.json", "feedback.md")


def write_skeleton(aee, per_file, generated, status, warnings):
    """The analyzer's template: an empty findings list and a zeroed log row per analysed file."""
    for f in per_file:
        dump_json(aee / "security" / f"{f['file']}.findings.json", [])
    log = aee / "security-log.jsonl"
    rows = [r for r in _json_lines(log) if r.get("file") not in {f["file"] for f in per_file}]
    rows += [{"file": f["file"], "findingCount": 0, "criticalCount": 0, "highCount": 0,
              "toolsRun": f["scannersCompleted"], "analyzedAt": generated} for f in per_file]
    rows.sort(key=lambda r: r["file"])
    write_text(log, "".join(json.dumps(r) + "\n" for r in rows))
    lines = ["# Security warnings", ""] + ([f"- {w}" for w in warnings] or ["None."])
    lines += ["", "## Scanners", ""] + [
        f"- {k}: {v['status']}" + (f" ({v['version']})" if v.get("version") else "")
        + (f" - {v['detail']}" if v.get("detail") else "") for k, v in sorted(status.items())]
    write_text(aee / "security-warnings.md", "\n".join(lines) + "\n")


def run_stage(a):
    aee = Path(a.aee)
    work = aee / "security-work"
    files, batch = assigned_files(aee, a.files)
    if not files:
        return None
    warnings = []
    for f in files:
        if not f["analyze"]:
            warnings.append(f"{f['file']} skipped: {f['skipReason']}")
    assigned_recs = [f for f in files if f["analyze"]]
    assigned = {f["file"]: f for f in assigned_recs}
    if not assigned_recs:
        dump_json(work / "files.json", {"generatedAt": now_iso(), "batch": batch, "files": files})
        dump_json(work / "warnings.json", warnings)
        for stale in STALE:
            (work / stale).unlink(missing_ok=True)
        return files, [], {}, warnings

    raw = Path(a.raw_dir)
    shutil.rmtree(raw, ignore_errors=True)
    status = run_scanners(assigned_recs, raw, warnings)
    kbs = {rel: load_json(f["kbPath"]) for rel, f in assigned.items()}
    cands = merge(collect(assigned, status, raw, warnings))
    groups = finish(cands, kbs)

    per_file = []
    for rel, f in assigned.items():
        done = [s for s, v in status.items() if v["status"] == "ok" and rel in v["files"]]
        per_file.append({"file": rel, "language": f["language"], "scannersCompleted": done,
                         "kbRecord": f["kbPath"], "candidateIds": [c["id"] for c in cands if c["file"] == rel]})
    generated = now_iso()
    cand_doc = {"generatedAt": generated, "batch": batch,
                "instructions": "Triage every candidate in decisions.json (keep or drop with a reason). "
                                "Gaps are in coverage.json. Never copy a secret value.",
                "files": per_file, "ruleGroups": groups, "candidates": cands,
                "crossFileSecretGroups": len({c["secretGroup"] for c in cands
                                              if c.get("secretGroup") and c["sameSecretInOtherFiles"]})}
    sink_gaps, surface_gaps = coverage_gaps(assigned_recs, cand_doc, kbs)
    for i, g in enumerate(sink_gaps + surface_gaps, 1):
        g["id"] = f"G{i:03d}"
    dump_json(work / "files.json", {"generatedAt": generated, "batch": batch, "files": files})
    dump_json(work / "scanner_status.json", {"generatedAt": generated, "airgapped": AIRGAPPED, "scanners": status})
    dump_json(work / "candidates.json", cand_doc)
    dump_json(work / "coverage.json", {"generatedAt": generated, "sinkGaps": sink_gaps, "surfaceGaps": surface_gaps})
    dump_json(work / "warnings.json", warnings)
    for stale in STALE:
        (work / stale).unlink(missing_ok=True)  # a new batch never inherits an earlier run's triage or verdict
    write_skeleton(aee, per_file, generated, status, warnings)
    return files, cands, status, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--raw-dir", default="/tmp/scan/security")
    a = ap.parse_args(argv)
    try:
        result = run_stage(a)
    except (StageError, OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print(f"[ERROR] {e}")
        return ERROR
    if result is None:
        print("[PASS]")
        return PASS
    files, cands, status, warnings = result
    n_ok = sum(1 for f in files if f["analyze"])
    print(f"{n_ok} files scanned ({len(files) - n_ok} skipped), {len(cands)} candidates; scanners: "
          + (", ".join(f"{k}={v['status']}" for k, v in status.items()) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
