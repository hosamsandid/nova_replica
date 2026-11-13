#!/usr/bin/env python3
"""Cross-component seam index. Works on any repository.

A seam is a literal that two components must spell identically for the system
to work: an HTTP route, a message topic, a database table, an environment
variable, an executable, a native (C) symbol. The tool extracts these literals
with fixed patterns and reports the ones that occur in at least two components.
Deterministic, static, no model involved.

Usage:
  aee-seams <source root> [--json] [--repo-root DIR]
                [--components FILE] [--config FILE]

Paths in the output are relative to the repository root, like every other path
in the pipeline. The repository root is --repo-root, else the git top level of
the source root, else the source root itself.

Components follow the File Intake Classifier: a component map
(--components, else <repo root>/.aee/components.json: {"name": ["path/prefix/"]},
longest prefix wins), else the first directory below the source root. A file
directly in the source root, or matching nothing, is `UNKNOWN`.

Config (--config, else <repo root>/.aee/seam-config.json), all keys optional:
  {
    "patterns":        [{"kind": "env-var", "regex": "\\b(ACME_[A-Z_]+)\\b",
                         "group": 1, "role": "unknown", "extensions": [".py"]}],
    "disable":         ["http-endpoint"],      kinds to switch off
    "ignore":          ["some-literal"],       literals never reported
    "ignoreDirs":      ["generated"],          extra directories to skip
    "requireProvider": ["executable", "native-symbol"]
  }
`role` is `provides`, `uses` or `unknown` (default). For a kind in
`requireProvider` a seam needs one component that provides the literal and
another that uses it.

Output (--json): a list sorted by a.component, b.component, kind, literal:
  {"kind", "literal", "a": {"component", "file", "line"},
   "b": {"component", "file", "line"}, "status": "matched"}
One entry per component pair; a and b are each component's first site.
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from itertools import combinations

SKIP_DIRS = {".git", "node_modules", "build", "target", "dist", "__pycache__",
             ".venv", "venv", ".tox", ".aee", ".agent-studio"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              "Cargo.lock", "go.sum"}
MAX_BYTES = 1_000_000
CODE = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".go", ".java", ".kt", ".py",
        ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".rb", ".php", ".cs"}
CONFIG = {".yaml", ".yml", ".json", ".tf", ".tfvars", ".xml", ".properties",
          ".toml", ".cfg", ".ini", ".sh", ".bash", ".env", ".sql", ".proto"}
C_FAMILY = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"}
ENV_FILES = {".yaml", ".yml", ".sh", ".bash", ".tf", ".tfvars"}
ENV_NOISE = {"PATH", "HOME", "PWD", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
             "TMPDIR", "HOSTNAME", "PYTHONPATH", "TRUE", "FALSE", "NULL", "CI"}
CGO_NOISE = {"CString", "GoString", "GoStringN", "GoBytes", "CBytes", "free",
             "malloc", "int", "uint", "long", "ulong", "char", "uchar", "float",
             "double", "size_t", "void", "struct"}
REQUIRE_PROVIDER = ["executable", "native-symbol"]

LIT = r"""['"`]"""
PATH = r"[A-Za-z0-9_\-.{}:<>~]"
# (kind, regex, role, extensions or None = any scanned file)
BASE_PATTERNS = [
    # HTTP: declared routes provide, client-side literals use.
    ("http-endpoint", rf"""@\w+\.(?:route|get|post|put|delete|patch)\(\s*{LIT}(/{PATH}*(?:/{PATH}*)*){LIT}""", "provides", {".py"}),
    ("http-endpoint", rf"""\b(?:Get|Post|Put|Delete|Patch|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?{LIT}(/{PATH}*(?:/{PATH}*)*){LIT}""", "provides", {".java", ".kt"}),
    ("http-endpoint", rf"""\b(?:app|router|server|r|mux|api)\.(?:get|post|put|delete|patch|use|route|Handle|HandleFunc|GET|POST|PUT|DELETE|PATCH)\(\s*{LIT}(/{PATH}*(?:/{PATH}*)*){LIT}""", "provides", {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rb", ".php"}),
    ("http-endpoint", rf"""\b(?:http\.)?HandleFunc\(\s*{LIT}(/{PATH}*(?:/{PATH}*)*){LIT}""", "provides", {".go"}),
    ("http-endpoint", rf"""{LIT}https?://[^/'"`\s]+(/{PATH}+(?:/{PATH}*)*)(?:\?[^'"`\s]*)?{LIT}""", "uses", None),
    ("http-endpoint", rf"""{LIT}(/(?:api|v[0-9]+|rest|graphql)(?:/{PATH}*)*){LIT}""", "unknown", None),
    # Message topics and queues: a topic/queue argument or an explicit publish/subscribe call.
    ("bus-topic", rf"""(?i)\b(?:topics?|queues?|exchange|routing_?key)\w*\s*[=:(,]\s*\[?\s*{LIT}([A-Za-z0-9][\w\-./:]{{2,}}){LIT}""", "unknown", None),
    ("bus-topic", rf"""\b(?:publish|subscribe|produce|consume|emit|send_?message)\w*\(\s*{LIT}((?:[A-Za-z0-9_\-]+[./:])+[A-Za-z0-9_\-]+){LIT}""", "unknown", None),
    # Database tables.
    ("db-table", r"""(?i)\b(?:FROM|INTO|JOIN|UPDATE)\s+(?:ONLY\s+)?([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)?)\b""", "unknown", None),
    ("db-table", r"""\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)?)""", "unknown", None),
    ("db-table", rf"""(?i)\b(?:__tablename__|table_?name|table)\s*[=:]\s*{LIT}([A-Za-z_][\w.]*){LIT}""", "unknown", None),
    ("db-table", rf"""@Table\(\s*name\s*=\s*"([A-Za-z_][\w.]*)\"""", "unknown", {".java", ".kt"}),
    # Environment variables: accessors read ("uses"), declarations set ("provides").
    ("env-var", rf"""\bos\.environ(?:\.get)?\s*[\[(]\s*{LIT}([A-Z][A-Z0-9_]{{2,}}){LIT}""", "uses", {".py"}),
    ("env-var", rf"""\bos\.getenv\(\s*{LIT}([A-Z][A-Z0-9_]{{2,}}){LIT}""", "uses", {".py"}),
    ("env-var", r"""\bprocess\.env\.([A-Z][A-Z0-9_]{2,})\b""", "uses", None),
    ("env-var", rf"""\bprocess\.env\[\s*{LIT}([A-Z][A-Z0-9_]{{2,}}){LIT}""", "uses", None),
    ("env-var", rf"""(?:System\.getenv|os\.Getenv|os\.LookupEnv|std::getenv|getenv|Environment\.GetEnvironmentVariable|ENV\.fetch|ENV)\s*[\[(]\s*{LIT}([A-Z][A-Z0-9_]{{2,}}){LIT}""", "uses", None),
    ("env-var", r"""^\s*(?:ENV|ARG)\s+([A-Z][A-Z0-9_]{2,})\b""", "provides", None),
    ("env-var", r"""^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})=""", "provides", {".sh", ".bash", ".env"}),
    ("env-var", r"""\$\{?([A-Z][A-Z0-9_]{2,})\b""", "uses", ENV_FILES),
    ("env-var", rf"""^\s*-?\s*name:\s*{LIT}?([A-Z][A-Z0-9_]{{2,}}){LIT}?\s*$""", "provides", {".yaml", ".yml"}),
    # Executables: spawned by name (uses); definitions come from the path rules below.
    ("executable", rf"""\bsubprocess\.\w+\(\s*\[?\s*{LIT}([\w./-]+)""", "uses", {".py"}),
    ("executable", rf"""\bos\.(?:system|popen)\(\s*{LIT}([\w./-]+)""", "uses", {".py"}),
    ("executable", rf"""\bexec\.Command(?:Context)?\(\s*(?:\w+,\s*)?"([\w./-]+)\"""", "uses", {".go"}),
    ("executable", rf"""(?:Runtime\.getRuntime\(\)\.exec|new\s+ProcessBuilder)\(\s*(?:new\s+String\[\]\s*\{{\s*)?"([\w./-]+)\"""", "uses", {".java", ".kt"}),
    ("executable", rf"""\b(?:spawn|spawnSync|exec|execSync|execFile|execFileSync)\(\s*{LIT}([\w./-]+)""", "uses", {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}),
    ("executable", rf"""\b(?:execvp?|execlp?|system|popen)\(\s*"([\w./-]+)\"""", "uses", C_FAMILY),
    ("executable", rf"""^\s*(?:ENTRYPOINT|CMD)\s+\[\s*"([\w./-]+)\"""", "provides", None),
    ("executable", r"""^\s*['"]?([\w-]+)\s*=\s*[\w.]+:[\w.]+['"]?,?\s*$""", "provides", {".cfg", ".toml", ".py"}),
    # Native symbols: cgo calls (uses); matching C definitions are added in a second pass.
    ("native-symbol", r"""\bC\.([A-Za-z_]\w*)\s*\(""", "uses", {".go"}),
]
COMMENT = re.compile(r"^\s*(?://|#(?!include|define|if|endif|else|pragma)|\*|/\*|--)")
SQL_WORD = re.compile(r"\b(?:select|insert|delete|update)\b", re.I)
SQL_RESERVED = {"select", "set", "values", "where", "the", "table", "dual", "lateral",
                "unnest", "generate_series", "information_schema"}
C_DEF = re.compile(r"""^[A-Za-z_][\w \t\*:<>,&]*?[ \t\*&]([A-Za-z_]\w*)\s*\(""")
BIN_PATH = re.compile(r"""(?:^|/)(?:bin|cmd)/([\w.-]+?)(?:\.[a-z]+)?(?:/|$)""")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def git_top_level(root):
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return out or root
    except (OSError, subprocess.CalledProcessError):
        return root


class Seams:
    def __init__(self, source_root, repo_root, components, config):
        self.source_root = os.path.abspath(source_root)
        self.repo_root = os.path.abspath(repo_root)
        self.source_prefix = os.path.relpath(self.source_root, self.repo_root).replace(os.sep, "/")
        self.components = components or {}
        self.disable = set(config.get("disable", []))
        self.ignore = set(config.get("ignore", []))
        self.skip_dirs = SKIP_DIRS | set(config.get("ignoreDirs", []))
        self.require = set(config.get("requireProvider", REQUIRE_PROVIDER))
        self.patterns = [(k, re.compile(r, re.M), role, exts) for k, r, role, exts in BASE_PATTERNS
                         if k not in self.disable]
        for extra in config.get("patterns", []):
            exts = set(extra["extensions"]) if extra.get("extensions") else None
            self.patterns.append((extra["kind"], re.compile(extra["regex"]),
                                  extra.get("role", "unknown"), exts, extra.get("group", 1)))
        # normalise to 5-tuples
        self.patterns = [p if len(p) == 5 else p + (1,) for p in self.patterns]
        self.hits = defaultdict(set)   # (kind, literal) -> {(component, role, file, line)}

    def component_of(self, rel):
        best, best_len = None, -1
        for name, prefixes in self.components.items():
            for prefix in prefixes:
                if rel.startswith(prefix) and len(prefix) > best_len:
                    best, best_len = name, len(prefix)
        if best:
            return best
        inside = rel
        if self.source_prefix not in (".", ""):
            if not rel.startswith(self.source_prefix + "/"):
                return "UNKNOWN"
            inside = rel[len(self.source_prefix) + 1:]
        return inside.split("/", 1)[0] if "/" in inside else "UNKNOWN"

    def add(self, kind, literal, role, rel, line):
        if kind in self.disable or literal in self.ignore or not literal:
            return
        if kind == "http-endpoint":
            literal = re.sub(r"\{[^}/]*\}|<[^>/]*>|:[A-Za-z_]\w*", "{}", literal)
            literal = re.sub(r"/(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27})(?=/|$)", "/{}", literal).rstrip("/") or "/"
            if literal.count("/") < 2 and not literal.startswith(("/api", "/v")):
                return
        if kind == "env-var" and literal in ENV_NOISE:
            return
        if kind == "native-symbol" and literal in CGO_NOISE:
            return
        if kind == "db-table" and literal.lower() in SQL_RESERVED:
            return
        self.hits[(kind, literal)].add((self.component_of(rel), role, rel, line))

    def scan_file(self, path, rel):
        ext = os.path.splitext(path)[1].lower()
        base = os.path.basename(path)
        if base == "Dockerfile" or base.startswith("Dockerfile.") or base.endswith(".dockerfile"):
            ext = ".dockerfile"
        elif base.startswith(".env"):
            ext = ".env"
        match = BIN_PATH.search(rel)
        if match:
            self.add("executable", match.group(1), "provides", rel, 1)
        if ext not in CODE and ext not in CONFIG and ext != ".dockerfile":
            return
        if os.path.getsize(path) > MAX_BYTES:
            return
        try:
            lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        except OSError:
            return
        for number, line in enumerate(lines, 1):
            if COMMENT.match(line) and ext not in {".env"}:
                continue
            for kind, pattern, role, exts, group in self.patterns:
                if exts is not None and ext not in exts:
                    continue
                for found in pattern.finditer(line):
                    value = found.group(group)
                    if kind == "db-table" and found.group(0).split()[0].lower() in {"from", "into", "join", "update"} \
                            and not found.group(0).split()[0].isupper() \
                            and not (SQL_WORD.search(line) and re.search(r"['\"`]", line)):
                        continue
                    self.add(kind, value, role, rel, number)

    def scan(self):
        for base, dirs, names in os.walk(self.source_root):
            dirs[:] = sorted(d for d in dirs if d not in self.skip_dirs)
            for name in sorted(names):
                if name in SKIP_FILES:
                    continue
                path = os.path.join(base, name)
                self.scan_file(path, os.path.relpath(path, self.repo_root).replace(os.sep, "/"))
        self.link_native()

    def link_native(self):
        """Add C/C++ definitions for the names Go calls through cgo."""
        wanted = {lit for (kind, lit) in self.hits if kind == "native-symbol"}
        if not wanted:
            return
        for base, dirs, names in os.walk(self.source_root):
            dirs[:] = sorted(d for d in dirs if d not in self.skip_dirs)
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() not in C_FAMILY:
                    continue
                path = os.path.join(base, name)
                rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
                try:
                    lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, 1):
                    found = C_DEF.match(line)
                    if found and found.group(1) in wanted:
                        self.add("native-symbol", found.group(1), "provides", rel, number)

    def pairs(self):
        out = []
        for (kind, literal), sites in self.hits.items():
            by_component = defaultdict(list)
            for component, role, rel, line in sorted(sites):
                by_component[component].append((role, rel, line))
            for ca, cb in combinations(sorted(by_component), 2):
                site_a, site_b = self.pick(kind, by_component[ca], by_component[cb])
                if site_a is None:
                    continue
                out.append({
                    "kind": kind, "literal": literal,
                    "a": {"component": ca, "file": site_a[1], "line": site_a[2]},
                    "b": {"component": cb, "file": site_b[1], "line": site_b[2]},
                    "status": "matched",
                })
        out.sort(key=lambda s: (s["a"]["component"], s["b"]["component"], s["kind"], s["literal"]))
        return out

    def pick(self, kind, a, b):
        if kind not in self.require:
            return a[0], b[0]
        for first, second in ((a, b), (b, a)):
            provider = next((s for s in first if s[0] == "provides"), None)
            user = next((s for s in second if s[0] == "uses"), None)
            if provider and user:
                return (provider, user) if first is a else (user, provider)
        return None, None


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    opts = {}
    it = iter(argv[1:])
    for token in it:
        if token in ("--repo-root", "--components", "--config"):
            opts[token] = next(it, None)
            if opts[token] in args:
                args.remove(opts[token])
    if not args:
        print(__doc__)
        return 2
    source = args[0]
    if not os.path.isdir(source):
        print("aee-seams: not a directory: %s" % source, file=sys.stderr)
        return 2
    repo_root = opts.get("--repo-root") or git_top_level(source)
    components = load_json(opts.get("--components") or os.path.join(repo_root, ".aee", "components.json"))
    config = load_json(opts.get("--config") or os.path.join(repo_root, ".aee", "seam-config.json")) or {}
    index = Seams(source, repo_root, components, config)
    index.scan()
    seams = index.pairs()
    if "--json" in argv:
        json.dump(seams, sys.stdout, indent=2)
        print()
        return 0
    print("Cross-component seams found: %d\n" % len(seams))
    for seam in seams:
        print("%-14s %s" % (seam["kind"], seam["literal"]))
        for side in ("a", "b"):
            s = seam[side]
            print("    %-12s %s:%d" % (s["component"], s["file"], s["line"]))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
