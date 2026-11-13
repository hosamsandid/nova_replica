"""Shared tables and helpers of the quality stages (imported by aee-quality-*.py, not a command).

Ported from the quality-pipeline pre-work: thresholds, rule equivalence, role and language detection. Paths are
relative to the repository root (the working directory). I/O helpers come from aee_security_lib.
"""
import fnmatch
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aee_security_lib import (dump_json, load_json, now_iso, read_lines, sha256_hex, write_text,  # noqa: E402,F401
                              GROUP_MIN)

CATEGORIES = ["bug-risk", "error-handling", "complexity", "duplication", "dead-code", "maintainability",
              "readability", "style", "shell-hygiene", "container-hygiene", "test-quality", "documentation"]
SEVERITIES = ["high", "medium", "low", "info"]
CONFIDENCES = ["confirmed", "probable", "needs_cross_file"]
TOOLS = {"ruff", "pylint", "shellcheck", "hadolint", "pmd", "checkstyle", "lizard", "tox", "manual"}
LINTERS = ["ruff", "pylint", "shellcheck", "hadolint", "pmd", "checkstyle"]
ALL_TOOLS = LINTERS + ["lizard"]
METRICS = ["ccn", "function_lines", "params", "nesting", "file_lines"]
FINDING_KEYS = ["file", "tool", "ruleId", "lineRange", "title", "detail", "category", "severity", "confidence"]
OPTIONAL_KEYS = ["evidenceQuote"]  # contract extension: mandatory for manual findings

THRESHOLDS = {"ccn": 15, "function_lines": 100, "params": 6, "nesting": 4, "file_lines": 1000}
TOOL_LANGS = {
    "ruff": ["python"], "pylint": ["python"], "shellcheck": ["shell"], "hadolint": ["dockerfile"],
    "pmd": ["java"], "checkstyle": ["java"],
    "lizard": ["python", "java", "javascript", "typescript", "go", "c", "cpp"],
}
UNLINTED_LANGUAGES = ["javascript", "typescript", "go", "c", "cpp"]
EXT_LANG = {
    ".py": "python", ".sh": "shell", ".bash": "shell", ".java": "java",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}
VENDORED_DIRS = ["third_party", "vendor", "node_modules"]
GENERATED_MARKERS = ["DO NOT EDIT", "@generated", "Code generated"]
FIXTURE_DIRS = ["tests/fixtures", "test/fixtures", "fixtures"]
TEST_DIRS = ["tests", "test"]
TEST_NAME_PATTERNS = ["test_*.py", "*_test.py", "*_test.go", "*.test.js", "*.test.ts", "*.spec.js", "*.spec.ts",
                      "*Test.java", "*Tests.java", "conftest.py"]
CONFIG_NAME_PATTERNS = ["setup.py", "*.config.js", "*.config.ts", "conf.py"]
SKIP_ROLES = ("generated", "vendored", "fixture")
SHELLCHECK_SEVERITY = "style"  # the brief says "warning", which hides SC2086 (info) and SC2002 (style)
HADOLINT_THRESHOLD = "warning"

