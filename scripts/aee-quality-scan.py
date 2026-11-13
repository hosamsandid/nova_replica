#!/usr/bin/env python3
"""Quality pre-stage: run the linters and complexity metrics, normalise and pre-triage their output (no model).

Usage: aee-quality-scan [--aee DIR] [--raw-dir DIR] [FILE ...]

  FILE ...   analyse exactly these files; otherwise the files of the last batch of the intake ledger
             (`skipped: false`). Files that are not a quality target (no Python, shell, Dockerfile, Java,
             JavaScript/TypeScript, Go, C or C++ by name, extension or shebang) are ignored. Generated,
             vendored and fixture files are skipped and reported, never described as clean.

Each linter runs ONCE over all files of its languages (cwd = repository root): ruff, pylint (Python), shellcheck
(shell), hadolint (Dockerfile), pmd, checkstyle (Java); lizard measures every supported language. A missing or failed
tool is a warning: its files are never described as lint-clean. JS/TS, Go, C and C++ have no linter here: lizard
metrics and the manual pass only. Raw output stays under --raw-dir (default /tmp/scan/quality), never committed.

Pre-triage, deterministic (ported from the quality-pipeline pre-work):
  * ruff `S*` and pylint `eval`/`exec` rules are dropped (they belong to the Security Analyzer), and pylint E0401
  * identical (file, line, rule-equivalent) hits of different tools are merged (rule equivalence table)
  * repeated hits of one rule in a file become one cluster candidate with `count` and `occurrences`
  * metric candidates: CCN > 15, function > 100 lines, > 6 parameters, nesting > 4, file > 1000 lines
  * a rule that fires as a non-error candidate in GROUP_MIN (10) files or more is a rule group (`R01`…)
Defaults are overridden by the optional <aee>/quality-config.json.

Writes <aee>/quality-work/ (the analyzer's input):
  files.json             analysed files: language, role, tools run and missing, candidate counts, lines, max CCN
  candidates/<file>.json per file: toolCandidates (T1…), metricCandidates (M1…), lizard `functions`, thresholds
  tool_status.json       per tool: status, version, config used, files it completed; `ruleGroups` are in files.json
  warnings.json          tool problems, unlinted languages, skipped files
and the skeleton the analyzer fills in (nothing is final yet):
  <aee>/quality/<file>.findings.json   `[]` for every analysed file
  <aee>/quality-log.jsonl              one row per analysed file with zero counts, `maxComplexity`, `lines`, `toolsRun`
  <aee>/quality-warnings.md            tools, languages without a linter, skipped files

Exit code and stdout: 0 done; 10 `[PASS]` (no quality file in the batch); 1 `[ERROR] ...`.
"""
import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aee_quality_lib import *  # noqa: E402,F401,F403

PASS, ERROR = 10, 1
ROOT = Path.cwd()
SEV_ORDER = {"info": 0, "warning": 1, "error": 2}
PYLINT_SEV = {"error": "error", "fatal": "error", "warning": "warning", "refactor": "warning",
              "convention": "info", "info": "info"}
SHELLCHECK_SEV = {"error": "error", "warning": "warning", "info": "info", "style": "info"}
HADOLINT_SEV = {"error": "error", "warning": "warning", "info": "info", "style": "info"}
PMD_SEV = {1: "error", 2: "error", 3: "warning", 4: "info", 5: "info"}
SECURITY_RE = {"ruff": re.compile(r"^S\d{3}$")}
SECURITY_IDS = {"pylint": {"W0122", "W0123"}}  # exec-used, eval-used
ENV_NOISE = {("pylint", "E0401")}
CLUSTER_MAX_OCCURRENCES = 20
RAW_NAME = {"ruff": "ruff.json", "pylint": "pylint.json", "shellcheck": "shellcheck.json", "hadolint": "hadolint.json",
            "pmd": "pmd.json", "checkstyle": "checkstyle.xml", "lizard": "lizard.csv"}


class StageError(Exception):
    pass


