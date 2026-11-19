"""Documentation drift detection and self-healing engine."""
from __future__ import annotations

import ast
import difflib
import hashlib
import os
import posixpath
import re
from dataclasses import asdict, dataclass, field

from .analyzer import ModuleInfo, Route, Symbol, analyze

SEVERITY_WEIGHT = {"error": 1.0, "warning": 0.5, "info": 0.2}
DOC_EXTS = (".md", ".markdown", ".mdx")
SKIP_CALLS = {
    "print", "len", "open", "range", "str", "int", "float", "dict", "list", "set", "tuple", "type", "isinstance",
    "require", "fetch", "console", "setTimeout", "map", "filter", "reduce", "super", "main", "sorted", "sum",
    "min", "max", "any", "all", "zip", "enumerate", "format", "input", "exit", "help", "dir", "vars", "repr",
}
CMD_LANGS = {"", "bash", "sh", "shell", "console", "zsh", "powershell", "cmd", "terminal", "text"}


# ---------------------------------------------------------------- data model

@dataclass
class Fix:
    kind: str  # replace | replace_range | insert
    file: str
    line: int  # 1-based. insert: insert after this line (-1 = end of file)
    old: str = ""
    new: str = ""
    end_line: int = 0


@dataclass
class Issue:
    rule: str
    severity: str  # error | warning | info
    file: str
    line: int
    message: str
    evidence: str = ""
    fix: Fix | None = None
    confidence: str = "high"  # high | medium | low (auto-heal only applies high)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raw = f"{self.rule}|{self.file}|{self.line}|{self.message}"
            self.id = hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CodeIndex:
    modules: list[ModuleInfo]
    functions: dict[str, list[Symbol]] = field(default_factory=dict)
    classes: dict[str, Symbol] = field(default_factory=dict)
    py_modules: dict[str, ModuleInfo] = field(default_factory=dict)
    routes: list[Route] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    scripts: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    code_text: str = ""


# ---------------------------------------------------------------- helpers

def is_doc(path: str) -> bool:
    return path.lower().endswith(DOC_EXTS)


VERB_SYNONYMS = {
    "delete": "remove", "destroy": "remove", "del": "remove", "rm": "remove", "drop": "remove",
    "create": "add", "insert": "add", "new": "add", "make": "add", "append": "add",
    "fetch": "get", "read": "get", "find": "get", "retrieve": "get", "load": "get",
    "edit": "update", "modify": "update", "set": "update", "change": "update", "patch": "update",
    "compute": "calculate", "calc": "calculate", "count": "calculate",
}


def _canon(name: str) -> str:
    tokens = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower().split("_")
    return "_".join(VERB_SYNONYMS.get(t, t) for t in tokens if t)


def closest(word: str, candidates, cutoff: float = 0.6) -> str | None:
    pool = list(dict.fromkeys(candidates))
    canon = _canon(word)
    same = [c for c in pool if _canon(c) == canon]
    if same:
        return same[0]
    m = difflib.get_close_matches(word, pool, n=1, cutoff=cutoff)
    if m:
        return m[0]
    low = {c.lower(): c for c in pool}
    m = difflib.get_close_matches(word.lower(), list(low), n=1, cutoff=cutoff)
    return low[m[0]] if m else None


def norm_route(path: str) -> str:
    path = re.sub(r"\{[^}]*\}|<[^>]*>|:\w+", "{}", path.strip())
    return path.rstrip("/") or "/"


ENV_PATTERNS = [
    r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Z][A-Z0-9_]+)['"]""",
    r"""os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)['"]""",
    r"""process\.env\.([A-Z][A-Z0-9_]+)""",
    r"""process\.env\[\s*['"]([A-Z][A-Z0-9_]+)['"]""",
    r"""(?:Getenv|getenv|env::var|ENV\.fetch|ENV\[)\(?\s*['"]([A-Z][A-Z0-9_]+)['"]""",
]


def build_index(files: dict[str, str]) -> CodeIndex:
    from .ingest import SourceFile, detect_language

    code_files = [SourceFile(p, c, detect_language(p) or "text") for p, c in files.items() if not is_doc(p)]
    idx = CodeIndex(modules=analyze(code_files), paths=set(files))
    idx.code_text = "\n".join(f.content for f in code_files)
    for m in idx.modules:
        if m.language == "python":
            dotted = m.path[:-3].replace("/", ".")
            if dotted.endswith(".__init__"):
                dotted = dotted[:-9]
            idx.py_modules[dotted] = m
        idx.routes += m.routes
        for s in m.symbols:
            if s.kind == "class":
                idx.classes[s.name] = s
                for c in s.children:
                    idx.functions.setdefault(c.name, []).append(c)
            else:
                idx.functions.setdefault(s.name, []).append(s)
    for f in code_files:
        if os.path.basename(f.path).startswith(".env"):
            continue
        for rx in ENV_PATTERNS:
            for name in re.findall(rx, f.content):
                idx.env_vars.setdefault(name, f.path)
    import json
    for p, c in files.items():
        if os.path.basename(p) == "package.json":
            try:
                idx.scripts |= set((json.loads(c).get("scripts") or {}).keys())
            except ValueError:
                pass
    return idx


