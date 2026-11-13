#!/usr/bin/env python3
"""Deterministic replacement of the Synthesizer agent (all but the purpose sentence).

Writes docs/components/<component>.md for every component, docs/components/index.md,
and a stub docs/components/UNKNOWN.md when files have no component. Everything comes
from the KB records, the Link Graph, the intake ledger and .aee/components.json; the
same inputs give the same bytes.

Usage: aee-synthesize [--aee DIR] [--docs DIR] [--file-docs DIR] [--sync] [--publish]

  --file-docs  per-file documents of the File Documenter (default docs); their Overview
               paragraphs go into the agent bundles

The one thing it cannot write is what a component is FOR. An agent does that, and
only that: it reads .aee/synth-inputs/<component>.json (written here: key files and
symbols, top external packages, neighbours, per-file document overviews) and writes
.aee/purposes.json:
    {"<component>": {"purpose": "<one or two sentences>", "inputHash": "<from the bundle>"}}
A purpose whose inputHash no longer matches is still shown, marked "to be refreshed",
and listed in .aee/synth-todo.json, so only changed components need the agent again.

Sections per component: Overview, File Inventory, Defined Symbols, External
Dependencies, Side Effects, Component Diagram, Call Flow, Cross-Component Interfaces.
"Call Flow" replaces the old "Local Data Flow": it is the static call graph of resolved
cross-file calls (see aee-callflow.py), not a data flow. Side effects are not extracted
by the deterministic Fact Extractor, and the document says so rather than showing an
empty table.

Code-line figures are the KB's `lines.code` throughout (the intake ledger's tokei
figures differ slightly and are not mixed in).

Exit code and stdout: 0 written; 10 `[PASS]` (nothing to document, or every file
already up to date); 1 `[ERROR] ...`.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PASS, ERROR = 10, 1
HERE = Path(__file__).resolve().parent
LANGUAGES = ("C", "C++", "Java", "Python", "Go", "JavaScript")
FILE_NODE_LIMIT = 40
NODE_CAP = 60
EDGE_MIN = 5


class StageError(Exception):
    pass


def git(*args, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise StageError(f"git {' '.join(args)}: {p.stderr.strip() or 'exit %d' % p.returncode}")
    return p


def callflow_module():
    sys.dont_write_bytecode = True  # no scripts/__pycache__ for a hyphenated module
    spec = importlib.util.spec_from_file_location("aee_callflow", HERE / "aee-callflow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fmt(n):
    return f"{n:,}"


def pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "0.0%"


def md_table(header, rows, right=()):
    sep = ["---:" if i in right else "---" for i in range(len(header))]
    return (["| " + " | ".join(header) + " |", "|" + "|".join(sep) + "|"]
            + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def label(text):
    return re.sub(r'["#;]', "", text)


# ---------------------------------------------------------------- inputs

class Inputs:
    def __init__(self, aee):
        self.aee = aee
        self.kb = {}
        for p in sorted((aee / "kb").rglob("*.json")):
            k = json.loads(p.read_text())
            self.kb[k["file"]] = k
        self.graph = (json.loads((aee / "link-graph.json").read_text())
                      if (aee / "link-graph.json").is_file() else None)
        self.ledger = {}
        if (aee / "intake.jsonl").is_file():
            for line in (aee / "intake.jsonl").read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    self.ledger[e["file"]] = e  # last line wins
        self.layout = (json.loads((aee / "components.json").read_text())
                       if (aee / "components.json").is_file() else {})
        cf = aee / "changed-files.json"
        self.source_folder = json.loads(cf.read_text()).get("sourceFolder", ".") if cf.is_file() else "."
        self.purposes = (json.loads((aee / "purposes.json").read_text())
                         if (aee / "purposes.json").is_file() else {})
        live = {f: e for f, e in self.ledger.items()
                if e.get("language") in LANGUAGES and e.get("error") != "missing"}
        self.source_files = live
        self.components = sorted({e["component"] for e in live.values()} - {"UNKNOWN"})
        self.unknown = sorted(f for f, e in live.items() if e["component"] == "UNKNOWN")
        self.unknown += sorted(f for f, e in self.ledger.items()
                               if e.get("language") == "UNKNOWN" and e.get("error") != "missing")
        self.unknown = sorted(set(self.unknown))

    def files_of(self, comp):
        return sorted(f for f, e in self.source_files.items() if e["component"] == comp)

    def kb_of(self, comp):
        return [self.kb[f] for f in self.files_of(comp) if f in self.kb]

    def prefix(self, comp):
        p = (self.layout.get(comp) or [""])[0]
        return "" if p == "./" else p

    def input_hash(self, comp):
        h = hashlib.sha256()
        for f in self.files_of(comp):
            h.update(f"{f}\0{self.kb[f]['hash'] if f in self.kb else '-'}\n".encode())
        return h.hexdigest()[:16]


def is_test(name):
    return name == "tests" or name.startswith("tests-")


# ---------------------------------------------------------------- sections

def code_lines(kb_records):
    return sum((k.get("lines") or {}).get("code", 0) for k in kb_records)


def neighbours(graph, comp):
    out, inc = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])  # other -> [file edges, refs]
    for e in (graph or {}).get("fileEdges", []):
        if not e.get("crossComponent"):
            continue
        if e["fromComponent"] == comp:
            out[e["toComponent"]][0] += 1
            out[e["toComponent"]][1] += e["count"]
        elif e["toComponent"] == comp:
            inc[e["fromComponent"]][0] += 1
            inc[e["fromComponent"]][1] += e["count"]
    # plain dicts: indexing a defaultdict in a sort key would create empty entries
    return dict(out), dict(inc)


def purpose_text(inp, comp):
    p = inp.purposes.get(comp)
    if not p or not p.get("purpose"):
        return "_Purpose not yet written._"
    stale = "" if p.get("inputHash") == inp.input_hash(comp) else " _(to be refreshed)_"
    return p["purpose"].strip() + stale


def overview(inp, comp, totals):
    files, kb = inp.files_of(comp), inp.kb_of(comp)
    code = code_lines(kb)
    out_n, in_n = neighbours(inp.graph, comp)
    names = sorted(set(out_n) | set(in_n), key=lambda n: (-(out_n.get(n, [0, 0])[1] + in_n.get(n, [0, 0])[1]), n))[:5]
    parts = []
    for n in names:
        sides = ([f"→{out_n[n][1]}"] if n in out_n else []) + ([f"←{in_n[n][1]}"] if n in in_n else [])
        parts.append(f"**{n}** ({'/'.join(sides)} refs)")
    partial = "" if len(kb) == len(files) else f" ({len(kb)} of {len(files)} files extracted)"
    lines = [purpose_text(inp, comp),
             f"{len(files)} files ({pct(len(files), totals['files'])} of {totals['files']} source files), "
             f"{fmt(code)} code lines ({pct(code, totals['code'])} of {fmt(totals['code'])} total){partial}."]
    if parts:
        lines.append("Main neighbours: " + "; ".join(parts) + ".")
    return lines


def file_inventory(inp, comp):
    prefix = inp.prefix(comp)
    groups = defaultdict(list)
    for f in inp.files_of(comp):
        rel = f[len(prefix):] if f.startswith(prefix) else f
        groups["(top-level)/" if "/" not in rel else rel.split("/")[0] + "/"].append((rel, f))
    out = []
    for g in sorted(groups, key=lambda g: (g != "(top-level)/", g)):
        rows = []
        for rel, f in sorted(groups[g]):
            k = inp.kb.get(f)
            ccns = [s["ccn"] for s in (k or {}).get("symbols", []) if "ccn" in s]
            rows.append([f"`{rel}`", fmt((k or {}).get("lines", {}).get("code", 0)) if k else "—",
                         max(ccns) if ccns else "—"])
        out += [f"### {g}", ""] + md_table(["File", "Code Lines", "Top CCN"], rows, right=(1, 2)) + [""]
    funcs = sorted(((s["ccn"], k["file"], s["lineRange"]["start"], s) for k in inp.kb_of(comp)
                    for s in k["symbols"] if s["kind"] in ("function", "method") and "ccn" in s),
                   key=lambda t: (-t[0], t[1], t[2]))[:5]
    if funcs:
        rows = [[f"`{(s['scope'] + '.' if s.get('scope') else '') + s['name']}`", s["kind"], c,
                 f"`{f[len(prefix):] if f.startswith(prefix) else f}`",
                 f"L{s['lineRange']['start']}–L{s['lineRange']['end']}"] for c, f, _, s in funcs]
        out += ["**Top 5 Functions by CCN**", ""] + md_table(
            ["Function", "Kind", "CCN", "File", "Line Range"], rows, right=(2,))
    return out


def defined_symbols(inp, comp):
    kinds = defaultdict(int)
    for k in inp.kb_of(comp):
        for s in k["symbols"]:
            if s["kind"] != "import":
                kinds[s["kind"]] += 1
    rows = [[k, fmt(n)] for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))]
    return [f"Total: {fmt(sum(kinds.values()))} (imports not counted)", ""] + md_table(
        ["Kind", "Count"], rows, right=(1,))


def root_of(language, module):
    parts = module.split(".") if language in ("Python", "Java") else module.split("/")
    if language == "Java":
        return ".".join(parts[:2])
    if language == "JavaScript" and module.startswith("@"):
        return "/".join(parts[:2])
    return parts[0]


def dep_kind(language, root, system):
    if system:
        return "system"
    if language == "Python":
        return "stdlib" if root in sys.stdlib_module_names else "third-party"
    if language == "Java":
        return "stdlib" if root.split(".")[0] in ("java", "javax") else "third-party"
    if language == "Go":
        return "stdlib" if "." not in root.split("/")[0] else "third-party"
    return "third-party"


def external_dependencies(inp, comp):
    # internal = a directory name on the path of any source file (`nova` for src/nova/...) or a
    # package a KB record declares; stdlib and third-party roots never match a project directory
    internal = {seg for f in inp.source_files for seg in f.split("/")[:-1]}
    internal |= {k["package"].split(".")[0] for k in inp.kb.values() if k.get("package")}
    users, kind = defaultdict(set), {}
    for k in inp.kb_of(comp):
        for d in k["dependencies"]:
            if d.get("relative") or not d.get("module"):
                continue
            if k["language"] in ("C", "C++") and not d.get("system"):
                continue
            root = root_of(k["language"], d["module"])
            if root in internal:
                continue
            users[root].add(k["file"])
            kind[root] = dep_kind(k["language"], root, d.get("system"))
    if not users:
        return ["No external packages imported."]
    rows = [[f"`{r}`", kind[r], len(f)] for r, f in sorted(users.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    return md_table(["Package", "Kind", "Files Importing"], rows, right=(2,))


def side_effects(inp, comp, cf):
    kb = inp.kb_of(comp)
    kinds = defaultdict(int)
    for k in kb:
        for s in k["sideEffects"]:
            kinds[s["kind"]] += 1
    out = []
    if kinds:
        out += md_table(["Kind", "Count"], [[k, n] for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
                        right=(1,))
    else:
        out.append("None recorded. The deterministic Fact Extractor does not extract side effects, so "
                   "this is not evidence that there are none.")
    table = cf.boundary_table({})
    b = cf.boundary_calls(inp.kb, [k["file"] for k in kb], table)
    cats = defaultdict(lambda: [0, set()])
    for by_cat in b.values():
        for cat, v in by_cat.items():
            cats[cat][0] += v["count"]
            cats[cat][1] |= v["modules"]
    if cats:
        out += ["", "Calls into external modules, by category (from the KB references):", ""] + md_table(
            ["Category", "Calls", "Modules"],
            [[c, fmt(n), ", ".join(f"`{m}`" for m in sorted(m))] for c, (n, m) in sorted(cats.items())],
            right=(1,))
    return out


def component_diagram(inp, comp):
    graph = inp.graph
    if graph is None:
        return ["> The Link Graph is not available; the diagram is omitted."]
    files, prefix = inp.files_of(comp), inp.prefix(comp)
    rel = lambda f: f[len(prefix):] if f.startswith(prefix) else f
    group = lambda f: (rel(f).split("/")[0] + "/") if "/" in rel(f) else "(top-level)"
    by_file = len(files) <= FILE_NODE_LIMIT
    node_of = (lambda f: rel(f)) if by_file else group
    internal = defaultdict(int)
    for e in graph["fileEdges"]:
        if e["fromComponent"] == comp and e["toComponent"] == comp:
            a, b = node_of(e["from"]), node_of(e["to"])
            if a != b:
                internal[(a, b)] += e["count"]
    if by_file:
        nodes = sorted({rel(f) for f in files})
    else:
        nodes = sorted({group(f) for f in files})
        keep = {k: v for k, v in internal.items() if v >= EDGE_MIN}
        for n in nodes:
            if not any(n in k for k in keep):
                strongest = sorted(((v, k) for k, v in internal.items() if n in k), reverse=True)[:3]
                keep.update({k: v for v, k in strongest})
        internal = keep
    out_n, in_n = neighbours(graph, comp)
    others = sorted(set(out_n) | set(in_n), key=lambda n: (-(out_n.get(n, [0, 0])[1] + in_n.get(n, [0, 0])[1]), n))
    omitted = 0
    while len(nodes) + 1 + len(others) > NODE_CAP and others:
        others.pop()
        omitted += 1
    seams = defaultdict(int)
    for s in graph.get("crossComponentSeams", []):
        a, b = s["a"]["component"], s["b"]["component"]
        if comp in (a, b):
            seams[b if a == comp else a] += 1
    ids = {n: f"f{i}" for i, n in enumerate(nodes, 1)}
    cids = {n: f"c{i}" for i, n in enumerate(sorted(others), 1)}
    lines = ["graph LR", f'  h["{label(comp)}"]']
    lines += [f'  {ids[n]}["{label(n)}"]' for n in nodes]
    lines += [f'  {cids[n]}["{label(n)}"]' for n in sorted(others)]
    for (a, b), v in sorted(internal.items()):
        lines.append(f"  {ids[a]} -->|\"{v}\"| {ids[b]}" if v > 1 else f"  {ids[a]} --> {ids[b]}")
    for n in sorted(others):
        if n in out_n:
            lines.append(f'  h -->|"{out_n[n][1]}"| {cids[n]}')
        if n in in_n:
            lines.append(f'  {cids[n]} -->|"{in_n[n][1]}"| h')
        if n in seams:
            lines.append(f'  h -.->|"{seams[n]} seams"| {cids[n]}')
    out = ["```mermaid"] + lines + ["```"]
    note = []
    if not by_file:
        note.append(f"More than {FILE_NODE_LIMIT} files: one node per subfolder; edges shown where the "
                    f"summed count is at least {EDGE_MIN}, or the strongest 3 of a subfolder with none.")
    if omitted:
        note.append(f"{omitted} weakest neighbouring components omitted to stay within {NODE_CAP} nodes.")
    return out + ([""] + note if note else [])


def call_flow(inp, comp, cf):
    if inp.graph is None:
        return ["> The Link Graph is not available; the call flow is omitted."]

    class A:
        max_nodes, sequences, depth = 40, 3, 3
    idx = cf.Index(inp.kb)
    edges = cf.build_edges(inp.kb, inp.graph, idx)
    bcalls = cf.boundary_calls(inp.kb, sorted(inp.kb), cf.boundary_table({}))
    md = cf.render_component(comp, inp.kb, edges, bcalls, A)
    lines = md.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## Summary")), 0)
    body, fence = [], False
    for l in lines[start:]:
        if l.startswith("```"):
            fence = not fence
        body.append(("#" + l) if (not fence and l.startswith("## ")) or (not fence and l.startswith("### "))
                    else l)
    return ["> Static call flow of resolved cross-file calls. Not a data flow: no parameters, no taint, "
            "no sanitisation.", ""] + body


def interfaces(inp, comp):
    graph = inp.graph
    if graph is None:
        return ["> The Link Graph is not available; the interfaces are omitted."]
    rows = defaultdict(lambda: [0, 0, set()])
    for e in graph["fileEdges"]:
        if not e.get("crossComponent"):
            continue
        if e["fromComponent"] == comp:
            key = (e["toComponent"], "→")
        elif e["toComponent"] == comp:
            key = (e["fromComponent"], "←")
        else:
            continue
        r = rows[key]
        r[0] += 1
        r[1] += e["count"]
        r[2] |= set(e.get("kinds", []))
    out = []
    if rows:
        out += md_table(["Component", "Dir", "File Edges", "Summed Count", "Kinds"],
                        [[f"**{c}**", d, r[0], r[1], ", ".join(sorted(r[2]))]
                         for (c, d), r in sorted(rows.items())], right=(2, 3))
    else:
        out.append("No cross-component file edges.")
    seams = [s for s in graph.get("crossComponentSeams", [])
             if comp in (s["a"]["component"], s["b"]["component"])]
    out.append("")
    if seams:
        out += md_table(["Component A", "Component B", "Kind", "Literal", "Site A", "Site B"],
                        [[s["a"]["component"], s["b"]["component"], s["kind"], f"`{s['literal']}`",
                          f"`{s['a']['file']}:{s['a']['line']}`", f"`{s['b']['file']}:{s['b']['line']}`"]
                         for s in seams])
    else:
        out.append("No cross-component seams recorded in the link graph.")
    out += ["", "A missing edge means no edge was found, never that there is no coupling (dynamic "
            "dispatch, calls on local variables and generated clients are not seen)."]
    return out


def component_doc(inp, comp, totals, cf):
    kb = inp.kb_of(comp)
    head = [f"# Component: {comp}", ""]
    if not kb:
        return "\n".join(head + [
            "> **Note:** No KB records received for this component yet; it is partially received. "
            f"{len(inp.files_of(comp))} source files are listed in the intake ledger.", ""]) + "\n"
    sections = [("Overview", overview(inp, comp, totals)), ("File Inventory", file_inventory(inp, comp)),
                ("Defined Symbols", defined_symbols(inp, comp)),
                ("External Dependencies", external_dependencies(inp, comp)),
                ("Side Effects", side_effects(inp, comp, cf)),
                ("Component Diagram", component_diagram(inp, comp)),
                ("Call Flow", call_flow(inp, comp, cf)),
                ("Cross-Component Interfaces", interfaces(inp, comp))]
    out = head
    for title, body in sections:
        out += [f"## {title}", ""] + body + [""]
    return "\n".join(out).rstrip("\n") + "\n"


def index_doc(inp, totals):
    def row(c):
        files, kb = inp.files_of(c), inp.kb_of(c)
        p = inp.purposes.get(c, {}).get("purpose")
        desc = p.split(". ")[0].rstrip(".") + "." if p else "_Purpose not yet written._"
        code = fmt(code_lines(kb)) + ("" if len(kb) == len(files) else f" ({len(kb)}/{len(files)} extracted)")
        return [f"[{c}]({c}.md)", desc, len(files), code]
    prod = [c for c in inp.components if not is_test(c)]
    tests = [c for c in inp.components if is_test(c)]
    out = ["# Component Index", "",
           f"Source folder: `{inp.source_folder}`. {fmt(totals['files'])} files, {fmt(totals['code'])} code "
           f"lines across {len(inp.components)} components."]
    for title, names in (("Production Components", prod), ("Test Components", tests)):
        if names:
            out += ["", f"## {title}", ""] + md_table(["Component", "Description", "Files", "Code Lines"],
                                                       [row(c) for c in names], right=(2, 3))
    if inp.unknown:
        out += ["", "Files without a component or language are listed in [UNKNOWN](UNKNOWN.md)."]
    return "\n".join(out) + "\n"


def unknown_doc(inp):
    rows = [[f"`{f}`", inp.ledger[f].get("language", ""), inp.ledger[f].get("component", "")]
            for f in inp.unknown]
    return "\n".join(["# Component: UNKNOWN", "",
                      "Files the intake could not classify. For human triage: add a prefix to "
                      "`.aee/components.json` or extend `.aee/intake-config.json`.", ""]
                     + md_table(["File", "Language", "Component"], rows)) + "\n"


def agent_bundle(inp, comp, file_docs):
    kb = inp.kb_of(comp)
    files = []
    for k in sorted(kb, key=lambda k: (-(k.get("lines") or {}).get("code", 0), k["file"]))[:25]:
        syms = [s for s in k["symbols"] if s["kind"] in ("class", "function") and not s.get("scope")]
        doc = Path(file_docs) / (k["file"] + ".md")
        overview_txt = ""
        if doc.is_file():
            m = re.search(r"## Overview\s*\n+(.+?)(?:\n\n|\Z)", doc.read_text(), re.S)
            overview_txt = (m.group(1).strip()[:400]) if m else ""
        files.append({"file": k["file"], "codeLines": (k.get("lines") or {}).get("code", 0),
                      "topLevelSymbols": [f"{s['kind']} {s['name']}" for s in syms[:10]],
                      "documentOverview": overview_txt})
    out_n, in_n = neighbours(inp.graph, comp)
    deps = defaultdict(set)
    for k in kb:
        for d in k["dependencies"]:
            if d.get("module") and not d.get("relative"):
                deps[root_of(k["language"], d["module"])].add(k["file"])
    return {"component": comp, "inputHash": inp.input_hash(comp),
            "task": "Write one or two sentences on what this component is for, using only the facts "
                    "below. No advice, no claims beyond the data.",
            "files": files,
            "topPackages": [r for r, _ in sorted(deps.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:10]],
            "callsOut": {n: v[1] for n, v in sorted(out_n.items())},
            "calledBy": {n: v[1] for n, v in sorted(in_n.items())}}


# ---------------------------------------------------------------- stage

def run(a):
    aee, docs = Path(a.aee), Path(a.docs)
    inp = Inputs(aee)
    if not inp.components and not inp.unknown:
        return None
    all_kb = [inp.kb_of(c) for c in inp.components]
    totals = {"files": sum(len(inp.files_of(c)) for c in inp.components),
              "code": sum(code_lines(k) for k in all_kb)}
    cf = callflow_module()
    outputs = {docs / f"{c}.md": component_doc(inp, c, totals, cf) for c in inp.components}
    outputs[docs / "index.md"] = index_doc(inp, totals)
    if inp.unknown:
        outputs[docs / "UNKNOWN.md"] = unknown_doc(inp)
    todo = []
    for c in inp.components:
        if not inp.kb_of(c):
            continue
        bundle = agent_bundle(inp, c, a.file_docs)
        outputs[aee / "synth-inputs" / f"{c}.json"] = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
        if inp.purposes.get(c, {}).get("inputHash") != bundle["inputHash"]:
            todo.append(c)
    outputs[aee / "synth-todo.json"] = json.dumps({"purposesNeeded": todo}, indent=2) + "\n"
    changed = []
    for path, text in sorted(outputs.items()):
        if not path.is_file() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            changed.append(path)
    return changed, todo, inp


def sync(branch="agent_workspace"):
    git("fetch", "origin")
    if git("ls-remote", "--exit-code", "--heads", "origin", branch, check=False).returncode != 0:
        git("checkout", "-b", branch, "origin/main")
        git("push", "-u", "origin", branch)
        return
    git("checkout", branch)
    git("pull", "--ff-only", "origin", branch)


def publish(inp, docs, aee, branch="agent_workspace"):
    artifact = shutil.which("aee-artifact")
    if not artifact:
        raise StageError("aee-artifact: not found on PATH")
    for c in inp.components:
        subprocess.run([artifact, "add", f"Component:{c}", str(docs / f"{c}.md")], check=True)
    subprocess.run([artifact, "add", "Component Index", str(docs / "index.md")], check=True)
    git("add", str(docs), str(aee / "synth-inputs"), str(aee / "synth-todo.json"),
        ".agent-studio/artifacts.jsonl")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-m", "docs: add component synthesis and index")
    git("pull", "--rebase", "origin", branch)
    git("push", "origin", branch)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--docs", default="docs/components")
    ap.add_argument("--file-docs", default="docs")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.sync:
            sync()
        result = run(a)
        if result is None or not result[0]:
            print("[PASS]")
            return PASS
        changed, todo, inp = result
        if a.publish:
            publish(inp, Path(a.docs), Path(a.aee))
        print(f"{len(changed)} files written; purposes needed for {len(todo)} components")
        return 0
    except (StageError, OSError, ValueError, KeyError) as e:
        print(f"[ERROR] {e}")
        return ERROR
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {' '.join(map(str, e.cmd))}: exit {e.returncode}")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
