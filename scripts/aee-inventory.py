#!/usr/bin/env python3
"""Deterministic code inventory (symbols and line counts) built on tree-sitter.

Usage: aee-inventory [--locals] [--pretty] PATH...

Emits one JSON document: per-file line counts and symbols (function, method,
class, interface, enum, type, constant, variable, field, module, import) with
1-based line ranges, plus a summary. Functions and methods also carry
`parameters` (name, type, default), `returnType` (declared only, else null) and
`returns` (line and expression of each return statement, not of nested functions;
an expression-bodied arrow function has one entry marked `implicit`).

Each file also carries the facts a file-to-file dependency graph is built from:
`package` (Java, Go), `imports` (every import / include / require, parsed into
module, names, alias, relativeness) and `refs` (every distinct call, inheritance
and type reference with its first line and a count). Resolving them to other
files is the consumer's job; the tool only reports what the source says.
Same input always gives the same output.
"""
import json
import os
import re
import sys

import tree_sitter_language_pack as tslp

GRAMMAR_DIR = os.environ.get("AEE_TS_CACHE", "/opt/aee/secscan/ts-grammars")
tslp.configure(tslp.PackConfig(cache_dir=GRAMMAR_DIR))

EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}
SKIP_DIRS = {".git", "node_modules", ".aee", ".agent-studio", "dist", "build", "target",
             "__pycache__", ".venv", "venv"}
MAX_BYTES = 2_000_000
UPPER = re.compile(r"[A-Z][A-Z0-9_]*")
FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}


def text(n):
    return n.text.decode("utf8", "replace") if n is not None and n.text else ""


def field(n, name):
    return n.child_by_field_name(name)


def has_token(n, tokens):
    return any(text(c) in tokens for c in n.children)


def decl_name(d):
    """Identifier inside a C/C++ declarator chain, or None for a prototype."""
    while d is not None:
        if d.type in ("identifier", "field_identifier", "type_identifier", "qualified_identifier",
                      "destructor_name", "operator_name"):
            return text(d)
        d = field(d, "declarator")
    return None


def is_prototype(d):
    while d is not None:
        if d.type == "function_declarator":
            return True
        d = field(d, "declarator")
    return False


# Each handler returns (symbols, container). A symbol is (kind, name, node);
# a container is (kind, name) and scopes the node's children.
def h_python(n, kinds, in_func):
    t = n.type
    if t == "function_definition":
        k = "method" if kinds and kinds[-1] == "class" else "function"
        return [(k, text(field(n, "name")), n)], ("function", text(field(n, "name")))
    if t == "class_definition":
        return [("class", text(field(n, "name")), n)], ("class", text(field(n, "name")))
    if t == "assignment" and n.parent is not None and n.parent.type in ("module", "block", "expression_statement"):
        left = field(n, "left")
        if left is not None and left.type == "identifier":
            name = text(left)
            if in_func:
                k = "variable"
            elif kinds and kinds[-1] == "class":
                k = "constant" if UPPER.fullmatch(name) else "field"
            else:
                k = "constant" if UPPER.fullmatch(name) else "variable"
            return [(k, name, n)], None
    if t in ("import_statement", "import_from_statement"):
        return [("import", text(n).split("\n")[0], n)], None
    return [], None


def h_js(n, kinds, in_func):
    t = n.type
    if t in ("function_declaration", "generator_function_declaration"):
        name = text(field(n, "name"))
        return [("function", name, n)], ("function", name)
    if t == "method_definition":
        name = text(field(n, "name"))
        return [("method", name, n)], ("function", name)
    if t in FUNC_VALUES:
        return [], ("function", "")
    if t in ("class_declaration", "abstract_class_declaration"):
        name = text(field(n, "name"))
        return [("class", name, n)], ("class", name)
    if t == "interface_declaration":
        name = text(field(n, "name"))
        return [("interface", name, n)], ("interface", name)
    if t == "enum_declaration":
        name = text(field(n, "name"))
        return [("enum", name, n)], ("enum", name)
    if t == "type_alias_declaration":
        return [("type", text(field(n, "name")), n)], None
    if t in ("lexical_declaration", "variable_declaration"):
        is_const = t == "lexical_declaration" and bool(n.children) and text(n.children[0]) == "const"
        out = []
        for c in n.children:
            if c.type != "variable_declarator":
                continue
            nm = field(c, "name")
            if nm is None or nm.type != "identifier":
                continue
            val = field(c, "value")
            if val is not None and val.type in FUNC_VALUES:
                out.append(("function", text(nm), c))
            else:
                out.append(("constant" if is_const else "variable", text(nm), c))
        return out, None
    if t in ("public_field_definition", "field_definition"):
        nm = field(n, "name") or field(n, "property")
        if nm is not None:
            return [("field", text(nm), n)], None
    if t == "import_statement":
        return [("import", text(n).split("\n")[0], n)], None
    return [], None