# ---------------------------------------------------------------- files
def batch_files(aee, explicit):
    """(files, batch): the last batch of the intake ledger, or the explicit list."""
    if explicit:
        return list(dict.fromkeys(explicit)), None
    rows = [r for r in jsonl(aee / "intake.jsonl") if r]
    if not rows:
        return [], None
    batch = rows[-1].get("batch")
    files = []
    for r in rows:
        if r.get("batch") == batch and r.get("skipped") is False and "error" not in r and r["file"] not in files:
            files.append(r["file"])
    return files, batch


def describe_files(wanted, cfg, ledger_loc):
    """[{"file", "language", "role", "analyze", "skipReason"?, "lines"}] for every quality target."""
    out = []
    for f in wanted:
        lang = quality_language(f)
        if lang is None:
            continue
        rec = {"file": f, "language": lang, "role": None, "analyze": False}
        if not Path(f).is_file():
            rec["skipReason"] = "file missing on disk"
        else:
            head = "\n".join(read_lines(f)[:20])
            rec["role"] = classify_role(f, head, cfg)
            if rec["role"] in SKIP_ROLES:
                rec["skipReason"] = f"role {rec['role']}"
            else:
                rec["analyze"] = True
        rec["lines"] = ledger_loc.get(f) or sum(1 for line in read_lines(f) if line.strip())
        out.append(rec)
    return out


# ---------------------------------------------------------------- tool runs
def rel_to_root(p):
    p = Path(p)
    return p.relative_to(ROOT).as_posix() if p.is_absolute() else p.as_posix()


def first_existing(base, names):
    for name in names:
        if (base / name).is_file():
            return rel_to_root(base / name)
    return None


def pyproject_has(base, section):
    p = base / "pyproject.toml"
    return p.is_file() and section in p.read_text(encoding="utf-8", errors="replace")


def detect_configs(base):
    """Repo config wins over tool defaults; None means the tool default. Paths are relative to the repo root."""
    ruff = first_existing(base, ["ruff.toml", ".ruff.toml"]) or (
        rel_to_root(base / "pyproject.toml") if pyproject_has(base, "[tool.ruff") else None)
    pylint = first_existing(base, [".pylintrc", "pylintrc"]) or (
        rel_to_root(base / "pyproject.toml") if pyproject_has(base, "[tool.pylint") else None)
    return {"ruff": ruff, "pylint": pylint, "shellcheck": first_existing(base, [".shellcheckrc"]),
            "hadolint": first_existing(base, [".hadolint.yaml", ".hadolint.yml"]),
            "pmd": first_existing(base, ["pmd-ruleset.xml", "ruleset.xml", "config/pmd/ruleset.xml"]),
            "checkstyle": first_existing(base, ["checkstyle.xml", "config/checkstyle/checkstyle.xml"]), "lizard": None}


def build_command(tool, exe, files, configs, cfg):
    if tool == "ruff":
        return [exe, "check", "--output-format", "json", "--no-cache", *files]
    if tool == "pylint":
        rc = configs.get("pylint")
        return [exe, "--output-format=json2", *(["--rcfile", rc] if rc and rc.endswith("rc") else []), *files]
    if tool == "shellcheck":
        return [exe, "-S", cfg["shellcheckSeverity"], "-f", "json", *files]
    if tool == "hadolint":
        return [exe, "--failure-threshold", cfg["hadolintThreshold"], "--format", "json",
                *(["--config", configs["hadolint"]] if configs.get("hadolint") else []), *files]
    if tool == "pmd":
        return [exe, "check", "--no-progress", "--no-cache", "-R", configs.get("pmd") or "rulesets/java/quickstart.xml",
                "-f", "json", "-d", ",".join(files)]
    if tool == "checkstyle":
        return [exe, "-c", configs.get("checkstyle") or "/google_checks.xml", "-f", "xml", *files]
    return [exe, "-End", "--csv", *files]  # lizard with the nesting-depth extension


def exit_ok(tool, rc, out):
    if rc in (124, 127):
        return False
    if tool == "ruff":
        return rc in (0, 1) and out.strip().startswith("[")
    if tool == "pylint":
        try:
            json.loads(out)
        except ValueError:
            return False
        return True
    if tool in ("shellcheck", "hadolint"):
        return rc in (0, 1) and out.strip().startswith("[")
    if tool == "pmd":
        return rc in (0, 4) and out.strip().startswith("{")
    if tool == "checkstyle":
        return out.lstrip().startswith("<?xml") or out.lstrip().startswith("<checkstyle")
    return rc == 0


