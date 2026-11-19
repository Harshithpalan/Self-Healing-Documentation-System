import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import engine
from app.main import app

client = TestClient(app)
SAMPLE = Path(__file__).resolve().parent.parent / "app" / "sample" / "inventory"


def sample_files() -> dict[str, str]:
    return {p.relative_to(SAMPLE).as_posix(): p.read_text() for p in SAMPLE.rglob("*") if p.is_file()}


def rules(report):
    return {i["rule"] for i in report["issues"]}


def by_rule(report, rule):
    return [i for i in report["issues"] if i["rule"] == rule]


def test_sample_detects_every_rule():
    r = engine.scan(sample_files())
    assert {"missing-symbol", "signature-drift", "stale-endpoint", "stale-config", "stale-command",
            "broken-link", "docstring-drift", "undocumented-endpoint", "undocumented-config"} <= rules(r)
    assert r["score"] < 70


def test_rename_suggestions():
    r = engine.scan(sample_files())
    fixes = {(i["fix"]["old"], i["fix"]["new"]) for i in r["issues"] if i["fix"] and i["fix"]["kind"] == "replace"}
    assert (".delete_item(", ".remove_item(") in fixes
    assert ("qty=", "quantity=") in fixes
    assert ("DB_URL", "DATABASE_URL") in fixes
    assert ("app.server:app", "app.main:app") in fixes


def test_heal_all_restores_health():
    files = sample_files()
    r = engine.scan(files)
    new, applied, skipped = engine.apply_fixes(files, [i for i in r["issues"] if i["fix"]])
    assert not skipped and applied
    after = engine.scan(new)
    assert after["score"] >= 90
    assert {i["rule"] for i in after["issues"]} <= {"stale-command"}
    assert "PATCH` | `/items/{id}" in new["docs/api.md"]
    assert "qty (int)" not in new["app/service.py"]
    assert "quantity (int)" in new["app/service.py"]
    assert new["README.md"].count("DATABASE_URL") == 2
    assert "INVENTORY_API_KEY" in new["README.md"]
    compile(new["app/service.py"], "service.py", "exec")


def test_clean_project_has_no_issues():
    files = {
        "lib.py": 'import os\nTOKEN = os.getenv("API_TOKEN")\n\ndef greet(name, punct="!"):\n    """Say hi.\n\n    Args:\n        name: Who.\n        punct: End.\n    """\n    return name\n',
        "README.md": "# Lib\n\nCall `greet(name, punct)`. Set `API_TOKEN`.\n\n```python\nfrom lib import greet\ngreet(\"a\", punct=\"?\")\n```\n",
    }
    r = engine.scan(files)
    assert r["issues"] == []
    assert r["score"] >= 95


def test_no_docs_scores_zero():
    assert engine.scan({"a.py": "def f():\n    pass\n"})["score"] == 0


def test_stale_fix_is_skipped():
    files = sample_files()
    issue = next(i for i in engine.scan(files)["issues"] if i["rule"] == "stale-config")
    files[issue["file"]] = files[issue["file"]].replace("DB_URL", "SOMETHING")
    _, applied, skipped = engine.apply_fixes(files, [issue])
    assert skipped == [issue["id"]] and not applied


def test_api_flow():
    s = client.get("/api/sample").json()
    report = client.post("/api/scan", json={"files": s["files"]}).json()
    chosen = [i for i in report["issues"] if i["fix"] and i["confidence"] == "high"]
    prev = client.post("/api/preview", json={"files": s["files"], "issues": chosen}).json()
    assert prev["diff"] and len(prev["applied"]) == len(chosen)
    healed = client.post("/api/heal", json={"files": s["files"], "issues": chosen}).json()
    assert healed["report"]["score"] > report["score"]


def test_ingest_zip_and_export():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/README.md", "# Hi\n\nRun `missing_fn()`.\n")
        zf.writestr("proj/main.py", "def present():\n    pass\n")
        zf.writestr("proj/node_modules/x.js", "x")
    r = client.post("/api/ingest", files={"files": ("proj.zip", buf.getvalue(), "application/zip")}).json()
    assert set(r["files"]) == {"README.md", "main.py"} and r["doc_files"] == 1
    res = client.post("/api/export", json={"files": r["files"], "name": "proj"})
    assert res.status_code == 200
    assert set(zipfile.ZipFile(io.BytesIO(res.content)).namelist()) == {"proj/README.md", "proj/main.py"}


def test_ai_fix_requires_key():
    s = client.get("/api/sample").json()
    issue = engine.scan(s["files"])["issues"][0]
    res = client.post("/api/ai-fix", json={"files": s["files"], "issue": issue, "llm": {"provider": "openai"}})
    assert res.status_code == 400


def test_scan_validation_and_index():
    assert client.post("/api/scan", json={"files": {}}).status_code == 400
    assert "Self-Healing Docs" in client.get("/").text
    assert "missing-symbol" in client.get("/api/config").json()["rules"]


def test_ai_fix_with_mock_provider(monkeypatch):
    import app.main as main

    async def fake_stream(settings, system, user):
        assert "run.py" in user
        yield "```text\n"
        yield "You can also run `uvicorn app.main:app` for a quick local server.\n```"

    monkeypatch.setattr(main, "stream_completion", fake_stream)
    s = client.get("/api/sample").json()
    issue = next(i for i in engine.scan(s["files"])["issues"] if i["fix"] is None)
    res = client.post("/api/ai-fix", json={"files": s["files"], "issue": issue,
                                           "llm": {"provider": "openai", "api_key": "x"}}).json()
    fix = res["fix"]
    assert fix["kind"] == "replace_range" and res["diff"]
    new, applied, _ = engine.apply_fixes(s["files"], [{"id": "ai", "fix": fix}])
    assert applied == ["ai"] and "python run.py" not in new["README.md"]
