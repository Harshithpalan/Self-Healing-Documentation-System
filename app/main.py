"""FastAPI entrypoint for the Self-Healing Documentation System."""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from .ingest import MAX_ZIP_BYTES, Collector, IngestError, fetch_github
from .llm import PROVIDERS, LLMError, LLMSettings, server_has_key, stream_completion

BASE = Path(__file__).resolve().parent
STATIC = BASE.parent / "static"
SAMPLE = BASE / "sample" / "inventory"

app = FastAPI(title="Self-Healing Docs", version="1.0.0")

RULES = {
    "missing-symbol": "Function, class or method referenced in docs no longer exists",
    "signature-drift": "Documented arguments differ from the real signature",
    "stale-endpoint": "Documented HTTP route or method is no longer served",
    "stale-config": "Documented environment variable is not read by the code",
    "stale-command": "Documented command points at a missing script, file or module",
    "broken-link": "Relative link points at a file that does not exist",
    "docstring-drift": "Docstring arguments do not match the function parameters",
    "undocumented-endpoint": "Route exists in code but is missing from the docs",
    "undocumented-config": "Environment variable is read but never documented",
}


class FilesBody(BaseModel):
    files: dict[str, str]


class FixBody(FilesBody):
    issues: list[dict] = Field(default_factory=list)


class LLMBody(BaseModel):
    provider: str = "openai"
    model: str = ""
    api_key: str = ""
    base_url: str = ""


class AIFixBody(FilesBody):
    issue: dict
    llm: LLMBody = Field(default_factory=LLMBody)


class ExportBody(FilesBody):
    name: str = "healed-docs"


def _check_files(files: dict[str, str]) -> None:
    if not files:
        raise HTTPException(400, "No files provided")
    if sum(len(v) for v in files.values()) > 5_000_000:
        raise HTTPException(413, "Project is too large (5 MB limit)")


@app.get("/api/config")
def config() -> dict:
    return {
        "providers": {k: {**v, "server_key": server_has_key(k)} for k, v in PROVIDERS.items()},
        "rules": RULES,
    }


@app.get("/api/sample")
def sample() -> dict:
    files = {p.relative_to(SAMPLE).as_posix(): p.read_text() for p in sorted(SAMPLE.rglob("*")) if p.is_file()}
    return {"name": "inventory-service (sample)", "files": files}


@app.post("/api/ingest")
async def ingest(files: list[UploadFile] = File(default=[]), github_url: str = Form(""),
                 github_token: str = Form("")) -> dict:
    col = Collector()
    name = "project"
    try:
        if github_url.strip():
            name = await fetch_github(github_url.strip(), col, github_token or os.getenv("GITHUB_TOKEN"))
        for up in files:
            data = await up.read()
            if (up.filename or "").lower().endswith(".zip"):
                if len(data) > MAX_ZIP_BYTES:
                    raise IngestError("Zip archive is too large")
                col.add_zip(data, strip_root=True)
                name = Path(up.filename).stem
            else:
                col.add(up.filename or "file.txt", data)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not col.files:
        raise HTTPException(400, "No supported source or documentation files found")
    result = {f.path: f.content for f in col.files}
    return {"name": name, "files": result, "skipped": col.skipped,
            "doc_files": sum(1 for p in result if engine.is_doc(p))}


@app.post("/api/scan")
def scan(body: FilesBody) -> dict:
    _check_files(body.files)
    return engine.scan(body.files)


@app.post("/api/preview")
def preview(body: FixBody) -> dict:
    _check_files(body.files)
    new, applied, skipped = engine.apply_fixes(body.files, body.issues)
    return {"diff": engine.unified_diff(body.files, new), "applied": applied, "skipped": skipped}


@app.post("/api/heal")
def heal(body: FixBody) -> dict:
    _check_files(body.files)
    new, applied, skipped = engine.apply_fixes(body.files, body.issues)
    return {"files": new, "diff": engine.unified_diff(body.files, new), "applied": applied,
            "skipped": skipped, "report": engine.scan(new)}


AI_SYSTEM = (
    "You repair technical documentation so it matches the source code exactly. "
    "You receive a documentation excerpt with line numbers, a detected problem and a summary of the real code. "
    "Return ONLY the corrected excerpt (same lines, without line numbers) inside a single ```text fenced block. "
    "Change as little as possible, keep formatting, never invent APIs that are not in the code summary."
)


@app.post("/api/ai-fix")
async def ai_fix(body: AIFixBody) -> dict:
    _check_files(body.files)
    issue = body.issue
    path = issue.get("file", "")
    if path not in body.files:
        raise HTTPException(400, "Issue file not found in project")
    lines = body.files[path].split("\n")
    line = max(1, min(int(issue.get("line", 1)), len(lines)))
    start, end = max(1, line - 6), min(len(lines), line + 6)
    excerpt = "\n".join(lines[start - 1:end])
    numbered = "\n".join(f"{n:>4}| {t}" for n, t in enumerate(lines[start - 1:end], start))
    user = (f"File: {path}\nProblem (line {line}, rule {issue.get('rule')}): {issue.get('message')}\n\n"
            f"Excerpt:\n{numbered}\n\nCode summary:\n{engine.code_context(body.files)}")
    settings = LLMSettings(**body.llm.model_dump(), temperature=0.1, max_tokens=2048)
    try:
        out = "".join([chunk async for chunk in stream_completion(settings, AI_SYSTEM, user)])
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    m = re.search(r"```[\w-]*\n(.*?)```", out, re.S)
    new = (m.group(1) if m else out).rstrip("\n")
    if not new.strip() or new == excerpt:
        raise HTTPException(422, "The AI did not propose a change")
    fix = {"kind": "replace_range", "file": path, "line": start, "end_line": end, "old": excerpt, "new": new}
    return {"fix": fix, "diff": engine.unified_diff({path: body.files[path]},
                                                    engine.apply_fixes({path: body.files[path]},
                                                                       [{"id": "ai", "fix": fix}])[0])}


@app.post("/api/export")
def export(body: ExportBody) -> StreamingResponse:
    _check_files(body.files)
    buf = io.BytesIO()
    safe = re.sub(r"[^\w.-]+", "-", body.name).strip("-") or "healed-docs"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in body.files.items():
            zf.writestr(f"{safe}/{path.lstrip('/')}", content)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
