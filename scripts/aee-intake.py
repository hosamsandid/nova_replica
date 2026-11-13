#!/usr/bin/env python3
"""Deterministic replacement of the File Intake Classifier agent.

Reads the `Changed Files` manifest and, for every file, appends one line to the
intake ledger (.aee/intake.jsonl): language by extension, owning component by
longest prefix in .aee/components.json, SHA-256, size from tokei, and whether the
content is unchanged. It then rewrites the Intake Summary and the cumulative Source
Overview. Semantics follow custom_prompts/agent_contracts/file_intake_classifier.md.

Usage: aee-intake [--manifest FILE] [--aee DIR] [--tokei-json FILE] [--now ISO]
                  [--sync] [--publish]

  --tokei-json  use this tokei JSON instead of running `tokei --files --output json`
  --sync        first fetch, check out and fast-forward agent_workspace
  --publish     afterwards register both artifacts, commit, rebase and push

Language rules (no model, no judgment):
  extension table       .c .h -> C (a .h with C++ constructs in its first 200 lines
                        -> C++), .cpp .cc .cxx .hpp -> C++, .java, .py, .go,
                        .js .mjs .cjs .jsx .ts .tsx -> JavaScript
  no extension          a `#!` line naming python or node decides; other shebangs
                        -> UNKNOWN (shell, perl, ... are code the pipeline can't read)
  OTHER                 only a closed list of non-source names/extensions (docs, data,
                        config, binaries, licences, lock files, templates ...)
  UNKNOWN               everything else, flagged in the summary for human review
Extend the lists in the optional .aee/intake-config.json:
  {"languages": {".ext": "Python"}, "otherExtensions": [".x"], "otherNames": ["Foo"]}

Exit code and stdout: 0 done; 10 `[PASS]` (manifest missing or empty, nothing written);
1 `[ERROR] <message>` (nothing written).
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PASS, ERROR = 10, 1

LANGUAGE_BY_EXT = {
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++",
    ".java": "Java", ".py": "Python", ".go": "Go",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "JavaScript", ".tsx": "JavaScript",
}
LANGUAGES = ("C", "C++", "Java", "Python", "Go", "JavaScript")
SOURCE_EXT = set(LANGUAGE_BY_EXT)
TEST_DIRS = {"tests", "test", "testing", "__tests__", "spec", "specs", "e2e"}

OTHER_EXT = {
    # docs and text
    ".md", ".rst", ".txt", ".adoc", ".org", ".po", ".pot", ".mo", ".pdf",
    # data and config
    ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg", ".conf", ".cnf", ".csv",
    ".tsv", ".properties", ".env", ".sample", ".example", ".lock", ".sum", ".mod",
    ".tf", ".tfvars", ".hcl", ".gitignore", ".gitkeep", ".gitattributes", ".editorconfig",
    # templates
    ".tpl", ".template", ".mako", ".j2", ".jinja", ".jinja2", ".html", ".htm", ".css",
    # keys, certificates, binaries, archives, media
    ".crt", ".key", ".pem", ".cer", ".csr", ".pub", ".gz", ".tgz", ".zip", ".tar", ".bz2",
    ".xz", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf",
    ".pyc", ".so", ".o", ".a", ".dll", ".jar", ".class", ".bin", ".db", ".sqlite",
}
OTHER_NAMES = (
    "dockerfile", "license", "licence", "notice", "authors", "copying", "readme",
    "changelog", "makefile", "manifest", "requirements", "pipfile", "tox", "codeowners",
    "py.typed",
)
SHEBANG = re.compile(rb"^#!\s*(?:/usr/bin/env\s+(?:-\S+\s+)?)?(?:\S*/)?(\w+)")
CPP_HEADER = re.compile(
    r"^\s*(class\s+\w+|namespace\b|template\s*<|using\s+namespace\b)"
    r"|std::|#\s*include\s*<[a-z_]+>", re.M)


class StageError(Exception):
    pass


# ---------------------------------------------------------------- helpers

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


def read_ledger(path):
    """Every ledger line, and the latest line per file path."""
    lines, latest = [], {}
    if path.is_file():
        for n, raw in enumerate(path.read_text().splitlines(), 1):
            if raw.strip():
                try:
                    e = json.loads(raw)
                except ValueError:
                    raise StageError(f"{path}:{n}: not valid JSON")
                lines.append(e)
                latest[e["file"]] = e
    return lines, latest


def is_source(language):
    return language in LANGUAGES


# ---------------------------------------------------------------- language

class Classifier:
    def __init__(self, config):
        self.langs = dict(LANGUAGE_BY_EXT)
        for ext, lang in config.get("languages", {}).items():
            if lang in LANGUAGES:
                self.langs[ext.lower()] = lang
        self.other_ext = OTHER_EXT | {e.lower() for e in config.get("otherExtensions", [])}
        self.other_names = tuple(n.lower() for n in OTHER_NAMES) + tuple(
            n.lower() for n in config.get("otherNames", []))

    def language(self, path):
        p = Path(path)
        # a dotfile such as `.gitkeep` has no suffix in pathlib: its whole name is the key
        ext = p.suffix.lower() or (p.name.lower() if p.name.startswith(".") else "")
        if ext in self.langs:
            lang = self.langs[ext]
            if ext == ".h" and self.langs[".h"] == "C" and self._cpp_header(path):
                return "C++"
            return lang
        name = p.name.lower()
        if ext in self.other_ext or name.startswith(self.other_names):
            return "OTHER"
        if not ext:
            return self._shebang(path)
        return "UNKNOWN"

    @staticmethod
    def _cpp_header(path):
        try:
            with open(path, "rb") as fh:
                head = b"".join(fh.readline() for _ in range(200)).decode("utf-8", "replace")
        except OSError:
            return False
        return bool(CPP_HEADER.search(head))

    @staticmethod
    def _shebang(path):
        try:
            with open(path, "rb") as fh:
                first = fh.readline(200)
        except OSError:
            return "UNKNOWN"
        m = SHEBANG.match(first)
        if m:
            interp = m.group(1).decode()
            if interp.startswith("python"):
                return "Python"
            if interp in ("node", "nodejs", "deno"):
                return "JavaScript"
        return "UNKNOWN"


# ---------------------------------------------------------------- components

def prefix_of(p):
    return "" if p == "./" else p


def component_of(path, layout):
    best, best_len = "UNKNOWN", -1
    for name, prefixes in layout.items():
        for pf in prefixes:
            pf = prefix_of(pf)
            if path.startswith(pf) and len(pf) > best_len:
                best, best_len = name, len(pf)
    return best


def derive_layout(source_folder, source_files):
    """{component: prefix} by rules 1-3 of step 3a, from the whole tree."""
    if not source_files:
        return {}
    root = source_folder.rstrip("/") if source_folder != "." else "."
    while True:
        prefix = "" if root == "." else root + "/"
        rel = [p[len(prefix):] for p in source_files if p.startswith(prefix)]
        direct = [r for r in rel if "/" not in r]
        subdirs = sorted({r.split("/")[0] for r in rel if "/" in r})
        if direct or len(subdirs) != 1:
            break
        root = prefix + subdirs[0]
    prefix = "" if root == "." else root + "/"
    rel = [p[len(prefix):] for p in source_files if p.startswith(prefix)]
    comps = {}
    for r in sorted(rel):
        parts = r.split("/")[:-1]
        if not parts:
            continue
        sub = parts[:2] if parts[0] in TEST_DIRS and len(parts) >= 2 else parts[:1]
        comps["-".join(sub)] = prefix + "/".join(sub) + "/"
    if any("/" not in r for r in rel):
        name = "root" if root == "." else Path(root).name
        if name in comps:
            name += "-root"
        comps[name] = prefix if prefix else "./"
    return dict(sorted(comps.items()))


def update_layout(existing, derived):
    """Never change an entry; add derived directories no existing entry covers."""
    layout = {k: list(v) for k, v in existing.items()}
    known = {prefix_of(p) for v in layout.values() for p in v}
    added = []
    for name, pf in derived.items():
        if prefix_of(pf) in known:
            continue
        if not any(prefix_of(pf).startswith(k) for k in known):
            continue  # outside every existing root
        base, n = name, 2
        while name in layout:
            name = f"{base}-{n}"
            n += 1
        layout[name] = [pf]
        known.add(prefix_of(pf))
        added.append(name)
    return layout, added


# ---------------------------------------------------------------- tokei

def tokei_counts(paths, tokei_json, raw_dir):
    """{path: {code, comments, blanks}}, or None if tokei is unavailable or failed."""
    if tokei_json:
        raw = Path(tokei_json).read_text()
    else:
        tokei = shutil.which("tokei")
        if not tokei:
            return None
        raw_dir.mkdir(parents=True, exist_ok=True)
        p = subprocess.run([tokei, "--files", "--output", "json", *paths],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None
        raw = p.stdout
        (raw_dir / "tokei.json").write_text(raw)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    counts = {}
    for lang, entry in data.items():
        if lang == "Total":
            continue
        for rep in entry.get("reports", []):
            st = rep.get("stats", rep)
            name = re.sub(r"^(\./)+", "", rep["name"])
            counts.setdefault(name, {"code": st["code"], "comments": st["comments"],
                                     "blanks": st["blanks"]})
    return counts


# ---------------------------------------------------------------- reports

def fmt(n):
    return f"{n:,}"


def pct(part, total):
    return f"{100.0 * part / total:.1f}%" if total else "0.0%"


def table(header, rows, align_right_from=1):
    sep = ["-" * (len(h) + 2 - (i >= align_right_from)) + (":" if i >= align_right_from else "")
           for i, h in enumerate(header)]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(sep) + "|"]
    out += ["| " + " | ".join(map(str, r)) + " |" for r in rows]
    return out


def totals_by(entries, key):
    agg = defaultdict(lambda: {"files": 0, "code": 0, "comments": 0, "blanks": 0})
    for e in entries:
        a = agg[e[key]]
        a["files"] += 1
        loc = e.get("loc") or {}
        a["code"] += loc.get("code", 0)
        a["comments"] += loc.get("comments", 0)
        a["blanks"] += loc.get("blanks", 0)
    return agg


def loc_table(label, agg):
    total = {k: sum(a[k] for a in agg.values()) for k in ("files", "code", "comments", "blanks")}
    order = sorted(agg, key=lambda k: (-agg[k]["code"], k))
    rows = [[k, fmt(agg[k]["files"]), fmt(agg[k]["code"]), fmt(agg[k]["comments"]),
             fmt(agg[k]["blanks"]), pct(agg[k]["code"], total["code"])] for k in order]
    rows.append([f"**Total**", f"**{fmt(total['files'])}**", f"**{fmt(total['code'])}**",
                 f"**{fmt(total['comments'])}**", f"**{fmt(total['blanks'])}**", "**100%**"])
    return table([label, "Files", "Code", "Comments", "Blanks", "% of Code"], rows)


def intake_summary(batch, batch_lines, no_loc_note, layout_note):
    extract = [e for e in batch_lines if not e.get("skipped") and is_source(e.get("language"))]
    skipped = [e for e in batch_lines if e.get("skipped")]
    other = [e for e in batch_lines if e.get("language") == "OTHER" and not e.get("skipped")]
    flagged = sorted(e["file"] for e in batch_lines if e.get("error") != "missing" and (
        e.get("language") == "UNKNOWN"
        or (e.get("component") == "UNKNOWN" and e.get("language") != "OTHER")))
    by_lang = totals_by(extract, "language")
    by_comp = totals_by(extract, "component")

    def count_table(label, agg):
        rows = [[k, a["files"], a["code"], a["comments"], a["blanks"]]
                for k, a in sorted(agg.items(), key=lambda kv: (-kv[1]["code"], kv[0]))]
        return table([label, "Files", "Lines of Code", "Comments", "Blanks"], rows)

    out = [f"Batch: {batch}", "", "## Overview", "",
           f"- **Files to extract** (new/changed, language not OTHER/UNKNOWN): {len(extract)}",
           f"- **Skipped files** (unchanged or missing): {len(skipped)}",
           f"- **OTHER files** (non-source): {len(other)}"]
    out += no_loc_note + layout_note
    out += ["", "## Counts by Language", ""] + count_table("Language", by_lang)
    out += ["", "## Counts by Component", ""] + count_table("Component", by_comp)
    out += ["", "## UNKNOWN Files — Flagged for Human Review", ""]
    out += [f"- `{f}`" for f in flagged] if flagged else ["None."]
    return "\n".join(out) + "\n"


def parse_growth(text):
    rows = []
    if not text:
        return rows
    m = re.search(r"^## Growth History\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return rows
    for line in m.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and re.fullmatch(r"[0-9a-f]{8}", cells[0]):
            try:
                rows.append({"batch": cells[0], "date": cells[1],
                             "files": int(cells[2].replace(",", "")),
                             "code": int(cells[3].replace(",", ""))})
            except ValueError:
                pass
    return rows


def folder_tree(source_folder, source_entries):
    base = "" if source_folder == "." else source_folder
    top = {"lines": 0, "files": 0}
    nodes = {}  # level-1 dir -> {"lines","files","kids": {name: {"lines","files"}}}
    for e in source_entries:
        rel = e["file"][len(base):] if e["file"].startswith(base) else e["file"]
        dirs = rel.split("/")[:-1]
        code = (e.get("loc") or {}).get("code", 0)
        if not dirs:
            top["lines"] += code
            top["files"] += 1
            continue
        n = nodes.setdefault(dirs[0], {"lines": 0, "files": 0, "kids": {}})
        n["lines"] += code
        n["files"] += 1
        kid = (dirs[1] + "/") if len(dirs) > 1 else "(top-level modules)"
        k = n["kids"].setdefault(kid, {"lines": 0, "files": 0})
        k["lines"] += code
        k["files"] += 1
    out = [source_folder if source_folder != "." else "./"]
    if top["files"]:
        out.append(f"  (top-level modules)  {fmt(top['lines'])} lines, {top['files']} files")
    for name in sorted(nodes):
        n = nodes[name]
        out.append(f"  {name}/  {fmt(n['lines'])} lines, {n['files']} files")
        for kid in sorted(n["kids"], key=lambda k: (k != "(top-level modules)", k)):
            k = n["kids"][kid]
            out.append(f"    {kid}  {fmt(k['lines'])} lines, {k['files']} files")
    return out


def source_overview(batch, now, source_folder, latest, previous_text):
    tracked = [e for e in latest.values() if e.get("error") != "missing"]
    source = [e for e in tracked if is_source(e.get("language"))]
    n_other = sum(1 for e in tracked if e.get("language") == "OTHER")
    unknown = sorted(e["file"] for e in tracked if e.get("language") == "UNKNOWN")
    code = sum((e.get("loc") or {}).get("code", 0) for e in source)
    comments = sum((e.get("loc") or {}).get("comments", 0) for e in source)
    blanks = sum((e.get("loc") or {}).get("blanks", 0) for e in source)
    no_loc = sum(1 for e in source if not e.get("loc"))

    history = [r for r in parse_growth(previous_text) if r["batch"] != batch[:8]]
    prev = history[-1] if history else {"files": 0, "code": 0}
    d_files, d_code = len(source) - prev["files"], code - prev["code"]
    history.append({"batch": batch[:8], "date": now[:10], "files": len(source), "code": code})

    out = [f"Batch: {batch}", f"Updated: {now}", f"Source folder: {source_folder}", "",
           "## Totals", ""]
    out += table(["Metric", "Value"], [
        ["Tracked files", fmt(len(tracked))],
        ["Source files (extractable)", fmt(len(source))],
        ["OTHER files", fmt(n_other)],
        ["UNKNOWN files", fmt(len(unknown))],
        ["Total code lines (source)", fmt(code)],
        ["Total comment lines (source)", fmt(comments)],
        ["Total blank lines (source)", fmt(blanks)],
        ["Source files without `loc`", fmt(no_loc)],
        ["Change (files) vs. prev batch", f"{d_files:+,}"],
        ["Change (code lines) vs. prev batch", f"{d_code:+,}"]])
    out += ["", "## Lines of Code by Language", ""]
    out += loc_table("Language", totals_by(source, "language"))
    comp_agg = totals_by(source, "component")
    out += ["", "## Lines of Code by Component", ""] + loc_table("Component", comp_agg)
    langs = sorted({e["language"] for e in source},
                   key=lambda lg: (-sum((e.get("loc") or {}).get("code", 0)
                                        for e in source if e["language"] == lg), lg))
    per = defaultdict(lambda: defaultdict(int))
    for e in source:
        per[e["component"]][e["language"]] += (e.get("loc") or {}).get("code", 0)
    rows = [[c] + [fmt(per[c][lg]) for lg in langs]
            for c in sorted(per, key=lambda c: (-comp_agg[c]["code"], c))]
    out += ["", "### Code Lines per Language by Component", ""]
    out += table(["Component"] + langs, rows)
    out += ["", "## Folder Structure", "",
            f"Source files only, two directory levels below `{source_folder}`.", "", "```"]
    out += folder_tree(source_folder, source) + ["```"]
    out += ["", "## Growth History", ""]
    prev_code = 0
    rows = []
    for r in history:
        rows.append([r["batch"], r["date"], fmt(r["files"]), fmt(r["code"]),
                     f"{r['code'] - prev_code:+,}"])
        prev_code = r["code"]
    out += table(["Batch", "Date", "Source Files", "Code Lines", "Change"], rows, 2)
    out += ["", "## Notes", "",
            "- **LOC** figures come from tokei output stored in the `loc` field of "
            "`.aee/intake.jsonl`. Source files without a `loc` entry are counted in file "
            "totals but excluded from all line counts.",
            "- **Components** come from `.aee/components.json`."]
    if unknown:
        shown = ", ".join(f"`{f}`" for f in unknown[:20])
        more = f" (+{len(unknown) - 20} more)" if len(unknown) > 20 else ""
        out.append(f"- **UNKNOWN files**: {shown}{more}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- stage

def run(a):
    aee = Path(a.aee)
    manifest_path = Path(a.manifest)
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files") or []
    if not files:
        return None
    source_folder, batch = manifest["sourceFolder"], manifest["afterSha"]
    now = a.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    config_path = aee / "intake-config.json"
    clf = Classifier(json.loads(config_path.read_text()) if config_path.is_file() else {})
    ledger_path = aee / "intake.jsonl"
    all_lines, latest = read_ledger(ledger_path)
    new_lines = []

    # 3a. component layout
    tree = [p for p in git("ls-files", "--", source_folder).stdout.splitlines()
            if p and clf.language(p) in LANGUAGES]
    derived = derive_layout(source_folder, tree)
    layout_path = aee / "components.json"
    layout_note = []
    if layout_path.is_file():
        existing = json.loads(layout_path.read_text())
        layout, added = update_layout(existing, derived)
        if added:
            layout_note = [f"- **Components added to layout**: {', '.join(added)}"]
    else:
        layout, added = {k: [v] for k, v in derived.items()}, []
    if layout and (not layout_path.is_file() or added):
        layout_path.parent.mkdir(parents=True, exist_ok=True)
        layout_path.write_text(json.dumps(layout, indent=2, ensure_ascii=False) + "\n")

    # 3. tokei, once for the batch
    existing_files = [f["path"] for f in files if Path(f["path"]).is_file()]
    counts = tokei_counts(existing_files, a.tokei_json, Path("/tmp/scan"))
    no_loc_note = [] if counts is not None else [
        "- **tokei failed or is not installed**: no `loc` recorded for this batch."]

    # 4. classify each manifest file
    manifest_paths = {f["path"] for f in files}
    for f in files:
        path = f["path"]
        if not Path(path).is_file():
            new_lines.append({"file": path, "batch": batch, "arrivedAt": now,
                              "skipped": True, "error": "missing"})
            continue
        digest = sha256(path)
        lang = clf.language(path)
        comp = component_of(path, layout)
        prev = latest.get(path)
        if (prev and prev.get("hash") == digest and prev.get("component") == comp
                and prev.get("language") == lang):
            line = {"file": path, "hash": digest, "language": lang, "component": comp}
            if prev.get("loc"):
                line["loc"] = prev["loc"]
            line.update(batch=batch, arrivedAt=now, skipped=True)
        else:
            line = {"file": path, "hash": digest, "language": lang, "component": comp}
            loc = (counts or {}).get(path)
            if loc:
                line["loc"] = loc
            line.update(batch=batch, arrivedAt=now, skipped=False)
        new_lines.append(line)

    # re-assignment: files outside the manifest whose component changed with the layout
    for path, e in sorted(latest.items()):
        if path in manifest_paths or e.get("error") == "missing" or not Path(path).is_file():
            continue
        comp = component_of(path, layout)
        if e.get("component") != comp:
            line = {k: e[k] for k in ("file", "hash", "language") if k in e}
            line["component"] = comp
            if e.get("loc"):
                line["loc"] = e["loc"]
            line.update(batch=batch, arrivedAt=now, skipped=False, recomponent=True)
            new_lines.append(line)

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a") as fh:
        fh.write("".join(jline(l) + "\n" for l in new_lines))
    for l in new_lines:
        latest[l["file"]] = l

    # 5-6. reports
    summary = intake_summary(batch, new_lines, no_loc_note, layout_note)
    (aee / "intake-summary.md").write_text(summary)
    overview_path = aee / "source-overview.md"
    previous = overview_path.read_text() if overview_path.is_file() else ""
    overview_path.write_text(source_overview(batch, now, source_folder, latest, previous))
    return batch, new_lines, layout_path


def sync(branch="agent_workspace"):
    git("fetch", "origin")
    if git("ls-remote", "--exit-code", "--heads", "origin", branch, check=False).returncode != 0:
        git("checkout", "-b", branch, "origin/main")
        git("push", "-u", "origin", branch)
        return
    git("checkout", branch)
    git("pull", "--ff-only", "origin", branch)


def publish(aee, layout_path, branch="agent_workspace"):
    artifact = shutil.which("aee-artifact")
    if not artifact:
        raise StageError("aee-artifact: not found on PATH")
    subprocess.run([artifact, "add", "Intake Summary", str(aee / "intake-summary.md")], check=True)
    subprocess.run([artifact, "add", "Source Overview", str(aee / "source-overview.md")], check=True)
    paths = [layout_path, aee / "intake.jsonl", aee / "intake-summary.md",
             aee / "source-overview.md", Path(".agent-studio/artifacts.jsonl")]
    git("add", *[str(p) for p in paths if p.exists()])
    git("commit", "-m", "chore(intake): classify files for pipeline run")
    git("pull", "--rebase", "origin", branch)
    git("push", "origin", branch)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", default=".aee/changed-files.json")
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--tokei-json")
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
        batch, lines, layout_path = result
        if a.publish:
            publish(Path(a.aee), layout_path)
        n_new = sum(1 for l in lines if not l.get("skipped"))
        print(f"batch {batch[:8]}: {len(lines)} ledger lines, {n_new} not skipped")
        return 0
    except StageError as e:
        print(f"[ERROR] {e}")
        return ERROR
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {' '.join(map(str, e.cmd))}: exit {e.returncode}")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
