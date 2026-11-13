#!/usr/bin/env python3
"""Deterministic file-to-file dependency resolver.

Usage: aee-link [--kb DIR] [--pretty]

Reads the knowledge-base records the Fact Extractor wrote (default `.aee/kb`,
one JSON per source file: `file`, `language`, `component`, `package`, `symbols`,
`dependencies`, `referencedSymbols`) and resolves every import, include,
call, inheritance and type reference to the file that provides it. Prints one
JSON document:

  fileEdges   one entry per (from, to) file pair, aggregated from `resolved`
  resolved    one entry per reference whose target file is known; `toLine` is
              the definition line, or 1 with kind `unknown` / `module` when
              the target file is known but the definition is not
  ambiguous   references with more than one plausible target file
  unresolved  imports that look project-internal but found no file
  warnings    KB files that could not be used
  summary     counts

Imports of things outside the extracted file set (stdlib, npm packages, the
JDK, system headers) are neither resolved nor unresolved; they are only counted
as `externalImports`. Same input always gives the same output; the order of the
input files does not matter. Standard library only.
"""
import json
import os
import posixpath
import sys
from collections import defaultdict

JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
LANG = {"python": "python", "javascript": "javascript", "java": "java", "go": "go", "c": "c", "c++": "c"}
DEF_SKIP = {"import"}