def param_names(sym: Symbol) -> list[str]:
    return [p["name"].lstrip("*") for p in sym.params]


def accepts_kwargs(sym: Symbol) -> bool:
    return any(p["name"].startswith("**") for p in sym.params)


def format_call(qual: str, sym: Symbol) -> str:
    return f"{qual}({', '.join(p['name'] for p in sym.params)})"


# ---------------------------------------------------------------- markdown parsing

@dataclass
class DocParts:
    inline: list[tuple[int, str]] = field(default_factory=list)  # (line, code span)
    links: list[tuple[int, str]] = field(default_factory=list)
    blocks: list[tuple[str, int, list[str]]] = field(default_factory=list)  # (lang, first content line, lines)
    lines: list[str] = field(default_factory=list)


FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)")


def parse_markdown(text: str) -> DocParts:
    parts = DocParts(lines=text.split("\n"))
    fence = None
    for i, line in enumerate(parts.lines, 1):
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0][0] and len(m.group(1)) >= len(fence[0]) and not m.group(2):
                parts.blocks.append((fence[1], fence[2], fence[3]))
                fence = None
            else:
                fence[3].append(line)
            continue
        if m:
            fence = [m.group(1), m.group(2).lower(), i + 1, []]
            continue
        parts.inline += [(i, s) for s in re.findall(r"`([^`\n]+)`", line)]
        parts.links += [(i, t) for t in re.findall(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)", line)]
    if fence:
        parts.blocks.append((fence[1], fence[2], fence[3]))
    return parts


# ---------------------------------------------------------------- detectors

CALL_REF = re.compile(r"^(?P<qual>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\((?P<args>[^()]*)\)$")
IDENT_ARG = re.compile(r"^\*{0,2}[A-Za-z_]\w*\s*(?::[^=]+)?(?:=.*)?$")


class Checker:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.idx = build_index(files)
        self.issues: list[Issue] = []
        self.checked = 0
        self.mentioned_routes: set[tuple[str, str]] = set()
        self.env_suggested: set[str] = set()
        self.doc_text = "\n".join(c for p, c in files.items() if is_doc(p))

    def add(self, *args, **kwargs) -> None:
        self.issues.append(Issue(*args, **kwargs))

    # ---- inline symbol references: `func(a, b)`, `Class.method()`
    def check_inline_ref(self, path: str, line: int, span: str) -> None:
        m = CALL_REF.match(span.strip())
        if not m:
            return
        qual, args = m["qual"], m["args"].strip()
        parts = qual.split(".")
        name, owner = parts[-1], (parts[-2] if len(parts) > 1 else None)
        if name in SKIP_CALLS or (owner and owner in ("console", "Math", "JSON", "os", "sys", "np", "pd", "self")):
            return
        if owner and owner[0].isupper() and owner in self.idx.classes:
            self.checked += 1
            cls = self.idx.classes[owner]
            methods = {c.name: c for c in cls.children}
            if name not in methods:
                best = closest(name, [n for n in methods if not n.startswith("_")], 0.5)
                new_span = format_call(qual.rsplit(".", 1)[0] + "." + best, methods[best]) if best and args else (
                    span.replace(f"{name}(", f"{best}(", 1) if best else "")
                self.add("missing-symbol", "error", path, line,
                         f"`{owner}.{name}()` no longer exists on class `{owner}`" + (f"; did you mean `{best}()`?" if best else ""),
                         f"Methods of {owner}: {', '.join(sorted(methods)) or '—'}",
                         Fix("replace", path, line, f"`{span}`", f"`{new_span}`") if best else None,
                         "high" if best else "low")
                return
            self._check_args(path, line, span, qual, args, [methods[name]])
            return
        candidates = self.idx.functions.get(name)
        if not candidates:
            if re.search(rf"\b{re.escape(name)}\b", self.idx.code_text) or not self.idx.functions:
                return  # defined somewhere we cannot analyse, or external
            self.checked += 1
            best = closest(name, [n for n in self.idx.functions if not n.startswith("_")])
            fix = None
            if best:
                sym = self.idx.functions[best][0]
                new_qual = ".".join(parts[:-1] + [best])
                new_span = format_call(new_qual, sym) if args else span.replace(f"{name}(", f"{best}(", 1)
                fix = Fix("replace", path, line, f"`{span}`", f"`{new_span}`")
            self.add("missing-symbol", "error", path, line,
                     f"`{name}()` is referenced in docs but not defined in the code" + (f"; renamed to `{best}()`?" if best else ""),
                     f"Closest match: {best}" if best else "No similar symbol found", fix, "high" if best else "low")
            return
        self.checked += 1
        self._check_args(path, line, span, qual, args, candidates)

    def _check_args(self, path, line, span, qual, args, candidates: list[Symbol]) -> None:
        if not args or args in ("...", "…"):
            return
        doc_args = [a.strip() for a in args.split(",") if a.strip()]
        if not all(IDENT_ARG.match(a) for a in doc_args):
            return
        names = [re.split(r"[:=\s]", a.lstrip("*"))[0] for a in doc_args]
        if any(param_names(c) == names for c in candidates):
            return
        sym = min(candidates, key=lambda c: len(set(param_names(c)) ^ set(names)))
        actual = param_names(sym)
        removed = [n for n in names if n not in actual]
        added = [n for n in actual if n not in names]
        detail = "; ".join(x for x in (f"removed: {', '.join(removed)}" if removed else "",
                                       f"added: {', '.join(added)}" if added else "",
                                       "order changed" if not removed and not added else "") if x)
        self.add("signature-drift", "warning", path, line,
                 f"Signature of `{qual.split('.')[-1]}()` in docs is out of date ({detail})",
                 f"Actual: {sym.signature}", Fix("replace", path, line, f"`{span}`", f"`{format_call(qual, sym)}`"))

    # ---- python code examples
    def check_python_block(self, path: str, start: int, lines: list[str]) -> None:
        code_lines = [re.sub(r"^(>>> |\.\.\. )", "", ln) for ln in lines]
        if any(ln.startswith(">>>") for ln in lines):
            code_lines = [re.sub(r"^(>>> |\.\.\. )", "", ln) for ln in lines if ln.startswith((">>>", "..."))]
            return  # doctest output lines make line mapping unreliable; skip
        src = "\n".join(code_lines)
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            if "..." in src or "…" in src:
                return
            self.add("broken-example", "warning", path, start + (exc.lineno or 1) - 1,
                     f"Python example does not parse: {exc.msg}", lines[(exc.lineno or 1) - 1].strip()
                     if exc.lineno and exc.lineno <= len(lines) else "", None, "low")
            return
        imported: dict[str, Symbol] = {}
        instances: dict[str, Symbol] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = self._resolve_module(node.module)
                if mod is None:
                    continue
                top = {s.name: s for s in mod.symbols}
                text = self.files.get(mod.path, "")
                for alias in node.names:
                    self.checked += 1
                    if alias.name in top:
                        imported[alias.asname or alias.name] = top[alias.name]
                    elif alias.name != "*" and not re.search(rf"^{re.escape(alias.name)}\s*[:=]", text, re.M):
                        best = closest(alias.name, [n for n in top if not n.startswith("_")])
                        ln = start + node.lineno - 1
                        self.add("missing-symbol", "error", path, ln,
                                 f"Example imports `{alias.name}` from `{node.module}`, which no longer defines it"
                                 + (f"; did you mean `{best}`?" if best else ""),
                                 lines[node.lineno - 1].strip(),
                                 Fix("replace", path, ln, alias.name, best) if best else None,
                                 "high" if best else "low")
                        if best:
                            imported[alias.asname or alias.name] = top[best]
                            for use in ast.walk(tree):
                                if isinstance(use, ast.Name) and use.id == alias.name and use.lineno != node.lineno:
                                    uln = start + use.lineno - 1
                                    self.add("missing-symbol", "error", path, uln,
                                             f"Example uses `{alias.name}`, which was renamed to `{best}`",
                                             lines[use.lineno - 1].strip(), Fix("replace", path, uln, alias.name, best))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                cls = imported.get(node.value.func.id)
                if cls and cls.kind == "class":
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            instances[t.id] = cls
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            sym, label = None, ""
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                sym = imported[node.func.id]
                label = node.func.id
                if sym.kind == "class":
                    sym = next((c for c in sym.children if c.name == "__init__"), None)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                cls = instances.get(node.func.value.id) or (
                    imported.get(node.func.value.id) if imported.get(node.func.value.id, Symbol("", "", "")).kind == "class" else None)
                if cls:
                    methods = {c.name: c for c in cls.children}
                    label = f"{node.func.value.id}.{node.func.attr}"
                    self.checked += 1
                    if node.func.attr not in methods:
                        best = closest(node.func.attr, [n for n in methods if not n.startswith("_")], 0.5)
                        ln = start + node.lineno - 1
                        self.add("missing-symbol", "error", path, ln,
                                 f"Example calls `{label}()` but `{cls.name}` has no method `{node.func.attr}`"
                                 + (f"; did you mean `{best}()`?" if best else ""),
                                 lines[node.lineno - 1].strip(),
                                 Fix("replace", path, ln, f".{node.func.attr}(", f".{best}(") if best else None,
                                 "high" if best else "low")
                        sym = methods.get(best) if best else None
                    else:
                        sym = methods[node.func.attr]
            if sym is None or accepts_kwargs(sym):
                continue
            params = param_names(sym)
            for kw in node.keywords:
                if kw.arg and kw.arg not in params:
                    best = closest(kw.arg, [p for p in params if p not in {k.arg for k in node.keywords}], 0.5)
                    ln = start + kw.value.lineno - 1
                    self.add("signature-drift", "error", path, ln,
                             f"Example passes unknown argument `{kw.arg}` to `{label}()`" + (f"; now `{best}`" if best else ""),
                             f"Actual: {sym.signature}",
                             Fix("replace", path, ln, f"{kw.arg}=", f"{best}=") if best else None,
                             "high" if best else "low")
            max_pos = len([p for p in sym.params if not p["name"].startswith("*")])
            if len(node.args) > max_pos and not any(p["name"].startswith("*") and not p["name"].startswith("**") for p in sym.params):
                self.add("signature-drift", "warning", path, start + node.lineno - 1,
                         f"Example passes {len(node.args)} positional arguments to `{label}()`, which accepts {max_pos}",
                         f"Actual: {sym.signature}", None, "low")

    def _resolve_module(self, dotted: str) -> ModuleInfo | None:
        mods = self.idx.py_modules
        if dotted in mods:
            return mods[dotted]
        for k, v in mods.items():
            if k.endswith("." + dotted):
                return v
        return None

    # ---- endpoints
    def check_endpoints(self, path: str, parts: DocParts) -> None:
        if not self.idx.routes:
            return
        known = {(m, norm_route(r.path)): r for r in self.idx.routes for m in r.method.replace(" ", "").split(",")}
        for i, line in enumerate(parts.lines, 1):
            for em in re.finditer(r"\b(GET|POST|PUT|PATCH|DELETE)`?\s*(?:\|\s*)?`?(/[\w\-./{}:<>]*)", line):
                method, rpath = em.group(1), em.group(2).rstrip(".`")
                matched = em.group(0).rstrip(".`")
                key = (method, norm_route(rpath))
                self.checked += 1
                if key in known:
                    self.mentioned_routes.add(key)
                    continue
                same_path = [m for (m, p) in known if p == key[1]]
                if same_path:
                    new_m = "PATCH" if "PATCH" in same_path and method == "PUT" else same_path[0]
                    self.mentioned_routes.add((new_m, key[1]))
                    self.add("stale-endpoint", "error", path, i,
                             f"`{method} {rpath}` is not served; the route now uses `{new_m}`",
                             f"Methods for {rpath}: {', '.join(sorted(same_path))}",
                             Fix("replace", path, i, matched, new_m + matched[len(method):]))
                    continue
                cands = {p: r for (m, p), r in known.items() if m == method}
                best = closest(key[1], cands, 0.7)
                if best:
                    self.mentioned_routes.add((method, best))
                    new_path = cands[best].path
                    self.add("stale-endpoint", "error", path, i,
                             f"`{method} {rpath}` no longer exists; closest route is `{method} {new_path}`",
                             f"Handler: {cands[best].handler or '—'} in {cands[best].file}",
                             Fix("replace", path, i, matched, matched[:len(matched) - len(rpath)] + new_path), "medium")
                else:
                    self.add("stale-endpoint", "error", path, i, f"`{method} {rpath}` is documented but not implemented",
                             "No matching route in the code", None, "low")

    # ---- env vars / config keys
    def check_env(self, path: str, parts: DocParts) -> None:
        tokens: list[tuple[int, str]] = [(ln, s) for ln, s in parts.inline if re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+", s)]
        for lang, start, lines in parts.blocks:
            if lang in CMD_LANGS | {"env", "dotenv", "ini"}:
                for j, ln in enumerate(lines):
                    m = re.match(r"^\s*(?:export\s+|set\s+|\$env:)?([A-Z][A-Z0-9]*_[A-Z0-9_]+)=", ln)
                    if m:
                        tokens.append((start + j, m.group(1)))
        if not self.idx.env_vars and not tokens:
            return
        for ln, name in tokens:
            self.checked += 1
            if name in self.idx.env_vars or re.search(rf"\b{name}\b", self.idx.code_text):
                continue
            best = closest(name, self.idx.env_vars, 0.5)
            if best:
                self.env_suggested.add(best)
            self.add("stale-config", "warning", path, ln,
                     f"Config/env var `{name}` is not read anywhere in the code" + (f"; did you mean `{best}`?" if best else ""),
                     f"Env vars used in code: {', '.join(sorted(self.idx.env_vars)) or '—'}",
                     Fix("replace", path, ln, name, best) if best else None, "high" if best else "low")

    # ---- commands
    def check_commands(self, path: str, parts: DocParts) -> None:
        candidates: list[tuple[int, str]] = [(ln, s) for ln, s in parts.inline]
        for lang, start, lines in parts.blocks:
            if lang in CMD_LANGS:
                candidates += [(start + j, ln) for j, ln in enumerate(lines)]
        py_files = [p for p in self.idx.paths if p.endswith(".py")]
        js_files = [p for p in self.idx.paths if p.endswith((".js", ".mjs", ".ts"))]
        for ln, text in candidates:
            for script in re.findall(r"\b(?:npm|pnpm)\s+run\s+([\w:.-]+)", text):
                if not self.idx.scripts:
                    continue
                self.checked += 1
                if script not in self.idx.scripts:
                    best = closest(script, self.idx.scripts, 0.5)
                    self.add("stale-command", "warning", path, ln, f"npm script `{script}` does not exist in package.json",
                             f"Available scripts: {', '.join(sorted(self.idx.scripts))}",
                             Fix("replace", path, ln, f"run {script}", f"run {best}") if best else None,
                             "high" if best else "low")
            for target in re.findall(r"\bpython3?\s+(?:-m\s+)?([\w/.-]+\.py)\b", text):
                self.checked += 1
                if target not in self.idx.paths and py_files:
                    best = self._closest_path(target, py_files)
                    self.add("stale-command", "warning", path, ln, f"`python {target}`: file `{target}` does not exist",
                             "", Fix("replace", path, ln, target, best) if best else None, "medium" if best else "low")
            for target in re.findall(r"\bnode\s+([\w/.-]+\.(?:js|mjs|cjs))\b", text):
                self.checked += 1
                if target not in self.idx.paths and js_files:
                    best = self._closest_path(target, js_files)
                    self.add("stale-command", "warning", path, ln, f"`node {target}`: file `{target}` does not exist",
                             "", Fix("replace", path, ln, target, best) if best else None, "medium" if best else "low")
            for mod, attr in re.findall(r"\b(?:uvicorn|gunicorn|hypercorn)\s+(?:[^\s]+\s+)*?([\w.]+):(\w+)", text):
                self.checked += 1
                if mod not in self.idx.py_modules:
                    apps = [k for k, m in self.idx.py_modules.items()
                            if re.search(rf"^{attr}\s*=\s*\w*(FastAPI|Flask|Starlette|Quart)\(", self.files.get(m.path, ""), re.M)]
                    best = closest(mod, apps, 0.3) or (apps[0] if len(apps) == 1 else None)
                    self.add("stale-command", "error", path, ln, f"ASGI/WSGI module `{mod}` does not exist",
                             f"App objects found in: {', '.join(apps) or '—'}",
                             Fix("replace", path, ln, f"{mod}:{attr}", f"{best}:{attr}") if best else None,
                             "high" if best else "low")
            for req in re.findall(r"\bpip3?\s+install\s+-r\s+([\w/.-]+)", text):
                self.checked += 1
                if req not in self.idx.paths:
                    best = self._closest_path(req, [p for p in self.idx.paths if "requirements" in p])
                    self.add("stale-command", "warning", path, ln, f"Requirements file `{req}` does not exist",
                             "", Fix("replace", path, ln, req, best) if best else None, "high" if best else "low")

    @staticmethod
    def _closest_path(target: str, paths: list[str]) -> str | None:
        base = posixpath.basename(target)
        same = [p for p in paths if posixpath.basename(p) == base]
        if len(same) == 1:
            return same[0]
        return closest(target, paths, 0.5)

    # ---- relative links
    def check_links(self, path: str, parts: DocParts) -> None:
        doc_dir = posixpath.dirname(path)
        dirs = {posixpath.dirname(p) for p in self.idx.paths}
        all_dirs = set()
        for d in dirs:
            while d:
                all_dirs.add(d)
                d = posixpath.dirname(d)
        for ln, target in parts.links:
            if re.match(r"^[a-z][a-z0-9+.-]*:|^#|^//", target, re.I):
                continue
            clean = target.split("#")[0].split("?")[0]
            if not clean:
                continue
            resolved = posixpath.normpath(posixpath.join(doc_dir, clean)).lstrip("./") if not clean.startswith("/") else clean.lstrip("/")
            self.checked += 1
            if resolved in self.idx.paths or resolved.rstrip("/") in all_dirs:
                continue
            if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|pdf)$", resolved, re.I):
                continue  # binary assets are not ingested
            best = self._closest_path(resolved, sorted(self.idx.paths))
            fix = None
            if best:
                rel = posixpath.relpath(best, doc_dir or ".")
                fix = Fix("replace", path, ln, f"({target}", f"({rel}{target[len(clean):]}")
            self.add("broken-link", "warning", path, ln, f"Link target `{target}` does not exist"
                     + (f"; moved to `{best}`?" if best else ""), "", fix, "high" if best else "low")

    # ---- python docstrings vs signatures
    def check_docstrings(self) -> None:
        for p, text in self.files.items():
            if not p.endswith(".py"):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            src = text.split("\n")
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not ast.get_docstring(node):
                    continue
                ds = node.body[0]
                self._check_docstring(p, src, node, ds.lineno, ds.end_lineno or ds.lineno)

    def _check_docstring(self, path: str, src: list[str], node, start: int, end: int) -> None:
        params = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
        if node.args.vararg:
            params.append(node.args.vararg.arg)
        if node.args.kwarg:
            params.append(node.args.kwarg.arg)
        for i in range(start - 1, end):
            header = src[i].strip().strip('"\'')
            if header not in ("Args:", "Arguments:", "Parameters:", "Params:"):
                continue
            indent = len(src[i]) - len(src[i].lstrip())
            entries: list[tuple[str, list[str]]] = []
            j = i + 1
            while j < end:
                line = src[j]
                stripped = line.strip().rstrip('"\'').strip() if j == end - 1 else line.strip()
                cur = len(line) - len(line.lstrip())
                if not line.strip() or cur <= indent or (j == end - 1 and not stripped):
                    break
                m = re.match(r"^\*{0,2}(\w+)\s*(\([^)]*\))?\s*:", stripped)
                if m and (not entries or cur <= len(entries[-1][1][0]) - len(entries[-1][1][0].lstrip())):
                    entries.append((m.group(1), [line]))
                elif entries:
                    entries[-1][1].append(line)
                else:
                    break
                j += 1
            if not entries:
                return
            documented = [e[0] for e in entries]
            self.checked += 1
            if documented == params:
                return
            stale = [d for d in documented if d not in params]
            missing = [p for p in params if p not in documented]
            if not stale and not missing:
                return  # only order differs; ignore
            entry_indent = re.match(r"\s*", entries[0][1][0]).group(0)
            by_name = dict(entries)
            renamed = dict(zip(stale, missing)) if len(stale) == len(missing) else {}
            new_lines: list[str] = []
            for prm in params:
                if prm in by_name:
                    new_lines += by_name[prm]
                else:
                    old = next((o for o, n in renamed.items() if n == prm), None)
                    if old:
                        new_lines += [by_name[old][0].replace(old, prm, 1)] + by_name[old][1:]
                    else:
                        new_lines.append(f"{entry_indent}{prm}: TODO: describe.")
            first, last = i + 2, j
            old_text = "\n".join(src[first - 1:last])
            detail = "; ".join(x for x in (f"stale: {', '.join(stale)}" if stale else "",
                                           f"undocumented: {', '.join(missing)}" if missing else "") if x)
            self.add("docstring-drift", "warning", path, first,
                     f"Docstring of `{node.name}()` does not match its parameters ({detail})",
                     f"Signature params: {', '.join(params) or '—'}",
                     Fix("replace_range", path, first, old_text, "\n".join(new_lines), last))
            return

    # ---- coverage: undocumented endpoints & env vars (aggregated)
    def check_coverage(self) -> None:
        docs = [p for p in self.files if is_doc(p)]
        if not docs:
            return
        primary = next((p for p in sorted(docs, key=lambda d: d.count("/")) if posixpath.basename(p).lower().startswith("readme")), docs[0])
        missing_routes = []
        seen = set()
        for r in self.idx.routes:
            for m in r.method.replace(" ", "").split(","):
                key = (m, norm_route(r.path))
                if key in self.mentioned_routes or key in seen:
                    continue
                seen.add(key)
                missing_routes.append((m, r))
        if missing_routes:
            target, line, style = self._find_section(docs, primary, r"api|endpoint|route", r"\|\s*method")
            rows = [(f"| `{m}` | `{r.path}` | {r.handler or 'TODO'} |" if style == "table"
                     else f"- `{m} {r.path}` — TODO: describe (`{r.handler}` in `{r.file}`)") for m, r in missing_routes]
            text = "\n".join(rows) if line != -1 else "\n## API Endpoints\n\n" + "\n".join(rows)
            self.add("undocumented-endpoint", "warning", target, max(line, 1),
                     f"{len(missing_routes)} endpoint(s) exist in code but are not documented",
                     ", ".join(f"{m} {r.path}" for m, r in missing_routes), Fix("insert", target, line, "", text), "medium")
        missing_env = [v for v in sorted(self.idx.env_vars)
                       if v not in self.env_suggested and not re.search(rf"\b{v}\b", self.doc_text)]
        if missing_env:
            target, line, style = self._find_section(docs, primary, r"config|environment|env|settings", r"\|\s*(variable|name|env)")
            rows = [(f"| `{v}` | TODO: describe |" if style == "table" else f"- `{v}` — TODO: describe (read in `{self.idx.env_vars[v]}`)")
                    for v in missing_env]
            text = "\n".join(rows) if line != -1 else "\n## Configuration\n\n" + "\n".join(rows)
            self.add("undocumented-config", "warning", target, max(line, 1),
                     f"{len(missing_env)} environment variable(s) are read by the code but not documented",
                     ", ".join(missing_env), Fix("insert", target, line, "", text), "medium")

    def _find_section(self, docs: list[str], primary: str, heading_rx: str, table_rx: str) -> tuple[str, int, str]:
        fallback = None
        for d in [primary] + [x for x in docs if x != primary]:
            lines = self.files[d].split("\n")
            for i, line in enumerate(lines):
                if re.match(r"^#{1,6}\s", line) and re.search(heading_rx, line, re.I):
                    j = i + 1
                    while j < len(lines) and not re.match(r"^#{1,6}\s", lines[j]):
                        j += 1
                    section = lines[i + 1:j]
                    table_end = None
                    for k, sl in enumerate(section):
                        if re.match(rf"^\s*{table_rx}", sl, re.I):
                            table_end = k
                            while table_end + 1 < len(section) and section[table_end + 1].strip().startswith("|"):
                                table_end += 1
                            break
                    if table_end is not None:
                        return d, i + 1 + table_end + 1, "table"
                    if fallback is None and any(sl.strip() for sl in section):
                        last = j
                        while last > i + 1 and not lines[last - 1].strip():
                            last -= 1
                        fallback = (d, last, "list")
        return fallback or (primary, -1, "list")

    def run(self) -> None:
        for path, text in self.files.items():
            if not is_doc(path):
                continue
            parts = parse_markdown(text)
            for ln, span in parts.inline:
                self.check_inline_ref(path, ln, span)
            for lang, start, lines in parts.blocks:
                if lang in ("python", "py", "python3"):
                    self.check_python_block(path, start, lines)
            self.check_endpoints(path, parts)
            self.check_env(path, parts)
            self.check_commands(path, parts)
            self.check_links(path, parts)
        self.check_docstrings()
        self.check_coverage()


# ---------------------------------------------------------------- scan

def scan(files: dict[str, str]) -> dict:
    c = Checker(files)
    c.run()
    issues: dict[str, Issue] = {}
    for i in c.issues:
        issues.setdefault(i.id, i)
    issue_list = sorted(issues.values(), key=lambda i: (["error", "warning", "info"].index(i.severity), i.file, i.line))
    idx = c.idx
    public = [s for m in idx.modules for s in m.symbols if not s.name.startswith("_")]
    public += [ch for s in public if s.kind == "class" for ch in s.children if not ch.name.startswith("_")]
    documented = [s for s in public if s.doc or re.search(rf"\b{re.escape(s.name)}\b", c.doc_text)]
    route_keys = {(m, norm_route(r.path)) for r in idx.routes for m in r.method.replace(" ", "").split(",")}
    stats = {
        "doc_files": sum(1 for p in files if is_doc(p)),
        "code_files": len(idx.modules),
        "checks": c.checked,
        "public_symbols": len(public),
        "documented_symbols": len(documented),
        "routes": len(route_keys),
        "documented_routes": len(route_keys & c.mentioned_routes),
        "env_vars": len(idx.env_vars),
        "documented_env_vars": sum(1 for v in idx.env_vars if re.search(rf"\b{v}\b", c.doc_text)),
        "errors": sum(1 for i in issue_list if i.severity == "error"),
        "warnings": sum(1 for i in issue_list if i.severity == "warning"),
        "infos": sum(1 for i in issue_list if i.severity == "info"),
        "auto_fixable": sum(1 for i in issue_list if i.fix),
    }
    return {"score": health_score(issue_list, stats), "stats": stats, "issues": [i.to_dict() for i in issue_list]}


def health_score(issues: list[Issue], stats: dict) -> int:
    if not stats["doc_files"]:
        return 0
    penalty = sum(SEVERITY_WEIGHT[i.severity] for i in issues)
    base = max(stats["checks"], 1) + stats["public_symbols"] * 0.25 + 5
    drift = max(0.0, 1 - penalty / base)
    coverage_parts = [(stats["documented_symbols"], stats["public_symbols"]), (stats["documented_routes"], stats["routes"]),
                      (stats["documented_env_vars"], stats["env_vars"])]
    ratios = [a / b for a, b in coverage_parts if b]
    coverage = sum(ratios) / len(ratios) if ratios else 1.0
    return round(100 * (0.75 * drift + 0.25 * coverage))


# ---------------------------------------------------------------- healing

def apply_fixes(files: dict[str, str], issues: list[dict]) -> tuple[dict[str, str], list[str], list[str]]:
    """Apply the fixes of the given issues. Returns (new_files, applied_ids, skipped_ids)."""
    out = dict(files)
    applied, skipped = [], []
    by_file: dict[str, list[tuple[int, dict]]] = {}
    for order, issue in enumerate(issues):
        fix = issue.get("fix")
        if not fix or fix["file"] not in out:
            skipped.append(issue["id"])
            continue
        by_file.setdefault(fix["file"], []).append((order, issue))
    for path, items in by_file.items():
        lines = out[path].split("\n")

        def pos(item):
            f = item[1]["fix"]
            return (len(lines) + 1 if f["line"] == -1 else f["line"], item[0])

        touched_ranges: list[tuple[int, int]] = []
        for _, issue in sorted(items, key=pos, reverse=True):
            f = issue["fix"]
            kind, ln = f["kind"], f["line"]
            ok = False
            if kind == "replace" and 1 <= ln <= len(lines) and f["old"] in lines[ln - 1]:
                if re.fullmatch(r"\w+", f["old"]):
                    new_line = re.sub(rf"\b{re.escape(f['old'])}\b", f["new"], lines[ln - 1], count=1)
                else:
                    new_line = lines[ln - 1].replace(f["old"], f["new"], 1)
                ok = new_line != lines[ln - 1]
                lines[ln - 1] = new_line
            elif kind == "replace_range":
                s, e = ln, f["end_line"]
                overlap = any(not (e < a or s > b) for a, b in touched_ranges)
                if not overlap and "\n".join(lines[s - 1:e]) == f["old"]:
                    lines[s - 1:e] = f["new"].split("\n")
                    touched_ranges.append((s, e))
                    ok = True
            elif kind == "insert":
                at = len(lines) if ln == -1 else ln
                if 0 <= at <= len(lines):
                    new = f["new"].split("\n")
                    if ln == -1 and lines and lines[-1] == "":
                        lines[len(lines) - 1:len(lines) - 1] = new
                    else:
                        lines[at:at] = new
                    ok = True
            (applied if ok else skipped).append(issue["id"])
        out[path] = "\n".join(lines)
    return out, applied, skipped


def unified_diff(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    result = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path, ""), new.get(path, "")
        if a == b:
            continue
        diff = "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                                            fromfile=f"a/{path}", tofile=f"b/{path}"))
        result.append({"path": path, "diff": diff,
                       "added": sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")),
                       "removed": sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))})
    return result


def code_context(files: dict[str, str], max_chars: int = 40_000) -> str:
    """Compact summary of the code's public surface for LLM prompts."""
    idx = build_index(files)
    out = []
    for m in idx.modules:
        sigs = []
        for s in m.symbols:
            sigs.append(f"  {s.signature}")
            sigs += [f"    {c.signature}" for c in s.children if not c.name.startswith("_") or c.name == "__init__"]
        if sigs:
            out.append(f"{m.path}:\n" + "\n".join(sigs))
    if idx.routes:
        out.append("HTTP routes:\n" + "\n".join(f"  {r.method} {r.path} -> {r.handler} ({r.file})" for r in idx.routes))
    if idx.env_vars:
        out.append("Environment variables read: " + ", ".join(sorted(idx.env_vars)))
    if idx.scripts:
        out.append("npm scripts: " + ", ".join(sorted(idx.scripts)))
    out.append("Files: " + ", ".join(sorted(files)))
    return "\n\n".join(out)[:max_chars]