def h_java(n, kinds, in_func):
    t = n.type
    kind_map = {"class_declaration": "class", "interface_declaration": "interface",
                "enum_declaration": "enum", "record_declaration": "class",
                "annotation_type_declaration": "interface"}
    if t in kind_map:
        name = text(field(n, "name"))
        return [(kind_map[t], name, n)], (kind_map[t], name)
    if t in ("method_declaration", "constructor_declaration"):
        name = text(field(n, "name"))
        return [("method", name, n)], ("function", name)
    if t == "field_declaration":
        mods = set()
        for c in n.children:
            if c.type == "modifiers":
                mods.update(text(c).split())
        const = ("static" in mods and "final" in mods) or (kinds and kinds[-1] == "interface")
        return [("constant" if const else "field", text(field(c, "name")), c)
                for c in n.children if c.type == "variable_declarator"], None
    if t == "local_variable_declaration":
        return [("variable", text(field(c, "name")), c)
                for c in n.children if c.type == "variable_declarator"], None
    if t == "enum_constant":
        return [("constant", text(field(n, "name")), n)], None
    if t == "import_declaration":
        return [("import", text(n).strip(), n)], None
    return [], None


def h_go(n, kinds, in_func):
    t = n.type
    if t == "function_declaration":
        name = text(field(n, "name"))
        return [("function", name, n)], ("function", name)
    if t == "method_declaration":
        name = text(field(n, "name"))
        return [("method", name, n)], ("function", name)
    if t == "type_spec":
        return [("type", text(field(n, "name")), n)], None
    if t == "const_spec":
        return [("constant", text(c), n) for c in n.children_by_field_name("name")], None
    if t == "var_spec":
        return [("variable", text(c), n) for c in n.children_by_field_name("name")], None
    if t == "short_var_declaration":
        left = field(n, "left")
        if left is not None:
            return [("variable", text(c), n) for c in left.children if c.type == "identifier"], None
    if t == "import_spec":
        return [("import", text(n).strip(), n)], None
    return [], None


def h_c(n, kinds, in_func):
    t = n.type
    in_type = bool(kinds) and kinds[-1] in ("class", "type")
    if t == "function_definition":
        name = decl_name(field(n, "declarator"))
        if name:
            return [("method" if in_type else "function", name, n)], ("function", name)
        return [], ("function", "")
    if t == "declaration":
        decls = n.children_by_field_name("declarator")
        const = any(c.type == "type_qualifier" and text(c) in ("const", "constexpr") for c in n.children)
        out = []
        for d in decls:
            if is_prototype(d):
                continue
            nm = decl_name(d)
            if nm:
                out.append(("constant" if const else "variable", nm, d))
        return out, None
    if t == "field_declaration":
        out = []
        for d in n.children_by_field_name("declarator"):
            nm = None if is_prototype(d) else decl_name(d)
            if nm:
                out.append(("field", nm, d))
        return out, None
    if t == "preproc_def":
        return [("constant", text(field(n, "name")), n)], None
    if t == "preproc_function_def":
        return [("function", text(field(n, "name")), n)], None
    if t == "preproc_include":
        return [("import", text(n).strip(), n)], None
    if t in ("struct_specifier", "union_specifier", "class_specifier") and field(n, "body") is not None:
        name = text(field(n, "name"))
        k = "class" if t == "class_specifier" else "type"
        return [(k, name, n)], (k, name)
    if t == "enum_specifier" and field(n, "body") is not None:
        name = text(field(n, "name"))
        return [("enum", name, n)], ("enum", name)
    if t == "enumerator":
        return [("constant", text(field(n, "name")), n)], None
    if t == "type_definition":
        d = field(n, "declarator")
        nm = decl_name(d)
        if nm:
            return [("type", nm, n)], None
    if t == "namespace_definition":
        name = text(field(n, "name"))
        return [("module", name, n)], ("module", name)
    return [], None


