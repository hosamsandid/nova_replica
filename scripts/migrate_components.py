#!/usr/bin/env python3
"""One-off migration to the structural component layout of the File Intake Classifier.

Mirrors step 3a of custom_prompts/file_intake_classifier.md:
  1. derives .aee/components.json from `git ls-files <sourceFolder>` (skipped if it exists),
  2. appends `recomponent` lines to .aee/intake.jsonl for files whose component changed,
  3. rewrites the `component` field of the KB records (.aee/kb/**),
  4. rewrites fromComponent/toComponent/crossComponent in .aee/link-graph.json.

Unlike the classifier, the appended ledger lines are `skipped: true`, so the Fact Extractor
does not re-extract every file; the KB records are updated here instead. Idempotent.

Usage: python3 -I scripts/migrate_components.py [--dry-run] [--source-folder src/]
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE_EXT = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".java", ".py", ".go",
              ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
TEST_DIRS = {"tests", "test", "testing", "__tests__", "spec", "specs", "e2e"}


def git_files(folder):
    out = subprocess.run(["git", "ls-files", folder], check=True, capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if p]


def derive_layout(source_folder):
    files = git_files(source_folder)
    src = [p for p in files if Path(p).suffix in SOURCE_EXT]
    if not src:
        sys.exit("no source files found under " + source_folder)
    # 1. root: descend while no source file sits directly here and exactly one subdir has source
    root = source_folder.rstrip("/") if source_folder not in (".", "./") else "."
    while True:
        prefix = "" if root == "." else root + "/"
        rel = [p[len(prefix):] for p in src if p.startswith(prefix)]
        direct = [r for r in rel if "/" not in r]
        subdirs = sorted({r.split("/")[0] for r in rel if "/" in r})
        if direct or len(subdirs) != 1:
            break
        root = prefix + subdirs[0]
    prefix = "" if root == "." else root + "/"
    rel = [p[len(prefix):] for p in src if p.startswith(prefix)]
    # 2. components
    comps = {}  # name -> prefix
    for r in rel:
        parts = r.split("/")[:-1]
        if not parts:
            continue
        if parts[0] in TEST_DIRS:
            sub = parts[:2] if len(parts) >= 2 else parts[:1]
        else:
            sub = parts[:1]
        comps["-".join(sub)] = prefix + "/".join(sub) + "/"
    # 3. top-level files
    if any("/" not in r for r in rel):
        name = "root" if root == "." else Path(root).name
        if name in comps:
            name += "-root"
        comps[name] = prefix if prefix else "./"
    return dict(sorted(comps.items()))


def component_of(path, layout):
    best, best_len = "UNKNOWN", -1
    for name, prefixes in layout.items():
        for pf in prefixes:
            pf = "" if pf == "./" else pf
            if path.startswith(pf) and len(pf) > best_len:
                best, best_len = name, len(pf)
    return best


def dump_like(obj, raw):
    """Serialise obj with the indentation style of raw (compact, or indent=2)."""
    if raw.lstrip().startswith("{\n") or raw.lstrip().startswith("[\n"):
        s = json.dumps(obj, indent=2, ensure_ascii=False)
    else:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s + ("\n" if raw.endswith("\n") else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source-folder")
    a = ap.parse_args()
    aee = Path(".aee")
    manifest = json.loads((aee / "changed-files.json").read_text())
    source_folder = a.source_folder or manifest["sourceFolder"]
    batch = manifest["afterSha"]

    # 1. layout
    cj = aee / "components.json"
    if cj.exists():
        layout = json.loads(cj.read_text())
        print(f"components.json exists, keeping it ({len(layout)} components)")
    else:
        layout = {k: [v] for k, v in derive_layout(source_folder).items()}
        print(f"derived {len(layout)} components: " + ", ".join(layout))
        if not a.dry_run:
            cj.write_text(json.dumps(layout, indent=2) + "\n")

    # 2. ledger
    ledger = aee / "intake.jsonl"
    latest = {}
    for line in ledger.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            latest[e["file"]] = e
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_lines, newcomp = [], {}
    for f, e in latest.items():
        if e.get("error") == "missing":
            continue
        c = component_of(f, layout)
        newcomp[f] = c
        if e.get("component") != c:
            line = {k: e[k] for k in ("file", "hash", "language") if k in e}
            line["component"] = c
            if "loc" in e:
                line["loc"] = e["loc"]
            line.update(batch=batch, arrivedAt=now, skipped=True, recomponent=True)
            new_lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
    print(f"ledger: {len(new_lines)} recomponent lines of {len(newcomp)} files")
    if new_lines and not a.dry_run:
        with ledger.open("a") as fh:
            fh.write("".join(l + "\n" for l in new_lines))

    # 3. KB records
    changed = 0
    pat = re.compile(r'("component"\s*:\s*)"[^"]*"')
    for kb in sorted((aee / "kb").rglob("*.json")):
        raw = kb.read_text()
        obj = json.loads(raw)
        f = obj.get("file")
        if f not in newcomp or obj.get("component") == newcomp[f]:
            continue
        out = pat.sub(lambda m: m.group(1) + json.dumps(newcomp[f]), raw, count=1)
        assert json.loads(out)["component"] == newcomp[f], kb
        changed += 1
        if not a.dry_run:
            kb.write_text(out)
    print(f"kb: {changed} records updated")

    # 4. link graph
    lg = aee / "link-graph.json"
    if lg.exists():
        raw = lg.read_text()
        g = json.loads(raw)
        comp = lambda p: newcomp.get(p) or component_of(p, layout)
        for e in g.get("fileEdges", []):
            e["fromComponent"], e["toComponent"] = comp(e["from"]), comp(e["to"])
            e["crossComponent"] = e["fromComponent"] != e["toComponent"]
        for r in g.get("resolved", []):
            if "to" in r:
                r["crossComponent"] = comp(r["from"]) != comp(r["to"])
        for s in g.get("crossComponentSeams", []):
            for side in ("a", "b"):
                if side in s and "file" in s[side]:
                    s[side]["component"] = comp(s[side]["file"])
        cross = sum(1 for e in g.get("fileEdges", []) if e["crossComponent"])
        g.setdefault("summary", {})["crossComponentEdges"] = cross
        print(f"link-graph: {cross} cross-component file edges of {len(g.get('fileEdges', []))}")
        if not a.dry_run:
            lg.write_text(dump_like(g, raw))
    print("dry run, nothing written" if a.dry_run else "done")


if __name__ == "__main__":
    main()