def tool_version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (r.stdout or r.stderr).splitlines():
        m = re.search(r"\d+\.\d+(?:\.\d+)?", line)
        if m:
            return m.group(0)
    return None


def run_tools(analysed, cfg, configs, raw, warnings):
    status = {}
    raw.mkdir(parents=True, exist_ok=True)
    for tool in ALL_TOOLS:
        files = [f["file"] for f in analysed if f["language"] in TOOL_LANGS[tool]]
        if not files:
            continue
        exe = shutil.which(tool)
        info = {"status": "MISSING", "detail": "not installed", "version": None, "files": [],
                "config": configs.get(tool) or "default"}
        if exe:
            r = subprocess.run(build_command(tool, exe, files, configs, cfg), cwd=ROOT, capture_output=True, text=True)
            if exit_ok(tool, r.returncode, r.stdout):
                (raw / RAW_NAME[tool]).write_text(r.stdout, encoding="utf-8")
                info.update(status="OK", detail="", version=tool_version(exe), files=files)
            else:
                info.update(status="FAILED", detail=f"exit {r.returncode}: {(r.stderr or r.stdout)[:200]}")
        if info["status"] != "OK":
            warnings.append(f"Tool `{tool}` is {info['status']} ({info['detail']}); its findings are missing, so "
                            f"{', '.join(TOOL_LANGS[tool])} files are never reported lint-clean.")
        status[tool] = info
    return status


# ---------------------------------------------------------------- parsers
def to_rel(path):
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return p.as_posix()
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def hit(file, tool, rule, line, end, message, sev):
    line = max(int(line), 1)
    return {"file": file, "tool": tool, "ruleId": rule, "line": line, "endLine": max(int(end or line), line),
            "message": message.strip(), "toolSeverity": sev}


def parse_ruff(text):
    out = []
    for item in json.loads(text or "[]"):
        code = item.get("code") or "syntax-error"
        loc = item["location"]
        end = item.get("end_location") or loc
        sev = "error" if re.match(r"^(E9|F63|F7|F82)", code) else "warning"
        out.append(hit(to_rel(item["filename"]), "ruff", code, loc["row"], end["row"], item["message"], sev))
    return out


def parse_pylint(text):
    data = json.loads(text or "{}")
    messages = data if isinstance(data, list) else data.get("messages", [])
    return [hit(to_rel(m["path"]), "pylint", m["messageId"], m["line"], m.get("endLine"), m["message"],
                PYLINT_SEV.get(m["type"], "warning")) for m in messages]


def parse_shellcheck(text):
    data = json.loads(text or "[]")
    items = data.get("comments", []) if isinstance(data, dict) else data
    return [hit(to_rel(i["file"]), "shellcheck", f"SC{i['code']}", i["line"], i.get("endLine"), i["message"],
                SHELLCHECK_SEV.get(i["level"], "warning")) for i in items]


def parse_hadolint(text):
    return [hit(to_rel(i["file"]), "hadolint", i["code"], i["line"], i["line"], i["message"],
                HADOLINT_SEV.get(i["level"], "warning")) for i in json.loads(text or "[]")]


def parse_pmd(text):
    out = []
    for f in json.loads(text or "{}").get("files", []):
        for v in f.get("violations", []):
            out.append(hit(to_rel(f["filename"]), "pmd", v["rule"], v["beginline"], v.get("endline"), v["description"],
                           PMD_SEV.get(v.get("priority", 3), "warning")))
    return out


def parse_checkstyle(text, force_info):
    out = []
    for f in ET.fromstring(text).iter("file"):
        name = to_rel(f.get("name", ""))
        for e in f.iter("error"):
            rule = (e.get("source") or "unknown").rsplit(".", 1)[-1].removesuffix("Check")
            sev = "info" if force_info else {"error": "error", "warning": "warning"}.get(e.get("severity"), "info")
            out.append(hit(name, "checkstyle", rule, int(e.get("line", 1)), None, e.get("message", ""), sev))
    return out