HANDLERS = {
    "python": h_python,
    "javascript": h_js, "typescript": h_js, "tsx": h_js,
    "java": h_java, "go": h_go, "c": h_c, "cpp": h_c,
}


def last_line(n):
    """1-based last line of a node; a node ending at column 0 stops on the previous line."""
    row = n.end_point.row
    if n.end_point.column == 0 and row > n.start_point.row:
        return row
    return row + 1


def clean(s):
    return " ".join((s or "").split())


def param(name=None, typ=None, default=None):
    p = {}
    if name:
        p["name"] = clean(name)
    if typ:
        p["type"] = clean(typ).lstrip(":").strip()
    if default:
        p["default"] = clean(default)
    return p


def named(n):
    return [c for c in n.named_children if c.type != "comment"] if n is not None else []


def params_python(p):
    out = []
    for c in named(p):
        t = c.type
        if t == "identifier":
            out.append(param(text(c)))
        elif t == "typed_parameter":
            out.append(param(text(named(c)[0]), text(field(c, "type"))))
        elif t == "default_parameter":
            out.append(param(text(field(c, "name")), None, text(field(c, "value"))))
        elif t == "typed_default_parameter":
            out.append(param(text(field(c, "name")), text(field(c, "type")), text(field(c, "value"))))
        else:
            out.append(param(text(c)))
    return out


def params_js(p):
    out = []
    if p is not None and p.type == "identifier":
        return [param(text(p))]
    for c in named(p):
        if c.type in ("required_parameter", "optional_parameter"):
            out.append(param(text(field(c, "pattern")), text(field(c, "type")), text(field(c, "value"))))
        elif c.type == "assignment_pattern":
            out.append(param(text(field(c, "left")), None, text(field(c, "right"))))
        else:
            out.append(param(text(c)))
    return out


def params_java(p):
    out = []
    for c in named(p):
        if c.type == "formal_parameter":
            out.append(param(text(field(c, "name")), text(field(c, "type"))))
        elif c.type == "spread_parameter":
            decl = next((x for x in named(c) if x.type == "variable_declarator"), None)
            typ = next((text(x) for x in named(c) if x.type not in ("variable_declarator", "modifiers")), "")
            out.append(param(text(field(decl, "name")) if decl is not None else None, typ + "..."))
        else:
            out.append(param(text(c)))
    return out


def params_go(p):
    out = []
    for c in named(p):
        typ = text(field(c, "type"))
        if c.type == "variadic_parameter_declaration":
            typ = "..." + typ
        names = c.children_by_field_name("name")
        if names:
            out.extend(param(text(nm), typ) for nm in names)
        else:
            out.append(param(None, typ))
    return out


def stars(d):
    """Pointer/reference depth of a C/C++ declarator, down to the function declarator."""
    count = 0
    while d is not None and d.type != "function_declarator":
        if d.type in ("pointer_declarator", "reference_declarator"):
            count += 1
        d = field(d, "declarator")
    return count, d


def params_c(plist):
    out = []
    for c in named(plist):
        if c.type == "parameter_declaration":
            d = field(c, "declarator")
            ptr = 0
            while d is not None and d.type in ("pointer_declarator", "reference_declarator", "abstract_pointer_declarator"):
                ptr += 1
                d = field(d, "declarator")
            out.append(param(decl_name(d), text(field(c, "type")) + "*" * max(ptr, 0)))
        else:
            out.append(param(text(c)))
    return out


NESTED_FUNCS = {
    "python": {"function_definition", "lambda"},
    "javascript": FUNC_VALUES | {"function_declaration", "generator_function_declaration", "method_definition"},
    "java": {"lambda_expression", "method_declaration", "constructor_declaration"},
    "go": {"func_literal", "function_declaration", "method_declaration"},
    "c": {"function_definition", "lambda_expression"},
}
NESTED_FUNCS["typescript"] = NESTED_FUNCS["tsx"] = NESTED_FUNCS["javascript"]
NESTED_FUNCS["cpp"] = NESTED_FUNCS["c"]


