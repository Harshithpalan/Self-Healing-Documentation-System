"""Static analysis of source code: symbols, routes, imports and project metadata."""
from __future__ import annotations

import ast
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field

from .ingest import SourceFile, file_tree

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "route", "api_route", "websocket"}


@dataclass
class Symbol:
    kind: str  # function | class | method
    name: str
    signature: str
    doc: str = ""
    line: int = 0
    params: list[dict] = field(default_factory=list)
    returns: str = ""
    children: list["Symbol"] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)


@dataclass
class Route:
    method: str
    path: str
    handler: str
    file: str


@dataclass
class ModuleInfo:
    path: str
    language: str
    doc: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    lines: int = 0


# ---------------------------------------------------------------- Python

def _unparse(node: ast.AST | None) -> str:
    return ast.unparse(node) if node is not None else ""


def _py_params(args: ast.arguments) -> list[dict]:
    params: list[dict] = []
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(positional, defaults):
        params.append({"name": a.arg, "type": _unparse(a.annotation), "default": _unparse(d)})
    if args.vararg:
        params.append({"name": "*" + args.vararg.arg, "type": _unparse(args.vararg.annotation), "default": ""})
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        params.append({"name": a.arg, "type": _unparse(a.annotation), "default": _unparse(d)})
    if args.kwarg:
        params.append({"name": "**" + args.kwarg.arg, "type": _unparse(args.kwarg.annotation), "default": ""})
    return [p for p in params if p["name"] not in ("self", "cls")]


