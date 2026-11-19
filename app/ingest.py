"""Collect source files from pasted text, uploads, zip archives and GitHub repositories."""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, asdict

import httpx

MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 3_000_000
MAX_FILES = 400
MAX_ZIP_BYTES = 50_000_000

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
    ".hpp": "cpp", ".swift": "swift", ".scala": "scala", ".dart": "dart", ".lua": "lua", ".r": "r",
    ".sh": "bash", ".bash": "bash", ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
    ".vue": "vue", ".svelte": "svelte", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".markdown": "markdown", ".mdx": "markdown", ".txt": "text", ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".gradle": "groovy",
    ".graphql": "graphql", ".proto": "protobuf", ".tf": "hcl", ".env.example": "dotenv",
}
SPECIAL_FILES = {"dockerfile": "dockerfile", "makefile": "makefile", "procfile": "text", ".env.example": "dotenv"}
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "out", ".next", ".nuxt", "venv", ".venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", "target", "vendor", ".idea", ".vscode", "coverage", ".gradle", "bin", "obj",
}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "composer.lock"}
PRIORITY_NAMES = (
    "readme", "package.json", "pyproject.toml", "requirements.txt", "setup.py", "go.mod", "cargo.toml",
    "pom.xml", "build.gradle", "dockerfile", "docker-compose", "main.", "app.", "index.", "server.", "cli.",
)


class IngestError(ValueError):
    pass


@dataclass
class SourceFile:
    path: str
    content: str
    language: str

    def to_dict(self) -> dict:
        return asdict(self) | {"size": len(self.content)}


def detect_language(path: str) -> str | None:
    name = os.path.basename(path).lower()
    if name in SPECIAL_FILES:
        return SPECIAL_FILES[name]
    return LANGUAGES.get(os.path.splitext(name)[1])


def is_wanted(path: str) -> bool:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts or any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    if parts[-1].lower() in SKIP_FILES or parts[-1].endswith((".min.js", ".min.css", ".map")):
        return False
    return detect_language(path) is not None


def decode(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


class Collector:
    def __init__(self) -> None:
        self.files: list[SourceFile] = []
        self.total = 0
        self.skipped = 0

    def add(self, path: str, data: bytes) -> None:
        path = path.replace("\\", "/").lstrip("/")
        if not is_wanted(path) or len(data) > MAX_FILE_BYTES:
            self.skipped += 1
            return
        if len(self.files) >= MAX_FILES or self.total + len(data) > MAX_TOTAL_BYTES:
            self.skipped += 1
            return
        text = decode(data)
        if text is None or not text.strip():
            self.skipped += 1
            return
        self.total += len(data)
        self.files.append(SourceFile(path, text, detect_language(path) or "text"))

    def add_zip(self, data: bytes, strip_root: bool = False) -> None:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise IngestError("Invalid zip archive") from exc
        names = [i for i in zf.infolist() if not i.is_dir()]
        root = ""
        if strip_root:
            tops = {n.filename.split("/", 1)[0] for n in names}
            if len(tops) == 1:
                root = tops.pop() + "/"
        for info in names:
            if info.file_size > MAX_FILE_BYTES:
                self.skipped += 1
                continue
            name = info.filename[len(root):] if root and info.filename.startswith(root) else info.filename
            self.add(name, zf.read(info))


GITHUB_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[^/]+)(?:/(?P<sub>.*))?)?/?$"
)


def parse_github_url(url: str) -> tuple[str, str, str, str]:
    m = GITHUB_RE.match(url.strip())
    if not m:
        raise IngestError("Not a valid GitHub repository URL (expected https://github.com/owner/repo)")
    return m["owner"], m["repo"], m["ref"] or "HEAD", (m["sub"] or "").strip("/")


async def fetch_github(url: str, collector: Collector, token: str | None = None) -> str:
    owner, repo, ref, sub = parse_github_url(url)
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", zip_url, headers=headers) as resp:
            if resp.status_code == 404:
                raise IngestError("Repository or branch not found (private repos are not supported)")
            if resp.status_code >= 400:
                raise IngestError(f"GitHub returned HTTP {resp.status_code}")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_ZIP_BYTES:
                    raise IngestError("Repository archive is too large (max 50 MB)")
    sub_collector = Collector()
    sub_collector.add_zip(bytes(buf), strip_root=True)
    for f in sub_collector.files:
        if sub and not f.path.startswith(sub + "/"):
            continue
        collector.add(f.path[len(sub) + 1:] if sub else f.path, f.content.encode("utf-8"))
    collector.skipped += sub_collector.skipped
    return repo


def priority(path: str) -> tuple[int, int, str]:
    name = os.path.basename(path).lower()
    rank = next((i for i, p in enumerate(PRIORITY_NAMES) if name.startswith(p)), len(PRIORITY_NAMES))
    is_test = 1 if re.search(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec)\.", path.lower()) else 0
    return (is_test, rank, path.count("/"), path)


def file_tree(paths: list[str]) -> str:
    tree: dict = {}
    for p in sorted(paths):
        node = tree
        for part in p.split("/"):
            node = node.setdefault(part, {})

    lines: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        items = sorted(node.items(), key=lambda kv: (not kv[1], kv[0]))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{name}{'/' if child else ''}")
            walk(child, prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines)


def build_bundle(files: list[SourceFile], max_chars: int = 150_000) -> tuple[str, list[str]]:
    """Concatenate files into a prompt-friendly bundle. Returns (bundle, truncated_paths)."""
    ordered = sorted(files, key=lambda f: priority(f.path))
    parts = [f"### File tree\n```\n{file_tree([f.path for f in files])}\n```"]
    used = len(parts[0])
    truncated: list[str] = []
    for f in ordered:
        header = f"\n### {f.path}\n```{f.language}\n"
        body = f.content
        remaining = max_chars - used - len(header) - 10
        if remaining <= 200:
            truncated.append(f.path)
            continue
        if len(body) > remaining:
            body = body[:remaining] + "\n... [truncated]"
            truncated.append(f.path)
        parts.append(header + body + "\n```")
        used += len(header) + len(body) + 4
    return "\n".join(parts), truncated