def returns_of(fn, lang):
    """return statements of fn itself, not of functions nested inside it."""
    stop = NESTED_FUNCS[lang]
    out = []
    stack = list(reversed(fn.children))
    while stack:
        n = stack.pop()
        if n.type in stop:
            continue
        if n.type == "return_statement":
            expr = clean(" ".join(text(c) for c in named(n)))
            out.append({"line": n.start_point.row + 1, "expr": expr[:200]})
        stack.extend(reversed(n.children))
    out.sort(key=lambda r: r["line"])
    return out


def signature(lang, n):
    """parameters, returnType and returns for a function or method node."""
    fn = n
    if n.type == "variable_declarator":
        fn = field(n, "value")
    if fn is None or fn.type == "preproc_function_def":
        return {}
    if lang == "python":
        params, ret = params_python(field(fn, "parameters")), text(field(fn, "return_type"))
    elif lang in ("javascript", "typescript", "tsx"):
        p = field(fn, "parameters") or field(fn, "parameter")
        params, ret = params_js(p), text(field(fn, "return_type"))
    elif lang == "java":
        params, ret = params_java(field(fn, "parameters")), text(field(fn, "type"))
    elif lang == "go":
        params, ret = params_go(field(fn, "parameters")), text(field(fn, "result"))
    else:
        depth, fd = stars(field(fn, "declarator"))
        params = params_c(field(fd, "parameters")) if fd is not None else []
        ret = text(field(fn, "type")) + "*" * depth if field(fn, "type") is not None else ""
    sig = {"parameters": params, "returns": returns_of(fn, lang)}
    body = field(fn, "body") if fn.type == "arrow_function" else None
    if body is not None and body.type != "statement_block":
        sig["returns"] = [{"line": body.start_point.row + 1, "expr": clean(text(body))[:200], "implicit": True}]
    ret = clean(ret).lstrip(":").strip()
    sig["returnType"] = ret or None
    return sig


def walk(root, lang, with_locals):
    handler = HANDLERS[lang]
    out = []
    stack = [(root, (), (), 0)]
    while stack:
        node, scope, kinds, in_func = stack.pop()
        syms, container = handler(node, kinds, in_func)
        qual = ".".join(s for s in scope if s)
        for kind, name, n in syms:
            if not name:
                continue
            local = bool(in_func) and kind in ("variable", "constant")
            if local and not with_locals:
                continue
            rec = {"kind": kind, "name": name, "scope": qual,
                   "startLine": n.start_point.row + 1, "endLine": last_line(n)}
            if local:
                rec["local"] = True
            if kind in ("function", "method"):
                rec.update(signature(lang, n))
            out.append(rec)
        if container is not None:
            scope = scope + (container[1],)
            kinds = kinds + (container[0],)
            if container[0] == "function":
                in_func += 1
        for child in reversed(node.children):
            stack.append((child, scope, kinds, in_func))
    out.sort(key=lambda r: (r["startLine"], r["endLine"], r["kind"], r["name"]))
    return out


# ── File dependency facts: imports and references ─────────────────────────
LEAF = {"identifier", "property_identifier", "field_identifier", "type_identifier",
        "namespace_identifier", "package_identifier", "this", "super"}
# node type -> (object field, member field, separator)
MEMBER = {
    "attribute": ("object", "attribute", "."),
    "member_expression": ("object", "property", "."),
    "selector_expression": ("operand", "field", "."),
    "field_expression": ("argument", "field", "."),
    "qualified_identifier": ("scope", "name", "::"),
    "nested_type_identifier": ("module", "name", "."),
    "qualified_type": ("package", "name", "."),
    "scoped_identifier": ("scope", "name", "."),
}
WRAP = {"generic_type", "template_type", "template_function"}
TYPE_NODES = {"type_identifier", "nested_type_identifier", "scoped_type_identifier",
              "qualified_type", "generic_type", "template_type"}
JS_LANGS = ("javascript", "typescript", "tsx")
DECL_WITH_NAME = {"class_declaration", "abstract_class_declaration", "interface_declaration",
                  "type_alias_declaration", "enum_declaration", "type_spec", "record_declaration",
                  "annotation_type_declaration"}
DECL_WITH_BODY = {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}


def name_part(n):
    """The name child of a generic/template wrapper (its type arguments are the rest)."""
    return field(n, "name") or (n.named_children[0] if n.named_children else None)