# Rules of different tools that describe the same defect: merged into one candidate.
EQUIVALENCE_GROUPS = {
    "unused-import": ['ruff:F401', 'pylint:W0611'],
    "unused-variable": ['ruff:F841', 'pylint:W0612'],
    "bare-except": ['ruff:E722', 'pylint:W0702'],
    "broad-except": ['ruff:BLE001', 'pylint:W0718'],
    "mutable-default": ['ruff:B006', 'pylint:W0102', 'pylint:W0160'],
    "shadowed-builtin": ['ruff:A001', 'ruff:A002', 'ruff:A003', 'pylint:W0622'],
    "too-many-args": ['ruff:PLR0913', 'pylint:R0913', 'pylint:R0917'],
    "too-many-branches": ['ruff:PLR0912', 'pylint:R0912'],
    "too-many-statements": ['ruff:PLR0915', 'pylint:R0915'],
    "too-many-returns": ['ruff:PLR0911', 'pylint:R0911'],
    "too-many-nested-blocks": ['ruff:PLR1702', 'pylint:R1702'],
    "unreachable-code": ['ruff:PLW0101', 'pylint:W0101'],
    "undefined-name": ['ruff:F821', 'pylint:E0602'],
    "magic-value": ['ruff:PLR2004', 'pylint:R2004'],
    "redefined-name": ['ruff:F811', 'pylint:W0404'],
    "inconsistent-return": ['ruff:RET503', 'pylint:R1710'],
    "unnecessary-else": ['ruff:RET505', 'pylint:R1705'],
    "f-string-no-placeholders": ['ruff:F541', 'pylint:W1309'],
    "unspecified-encoding": ['ruff:PLW1514', 'pylint:W1514'],
    "open-without-context": ['ruff:SIM115', 'pylint:R1732'],
    "empty-catch": ['pmd:EmptyCatchBlock', 'checkstyle:EmptyCatchBlock'],
    "unused-private-method": ['pmd:UnusedPrivateMethod'],
    "unquoted-variable": ['shellcheck:SC2086'],
    "useless-cat": ['shellcheck:SC2002'],
    "unpinned-apt": ['hadolint:DL3008'],
    "latest-tag": ['hadolint:DL3007'],
    "multiple-run": ['hadolint:DL3059'],
}
_EQUIV = {member: name for name, members in EQUIVALENCE_GROUPS.items() for member in members}


def canonical_rule(tool, rule_id):
    key = f"{tool}:{rule_id}"
    return _EQUIV.get(key, key)


def load_config(aee):
    """Defaults overridden by the optional <aee>/quality-config.json (thresholds, dirs, markers, tool levels)."""
    cfg = {"thresholds": dict(THRESHOLDS), "vendoredDirs": list(VENDORED_DIRS), "generatedMarkers": list(GENERATED_MARKERS),
           "fixtureDirs": list(FIXTURE_DIRS), "testDirs": list(TEST_DIRS), "unlintedLanguages": list(UNLINTED_LANGUAGES),
           "shellcheckSeverity": SHELLCHECK_SEVERITY, "hadolintThreshold": HADOLINT_THRESHOLD}
    user = load_json(Path(aee) / "quality-config.json") or {}
    for k, v in user.items():
        if k == "thresholds" and isinstance(v, dict):
            cfg["thresholds"].update({a: b for a, b in v.items() if a in THRESHOLDS})
        elif k in cfg:
            cfg[k] = v
    return cfg


def quality_language(path):
    """Language for the quality tools, by name, extension or shebang; None if the file is not a quality target."""
    p = Path(path)
    name = p.name
    if name == "Dockerfile" or name.startswith("Dockerfile.") or name.lower().endswith(".dockerfile"):
        return "dockerfile"
    lang = EXT_LANG.get(p.suffix.lower())
    if lang:
        return lang
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200).decode("utf-8", "replace").strip()
    except OSError:
        return None
    if first.startswith("#!"):
        tokens = first[2:].split()
        prog = tokens[-1] if tokens and tokens[0].endswith("env") else (tokens[0] if tokens else "")
        prog = prog.split("/")[-1]
        if prog in ("sh", "bash", "zsh", "dash", "ksh"):
            return "shell"
        if prog.startswith("python"):
            return "python"
    return None


def classify_role(rel, head, cfg):
    parts = rel.split("/")
    if set(parts[:-1]) & set(cfg["vendoredDirs"]):
        return "vendored"
    if any(m in line for m in cfg["generatedMarkers"] for line in head.splitlines()[:10]):
        return "generated"
    if any(rel.startswith(d.rstrip("/") + "/") or f"/{d.rstrip('/')}/" in f"/{rel}" for d in cfg["fixtureDirs"]):
        return "fixture"
    name = parts[-1]
    if set(parts[:-1]) & set(cfg["testDirs"]) or any(fnmatch.fnmatch(name, pat) for pat in TEST_NAME_PATTERNS):
        return "test"
    if any(fnmatch.fnmatch(name, pat) for pat in CONFIG_NAME_PATTERNS):
        return "config"
    return "source"


def jsonl(path):
    out = []
    if Path(path).is_file():
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    out.append(None)
    return out
