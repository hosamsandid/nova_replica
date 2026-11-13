#!/usr/bin/env python3
"""Static call flow per component as Mermaid diagrams, deterministically (prototype).

Usage: aee-callflow [--aee DIR] [--out DIR] [--component NAME ...]
                    [--max-nodes N] [--sequences K] [--depth D]

Reads the KB records (DIR/kb), the link graph (DIR/link-graph.json) and optionally
DIR/callflow-config.json, and writes one Markdown file per component to OUT
(default DIR/callflow/<component>.md) with
  * a flowchart: entry points, the cross-file calls between the component's symbols,
    calls to and from other components, and boundary nodes for external modules
    (database, network, process, filesystem, ...);
  * up to K sequence diagrams, one per top entry point (most reachable symbols),
    depth-limited, messages in call-site line order.
No model is involved: same input, same bytes.

What it is, and is not. It is the CALL flow of resolved cross-file calls. A caller or
callee symbol is the innermost function, method or class whose line range holds the
call site or the definition line. It is not a data flow: no parameters, no taint, no
sanitisation. Intra-file calls are not in the KB (the Fact Extractor drops local
references), calls on local variables are not resolved, and message order is source
order, not execution order. Boundary calls are attributed to files, not functions,
because the KB records only the first line of each reference.

Config (all keys optional): {"boundaries": {"<module root>": "<category>"}, "ignore": [...]}
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BOUNDARIES = {
    "database": ["sqlalchemy", "alembic", "oslo_db", "sqlite3", "psycopg2", "pymysql", "MySQLdb",
                 "pymongo", "redis", "peewee"],
    "network": ["requests", "urllib", "urllib3", "http", "httpx", "aiohttp", "socket", "grpc",
                "oslo_messaging", "kombu", "paramiko", "keystoneauth1", "websocket"],
    "process": ["subprocess", "multiprocessing", "oslo_concurrency", "pexpect"],
    "filesystem": ["shutil", "tempfile", "pathlib", "glob", "fcntl"],
    "crypto": ["hashlib", "hmac", "cryptography", "ssl", "secrets", "passlib"],
    "serialization": ["pickle", "yaml", "marshal", "shelve"],
}
SYMBOL_KINDS = ("function", "method", "class")
MODULE_NODE = "(module)"


def load(aee):
    kb = {}
    for p in sorted((aee / "kb").rglob("*.json")):
        k = json.loads(p.read_text())
        kb[k["file"]] = k
    graph = json.loads((aee / "link-graph.json").read_text())
    config = {}
    if (aee / "callflow-config.json").is_file():
        config = json.loads((aee / "callflow-config.json").read_text())
    return kb, graph, config


class Index:
    def __init__(self, kb):
        self.kb = kb
        self.spans = {}
        for f, k in kb.items():
            self.spans[f] = sorted(
                ((s["lineRange"]["start"], s["lineRange"]["end"], s) for s in k["symbols"]
                 if s["kind"] in SYMBOL_KINDS), key=lambda t: (t[0], -t[1]))

    def enclosing(self, file, line):
        best = None
        for a, b, s in self.spans.get(file, []):
            if a <= line <= b:
                best = s  # later (inner) spans start at or after outer ones
        return best

    def at_definition(self, file, line):
        for a, b, s in self.spans.get(file, []):
            if a == line:
                return s
        return self.enclosing(file, line)

    @staticmethod
    def qual(sym):
        if sym is None:
            return MODULE_NODE
        return (sym["scope"] + "." if sym.get("scope") else "") + sym["name"]


def short(path):
    parts = path.split("/")
    return "/".join(parts[-2:])


def san(text):
    """Label text safe for Mermaid (no quotes, semicolons, hashes, colons in sequence text)."""
    return re.sub(r"[\"#;]", "", text).replace(":", "·")


def build_edges(kb, graph, idx):
    """{(caller_key, callee_key): {count, line}} with key = (file, qualname)."""
    edges = {}
    for r in graph.get("resolved", []):
        if r.get("usage") != "call" or r["from"] not in kb or r["to"] not in kb:
            continue
        caller = (r["from"], idx.qual(idx.enclosing(r["from"], r["fromLine"])))
        callee = (r["to"], idx.qual(idx.at_definition(r["to"], r["toLine"])))
        e = edges.setdefault((caller, callee), {"count": 0, "line": r["fromLine"]})
        e["count"] += 1
        e["line"] = min(e["line"], r["fromLine"])
    return edges


def boundary_table(config):
    table = {m: cat for cat, mods in BOUNDARIES.items() for m in mods}
    table.update(config.get("boundaries", {}))
    return table


def boundary_calls(kb, files, table):
    """{file: {category: {"count": n, "modules": set}}} from calls through imported modules."""
    out = {}
    for f in files:
        for r in kb[f]["referencedSymbols"]:
            if r["usage"] != "call" or not r.get("via"):
                continue
            root = r["via"].split(".")[0]
            cat = table.get(root)
            if cat:
                c = out.setdefault(f, {}).setdefault(cat, {"count": 0, "modules": set()})
                c["count"] += r["count"]
                c["modules"].add(root)
    return out


class Flow:
    """Edges of one component, at symbol or file granularity."""

    def __init__(self, comp, kb, edges, collapse):
        self.comp = comp
        comp_of = lambda f: kb[f]["component"]
        node = (lambda key: (key[0], "")) if collapse else (lambda key: key)
        self.internal, self.out, self.inc = defaultdict(lambda: {"count": 0, "line": 10 ** 9}), \
            defaultdict(lambda: {"count": 0, "line": 10 ** 9, "syms": set()}), \
            defaultdict(lambda: {"count": 0, "line": 10 ** 9, "syms": set()})
        for (a, b), e in edges.items():
            ca, cb = comp_of(a[0]), comp_of(b[0])
            if ca == comp and cb == comp:
                t = self.internal[(node(a), node(b))]
            elif ca == comp:
                t = self.out[(node(a), cb)]
                t["syms"].add(short(b[0]) + "::" + b[1] if not collapse else short(b[0]))
            elif cb == comp:
                t = self.inc[(ca, node(b))]
                t["syms"].add(short(a[0]) + "::" + a[1] if not collapse else short(a[0]))
            else:
                continue
            t["count"] += e["count"]
            t["line"] = min(t["line"], e["line"])
        self.nodes = sorted({n for e in self.internal for n in e}
                            | {a for a, _ in self.out} | {b for _, b in self.inc})

    def callees(self, n):
        return sorted(((b, e) for (a, b), e in self.internal.items() if a == n),
                      key=lambda t: (t[1]["line"], t[0]))

    def reach(self, n):
        seen, todo = {n}, [n]
        while todo:
            for b, _ in self.callees(todo.pop()):
                if b not in seen:
                    seen.add(b)
                    todo.append(b)
        return len(seen) - 1

    def entries(self):
        called = {b for (_, b) in self.internal}
        exposed = {b for (_, b) in self.inc}
        calls_out = {a for (a, _) in self.out}
        return [n for n in self.nodes
                if n not in called and (self.callees(n) or n in exposed or n in calls_out)]


def label(node, collapse):
    f, q = node
    return short(f) if collapse or not q else f"{short(f)}::{q}"


def flowchart(flow, bcalls, collapse, cap, emph):
    """Mermaid flowchart of the strongest edges within `cap` nodes. Returns (text, omitted)."""
    items = sorted(
        [("int", a, b, e) for (a, b), e in flow.internal.items()]
        + [("out", a, ("comp", cb), e) for (a, cb), e in flow.out.items()]
        + [("inc", ("comp", ca), b, e) for (ca, b), e in flow.inc.items()],
        key=lambda t: (-t[3]["count"], t[0], str(t[1]), str(t[2])))
    kept, nodes, omitted = [], set(), 0
    for item in items:
        ends = {item[1], item[2]}
        if len(nodes | ends) > cap:
            omitted += 1
            continue
        nodes |= ends
        kept.append(item)
    kept.sort(key=lambda t: (t[0], str(t[1]), str(t[2])))

    sym_nodes = sorted(n for n in nodes if n[0] != "comp")
    comp_nodes = sorted(n for n in nodes if n[0] == "comp")
    ids = {n: f"n{i}" for i, n in enumerate(sym_nodes, 1)}
    ids.update({n: f"x{i}" for i, n in enumerate(comp_nodes, 1)})
    files = sorted({n[0] for n in sym_nodes})
    file_ids = {f: f"f{i}" for i, f in enumerate(files, 1)}
    exposed = {b for (_, b) in flow.inc}

    out = ["flowchart LR"]

    def node_line(n, indent):
        cls = ":::exposed" if n in exposed else (":::entry" if n in emph else "")
        return f'{indent}{ids[n]}["{san(label(n, collapse))}"]{cls}'

    if collapse:
        out += [node_line(n, "  ") for n in sym_nodes]
    else:
        for f in files:
            out.append(f'  subgraph {file_ids[f]}["{san(short(f))}"]')
            out += [f'    {ids[n]}["{san(n[1] or short(n[0]))}"]'
                    + (":::exposed" if n in exposed else (":::entry" if n in emph else ""))
                    for n in sym_nodes if n[0] == f]
            out.append("  end")
    out += [f'  {ids[n]}(["{san(n[1])} (other component)"]):::neighbour' for n in comp_nodes]
    for kind, a, b, e in kept:
        lab = f'|"{e["count"]}"|' if e["count"] > 1 else ""
        out.append(f"  {ids[a]} -->{lab} {ids[b]}")
    cats = defaultdict(list)
    for f in files:
        for cat, v in sorted(bcalls.get(f, {}).items()):
            cats[cat].append((f, v))
    for k, (cat, entries) in enumerate(sorted(cats.items()), 1):
        out.append(f'  b{k}[("{cat}")]:::boundary')
        for f, v in entries:
            src = ids[(f, "")] if collapse else file_ids[f]
            out.append(f'  {src} -.->|"{v["count"]} via {", ".join(sorted(v["modules"]))}"| b{k}')
    out += ["  classDef entry stroke-width:3px",
            "  classDef exposed stroke-width:3px,stroke-dasharray:2 2",
            "  classDef neighbour stroke-dasharray:4 3",
            "  classDef boundary stroke-dasharray:1 3"]
    return "\n".join(out), omitted


def sequence(flow, entry, bcalls, depth, max_msgs=25, max_parts=8):
    parts, msgs, notes_done = {}, [], set()
    truncated = False

    def part(f):
        if f not in parts:
            parts[f] = f"P{len(parts) + 1}"
        return parts[f]

    def note(f):
        if f in notes_done or f not in bcalls:
            return
        notes_done.add(f)
        txt = ", ".join(f'{cat} x{v["count"]} ({"/".join(sorted(v["modules"]))})'
                        for cat, v in sorted(bcalls[f].items()))
        msgs.append(f"  Note over {part(f)}: {san(txt)}")

    def visit(n, d, stack):
        nonlocal truncated
        for b, e in flow.callees(n):
            if len(msgs) >= max_msgs or (b[0] not in parts and len(parts) >= max_parts):
                truncated = True
                return
            msgs.append(f"  {part(n[0])}->>{part(b[0])}: {san(b[1] or short(b[0]))}"
                        + (f" x{e['count']}" if e["count"] > 1 else ""))
            note(b[0])
            if d < depth and b not in stack:
                visit(b, d + 1, stack | {b})

    part(entry[0])
    msgs.append(f"  Caller->>{part(entry[0])}: {san(entry[1] or short(entry[0]))}")
    note(entry[0])
    visit(entry, 1, {entry})
    head = ["sequenceDiagram", "  participant Caller"] + [
        f"  participant {i} as {san(short(f))}" for f, i in parts.items()]
    return "\n".join(head + msgs), truncated


def symbol_nodes(flow):
    return {n for e in flow.internal for n in e} | {x for x, _ in flow.out} | {y for _, y in flow.inc}


def render_component(comp, kb, edges, bcalls_all, a):
    files = sorted(f for f, k in kb.items() if k["component"] == comp)
    bcalls = {f: v for f, v in bcalls_all.items() if f in files}
    sym, fil = Flow(comp, kb, edges, False), Flow(comp, kb, edges, True)
    out = [f"# Call flow: {comp}", "",
           "> Static call flow of resolved cross-file calls. Not a data flow: no parameters, "
           "no taint, no sanitisation. Intra-file calls and calls on local variables are not "
           "included, and message order is source order, not execution order.", ""]
    cats = sorted({c for v in bcalls.values() for c in v})
    out += ["## Summary", "",
            f"- Files: {len(files)}; symbols in call edges: {len(symbol_nodes(sym))}",
            f"- Cross-file calls: {sum(e['count'] for e in sym.internal.values())} inside the "
            f"component, {sum(e['count'] for e in sym.out.values())} to other components, "
            f"{sum(e['count'] for e in sym.inc.values())} from other components",
            "- Boundary calls: " + (", ".join(
                f"{c} x{sum(v[c]['count'] for v in bcalls.values() if c in v)}" for c in cats)
                or "none")]
    if not (sym.internal or sym.out or sym.inc):
        return "\n".join(out + ["", "No resolved cross-file calls touch this component."]) + "\n"

    ranked = sorted(sym.entries(), key=lambda n: (-sym.reach(n), n))
    exposed = defaultdict(int)
    for (_, b), e in sym.inc.items():
        exposed[b] += e["count"]
    shown = ranked[:15]
    out += ["", "## Entry points", "",
            "Symbols no other symbol of this component calls (or that other components call).", "",
            "| Entry | Reaches | Called from other components |", "|---|---|---|"]
    out += [f"| {label(n, False)} | {sym.reach(n)} | {exposed.get(n, 0)} |" for n in shown]
    if len(ranked) > len(shown):
        out += ["", f"{len(ranked) - len(shown)} more entry points not listed."]

    emph = {n for n in ranked if sym.reach(n) >= 2}
    text, _ = flowchart(fil, bcalls, True, a.max_nodes, set())
    out += ["", "## File-level graph", "",
            "Dashed border: called from another component. Dotted edge: calls into an external "
            "module.", "", "```mermaid", text, "```"]
    text, omitted = flowchart(sym, bcalls, False, a.max_nodes, emph)
    out += ["", "## Symbol-level graph", "",
            "Files are boxes; thick border: entry point reaching two or more symbols. Boundary "
            "calls are attributed to the file.", "", "```mermaid", text, "```"]
    if omitted:
        out += ["", f"{omitted} weaker edges omitted to stay within {a.max_nodes} nodes."]

    seq = [n for n in ranked if sym.reach(n) >= 2][:a.sequences]
    if seq:
        out += ["", "## Sequences", "",
                f"The {len(seq)} entry point(s) reaching the most symbols, depth {a.depth}, "
                "messages ordered by call-site line."]
        for n in seq:
            text, truncated = sequence(sym, n, bcalls, a.depth)
            out += ["", f"### {label(n, False)}", "", "```mermaid", text, "```"]
            if truncated:
                out += ["", "Truncated at the message or participant limit."]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--aee", default=".aee")
    ap.add_argument("--out")
    ap.add_argument("--component", action="append")
    ap.add_argument("--max-nodes", type=int, default=40)
    ap.add_argument("--sequences", type=int, default=3)
    ap.add_argument("--depth", type=int, default=3)
    a = ap.parse_args(argv)
    aee = Path(a.aee)
    try:
        kb, graph, config = load(aee)
    except (OSError, ValueError) as e:
        print(f"[ERROR] {e}")
        return 1
    idx = Index(kb)
    edges = build_edges(kb, graph, idx)
    table = boundary_table(config)
    bcalls = boundary_calls(kb, sorted(kb), table)
    out_dir = Path(a.out) if a.out else aee / "callflow"
    out_dir.mkdir(parents=True, exist_ok=True)
    comps = sorted({k["component"] for k in kb.values()})
    for comp in a.component or comps:
        (out_dir / f"{comp}.md").write_text(render_component(comp, kb, edges, bcalls, a))
        print(f"{out_dir / (comp + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