def dotted(n):
    """Dotted name of a plain name chain (`a.b.c`, `ns::f`), generics stripped.
    A chain starting from an expression (`f().g`) becomes `<expr>.g`; anything
    that is not a name chain gives None."""
    if n is None:
        return None
    t = n.type
    if t in LEAF:
        return text(n)
    if t in WRAP:
        return dotted(name_part(n))
    if t == "scoped_type_identifier":
        parts = [dotted(c) for c in n.named_children]
        return ".".join(parts) if parts and all(parts) else None
    if t in MEMBER:
        obj_f, mem_f, sep = MEMBER[t]
        member = dotted(field(n, mem_f))
        if member is None:
            return None
        obj = dotted(field(n, obj_f))
        return f"{obj if obj is not None else '<expr>'}{sep}{member}"
    return None


def claim(n, claimed):
    """Mark a name (and its parts) as already reported, so it is not also a plain type use."""
    if n is None:
        return
    claimed.add(n.id)
    t = n.type
    if t in MEMBER:
        obj_f, mem_f, _ = MEMBER[t]
        claim(field(n, mem_f), claimed)
        claim(field(n, obj_f), claimed)
    elif t in WRAP:
        claim(name_part(n), claimed)
    elif t == "scoped_type_identifier":
        for c in n.named_children:
            claim(c, claimed)


def str_text(n):
    parts = [text(c) for c in n.children if c.type in ("string_fragment", "string_content")]
    return "".join(parts) if parts else text(n).strip("'\"`")


def import_entry(n, style, module, names=None, alias=None, relative=False, **extra):
    e = {"style": style, "module": module, "names": names or [], "alias": alias,
         "relative": relative, "startLine": n.start_point.row + 1, "endLine": last_line(n)}
    e.update(extra)
    return e


def nm(name, alias=None):
    return {"name": name, "alias": alias}


def python_imports(n):
    out = []
    if n.type == "import_statement":
        for c in n.children_by_field_name("name"):
            if c.type == "aliased_import":
                out.append(import_entry(n, "import", "".join(text(field(c, "name")).split()),
                                        alias=text(field(c, "alias"))))
            else:
                out.append(import_entry(n, "import", "".join(text(c).split())))
    elif n.type == "future_import_statement":  # `from __future__ import x`, its own node type
        names = []
        for c in n.children_by_field_name("name"):
            if c.type == "aliased_import":
                names.append(nm(text(field(c, "name")), text(field(c, "alias"))))
            else:
                names.append(nm(text(c)))
        out.append(import_entry(n, "from", "__future__", names))
    elif n.type == "import_from_statement":
        m = field(n, "module_name")
        level, module = 0, ""
        if m is not None and m.type == "relative_import":
            prefix = next((c for c in m.children if c.type == "import_prefix"), None)
            level = len(text(prefix)) if prefix is not None else 0
            dn = next((c for c in m.named_children if c.type == "dotted_name"), None)
            module = "".join(text(dn).split()) if dn is not None else ""
        elif m is not None:
            module = "".join(text(m).split())
        names = []
        for c in n.children_by_field_name("name"):
            if c.type == "aliased_import":
                names.append(nm(text(field(c, "name")), text(field(c, "alias"))))
            else:
                names.append(nm(text(c)))
        if any(c.type == "wildcard_import" for c in n.children):
            names.append(nm("*"))
        extra = {"level": level} if level else {}
        out.append(import_entry(n, "from", module, names, relative=level > 0, **extra))
    return out


