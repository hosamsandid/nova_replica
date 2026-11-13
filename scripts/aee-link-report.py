#!/usr/bin/env python3
"""Verify the Link Graph and render the Link Summary, deterministically.

Usage: aee-link-report [--graph FILE] [--out FILE]
  --graph  link graph to read  (default .aee/link-graph.json)
  --out    summary to write    (default .aee/link-summary.md)

Step 8 of the Relationship Linker runs first: every `resolved` entry has `fromLine`
and `toLine`, and for every (from, to) pair the number of `resolved` entries equals
that file edge's `count` (and every pair of `resolved` entries has a file edge). If
a check fails, the problems are printed, nothing is written and the exit code is 1.
Otherwise the summary is written: counts, component pairs (from cross-component
file edges), seams per kind, unresolved imports, ambiguous references, warnings.
The same graph always gives the same bytes.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_SEAM_KINDS = ("http-endpoint", "bus-topic", "db-table", "env-var", "executable",
                      "native-symbol")


def verify(graph):
    """List of integrity problems; empty if the graph is consistent."""
    problems = []
    missing = [r for r in graph.get("resolved", [])
               if r.get("fromLine") is None or r.get("toLine") is None]
    if missing:
        first = missing[0]
        problems.append(f"{len(missing)} resolved entries lack fromLine or toLine "
                        f"(first: {first.get('from')} -> {first.get('to')})")
    per_pair = Counter((r.get("from"), r.get("to")) for r in graph.get("resolved", []))
    edge_counts = {(e["from"], e["to"]): e["count"] for e in graph.get("fileEdges", [])}
    for pair, count in sorted(edge_counts.items()):
        if per_pair.get(pair, 0) != count:
            problems.append(f"file edge {pair[0]} -> {pair[1]} has count {count} but "
                            f"{per_pair.get(pair, 0)} resolved entries")
    for pair in sorted(set(per_pair) - set(edge_counts)):
        problems.append(f"{per_pair[pair]} resolved entries for {pair[0]} -> {pair[1]} "
                        "but no file edge")
    return problems


def cell(value):
    """Markdown table cell: no pipes or newlines."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return out


def render(graph):
    summary = graph.get("summary", {})
    edges = graph.get("fileEdges", [])
    seams = graph.get("crossComponentSeams", [])
    unresolved = graph.get("unresolved", [])
    ambiguous = graph.get("ambiguous", [])
    warnings = graph.get("warnings", [])

    out = ["# Link Summary", "", "## Counts", ""]
    out += table(["Metric", "Value"], [
        ["Files", summary.get("files", 0)],
        ["File edges", summary.get("fileEdges", len(edges))],
        ["Cross-component edges", summary.get("crossComponentEdges",
                                              sum(1 for e in edges if e.get("crossComponent")))],
        ["Resolved", summary.get("resolved", len(graph.get("resolved", [])))],
        ["Ambiguous", summary.get("ambiguous", len(ambiguous))],
        ["Unresolved", summary.get("unresolved", len(unresolved))],
        ["External imports", summary.get("externalImports", 0)],
        ["Seams", summary.get("seams", len(seams))]])

    pairs = defaultdict(lambda: [0, 0])  # (from, to) -> [file edges, resolved entries]
    for e in edges:
        if e.get("crossComponent"):
            p = pairs[(e["fromComponent"], e["toComponent"])]
            p[0] += 1
            p[1] += e["count"]
    out += ["", "## Component Dependencies", ""]
    if pairs:
        out += [f"{len(pairs)} component pairs, {sum(p[0] for p in pairs.values())} "
                f"cross-component file edges, {sum(p[1] for p in pairs.values())} resolved "
                "references.", ""]
        out += table(["From", "To", "File Edges", "Resolved Refs"],
                     [[f, t, v[0], v[1]] for (f, t), v in sorted(pairs.items())])
    else:
        out.append("No cross-component file edges.")

    kinds = Counter(s["kind"] for s in seams)
    all_kinds = list(DEFAULT_SEAM_KINDS) + sorted(k for k in kinds if k not in DEFAULT_SEAM_KINDS)
    out += ["", "## Seams", ""]
    out += table(["Kind", "Count"], [[k, kinds.get(k, 0)] for k in all_kinds])
    out.append("")
    if seams:
        empty = [k for k in all_kinds if not kinds.get(k)]
        out += table(["Component A", "Component B", "Kind", "Literal"],
                     [[s["a"]["component"], s["b"]["component"], s["kind"], s["literal"]]
                      for s in seams])
        if empty:
            out += ["", "Kinds that produced no seams: " + ", ".join(empty) + "."]
    else:
        out.append("No cross-component seams found. All seam kinds produced no results.")

    out += ["", "## Unresolved Imports", ""]
    if unresolved:
        out += ["An import can be unresolved because its target has not been extracted yet; "
                "it may resolve in a later run.", ""]
        out += table(["File", "Line", "Symbol", "Reason"],
                     [[u["from"], u.get("fromLine", ""), u["symbol"], u.get("reason", "")]
                      for u in unresolved])
    else:
        out.append("None.")

    out += ["", "## Ambiguous References", ""]
    if ambiguous:
        out += table(["File", "Line", "Symbol", "Candidates"],
                     [[a["from"], a.get("fromLine", ""), a["symbol"],
                       ", ".join(f"{c['to']}:{c.get('toLine', '')}" for c in a.get("candidates", []))]
                      for a in ambiguous])
    else:
        out.append("None.")

    out += ["", "## Warnings", ""]
    out += [f"- {w}" for w in warnings] if warnings else ["None."]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default=".aee/link-graph.json")
    ap.add_argument("--out", default=".aee/link-summary.md")
    a = ap.parse_args(argv)
    try:
        graph = json.loads(Path(a.graph).read_text())
    except (OSError, ValueError) as e:
        print(f"[ERROR] cannot read {a.graph}: {e}")
        return 1
    problems = verify(graph)
    if problems:
        print(f"[ERROR] link graph failed verification ({len(problems)} problems):")
        for p in problems[:20]:
            print(f"  - {p}")
        return 1
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(render(graph))
    print(f"{a.out}: written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
