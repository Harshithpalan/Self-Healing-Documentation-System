# Self-Healing Docs

A web app that keeps documentation in sync with the code. It scans a project's source code and its Markdown docs together, finds every place where the docs have drifted from reality, scores documentation health, and proposes fixes you can preview and apply in one click.

It works fully offline with a built-in rule engine. An AI provider (OpenAI-compatible, Anthropic Claude or Google Gemini) is optional and adds an **AI fix** button for issues the rules can't repair.

## What it detects

| Rule | Example | Auto-fix |
|---|---|---|
| `missing-symbol` | Docs call `service.delete_item()` but the method is now `remove_item()` | Rename to the closest match (synonym-aware: delete→remove, create→add, …) |
| `signature-drift` | `add_item(name, qty=5)` but the parameter is now `quantity` | Rewrite the call / argument names |
| `stale-endpoint` | Docs list `PUT /items/{id}`, the code serves `PATCH` | Update method or path |
| `stale-config` | Docs say `export DB_URL=…`, code reads `DATABASE_URL` | Rename the variable |
| `stale-command` | `uvicorn app.server:app`, `npm run <missing>`, `python missing.py` | Point at the real module/script |
| `broken-link` | `[guide](CONTRIBUTING.md)` after the file moved to `docs/` | Re-link to the moved file |
| `docstring-drift` | Python docstring `Args:` lists `qty` / `tax_rate` that no longer exist | Rewrite the `Args:` block |
| `undocumented-endpoint` | Route in code, missing from the API docs | Insert table rows / list items |
| `undocumented-config` | `os.getenv("API_KEY")` never mentioned in docs | Insert config table rows |

Code is analysed with the Python AST and regex parsers for JavaScript/TypeScript, Go, Java, Rust, Ruby, PHP, C#, Kotlin and C/C++. Routes are detected for FastAPI/Flask, Express, Spring, Go and more. Python code blocks inside docs are parsed and checked (imports, method names, keyword arguments, argument counts).

Each issue carries a severity, a confidence level, the evidence from the code and a machine-applicable fix. **Auto-heal** only applies high-confidence fixes; medium-confidence fixes and AI fixes must be selected manually. Fixes are re-validated against the current file before they're applied, so stale fixes are skipped instead of corrupting files.

## Health score

0–100, weighted 75% drift (errors 1.0, warnings 0.5, info 0.2 relative to the number of references checked) and 25% coverage (share of public symbols, routes and env vars that are documented). Projects without docs score 0.

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000 and click **Load sample project (with drift)** to see it in action, or upload files / a `.zip`, or import a public GitHub repository.

### Docker

```bash
docker build -t self-healing-docs .
docker run -p 8000:8000 self-healing-docs
```

## Workflow

1. Load a project (sample, upload, zip or GitHub URL).
2. Scan: the dashboard shows the health score, coverage and every drift issue with evidence.
3. Review: filter by severity/rule, pick fixes, click **Preview fixes** to see a unified diff.
4. Heal: apply the selection, or **Auto-heal** to apply every high-confidence fix and rescan automatically.
5. Edit any file in the **Files** tab and rescan; the healing timeline tracks the score over time.
6. Download the healed project as a `.zip`.

## AI fixes (optional)

Open **AI settings** and choose a provider, model and API key. Keys are stored in your browser only and sent with each AI request; they are never stored on the server. Server-side defaults can be set with environment variables (see `.env.example`).

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config` | Providers and rule descriptions |
| `GET` | `/api/sample` | Sample project with intentional drift |
| `POST` | `/api/ingest` | Multipart `files` (files or `.zip`) and/or `github_url` → `{files}` |
| `POST` | `/api/scan` | `{files}` → `{score, stats, issues}` |
| `POST` | `/api/preview` | `{files, issues}` → unified diff of the fixes |
| `POST` | `/api/heal` | `{files, issues}` → healed files, diff and a fresh report |
| `POST` | `/api/ai-fix` | `{files, issue, llm}` → AI-proposed fix |
| `POST` | `/api/export` | `{files, name}` → `.zip` download |

## Project structure

```
app/
  main.py       FastAPI app and API routes
  engine.py     Drift rules, health score, fix application, diffs
  analyzer.py   Static code analysis (symbols, signatures, routes)
  ingest.py     File, zip and GitHub ingestion
  llm.py        Streaming clients for OpenAI-compatible, Anthropic and Gemini APIs
  sample/       Sample project with intentional documentation drift
static/         Frontend (vanilla HTML/CSS/JS)
tests/          pytest suite
```

## Tests

```bash
pytest
```