def parse_lizard(text):
    """lizard -End --csv: nloc,ccn,token,param,length,location,file,function,long_name,start,end,ND"""
    by_file = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 11:
            continue
        nloc, ccn, _tok, params, _len, _loc, file, func, _long, start, end = row[:11]
        nesting = int(row[11]) if len(row) > 11 and row[11].strip().isdigit() else 0
        by_file.setdefault(to_rel(file), []).append({
            "function": func, "startLine": int(start), "endLine": int(end), "ccn": int(ccn), "nloc": int(nloc),
            "params": int(params), "maxNesting": nesting})
    return by_file


def load_raw_hits(raw, checkstyle_default):
    parsers = {"ruff.json": parse_ruff, "pylint.json": parse_pylint, "shellcheck.json": parse_shellcheck,
               "hadolint.json": parse_hadolint, "pmd.json": parse_pmd,
               "checkstyle.xml": lambda t: parse_checkstyle(t, checkstyle_default)}
    hits = []
    for name, parser in parsers.items():
        p = raw / name
        if p.is_file():
            hits.extend(parser(p.read_text(encoding="utf-8")))
    lizard = parse_lizard((raw / "lizard.csv").read_text(encoding="utf-8")) if (raw / "lizard.csv").is_file() else {}
    return hits, lizard


# ---------------------------------------------------------------- pre-triage
def drop_reason(h):
    pattern = SECURITY_RE.get(h["tool"])
    if (pattern and pattern.match(h["ruleId"])) or h["ruleId"] in SECURITY_IDS.get(h["tool"], ()):
        return "security_scope"
    if (h["tool"], h["ruleId"]) in ENV_NOISE:
        return "env_noise"
    return None


def merge_hits(hits):
    groups = {}
    for h in sorted(hits, key=lambda x: (x["line"], x["tool"], x["ruleId"])):
        canon = canonical_rule(h["tool"], h["ruleId"])
        key = (h["file"], canon, h["line"])
        g = groups.get(key)
        if g is None:
            groups[key] = {"canonical": canon, "tools": {h["tool"]}, "ruleIds": {h["ruleId"]}, "line": h["line"],
                           "endLine": h["endLine"], "message": h["message"], "toolSeverity": h["toolSeverity"],
                           "perTool": {h["tool"]: 1}}
        else:
            g["tools"].add(h["tool"])
            g["ruleIds"].add(h["ruleId"])
            g["endLine"] = max(g["endLine"], h["endLine"])
            if SEV_ORDER[h["toolSeverity"]] > SEV_ORDER[g["toolSeverity"]]:
                g["toolSeverity"] = h["toolSeverity"]
            g["perTool"][h["tool"]] = g["perTool"].get(h["tool"], 0) + 1
    return sorted(groups.values(), key=lambda g: (g["line"], g["canonical"]))


def cluster(merged):
    """One candidate per rule-equivalent per file: count + first range + occurrences."""
    by_rule = {}
    for g in merged:
        by_rule.setdefault(g["canonical"], []).append(g)
    out = []
    for canon, items in by_rule.items():
        first = items[0]
        per_tool = {}
        for g in items:
            for tool, n in g["perTool"].items():
                per_tool[tool] = per_tool.get(tool, 0) + n
        out.append({"canonical": canon, "tools": sorted({t for g in items for t in g["tools"]}),
                    "ruleIds": sorted({r for g in items for r in g["ruleIds"]}), "line": first["line"],
                    "endLine": first["endLine"], "message": first["message"],
                    "toolSeverity": max((g["toolSeverity"] for g in items), key=SEV_ORDER.__getitem__),
                    "count": max(per_tool.values()),
                    "occurrences": [{"line": g["line"], "endLine": g["endLine"]} for g in items][:CLUSTER_MAX_OCCURRENCES]})
    return sorted(out, key=lambda c: (c["line"], c["canonical"]))