def js_imports(n):
    t = n.type
    out = []
    if t == "import_statement":
        src = field(n, "source")
        if src is not None:
            names = []
            for clause in (c for c in n.named_children if c.type == "import_clause"):
                for c in clause.named_children:
                    if c.type == "identifier":
                        names.append(nm("default", text(c)))
                    elif c.type == "namespace_import":
                        names.append(nm("*", text(c.named_children[0])))
                    elif c.type == "named_imports":
                        for sp in c.named_children:
                            if sp.type == "import_specifier":
                                a = field(sp, "alias")
                                names.append(nm(text(field(sp, "name")), text(a) if a is not None else None))
            mod = str_text(src)
            out.append(import_entry(n, "esm", mod, names, relative=mod.startswith(".")))
        else:
            req = next((c for c in n.named_children if c.type == "import_require_clause"), None)
            if req is not None and field(req, "source") is not None:
                mod = str_text(field(req, "source"))
                out.append(import_entry(n, "require", mod, alias=text(req.named_children[0]),
                                        relative=mod.startswith(".")))
    elif t == "export_statement" and field(n, "source") is not None:
        mod = str_text(field(n, "source"))
        names = []
        for c in n.named_children:
            if c.type == "export_clause":
                for sp in c.named_children:
                    if sp.type == "export_specifier":
                        a = field(sp, "alias")
                        names.append(nm(text(field(sp, "name")), text(a) if a is not None else None))
            elif c.type == "namespace_export":
                names.append(nm("*", text(c.named_children[0]) if c.named_children else None))
        if not names:
            names.append(nm("*"))
        out.append(import_entry(n, "reexport", mod, names, relative=mod.startswith(".")))
    elif t == "call_expression":
        fn = field(n, "function")
        args = field(n, "arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if fn is None or first is None or first.type != "string":
            return out
        mod = str_text(first)
        if fn.type == "import":
            out.append(import_entry(n, "dynamic", mod, relative=mod.startswith(".")))
        elif fn.type == "identifier" and text(fn) == "require":
            names, alias = [], None
            p = n.parent
            if p is not None and p.type == "variable_declarator" and field(p, "value") == n:
                target = field(p, "name")
                if target is not None and target.type == "identifier":
                    alias = text(target)
                elif target is not None and target.type == "object_pattern":
                    for c in target.named_children:
                        if c.type == "shorthand_property_identifier_pattern":
                            names.append(nm(text(c)))
                        elif c.type == "pair_pattern":
                            key, val = text(field(c, "key")), text(field(c, "value"))
                            names.append(nm(key, val if val != key else None))
            out.append(import_entry(n, "require", mod, names, alias, relative=mod.startswith(".")))
    return out


def java_imports(n):
    if n.type != "import_declaration":
        return []
    path = next((c for c in n.named_children if c.type in ("scoped_identifier", "identifier")), None)
    if path is None:
        return []
    full = text(path)
    static = any(not c.is_named and text(c) == "static" for c in n.children)
    style = "static" if static else "import"
    if any(c.type == "asterisk" for c in n.named_children):
        return [import_entry(n, style, full, [nm("*")])]
    module, _, last = full.rpartition(".")
    return [import_entry(n, style, module, [nm(last)])]


def go_imports(n):
    if n.type != "import_spec":
        return []
    alias = field(n, "name")
    path = field(n, "path")
    if path is None:
        return []
    return [import_entry(n, "import", text(path).strip('"`'), alias=text(alias) if alias is not None else None)]


def c_imports(n):
    if n.type != "preproc_include":
        return []
    path = field(n, "path")
    if path is None:
        return []
    if path.type == "system_lib_string":
        return [import_entry(n, "include", text(path).strip("<>"), system=True)]
    return [import_entry(n, "include", str_text(path), system=False)]


IMPORTERS = {"python": python_imports, "java": java_imports, "go": go_imports,
             "c": c_imports, "cpp": c_imports,
             "javascript": js_imports, "typescript": js_imports, "tsx": js_imports}


def imports_of(root, lang):
    handler = IMPORTERS[lang]
    out = []
    stack = [root]
    while stack:
        n = stack.pop()
        out.extend(handler(n))
        stack.extend(reversed(n.children))
    out.sort(key=lambda e: e["startLine"])
    return out


def package_of(root, lang):
    """Java `package a.b;` and Go `package main`, else None."""
    want = {"java": "package_declaration", "go": "package_clause"}.get(lang)
    if want is None:
        return None
    for c in root.named_children:
        if c.type == want:
            inner = next((x for x in c.named_children if x.type in ("scoped_identifier", "identifier", "package_identifier")), None)
            return text(inner) if inner is not None else None
    return None


def call_ref(n, lang):
    """(name, target node) if n is a call or an instantiation, else None."""
    t = n.type
    if lang == "java":
        if t == "method_invocation":
            name, obj = text(field(n, "name")), field(n, "object")
            if obj is None:
                return name, None
            o = dotted(obj)
            return f"{o if o is not None else '<expr>'}.{name}", None
        if t == "object_creation_expression":
            tg = field(n, "type")
            name = dotted(tg)
            return (name, tg) if name else None
        return None
    tg = None
    if lang == "python":
        tg = field(n, "function") if t == "call" else None
    elif lang in JS_LANGS:
        if t == "call_expression":
            tg = field(n, "function")
            # `await (x)` is sometimes parsed as a call to a function named `await`
            if tg is not None and (tg.type == "import" or (tg.type == "identifier" and text(tg) in ("require", "await", "yield"))):
                return None
        elif t == "new_expression":
            tg = field(n, "constructor")
    else:  # go, c, cpp
        if t == "call_expression":
            tg = field(n, "function")
        elif t == "new_expression" and lang == "cpp":
            tg = field(n, "type")
    name = dotted(tg) if tg is not None else None
    return (name, tg) if name else None


def inherit_nodes(n, lang):
    """Nodes naming a base class, implemented interface or embedded type."""
    t = n.type
    if lang == "python" and t == "class_definition":
        sc = field(n, "superclasses")
        return [c for c in named(sc) if c.type not in ("keyword_argument", "list_splat", "dictionary_splat")]
    if lang in JS_LANGS:
        if t == "class_heritage":
            out = []
            for c in named(n):
                if c.type in ("extends_clause", "implements_clause"):
                    out.extend(x for x in named(c) if x.type != "type_arguments")
                else:
                    out.append(c)
            return out
        if t == "extends_type_clause":
            return [c for c in named(n) if c.type != "type_arguments"]
    if lang == "java":
        out = []
        if t in ("class_declaration", "enum_declaration", "record_declaration"):
            sc = field(n, "superclass")
            if sc is not None:
                out.extend(named(sc))
            itf = field(n, "interfaces")
            if itf is not None:
                for tl in named(itf):
                    out.extend(named(tl) if tl.type == "type_list" else [tl])
        elif t == "interface_declaration":
            for ext in (c for c in n.named_children if c.type == "extends_interfaces"):
                for tl in named(ext):
                    out.extend(named(tl) if tl.type == "type_list" else [tl])
        return out
    if lang == "go" and t == "field_declaration" and field(n, "name") is None:
        ty = field(n, "type")
        if ty is not None and ty.type == "pointer_type" and named(ty):
            ty = named(ty)[0]
        return [ty] if ty is not None else []
    if lang == "cpp" and t in ("class_specifier", "struct_specifier"):
        base = next((c for c in n.named_children if c.type == "base_class_clause"), None)
        if base is not None:
            return [c for c in named(base) if c.type not in ("access_specifier", "virtual_specifier")]
    return []


def is_declaration_name(n):
    """The name being declared (class A, type S, typedef Y), not a use of a type."""
    p = n.parent
    if p is None:
        return False
    if p.type == "type_parameter":
        return True
    if p.type in DECL_WITH_NAME and field(p, "name") == n:
        return True
    if p.type in DECL_WITH_BODY and field(p, "name") == n and field(p, "body") is not None:
        return True
    return p.type == "type_definition" and field(p, "declarator") == n


def refs_of(root, lang, defined, import_roots):
    """Distinct (usage, name) references: calls, inheritance, type uses."""
    found, claimed = {}, set()

    def add(usage, name, node):
        if not name:
            return
        key = (usage, name)
        line = node.start_point.row + 1
        r = found.get(key)
        if r is None:
            found[key] = {"name": name, "usage": usage, "line": line, "count": 1}
        else:
            r["count"] += 1
            r["line"] = min(r["line"], line)

    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        kids = n.children
        call = call_ref(n, lang)
        if call is not None:
            add("call", call[0], n)
            claim(call[1], claimed)
        for b in inherit_nodes(n, lang):
            name = dotted(b)
            if name:
                add("inherit", name, b)
                claim(b, claimed)
        if lang == "java" and t in ("marker_annotation", "annotation"):
            add("type", dotted(field(n, "name")), n)
        if t in TYPE_NODES:
            if not is_declaration_name(n):
                if n.id not in claimed:
                    add("type", dotted(n), n)
                part = name_part(n) if t in WRAP else None
                kids = [c for c in n.children if part is None or c != part] if t in WRAP else []
            else:
                kids = []
        elif t == "qualified_identifier" and lang in ("c", "cpp") and n.parent is not None \
                and field(n.parent, "type") == n:
            if n.id not in claimed:
                add("type", dotted(n), n)
            claim(n, claimed)
            inner = field(n, "name")
            while inner is not None and inner.type == "qualified_identifier":
                inner = field(inner, "name")
            # only the template arguments of the final name can hold further type uses
            kids = [c for c in inner.children if c != name_part(inner)] \
                if inner is not None and inner.type in WRAP else []
        stack.extend(reversed(kids))

    out = []
    for r in found.values():
        name = r["name"]
        parts = name.split(".")
        if len(parts) == 1:
            local = name in defined
        elif len(parts) == 2 and parts[0] in ("this", "self", "cls"):
            local = parts[1] in defined
        else:
            local = False
        root_name = re.split(r"\.|::", name)[0]
        r["local"] = local
        r["via"] = import_roots.get(root_name)
        out.append(r)
    out.sort(key=lambda r: (r["line"], r["usage"], r["name"]))
    return out


def import_roots_of(imports, lang):
    """Local name -> module of the import that brings it into scope."""
    roots = {}
    for i in imports:
        mod = i["module"]
        if lang == "python":
            if i["style"] == "import":
                roots.setdefault(i["alias"] or mod.split(".")[0], mod)
            else:
                for n in i["names"]:
                    if n["name"] != "*":
                        roots.setdefault(n["alias"] or n["name"], mod)
        elif lang in JS_LANGS:
            if i["alias"]:
                roots.setdefault(i["alias"], mod)
            for n in i["names"]:
                if n["name"] != "*" or n["alias"]:
                    roots.setdefault(n["alias"] or n["name"], mod)
        elif lang == "java":
            for n in i["names"]:
                if n["name"] != "*":
                    roots.setdefault(n["name"], mod)
        elif lang == "go":
            local = i["alias"] or mod.rsplit("/", 1)[-1]
            if local not in ("_", "."):
                roots.setdefault(local, mod)
    return roots


def line_counts(src, lang):
    lines = src.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    counts = {"total": len(lines), "blank": sum(1 for line in lines if not line.strip())}
    try:
        m = tslp.process(src, tslp.ProcessConfig(language=lang, structure=False, symbols=False, imports=False)).metrics
        counts["code"] = m.code_lines
        counts["comment"] = m.comment_lines
    except Exception:
        pass
    return counts


def iter_files(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for f in sorted(filenames):
                yield os.path.join(dirpath, f)


def main(argv):
    with_locals = "--locals" in argv
    pretty = "--pretty" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    files, skipped = [], []
    parsers = {}
    for path in iter_files(paths):
        lang = EXT.get(os.path.splitext(path)[1].lower())
        if not lang:
            continue
        try:
            if os.path.getsize(path) > MAX_BYTES:
                skipped.append({"file": path, "reason": "larger than 2 MB"})
                continue
            with open(path, "rb") as fh:
                raw = fh.read()
            if lang not in parsers:
                parsers[lang] = tslp.get_parser(lang)
            tree = parsers[lang].parse(raw)
            src = raw.decode("utf8", "replace")
            symbols = walk(tree.root_node, lang, with_locals)
            imports = imports_of(tree.root_node, lang)
            defined = {s["name"] for s in symbols if s["kind"] != "import"}
            files.append({
                "file": path, "language": lang, "lines": line_counts(src, lang),
                "parseErrors": bool(tree.root_node.has_error),
                "package": package_of(tree.root_node, lang),
                "symbols": symbols,
                "imports": imports,
                "refs": refs_of(tree.root_node, lang, defined, import_roots_of(imports, lang)),
            })
        except Exception as exc:
            skipped.append({"file": path, "reason": f"{type(exc).__name__}: {exc}"})
    by_kind, by_lang = {}, {}
    for f in files:
        bl = by_lang.setdefault(f["language"], {"files": 0, "total": 0, "code": 0, "comment": 0, "blank": 0})
        bl["files"] += 1
        for k in ("total", "code", "comment", "blank"):
            bl[k] += f["lines"].get(k, 0)
        for s in f["symbols"]:
            by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
    doc = {"tool": "aee-inventory", "locals": with_locals,
           "summary": {"files": len(files), "byLanguage": by_lang, "symbolsByKind": by_kind},
           "files": files, "skipped": skipped}
    json.dump(doc, sys.stdout, indent=2 if pretty else None, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
