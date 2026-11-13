#!/usr/bin/env python3
"""Deterministic replacement of the Fact Extractor agent.

Builds one knowledge-base record per source file from tool output only:
`aee-inventory` (symbols, line counts, package, imports, references), `lizard`
(nloc and cyclomatic complexity per function) and, for files the inventory cannot
parse, `ctags`. Nothing is read or interpreted by a model, so the agent-filled
parts of the old record are gone: no `source: "manual"` symbols, and `sideEffects`,
`securitySurface` and `injection_flags` are always `[]`.

Usage: aee-extract [--aee DIR] [--sync] [--publish] [FILE ...]

  FILE ...   extract exactly these files (trigger-prompt mode); otherwise the files
             of the batch named on the first line of .aee/intake-summary.md
             (`Batch: <id>`): ledger lines of that batch with `skipped: false`.
  --sync     first fetch, check out and fast-forward agent_workspace
  --publish  afterwards register `KB:<file>` and `Extract Log`, commit, rebase, push

A candidate is dropped, silently, when its language is OTHER/UNKNOWN, when the file
on disk no longer matches the ledger hash, or when .aee/kb/<file>.json already holds
that hash. Output: .aee/kb/<file>.json per file, and one line per file appended to
.aee/extract-log.jsonl. Raw tool output goes under /tmp/scan/ and is never committed.

Exit code and stdout: 0 done; 10 `[PASS]` (nothing to extract); 1 `[ERROR] ...`.
"""
import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PASS, ERROR = 10, 1
EXTRACTABLE = ("C", "C++", "Java", "Python", "Go", "JavaScript")
CHUNK = 400
HERE = Path(__file__).resolve().parent
CTAGS_KIND = {"function": "function", "method": "method", "member": "method", "class": "class",
              "struct": "struct", "union": "union", "enum": "enum", "enumerator": "constant",
              "typedef": "type", "macro": "macro", "variable": "variable", "field": "field",
              "interface": "interface", "namespace": "module", "package": "module",
              "module": "module", "constant": "constant", "type": "type", "prototype": "function"}


class StageError(Exception):
    pass