def load(kb_dir):
    records, warnings = {}, []
    for dirpath, dirnames, filenames in os.walk(kb_dir):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, kb_dir)
            try:
                with open(path, encoding="utf8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError) as exc:
                warnings.append({"file": rel, "reason": f"unreadable: {exc}"})
                continue
            if not isinstance(rec, dict) or "file" not in rec or "language" not in rec:
                warnings.append({"file": rel, "reason": "not a KB record (no `file` / `language`)"})
                continue
            rec.setdefault("component", "UNKNOWN")
            rec["symbols"] = rec.get("symbols") or []
            rec["dependencies"] = rec.get("dependencies") or []
            rec["referencedSymbols"] = rec.get("referencedSymbols") or []
            records[rec["file"]] = rec
    return records, warnings


def suffix_index(items):
    """{trailing path segments: [value, ...]} for (path, value) pairs."""
    idx = defaultdict(list)
    for path, value in items:
        parts = path.split("/")
        for i in range(len(parts)):
            idx["/".join(parts[i:])].append(value)
    return idx


def common_dirs(a, b):
    n = 0
    for x, y in zip(posixpath.dirname(a).split("/"), posixpath.dirname(b).split("/")):
        if x != y or x == "":
            break
        n += 1
    return n


def nearest(cands, from_file):
    """(file, []) if one candidate stands out by shared directory prefix, else (None, candidates)."""
    cands = sorted(set(cands))
    if len(cands) == 1:
        return cands[0], []
    scores = {c: common_dirs(from_file, c) for c in cands}
    best = max(scores.values())
    top = [c for c in cands if scores[c] == best]
    if len(top) == 1 and best > 0:
        return top[0], []
    return None, cands


class Linker:
    def __init__(self, records):
        self.recs = records
        self.lang = {f: LANG.get(str(r["language"]).lower(), "other") for f, r in records.items()}
        self.resolved, self.ambiguous, self.unresolved = {}, [], []
        self.external = 0
        self.defs = {}      # file -> {name: (kind, line)} for top-level definitions
        self.members = {}   # file -> {name: (kind, line)} for definitions at any scope
        for f, r in records.items():
            top, anyscope = {}, {}
            for s in r["symbols"]:
                if s.get("kind") in DEF_SKIP:
                    continue
                entry = (s["kind"], s["lineRange"]["start"])
                anyscope.setdefault(s["name"], entry)
                if not s.get("scope"):
                    top.setdefault(s["name"], entry)
            self.defs[f], self.members[f] = top, anyscope
        self.dirs = defaultdict(list)
        for f in records:
            self.dirs[posixpath.dirname(f)].append(f)
        self.build_python_index()
        self.build_java_index()
        self.build_c_index()

    # ── output ───────────────────────────────────────────────────────────
    def evidence(self, frm, to, symbol, kind, usage, from_line, to_line):
        if frm == to:
            return
        key = (frm, to, symbol, usage, from_line)
        self.resolved[key] = {
            "from": frm, "to": to, "symbol": symbol, "kind": kind, "usage": usage,
            "fromLine": from_line, "toLine": to_line,
            "crossComponent": self.recs[frm]["component"] != self.recs[to]["component"]}

    def ambiguity(self, frm, symbol, usage, from_line, cands, lines=None):
        self.ambiguous.append({
            "from": frm, "symbol": symbol, "usage": usage, "fromLine": from_line, "reason": "multiple-targets",
            "candidates": [{"to": c, "toLine": (lines or {}).get(c, 1)} for c in sorted(cands)[:10]]})

    def not_found(self, frm, symbol, from_line, reason):
        self.unresolved.append({"from": frm, "symbol": symbol, "usage": "import", "fromLine": from_line, "reason": reason})

    def definition(self, file, name):
        return self.defs[file].get(name) or self.members[file].get(name)

    def to_symbol(self, frm, to, name, usage, from_line):
        """Evidence for `name` provided by file `to`: its definition line, or the module-level fallback."""
        d = self.definition(to, name)
        if d:
            self.evidence(frm, to, name, d[0], usage, from_line, d[1])
        else:
            self.evidence(frm, to, name, "unknown", usage, from_line, 1)

    # ── driver ───────────────────────────────────────────────────────────
    def run(self):
        for f in sorted(self.recs):
            handler = {"python": self.link_python, "javascript": self.link_js, "java": self.link_java,
                       "go": self.link_go, "c": self.link_c}.get(self.lang[f])
            if handler:
                handler(f, self.recs[f])
        return self.output()

    def output(self):
        resolved = sorted(self.resolved.values(), key=lambda r: (r["from"], r["fromLine"], r["to"], r["usage"], r["symbol"]))
        agg = {}
        for r in resolved:
            e = agg.setdefault((r["from"], r["to"]), {"kinds": set(), "count": 0, "firstFromLine": r["fromLine"]})
            e["kinds"].add(r["usage"])
            e["count"] += 1
            e["firstFromLine"] = min(e["firstFromLine"], r["fromLine"])
        edges = []
        for (frm, to), e in sorted(agg.items()):
            fc, tc = self.recs[frm]["component"], self.recs[to]["component"]
            edges.append({"from": frm, "to": to, "fromComponent": fc, "toComponent": tc, "crossComponent": fc != tc,
                          "kinds": sorted(e["kinds"]), "count": e["count"], "firstFromLine": e["firstFromLine"]})
        amb = sorted(self.ambiguous, key=lambda a: (a["from"], a["fromLine"], a["symbol"], a["usage"]))
        unres = sorted(self.unresolved, key=lambda u: (u["from"], u["fromLine"], u["symbol"]))
        return {"tool": "aee-link", "fileEdges": edges, "resolved": resolved, "ambiguous": amb, "unresolved": unres,
                "summary": {"files": len(self.recs), "fileEdges": len(edges),
                            "crossComponentEdges": sum(1 for e in edges if e["crossComponent"]),
                            "resolved": len(resolved), "ambiguous": len(amb), "unresolved": len(unres),
                            "externalImports": self.external}}

    # ── Python ───────────────────────────────────────────────────────────
    def build_python_index(self):
        """{dotted module path: [files]} for every name a file can be imported by.

        A file inside a chain of packages (directories with an `__init__.py`) is only
        importable by its full dotted path from the top of that chain. Files in plain
        directories (scripts, namespace packages, a `src/` layout) can be reached by every
        trailing part of their path, because the import root is not known."""
        self.py_index = defaultdict(list)
        self.py_roots = set()        # first parts of importable names: what "looks internal"
        for f in sorted(self.recs):
            if self.lang[f] != "python" or not f.endswith(".py"):
                continue
            parts = f[:-3].split("/")
            is_init = parts[-1] == "__init__"
            if is_init:
                parts = parts[:-1]
            if not parts:
                continue
            pkg_dirs = parts if is_init else parts[:-1]
            top = len(pkg_dirs)                       # index where the unbroken package chain begins
            while top > 0 and "/".join(pkg_dirs[:top] + ["__init__.py"]) in self.recs:
                top -= 1
            for start in range(top + 1):
                self.py_index["/".join(parts[start:])].append(f)
                self.py_roots.add(parts[start])

    def py_module(self, from_file, module, level):
        """('ok', file) | ('ambiguous', [files]) | ('none', None) for a dotted module."""
        if level:
            base = posixpath.dirname(from_file)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            rel = posixpath.join(base, *module.split(".")) if module else base
            for cand in ((rel + ".py"), posixpath.join(rel, "__init__.py")):
                if cand in self.recs:
                    return "ok", cand
            return "none", None
        cands = self.py_index.get(module.replace(".", "/"), [])
        if not cands:
            return "none", None
        best, many = nearest(cands, from_file)
        return ("ok", best) if best else ("ambiguous", many)

    def py_internal(self, module):
        return module.split(".")[0] in self.py_roots

    def link_python(self, f, rec):
        binds, wild = {}, []
        for d in rec["dependencies"]:
            module, level, line, names = d["module"], d.get("level") or 0, d["lineRange"]["start"], d["names"]
            status, mod = self.py_module(f, module, level)
            if d["style"] == "import":
                if status == "ambiguous":
                    self.ambiguity(f, module, "import", line, mod)
                elif status == "none":
                    self.py_missing(f, module, line, level)
                else:
                    self.evidence(f, mod, module, "module", "import", line, 1)
                    binds.setdefault(d["alias"] or module.split(".")[0], []).append(("module", mod, module))
                continue
            # from <module> import <names>
            any_hit = False
            for n in names:
                name = n["name"]
                if name == "*":
                    if status == "ok":
                        any_hit = True
                        wild.append(mod)
                        self.evidence(f, mod, "*", "module", "import", line, 1)
                    continue
                sub_status, sub = self.py_module(f, f"{module}.{name}" if module else name, level)
                if status == "ok" and self.defs[mod].get(name):
                    kind, ln = self.defs[mod][name]
                    self.evidence(f, mod, name, kind, "import", line, ln)
                    binds.setdefault(n["alias"] or name, []).append(("name", mod, name))
                    any_hit = True
                elif sub_status == "ok":
                    self.evidence(f, sub, name, "module", "import", line, 1)
                    binds.setdefault(n["alias"] or name, []).append(("module", sub, name))
                    any_hit = True
                elif status == "ok":
                    self.evidence(f, mod, name, "unknown", "import", line, 1)
                    binds.setdefault(n["alias"] or name, []).append(("name", mod, name))
                    any_hit = True
            if not names and status == "ok":
                self.evidence(f, mod, module, "module", "import", line, 1)
                any_hit = True
            if status == "ambiguous" and not any_hit:
                self.ambiguity(f, module, "import", line, mod)
            elif status == "none" and not any_hit:
                self.py_missing(f, module, line, level)
        for r in rec["referencedSymbols"]:
            segs = r["name"].split(".")
            line, usage = r["lineRange"]["start"], r["usage"]
            hit = binds.get(segs[0])
            if hit:
                kind, to, orig = self.py_pick(hit, segs)
                if kind == "name":
                    self.to_symbol(f, to, orig, usage, line)
                elif len(segs) > 1:
                    consumed = len(orig.split(".")) if segs[:len(orig.split("."))] == orig.split(".") else 1
                    if len(segs) > consumed:
                        self.to_symbol(f, to, segs[consumed], usage, line)
            elif len(segs) == 1:
                for m in wild:
                    if self.defs[m].get(segs[0]):
                        self.to_symbol(f, m, segs[0], usage, line)
                        break

    def py_pick(self, hits, segs):
        """The binding whose dotted module path is the longest prefix of the referenced name
        (`import a.b` and `import a.c` both bind `a`); the first binding otherwise."""
        best, best_len = hits[0], 0
        for h in hits:
            parts = h[2].split(".") if h[0] == "module" else []
            if parts and segs[:len(parts)] == parts and len(parts) > best_len:
                best, best_len = h, len(parts)
        return best

    def py_missing(self, f, module, line, level):
        if level:
            self.not_found(f, module or ".", line, "relative-import-not-found")
        elif self.py_internal(module):
            self.not_found(f, module, line, "module-not-found")
        else:
            self.external += 1

    # ── JavaScript / TypeScript ──────────────────────────────────────────
    def js_resolve(self, f, spec):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(f), spec))
        root, ext = posixpath.splitext(base)
        cands = [base] + [base + e for e in JS_EXTS]
        if ext in (".js", ".mjs", ".cjs", ".jsx"):
            cands += [root + ".ts", root + ".tsx"]
        cands += [posixpath.join(base, "index" + e) for e in JS_EXTS]
        for c in cands:
            if c in self.recs and self.lang[c] == "javascript":
                return c
        return None

    def link_js(self, f, rec):
        binds = {}
        for d in rec["dependencies"]:
            spec, line = d["module"], d["lineRange"]["start"]
            if not spec.startswith("."):
                self.external += 1
                continue
            to = self.js_resolve(f, spec)
            if to is None:
                if posixpath.splitext(spec)[1].lower() in JS_EXTS or posixpath.splitext(spec)[1] == "":
                    self.not_found(f, spec, line, "relative-import-not-found")
                else:
                    self.external += 1      # an asset (.css, .json, .svg, ...), not code
                continue
            names = [n for n in d["names"] if not (n["name"] == "*" and not n["alias"])]
            if d["alias"]:
                binds[d["alias"]] = ("ns", to, None)
            for n in names:
                if n["name"] == "default":
                    binds[n["alias"]] = ("default", to, None)
                    named = self.definition(to, n["alias"])
                    self.evidence(f, to, "default", named[0] if named else "unknown", "import", line, named[1] if named else 1)
                elif n["name"] == "*":
                    binds[n["alias"]] = ("ns", to, None)
                    self.evidence(f, to, "*", "module", "import", line, 1)
                else:
                    binds[n["alias"] or n["name"]] = ("named", to, n["name"])
                    self.to_symbol(f, to, n["name"], "import", line)
            if not names:
                self.evidence(f, to, spec, "module", "import", line, 1)
        for r in rec["referencedSymbols"]:
            segs = r["name"].split(".")
            b = binds.get(segs[0])
            if not b:
                continue
            kind, to, orig = b
            line, usage = r["lineRange"]["start"], r["usage"]
            if kind == "named":
                self.to_symbol(f, to, orig, usage, line)
            elif kind == "ns" and len(segs) > 1:
                self.to_symbol(f, to, segs[1], usage, line)
            elif kind == "default":
                # an `export default` has no name of its own; a same-named definition is the best guess
                self.to_symbol(f, to, segs[0] if self.definition(to, segs[0]) else "default", usage, line)

    # ── Java ─────────────────────────────────────────────────────────────
    def build_java_index(self):
        self.java_types, self.java_pkgs = {}, set()
        for f, r in self.recs.items():
            if self.lang[f] != "java":
                continue
            pkg = r.get("package") or ""
            self.java_pkgs.add(pkg)
            for s in r["symbols"]:
                if s["kind"] in ("class", "interface", "enum") and not s.get("scope"):
                    self.java_types.setdefault((pkg, s["name"]), f)

    def link_java(self, f, rec):
        own_pkg = rec.get("package") or ""
        named, static, wild = {}, {}, []
        for d in rec["dependencies"]:
            module, line = d["module"], d["lineRange"]["start"]
            if d["style"] == "static":
                pkg, _, cls = module.rpartition(".")
                to = self.java_types.get((pkg, cls))
                if to is None:
                    self.java_missing(f, module, line, pkg)
                    continue
                for n in d["names"]:
                    if n["name"] == "*":
                        wild.append(pkg)
                    else:
                        static[n["name"]] = to
                        self.to_symbol(f, to, n["name"], "import", line)
                continue
            for n in d["names"]:
                if n["name"] == "*":
                    if module in self.java_pkgs:
                        wild.append(module)
                    else:
                        self.external += 1
                    continue
                to = self.java_types.get((module, n["name"]))
                if to is None:
                    self.java_missing(f, f"{module}.{n['name']}", line, module)
                    continue
                named[n["name"]] = to
                self.to_symbol(f, to, n["name"], "import", line)
        for r in rec["referencedSymbols"]:
            segs = r["name"].split(".")
            root, line, usage = segs[0], r["lineRange"]["start"], r["usage"]
            to = named.get(root)
            if to is None and root in static and len(segs) == 1:
                self.to_symbol(f, static[root], root, usage, line)
                continue
            if to is None:
                to = self.java_types.get((own_pkg, root))
            if to is None:
                for pkg in wild:
                    to = self.java_types.get((pkg, root))
                    if to:
                        break
            if to is None:
                continue
            if len(segs) > 1 and self.members[to].get(segs[1]):
                self.to_symbol(f, to, segs[1], usage, line)
            else:
                self.to_symbol(f, to, root, usage, line)

    def java_missing(self, f, symbol, line, pkg):
        if pkg in self.java_pkgs:
            self.not_found(f, symbol, line, "type-not-found")
        else:
            self.external += 1

    # ── Go ───────────────────────────────────────────────────────────────
    def go_dir(self, path):
        dirs = {posixpath.dirname(f) for f in self.recs if self.lang[f] == "go"}
        best, score = [], 0
        for d in dirs:
            if d == "":
                continue
            if path == d:
                n = len(d.split("/")) + 1
            elif "/" in path and (path.endswith("/" + d) or d.endswith("/" + path)):
                n = min(len(d.split("/")), len(path.split("/")))
            else:
                continue
            if n > score:
                best, score = [d], n
            elif n == score:
                best.append(d)
        return best

    def link_go(self, f, rec):
        own_dir = posixpath.dirname(f)
        deps, used = [], set()
        for i, d in enumerate(rec["dependencies"]):
            path, line = d["module"], d["lineRange"]["start"]
            dirs = self.go_dir(path)
            if not dirs:
                self.external += 1
                continue
            if len(dirs) > 1:
                self.ambiguity(f, path, "import", line, [x for dd in dirs for x in self.dirs[dd]])
                continue
            files = sorted(x for x in self.dirs[dirs[0]] if self.lang[x] == "go")
            pkg = next((self.recs[x].get("package") for x in files if self.recs[x].get("package")), None)
            local = d["alias"] or pkg or path.rsplit("/", 1)[-1]
            deps.append((i, local, files, path, line))
        for r in rec["referencedSymbols"]:
            segs = r["name"].split(".")
            line, usage = r["lineRange"]["start"], r["usage"]
            if len(segs) > 1:
                for i, local, files, path, dline in deps:
                    if local == segs[0]:
                        owners = [x for x in files if self.defs[x].get(segs[1])]
                        if len(owners) == 1:
                            self.to_symbol(f, owners[0], segs[1], usage, line)
                            used.add(i)
                        elif owners:
                            self.ambiguity(f, segs[1], usage, line, owners, {x: self.defs[x][segs[1]][1] for x in owners})
                            used.add(i)
                        break
            else:
                owners = [x for x in self.dirs[own_dir] if x != f and self.lang[x] == "go" and self.defs[x].get(segs[0])]
                if len(owners) == 1:
                    self.to_symbol(f, owners[0], segs[0], usage, line)
                elif owners:
                    self.ambiguity(f, segs[0], usage, line, owners, {x: self.defs[x][segs[0]][1] for x in owners})
        for i, local, files, path, line in deps:
            if i not in used:       # only constants/variables or nothing was referenced: depend on the package
                for x in files:
                    self.evidence(f, x, path, "package", "import", line, 1)

    # ── C / C++ ──────────────────────────────────────────────────────────
    def build_c_index(self):
        self.c_defs = defaultdict(list)
        for f, r in self.recs.items():
            if self.lang[f] != "c":
                continue
            for s in r["symbols"]:
                if s["kind"] in ("function", "class", "type", "enum", "constant", "variable") and s["kind"] != "import":
                    self.c_defs[s["name"]].append((f, s["kind"], s["lineRange"]["start"]))
        self.c_suffix = suffix_index((f, f) for f in self.recs if self.lang[f] == "c")

    def link_c(self, f, rec):
        included = set()
        for d in rec["dependencies"]:
            spec, line = d["module"], d["lineRange"]["start"]
            local = posixpath.normpath(posixpath.join(posixpath.dirname(f), spec))
            to = local if local in self.recs else None
            if to is None:
                best, many = nearest(self.c_suffix.get(spec, []), f) if self.c_suffix.get(spec) else (None, [])
                if best is None and many:
                    self.ambiguity(f, spec, "import", line, many)
                    continue
                to = best
            if to:
                included.add(to)
                self.evidence(f, to, spec, "module", "import", line, 1)
            elif d.get("system"):
                self.external += 1
            else:
                self.not_found(f, spec, line, "include-not-found")
        stems = {posixpath.splitext(posixpath.basename(x))[0] for x in included}
        for r in rec["referencedSymbols"]:
            name = r["name"].split("::")[-1]
            if "." in name:
                continue
            line, usage = r["lineRange"]["start"], r["usage"]
            cands = [c for c in self.c_defs.get(name, []) if c[0] != f]
            if len(cands) > 1:
                near = [c for c in cands if posixpath.splitext(posixpath.basename(c[0]))[0] in stems]
                cands = near or cands
            if len(cands) == 1:
                self.evidence(f, cands[0][0], name, cands[0][1], usage, line, cands[0][2])
            elif cands:
                self.ambiguity(f, name, usage, line, [c[0] for c in cands], {c[0]: c[2] for c in cands})


def main(argv):
    kb_dir, pretty, i = ".aee/kb", False, 0
    while i < len(argv):
        if argv[i] == "--kb" and i + 1 < len(argv):
            kb_dir, i = argv[i + 1], i + 1
        elif argv[i] == "--pretty":
            pretty = True
        else:
            print(__doc__, file=sys.stderr)
            return 2
        i += 1
    if not os.path.isdir(kb_dir):
        print(f"aee-link: KB directory not found: {kb_dir}", file=sys.stderr)
        return 2
    records, warnings = load(kb_dir)
    doc = Linker(records).run()
    doc["warnings"] = warnings
    json.dump(doc, sys.stdout, indent=2 if pretty else None, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
