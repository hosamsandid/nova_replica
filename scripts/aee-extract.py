#!/usr/bin/env python3
"""Deterministic replacement of the Fact Extractor agent.

Builds one knowledge-base record per source file from tool output and fixed pattern
rules; nothing is read or interpreted by a model. Symbols, imports, references and line
counts come from `aee-inventory`, complexity from `lizard`, and for files the inventory
cannot parse symbols come from `ctags`. `securitySurface`, `sideEffects` and
`injection_flags` come from pattern rules (see "surface rules" below): coarser than a
reader's judgment, but reproducible. There are no `source: "manual"` symbols.

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
import ast
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
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


# ---------------------------------------------------------------- surface rules
# Pattern rules for securitySurface (entry-point / sink / privilege), sideEffects and
# injection_flags. Python is read with the `ast` module; JavaScript, Go, Java, C and C++ with line
# regexes (block comments skipped). A language without rules says so in `warnings`.
# Descriptions never contain string literals, so no secret value can reach a KB record.

INJECTION_RE = re.compile(
    r"ignore (all )?(previous|prior) instructions|disregard (the )?(above|previous)|you are now"
    r"|system prompt|mark (all|every)\b.*\b(safe|clean|false positive)|do not report", re.I)
ROUTE_ATTRS = {"route", "get", "post", "put", "delete", "patch"}
PRIV_RE = re.compile(r"require|login|admin|permission|role|auth", re.I)
PY_SINKS = [
    (r"^subprocess\.\w+$|^os\.(system|popen)$", "command-exec", "process / shell execution"),
    (r"^(pickle|cPickle)\.loads?$|^yaml\.(load|unsafe_load)$|^marshal\.loads?$", "deserialization", "deserialization of data"),
    (r"\.(execute|executemany|executescript)$", "sql", "SQL statement execution"),
    (r"^(requests|httpx)\.\w+$", "http-client", "outgoing HTTP request"),
    (r"^send_file$|^send_from_directory$|^open$", "file-access", "file access"),
    (r"^hashlib\.\w+$|^hmac\.\w+$", "crypto", "hash / MAC computation"),
    (r"^random\.\w+$", "random", "non-cryptographic random"),
    (r"^secrets\.\w+$", "random", "cryptographic random"),
    (r"^(eval|exec|compile)$", "code-eval", "dynamic code evaluation"),
    (r"^boto3\.\w+$", "cloud-client", "cloud SDK client"),
]
PY_ENTRY_CALLS = [(r"^input$", "stdin", "reads standard input"),
                  (r"^os\.getenv$|^os\.environ\.get$", "env", "reads an environment variable"),
                  (r"\.parse_args$", "cli-args", "command line arguments")]
PY_ENTRY_ATTRS = [("sys.argv", "cli-args", "command line arguments"),
                  ("os.environ", "env", "reads the environment"),
                  ("sys.stdin", "stdin", "reads standard input")]
JS_RULES = [
    ("entry-point", "http-route", r"\b(app|router)\.(get|post|put|delete|use)\s*\(", "HTTP route registration"),
    ("entry-point", "cli-args", r"\bprocess\.argv\b", "command line input"),
    ("entry-point", "env", r"\bprocess\.env\b", "environment variable"),
    ("entry-point", "browser-input", r"\b(location\.(search|hash|href)|URLSearchParams|\.value\b)", "browser supplied input"),
    ("sink", "code-eval", r"\beval\s*\(|\bnew\s+Function\s*\(", "dynamic code evaluation"),
    ("sink", "dom-write", r"\.(innerHTML|outerHTML)\s*=|document\.write\s*\(", "unescaped DOM write"),
    ("sink", "command-exec", r"\b(exec|execFile|spawn|execSync)\s*\(", "process execution"),
    ("sink", "http-client", r"\bfetch\s*\(", "outgoing HTTP request"),
]
GO_RULES = [
    ("entry-point", "http-route", r"\b(http\.HandleFunc|mux\.Handle\w*|router\.\w+)\s*\(", "HTTP route registration"),
    ("entry-point", "http-param", r"\br\.(URL\.Query|FormValue|Body)\b", "request data"),
    ("entry-point", "cli-args", r"\bos\.Args\b", "command line input"),
    ("entry-point", "env", r"\bos\.Getenv\s*\(", "environment variable"),
    ("sink", "command-exec", r"\bexec\.Command(Context)?\s*\(", "process execution"),
    ("sink", "tls-config", r"\btls\.Config\s*\{", "TLS client configuration"),
    ("sink", "http-client", r"\bhttp\.(Client|Transport|Get|Post)\b", "outgoing HTTP request"),
    ("sink", "sql", r"\.(Exec|Query|QueryRow)\s*\(", "SQL statement execution"),
    ("sink", "file-access", r"\bos\.(Open|ReadFile|Create)\s*\(|\bioutil\.ReadFile\s*\(", "file access"),
]
JAVA_RULES = [
    ("entry-point", "http-route", r"@(RestController|Controller|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\b", "HTTP route mapping"),
    ("entry-point", "http-route", r"\b(void|public)\s+do(Get|Post|Put|Delete)\s*\(\s*HttpServletRequest", "servlet request handler"),
    ("entry-point", "http-param", r"\.(getParameter|getParameterMap|getHeader|getQueryString|getInputStream|getReader|getCookies)\s*\(|@(RequestParam|PathVariable|RequestBody|RequestHeader)\b", "request data"),
    ("entry-point", "cli-args", r"\bpublic\s+static\s+void\s+main\s*\(\s*(final\s+)?String", "command line arguments"),
    ("entry-point", "env", r"\bSystem\.(getenv|getProperty)\s*\(", "environment or system property"),
    ("entry-point", "stdin", r"\bSystem\.in\b", "reads standard input"),
    ("privilege", "access-control", r"@(PreAuthorize|PostAuthorize|RolesAllowed|Secured)\b", "access-control annotation"),
    ("sink", "command-exec", r"\bRuntime\.getRuntime\(\)\s*\.exec\s*\(|\bnew\s+ProcessBuilder\s*\(", "process execution"),
    ("sink", "deserialization", r"\bnew\s+ObjectInputStream\s*\(|\.readObject\s*\(|\bXMLDecoder\b", "deserialization of data"),
    ("sink", "sql", r"\.(executeQuery|executeUpdate|executeBatch|createNativeQuery|prepareStatement|createStatement)\s*\(|\bStatement\.execute\s*\(", "SQL statement execution"),
    ("sink", "file-access", r"\bnew\s+(File|FileInputStream|FileOutputStream|FileReader|FileWriter)\s*\(|\bFiles\.(read|write|newInputStream|newOutputStream|copy|delete|walk)\w*\s*\(", "file access"),
    ("sink", "http-client", r"\b(HttpClient|HttpURLConnection|RestTemplate|WebClient|OkHttpClient)\b|\.openConnection\s*\(", "outgoing HTTP request"),
    ("sink", "crypto", r"\b(MessageDigest|Cipher|KeyGenerator|SecretKeySpec|Mac)\.getInstance\s*\(|\bnew\s+SecretKeySpec\s*\(", "cryptographic primitive"),
    ("sink", "random", r"\bnew\s+Random\s*\(|\bMath\.random\s*\(", "non-cryptographic random"),
    ("sink", "code-eval", r"\bScriptEngine\b|\.eval\s*\(|\bClass\.forName\s*\(", "dynamic code evaluation or loading"),
]
C_RULES = [
    ("entry-point", "cli-args", r"\bmain\s*\(\s*int\s+argc\b|\bargv\s*\[", "command line arguments"),
    ("entry-point", "env", r"\b(secure_)?getenv\s*\(", "environment variable"),
    ("entry-point", "stdin", r"\b(scanf|gets|getchar)\s*\(|\bfgets\s*\([^)]*\bstdin\b|\bstd::cin\b", "reads standard input"),
    ("entry-point", "network-input", r"\b(recv|recvfrom|recvmsg|accept)\s*\(", "network input"),
    ("privilege", "privilege-op", r"\b(setuid|setgid|seteuid|setegid|setresuid|setresgid|chroot|capset|prctl)\s*\(", "privilege or sandbox operation"),
    ("sink", "command-exec", r"\b(system|popen|execl|execlp|execle|execv|execvp|execve|posix_spawn)\s*\(", "process execution"),
    ("sink", "memory-unsafe", r"\b(strcpy|strcat|sprintf|vsprintf|gets|stpcpy)\s*\(", "unbounded copy or format"),
    ("sink", "memory-copy", r"\b(memcpy|memmove|strncpy|strncat)\s*\(", "memory copy"),
    ("sink", "file-access", r"\b(fopen|open|openat|unlink|remove|rename|mkstemp|tmpnam|creat)\s*\(|\bstd::(i|o)?fstream\b", "file access"),
    ("sink", "network-send", r"\b(connect|send|sendto|sendmsg|bind|listen)\s*\(", "network operation"),
    ("sink", "random", r"\b(rand|srand|random|srandom)\s*\(|\bstd::rand\b", "non-cryptographic random"),
    ("sink", "code-eval", r"\bdlopen\s*\(", "dynamic library loading"),
]
SIDE_EFFECT_OF_SINK = {"command-exec": "process-spawn", "file-access": "file-io", "http-client": "network",
                       "sql": "network", "cloud-client": "network", "network-send": "network"}
RULE_LANGUAGES = ("Python", "JavaScript", "Go", "Java", "C", "C++")


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def python_surface(text):
    """(securitySurface, sideEffects) of a Python source, [] for both if it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], []
    surface, effects, seen = [], [], set()

    def add(kind, category, desc, start, end, effect=None, target=None):
        key = (kind, category, start, end)
        if key in seen:
            return
        seen.add(key)
        surface.append({"kind": kind, "category": category, "description": desc,
                        "lineRange": lr(start, end)})
        if effect:
            effects.append({"kind": effect, "target": target, "lineRange": lr(start, end)})

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                name = dotted(dec.func if isinstance(dec, ast.Call) else dec)
                if name.split(".")[-1] in ROUTE_ATTRS and "." in name:
                    add("entry-point", "http-route", f"HTTP route handler {node.name}", dec.lineno, node.lineno)
                elif PRIV_RE.search(name):
                    add("privilege", "access-control", f"{node.name} is guarded by {name}", dec.lineno, node.lineno)
        elif isinstance(node, ast.Call):
            name = dotted(node.func)
            end = node.end_lineno or node.lineno
            if name == "signal.signal":
                effects.append({"kind": "signal-handling", "target": name, "lineRange": lr(node.lineno, end)})
            for rx, cat, what in PY_ENTRY_CALLS:
                if name and re.search(rx, name):
                    add("entry-point", cat, what, node.lineno, end)
            for rx, cat, what in PY_SINKS:
                if name and re.search(rx, name):
                    add("sink", cat, f"call to {name}: {what}", node.lineno, end,
                        SIDE_EFFECT_OF_SINK.get(cat), name)
                    break
        elif isinstance(node, ast.Attribute):
            name = dotted(node)
            if name.startswith("request."):
                add("entry-point", "request-data", f"reads {name}", node.lineno, node.lineno)
            for prefix, cat, what in PY_ENTRY_ATTRS:
                if name == prefix:
                    add("entry-point", cat, what, node.lineno, node.lineno)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            effects.append({"kind": "global-mutation", "target": ", ".join(node.names),
                            "lineRange": lr(node.lineno, node.end_lineno or node.lineno)})
    return surface, effects