def git(*args, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise StageError(f"git {' '.join(args)}: {p.stderr.strip() or 'exit %d' % p.returncode}")
    return p


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jline(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- candidates

def latest_lines(ledger):
    latest, lines = {}, []
    if ledger.is_file():
        for raw in ledger.read_text().splitlines():
            if raw.strip():
                e = json.loads(raw)
                lines.append(e)
                latest[e["file"]] = e
    return lines, latest


def candidates(aee, trigger_files):
    """[latest ledger line] of the files to extract, before the drop rules."""
    lines, latest = latest_lines(aee / "intake.jsonl")
    if trigger_files:
        return [latest[f] for f in trigger_files if f in latest]
    summary = aee / "intake-summary.md"
    if not summary.is_file():
        return []
    m = re.match(r"Batch:\s*(\S+)", summary.read_text().splitlines()[0] if summary.stat().st_size else "")
    if not m:
        return []
    batch = m.group(1)
    seen, out = set(), []
    for e in lines:
        if e.get("batch") == batch and e.get("skipped") is False and e["file"] not in seen:
            seen.add(e["file"])
            out.append(latest[e["file"]])
    return out


def drop_rules(aee, cands):
    keep = []
    for e in cands:
        path = e["file"]
        if e.get("language") not in EXTRACTABLE or e.get("error") or "hash" not in e:
            continue
        if not Path(path).is_file() or sha256(path) != e["hash"]:
            continue
        kb = aee / "kb" / (path + ".json")
        if kb.is_file():
            try:
                if json.loads(kb.read_text()).get("hash") == e["hash"]:
                    continue
            except ValueError:
                pass
        keep.append(e)
    return keep


# ---------------------------------------------------------------- tools

def find_tool(name):
    exe = shutil.which(name)
    if exe:
        return [exe]
    sibling = HERE / f"{name}.py"
    if sibling.is_file():
        return [sys.executable, str(sibling)]
    return None


def run_inventory(files, raw_dir):
    cmd = find_tool("aee-inventory")
    if not cmd:
        raise StageError("aee-inventory: not found on PATH")
    by_file, skipped = {}, {}
    for part in chunks(files):
        p = subprocess.run([*cmd, *part], capture_output=True, text=True)
        if p.returncode != 0:
            raise StageError(f"aee-inventory: {p.stderr.strip() or 'exit %d' % p.returncode}")
        try:
            doc = json.loads(p.stdout)
        except ValueError:
            raise StageError("aee-inventory: output is not JSON")
        by_file.update({f["file"]: f for f in doc.get("files", [])})
        skipped.update({s["file"]: s.get("reason", "") for s in doc.get("skipped", [])})
    (raw_dir / "inventory.json").write_text(json.dumps({"files": list(by_file.values()),
                                                         "skipped": skipped}))
    return by_file, skipped


def run_lizard(files, raw_dir):
    """{file: [row]} with row = {name, start, end, nloc, ccn}, or None if lizard failed."""
    exe = shutil.which("lizard")
    if not exe:
        return None
    rows, raw = {}, []
    for part in chunks(files):
        p = subprocess.run([exe, "--csv", *part], capture_output=True, text=True)
        if p.returncode != 0:
            return None
        raw.append(p.stdout)
        for r in csv.reader(io.StringIO(p.stdout)):
            if len(r) < 11:
                continue
            try:
                rows.setdefault(r[6], []).append({
                    "name": r[7], "start": int(r[9]), "end": int(r[10]),
                    "nloc": int(r[0]), "ccn": int(r[1])})
            except ValueError:
                continue
    (raw_dir / "lizard.csv").write_text("".join(raw))
    return rows


def run_ctags(path):
    """Symbols of one file from universal-ctags, or None."""
    exe = shutil.which("ctags")
    if not exe:
        return None
    p = subprocess.run([exe, "--output-format=json", "--fields=+nKS", "-f", "-", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    symbols = []
    for raw in p.stdout.splitlines():
        try:
            t = json.loads(raw)
        except ValueError:
            continue
        if t.get("_type") != "tag":
            continue
        kind = CTAGS_KIND.get(t.get("kind", ""))
        if not kind:
            continue
        start = int(t.get("line", 0))
        sym = {"name": t["name"], "kind": kind, "scope": t.get("scope", ""),
               "lineRange": {"start": start, "end": int(t.get("end", start))}}
        if t.get("signature"):
            sym["signature"] = t["name"] + t["signature"]
        sym["source"] = "ctags"
        symbols.append(sym)
    return symbols or None


# ---------------------------------------------------------------- record

def lr(start, end=None):
    return {"start": start, "end": start if end is None else end}


def attach_complexity(symbols, rows):
    """Add nloc/ccn from the lizard row of the same function (same name, start inside the range)."""
    for s in symbols:
        if s["kind"] not in ("function", "method"):
            continue
        a, b = s["lineRange"]["start"], s["lineRange"]["end"]
        best = None
        for r in rows:
            last = re.split(r"::|\.", r["name"])[-1]
            if last == s["name"] and a <= r["start"] <= b:
                if best is None or r["start"] - a < best["start"] - a:
                    best = r
        if best:
            s["nloc"], s["ccn"] = best["nloc"], best["ccn"]


def build_symbols(inv):
    out = []
    for s in inv.get("symbols", []):
        sym = {"name": s["name"], "kind": s["kind"], "scope": s.get("scope", ""),
               "lineRange": lr(s["startLine"], s["endLine"])}
        for k, v in s.items():
            if k not in ("name", "kind", "scope", "startLine", "endLine"):
                sym[k] = v
        sym["source"] = "aee-inventory"
        out.append(sym)
    return out


def build_dependencies(inv):
    deps = []
    for i in inv.get("imports", []):
        d = {k: v for k, v in i.items() if k not in ("startLine", "endLine")}
        d["lineRange"] = lr(i["startLine"], i["endLine"])
        deps.append(d)
    return deps


def build_references(inv):
    refs = []
    for r in inv.get("refs", []):
        if r.get("local"):
            continue
        if r["usage"] == "call" and ("." in r["name"] or "::" in r["name"]) and not r.get("via"):
            continue
        refs.append({"name": r["name"], "usage": r["usage"], "via": r.get("via"),
                     "count": r["count"], "lineRange": lr(r["line"])})
    return refs


def build_record(entry, inv, rows, lizard_ok, ctags_symbols):
    warnings = []
    rec = {"file": entry["file"], "hash": entry["hash"], "language": entry["language"],
           "component": entry["component"], "package": None, "lines": None,
           "symbols": [], "referencedSymbols": [], "dependencies": [],
           "sideEffects": [], "securitySurface": [], "injection_flags": [], "warnings": warnings}
    tools = []
    if inv is not None:
        tools.append("aee-inventory")
        rec["package"] = inv.get("package")
        rec["lines"] = inv.get("lines")
        rec["symbols"] = build_symbols(inv)
        rec["dependencies"] = build_dependencies(inv)
        rec["referencedSymbols"] = build_references(inv)
        if inv.get("parseErrors"):
            warnings.append("aee-inventory reported parse errors; symbols may be incomplete")
        if lizard_ok:
            tools.append("lizard")
            attach_complexity(rec["symbols"], rows)
        else:
            warnings.append("lizard failed or is missing: nloc and ccn not recorded")
    else:
        tools.append("ctags")
        rec["symbols"] = ctags_symbols
        warnings.append("aee-inventory could not parse this file: symbols come from ctags only; "
                        "dependencies, references and line counts are not available")
    if rec["lines"] is None:
        del rec["lines"]
        warnings.append("line counts not available")
    return rec, tools


def ordered(rec):
    """Key order of the KB schema (lines may be absent)."""
    keys = ["file", "hash", "language", "component", "package", "lines", "symbols",
            "referencedSymbols", "dependencies", "sideEffects", "securitySurface",
            "injection_flags", "warnings"]
    return {k: rec[k] for k in keys if k in rec}


# ---------------------------------------------------------------- stage

def run(a):
    aee = Path(a.aee)
    cands = drop_rules(aee, candidates(aee, a.files))
    if not cands:
        return None
    raw_dir = Path("/tmp/scan")
    raw_dir.mkdir(parents=True, exist_ok=True)
    files = [e["file"] for e in cands]
    inventory, skipped = run_inventory(files, raw_dir)
    lizard = run_lizard([f for f in files if f in inventory], raw_dir)
    now = a.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log_lines, written = [], []
    for e in cands:
        path = e["file"]
        inv = inventory.get(path)
        ctags_symbols = None
        if inv is None:
            ctags_symbols = run_ctags(path)
            if ctags_symbols is None:
                log_lines.append({"file": path, "batch": e["batch"], "hash": e["hash"],
                                  "error": "unparseable", "extractedAt": now})
                continue
        rec, tools = build_record(e, inv, (lizard or {}).get(path, []), lizard is not None,
                                  ctags_symbols)
        kb = aee / "kb" / (path + ".json")
        kb.parent.mkdir(parents=True, exist_ok=True)
        kb.write_text(json.dumps(ordered(rec), indent=2, ensure_ascii=False) + "\n")
        written.append(path)
        log_lines.append({"file": path, "batch": e["batch"], "hash": e["hash"],
                          "symbolCount": len(rec["symbols"]), "depCount": len(rec["dependencies"]),
                          "sideEffectCount": 0, "securitySurfaceCount": 0,
                          "toolsRun": tools, "mode": "deterministic", "extractedAt": now})
    log = aee / "extract-log.jsonl"
    with log.open("a") as fh:
        fh.write("".join(jline(l) + "\n" for l in log_lines))
    return written, log_lines


def sync(branch="agent_workspace"):
    git("fetch", "origin")
    if git("ls-remote", "--exit-code", "--heads", "origin", branch, check=False).returncode != 0:
        git("checkout", "-b", branch, "origin/main")
        git("push", "-u", "origin", branch)
        return
    git("checkout", branch)
    git("pull", "--ff-only", "origin", branch)


def publish(aee, written, branch="agent_workspace"):
    artifact = shutil.which("aee-artifact")
    if not artifact:
        raise StageError("aee-artifact: not found on PATH")
    for path in written:
        subprocess.run([artifact, "add", f"KB:{path}", str(aee / "kb" / (path + ".json"))], check=True)
    subprocess.run([artifact, "add", "Extract Log", str(aee / "extract-log.jsonl")], check=True)
    git("add", str(aee / "kb"), str(aee / "extract-log.jsonl"), ".agent-studio/artifacts.jsonl")
    git("commit", "-m", "chore(extract): add KB records and extract log")
    git("pull", "--rebase", "origin", branch)
    git("push", "origin", branch)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--now")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.sync:
            sync()
        result = run(a)
        if result is None:
            print("[PASS]")
            return PASS
        written, log_lines = result
        if a.publish:
            publish(Path(a.aee), written)
        errors = sum(1 for l in log_lines if "error" in l)
        print(f"{len(written)} KB records written, {errors} unparseable")
        return 0
    except StageError as e:
        print(f"[ERROR] {e}")
        return ERROR
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {' '.join(map(str, e.cmd))}: exit {e.returncode}")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