def metric_candidates(functions, lines, thresholds):
    out = []
    checks = [("ccn", lambda f: f["ccn"]), ("function_lines", lambda f: f["endLine"] - f["startLine"] + 1),
              ("params", lambda f: f["params"]), ("nesting", lambda f: f["maxNesting"])]
    for fn in functions:
        for metric, getter in checks:
            value = getter(fn)
            if value > thresholds[metric]:
                out.append({"metric": metric, "value": value, "threshold": thresholds[metric],
                            "function": fn["function"], "line": fn["startLine"], "endLine": fn["endLine"]})
    if lines > thresholds["file_lines"]:
        out.append({"metric": "file_lines", "value": lines, "threshold": thresholds["file_lines"], "function": None,
                    "line": 1, "endLine": lines})
    for i, c in enumerate(out, 1):
        c["id"] = f"M{i}"
    return out


def rule_groups(per_file_candidates):
    """Non-error rule candidates that appear in GROUP_MIN files or more: droppable in one decision after sampling."""
    by_rule = {}
    for rel, cands in per_file_candidates.items():
        for c in cands:
            if c["toolSeverity"] != "error":
                by_rule.setdefault(c["canonical"], []).append((rel, c))
    out = []
    groups = sorted(((k, v) for k, v in by_rule.items() if len({r for r, _ in v}) >= GROUP_MIN),
                    key=lambda kv: (-len(kv[1]), kv[0]))
    for i, (canon, members) in enumerate(groups, 1):
        gid = f"R{i:02d}"
        for _, c in members:
            c["ruleGroup"] = gid
        out.append({"id": gid, "rule": canon, "count": len(members), "files": len({r for r, _ in members}),
                    "toolSeverity": max((c["toolSeverity"] for _, c in members), key=SEV_ORDER.__getitem__),
                    "ids": [f"{r}#{c['id']}" for r, c in members]})
    return out