def regex_surface(lines, rules):
    surface, effects, in_block = [], [], False
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if in_block:
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("/*") and "*/" not in stripped:
            in_block = True
            continue
        if stripped.startswith(("//", "*", "/*")):
            continue
        for kind, cat, rx, what in rules:
            if re.search(rx, line):
                surface.append({"kind": kind, "category": cat, "description": what, "lineRange": lr(n)})
                if kind == "sink" and cat in SIDE_EFFECT_OF_SINK:
                    effects.append({"kind": SIDE_EFFECT_OF_SINK[cat], "target": what, "lineRange": lr(n)})
    return surface, effects


def injection_flags(text, language):
    """Lines whose text looks like an instruction to an AI reader; recorded, never followed."""
    wheres = {}
    if language == "Python":
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    for n in range(tok.start[0], tok.end[0] + 1):
                        wheres[n] = "comment" if tok.type == tokenize.COMMENT else "string"
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if INJECTION_RE.search(line):
            where = wheres.get(n) or ("comment" if line.strip().startswith(("#", "//", "*", "/*")) else "string")
            out.append({"where": where, "excerpt": line.strip()[:120], "lineRange": lr(n)})
    return out


def surface_for(path, language):
    """(securitySurface, sideEffects, injection_flags, warnings) of one source file."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], [], ["source file could not be read for the surface rules"]
    flags = injection_flags(text, language)
    if language == "Python":
        surface, effects = python_surface(text)
    elif language == "JavaScript":
        surface, effects = regex_surface(text.splitlines(), JS_RULES)
    elif language == "Go":
        surface, effects = regex_surface(text.splitlines(), GO_RULES)
    elif language == "Java":
        surface, effects = regex_surface(text.splitlines(), JAVA_RULES)
    elif language in ("C", "C++"):
        surface, effects = regex_surface(text.splitlines(), C_RULES)
    else:
        return [], [], flags, [f"securitySurface and sideEffects are not extracted for {language}: "
                               "no pattern rules, so empty lists are not evidence of none"]
    surface.sort(key=lambda e: (e["lineRange"]["start"], e["kind"], e["category"]))
    effects.sort(key=lambda e: (e["lineRange"]["start"], e["kind"]))
    return surface, effects, flags, []


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
    rec["securitySurface"], rec["sideEffects"], rec["injection_flags"], surface_warnings = \
        surface_for(entry["file"], entry["language"])
    warnings.extend(surface_warnings)
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
                          "sideEffectCount": len(rec["sideEffects"]),
                          "securitySurfaceCount": len(rec["securitySurface"]),
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