def _py_function(node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> Symbol:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = _unparse(node.returns)
    sig = f"{prefix} {node.name}({_unparse(node.args)})" + (f" -> {returns}" if returns else "")
    return Symbol(kind, node.name, sig, ast.get_docstring(node) or "", node.lineno, _py_params(node.args), returns)


def analyze_python(f: SourceFile) -> ModuleInfo:
    info = ModuleInfo(f.path, f.language, lines=f.content.count("\n") + 1)
    try:
        tree = ast.parse(f.content)
    except SyntaxError:
        return info
    info.doc = ast.get_docstring(tree) or ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            info.imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            info.imports.append("." * node.level + node.module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    method = dec.func.attr.lower()
                    if method in HTTP_METHODS and dec.args and isinstance(dec.args[0], ast.Constant):
                        verbs = method.upper()
                        for kw in dec.keywords:
                            if kw.arg == "methods":
                                verbs = ", ".join(str(getattr(e, "value", "")) for e in getattr(kw.value, "elts", []))
                        if method == "route" and verbs == "ROUTE":
                            verbs = "GET"
                        info.routes.append(Route(verbs, str(dec.args[0].value), node.name, f.path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.symbols.append(_py_function(node, "function"))
        elif isinstance(node, ast.ClassDef):
            cls = Symbol("class", node.name, f"class {node.name}" + (
                f"({', '.join(_unparse(b) for b in node.bases)})" if node.bases else ""),
                ast.get_docstring(node) or "", node.lineno, bases=[_unparse(b) for b in node.bases])
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cls.children.append(_py_function(item, "method"))
            info.symbols.append(cls)
    return info


# ---------------------------------------------------------------- JS / TS and generic

JS_PATTERNS = [
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*(\([^)]*\))", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?(\([^)]*\)|\w+)\s*(?::[^=]+)?=>", re.M)),
]
JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*|require\s*\(\s*)['"]([^'"]+)['"]""")
JS_ROUTE = re.compile(r"""\b(?:app|router|server|api)\.(get|post|put|patch|delete|all)\s*\(\s*['"`]([^'"`]+)['"`]""", re.I)

GENERIC_PATTERNS = {
    "go": [("function", re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*(\([^)]*\))", re.M)),
           ("class", re.compile(r"^type\s+(\w+)\s+(struct|interface)", re.M))],
    "java": [("class", re.compile(r"^\s*(?:public\s+|abstract\s+|final\s+)*(?:class|interface|enum|record)\s+(\w+)", re.M)),
             ("function", re.compile(r"^\s*(?:public|protected)\s+(?:static\s+)?[\w<>\[\],\s]+?\s+(\w+)\s*(\([^)]*\))\s*(?:throws [\w, ]+)?\{", re.M))],
    "rust": [("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?(\([^)]*\))", re.M)),
             ("class", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)", re.M))],
    "ruby": [("class", re.compile(r"^\s*(?:class|module)\s+([\w:]+)", re.M)),
             ("function", re.compile(r"^\s*def\s+([\w.?!]+)\s*(\([^)]*\))?", re.M))],
    "php": [("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", re.M)),
            ("function", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)\s*(\([^)]*\))", re.M))],
    "csharp": [("class", re.compile(r"^\s*(?:public\s+|internal\s+|static\s+|abstract\s+|sealed\s+|partial\s+)*(?:class|interface|record|struct)\s+(\w+)", re.M)),
               ("function", re.compile(r"^\s*(?:public|protected|internal)\s+(?:static\s+|async\s+|virtual\s+|override\s+)*[\w<>\[\],?]+\s+(\w+)\s*(\([^)]*\))", re.M))],
}
GENERIC_PATTERNS["kotlin"] = [("class", re.compile(r"^\s*(?:data\s+|open\s+|abstract\s+)?class\s+(\w+)", re.M)),
                              ("function", re.compile(r"^\s*(?:suspend\s+)?fun\s+(\w+)\s*(\([^)]*\))", re.M))]
GENERIC_PATTERNS["c"] = GENERIC_PATTERNS["cpp"] = [
    ("function", re.compile(r"^[\w\*\s:<>]+?\s+\**(\w+)\s*(\([^;{)]*\))\s*\{", re.M))]

COMMENT_BEFORE = re.compile(r"(?:/\*\*(.*?)\*/|((?:^\s*(?://|#)[^\n]*\n)+))\s*$", re.S | re.M)


def _leading_comment(text: str, pos: int) -> str:
    m = COMMENT_BEFORE.search(text[max(0, pos - 1500):pos])
    if not m:
        return ""
    raw = m.group(1) or m.group(2) or ""
    lines = [re.sub(r"^\s*(?:\*|//|#)\s?", "", ln).rstrip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def analyze_regex(f: SourceFile) -> ModuleInfo:
    info = ModuleInfo(f.path, f.language, lines=f.content.count("\n") + 1)
    patterns = JS_PATTERNS if f.language in ("javascript", "typescript", "vue", "svelte") else GENERIC_PATTERNS.get(f.language, [])
    found: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for kind, rx in patterns:
        for m in rx.finditer(f.content):
            name = m.group(1)
            if name in ("if", "for", "while", "switch", "return", "catch", "main") and kind == "function" and f.language != "go":
                continue
            line = f.content.count("\n", 0, m.start()) + 1
            if (name, line) in seen:
                continue
            seen.add((name, line))
            extra = m.group(2) if m.lastindex and m.lastindex >= 2 and m.group(2) else ""
            sig = m.group(0).strip().rstrip("{").strip()
            bases = [extra] if kind == "class" and extra and not extra.startswith("(") else []
            params = []
            if kind == "function" and extra.startswith("("):
                for p in [p.strip() for p in extra[1:-1].split(",") if p.strip()]:
                    name_part, _, default = p.partition("=")
                    pname, _, ptype = name_part.partition(":")
                    params.append({"name": pname.strip(), "type": ptype.strip(), "default": default.strip()})
            found.append(Symbol(kind, name, sig, _leading_comment(f.content, m.start()), line, params, bases=bases))
    info.symbols = sorted(found, key=lambda s: s.line)
    if f.language in ("javascript", "typescript", "vue", "svelte"):
        info.imports = JS_IMPORT.findall(f.content)
        info.routes = [Route(m.group(1).upper(), m.group(2), "", f.path) for m in JS_ROUTE.finditer(f.content)]
    return info


CODE_LANGS = {"python", "javascript", "typescript", "vue", "svelte", *GENERIC_PATTERNS.keys()}


def analyze(files: list[SourceFile]) -> list[ModuleInfo]:
    return [analyze_python(f) if f.language == "python" else analyze_regex(f) for f in files if f.language in CODE_LANGS]


# ---------------------------------------------------------------- project metadata

def detect_project(files: list[SourceFile]) -> dict:
    by_name = {os.path.basename(f.path).lower(): f for f in files if f.path.count("/") == 0} or {}
    meta: dict = {"dependencies": [], "scripts": {}, "install": [], "run": [], "test": [], "description": "", "name": ""}
    pkg = by_name.get("package.json")
    if pkg:
        try:
            data = json.loads(pkg.content)
            meta["name"] = data.get("name", "")
            meta["description"] = data.get("description", "")
            meta["dependencies"] += list((data.get("dependencies") or {}).keys())
            meta["scripts"] = data.get("scripts") or {}
            meta["install"].append("npm install")
            for key in ("dev", "start"):
                if key in meta["scripts"]:
                    meta["run"].append(f"npm run {key}" if key != "start" else "npm start")
            if "test" in meta["scripts"]:
                meta["test"].append("npm test")
        except ValueError:
            pass
    req = by_name.get("requirements.txt")
    if req:
        meta["dependencies"] += [re.split(r"[<>=\[;~! ]", ln.strip())[0] for ln in req.content.splitlines()
                                 if ln.strip() and not ln.strip().startswith(("#", "-"))]
        meta["install"].append("pip install -r requirements.txt")
    pyproject = by_name.get("pyproject.toml")
    if pyproject:
        m = re.search(r'^name\s*=\s*"([^"]+)"', pyproject.content, re.M)
        if m and not meta["name"]:
            meta["name"] = m.group(1)
        d = re.search(r'^description\s*=\s*"([^"]+)"', pyproject.content, re.M)
        if d and not meta["description"]:
            meta["description"] = d.group(1)
        if not req:
            meta["install"].append("pip install -e .")
    if "go.mod" in by_name:
        meta["install"].append("go mod download")
        meta["run"].append("go run .")
        meta["test"].append("go test ./...")
    if "cargo.toml" in by_name:
        meta["run"].append("cargo run")
        meta["test"].append("cargo test")
    if "pom.xml" in by_name:
        meta["install"].append("mvn install")
        meta["test"].append("mvn test")
    if "dockerfile" in by_name:
        meta["docker"] = True
    paths = [f.path for f in files]
    if any(p.endswith(".py") for p in paths):
        for cand in ("main.py", "app.py", "manage.py", "server.py", "cli.py", "app/main.py"):
            if cand in paths:
                if any("fastapi" in d.lower() for d in meta["dependencies"]):
                    mod = cand[:-3].replace("/", ".")
                    meta["run"].append(f"uvicorn {mod}:app --reload")
                else:
                    meta["run"].append(f"python {cand}")
                break
        if any(re.search(r"(^|/)tests?/|test_\w+\.py$", p) for p in paths):
            meta["test"].append("pytest")
    env_vars: set[str] = set()
    for f in files:
        env_vars.update(re.findall(r"""(?:os\.getenv|os\.environ\.get|os\.environ\[)\(?\s*['"]([A-Z][A-Z0-9_]+)['"]""", f.content))
        env_vars.update(re.findall(r"process\.env\.([A-Z][A-Z0-9_]+)", f.content))
        env_vars.update(re.findall(r"""os\.Getenv\(\s*"([A-Z][A-Z0-9_]+)"\)""", f.content))
        if os.path.basename(f.path).startswith(".env"):
            env_vars.update(re.findall(r"^([A-Z][A-Z0-9_]+)=", f.content, re.M))
    meta["env_vars"] = sorted(env_vars)
    meta["dependencies"] = sorted(set(d for d in meta["dependencies"] if d))
    return meta


def _first_line(text: str) -> str:
    return text.strip().split("\n\n")[0].replace("\n", " ") if text else ""


def _lang_stats(files: list[SourceFile]) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for f in files:
        if f.language not in ("markdown", "text", "json", "yaml", "toml", "ini"):
            c[f.language] += f.content.count("\n") + 1
    return c.most_common()