# ---------------------------------------------------------------- outputs
def write_skeleton(aee, analysed, details, generated, status, warnings, skipped, cfg):
    for f in analysed:
        dump_json(aee / "quality" / f"{f['file']}.findings.json", [])
    names = {f["file"] for f in analysed}
    rows = [r for r in jsonl(aee / "quality-log.jsonl") if r and r.get("file") not in names]
    for f in analysed:
        d = details[f["file"]]
        rows.append({"file": f["file"], "language": f["language"], "findingCount": 0, "highCount": 0, "mediumCount": 0,
                     "maxComplexity": d["maxCcn"], "lines": f["lines"], "toolsRun": d["toolsRun"], "analyzedAt": generated})
    rows.sort(key=lambda r: r["file"])
    write_text(aee / "quality-log.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    lines = ["# Quality warnings", ""] + ([f"- {w}" for w in warnings] or ["None."])
    lines += ["", "## Tools", ""] + [
        f"- {k}: {v['status']}" + (f" ({v['version']})" if v.get("version") else "") + f", config: {v['config']}"
        + (f" - {v['detail']}" if v.get("detail") else "") for k, v in sorted(status.items())]
    present = {f["language"] for f in analysed}
    unlinted = [lang for lang in cfg["unlintedLanguages"] if lang in present]
    lines += ["", "## Languages without a linter", ""] + (
        [f"- {lang}: lizard metrics and the manual pass only; never reported lint-clean" for lang in unlinted] or ["None."])
    lines += ["", "## Skipped files", ""] + ([f"- {s['file']}: {s['skipReason']}" for s in skipped] or ["None."])
    write_text(aee / "quality-warnings.md", "\n".join(lines) + "\n")


STALE = ("triage.json", "verdict.json", "escalation.md", "feedback.md")


def run_stage(a):
    aee = Path(a.aee)
    work = aee / "quality-work"
    cfg = load_config(aee)
    wanted, batch = batch_files(aee, a.files)
    loc = {r["file"]: (r.get("loc") or {}).get("code") for r in jsonl(aee / "intake.jsonl") if r}
    files = describe_files(wanted, cfg, loc)
    if not files:
        return None
    warnings = []
    skipped = [f for f in files if not f["analyze"]]
    warnings += [f"{f['file']} skipped: {f['skipReason']}" for f in skipped]
    analysed = [f for f in files if f["analyze"]]
    generated = now_iso()
    if not analysed:
        dump_json(work / "files.json", {"generatedAt": generated, "batch": batch, "thresholds": cfg["thresholds"],
                                        "ruleGroups": [], "files": files})
        dump_json(work / "warnings.json", warnings)
        for stale in STALE:
            (work / stale).unlink(missing_ok=True)
        return files, {}, warnings

    src = Path((load_json(aee / "changed-files.json") or {}).get("sourceFolder", "."))
    configs = detect_configs(ROOT / src if str(src) != "." else ROOT)
    raw = Path(a.raw_dir)
    shutil.rmtree(raw, ignore_errors=True)
    status = run_tools(analysed, cfg, configs, raw, warnings)
    present = {f["language"] for f in analysed}
    for lang in cfg["unlintedLanguages"]:
        if lang in present:
            warnings.append(f"No linter configured for {lang}: lizard metrics and manual review only; {lang} files are "
                            "never reported lint-clean.")
    hits, lizard = load_raw_hits(raw, configs.get("checkstyle") is None)

    by_file = {}
    for h in hits:
        by_file.setdefault(h["file"], []).append(h)
    per_file_cands, details = {}, {}
    for f in analysed:
        rel = f["file"]
        kept, dropped = [], {}
        for h in by_file.get(rel, []):
            reason = drop_reason(h)
            if reason:
                dropped[reason] = dropped.get(reason, 0) + 1
            else:
                kept.append(h)
        cands = cluster(merge_hits(kept))
        for i, c in enumerate(cands, 1):
            c["id"] = f"T{i}"
        functions = sorted(lizard.get(rel, []), key=lambda x: x["startLine"])
        m_cands = metric_candidates(functions, f["lines"] if f["lines"] else 0, cfg["thresholds"])
        tools_ok = [t for t in ALL_TOOLS if f["language"] in TOOL_LANGS[t] and status.get(t, {}).get("status") == "OK"]
        tools_missing = [t for t in ALL_TOOLS if f["language"] in TOOL_LANGS[t] and status.get(t, {}).get("status") != "OK"]
        per_file_cands[rel] = cands
        details[rel] = {"toolsRun": tools_ok, "toolsMissing": tools_missing, "functions": functions,
                        "metricCandidates": m_cands, "dropped": dropped,
                        "maxCcn": max((fn["ccn"] for fn in functions), default=None)}
    groups = rule_groups(per_file_cands)

    index = []
    for f in files:
        rec = dict(f)
        if f["analyze"]:
            d = details[f["file"]]
            path = work / "candidates" / f"{f['file']}.json"
            dump_json(path, {"file": f["file"], "language": f["language"], "role": f["role"], "toolsRun": d["toolsRun"],
                             "toolsMissing": d["toolsMissing"], "thresholds": cfg["thresholds"],
                             "toolCandidates": per_file_cands[f["file"]], "metricCandidates": d["metricCandidates"],
                             "functions": d["functions"], "dropped": d["dropped"]})
            rec.update(toolsRun=d["toolsRun"], toolsMissing=d["toolsMissing"], maxCcn=d["maxCcn"],
                       toolCandidates=len(per_file_cands[f["file"]]), metricCandidates=len(d["metricCandidates"]),
                       candidatesPath=path.as_posix())
        index.append(rec)
    dump_json(work / "files.json", {"generatedAt": generated, "batch": batch, "thresholds": cfg["thresholds"],
                                    "ruleGroups": groups, "files": index})
    dump_json(work / "tool_status.json", {"generatedAt": generated, "tools": status})
    dump_json(work / "warnings.json", warnings)
    for stale in STALE:
        (work / stale).unlink(missing_ok=True)  # a new batch never inherits an earlier run's triage or verdict
    write_skeleton(aee, analysed, details, generated, status, warnings, skipped, cfg)
    return files, status, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--raw-dir", default="/tmp/scan/quality")
    a = ap.parse_args(argv)
    try:
        result = run_stage(a)
    except (StageError, OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print(f"[ERROR] {e}")
        return ERROR
    if result is None:
        print("[PASS]")
        return PASS
    files, status, warnings = result
    n = sum(f["analyze"] for f in files)
    idx = load_json(Path(a.aee) / "quality-work" / "files.json")
    cands = sum(f.get("toolCandidates", 0) + f.get("metricCandidates", 0) for f in idx["files"])
    print(f"{n} files analysed ({len(files) - n} skipped), {cands} candidates; tools: "
          + (", ".join(f"{k}={v['status']}" for k, v in status.items()) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
