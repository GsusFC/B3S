"""B3S lab web app: submit a brand URL, watch the scoring run, read the evidence."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.report_store import list_reports, load_report
from web.scan_runner import scan_status, start_scan

app = FastAPI(title="B3S — Brand Evidence Lab")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
def _load_env() -> None:
    env_file = Path(".env")
    if env_file.is_file():
        from scripts.sv9_flow_shadow_run import _load_env_file

        _load_env_file(str(env_file))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request,
        "index.html.j2",
        {"reports": list_reports(), "error": error},
    )


@app.post("/scan")
def create_scan(url: str = Form(...), brand_name: str = Form("")):
    try:
        scan_id = start_scan(url, brand_name)
    except ValueError as exc:
        return RedirectResponse(f"/?error={exc}", status_code=303)
    return RedirectResponse(f"/scan/{scan_id}", status_code=303)


@app.get("/scan/{scan_id}")
def scan_view(request: Request, scan_id: str):
    status = scan_status(scan_id)
    if status is None:
        if load_report(scan_id) is not None:
            return RedirectResponse(f"/report/{scan_id}", status_code=303)
        return RedirectResponse("/?error=Unknown scan", status_code=303)
    return templates.TemplateResponse(request, "scan.html.j2", {"scan": status})


@app.get("/api/scan/{scan_id}")
def scan_api(scan_id: str):
    status = scan_status(scan_id)
    if status is None:
        if load_report(scan_id) is not None:
            return JSONResponse({"state": "done", "id": scan_id})
        return JSONResponse({"state": "unknown", "id": scan_id}, status_code=404)
    status.pop("raw", None)
    return JSONResponse(status)


@app.get("/report/{scan_id}")
def report_view(request: Request, scan_id: str):
    report = load_report(scan_id)
    if report is None:
        return RedirectResponse("/?error=Report not found", status_code=303)
    return templates.TemplateResponse(request, "report.html.j2", {"report": report})
