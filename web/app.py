"""B3S lab web app: submit a brand URL, watch the scoring run, read the evidence."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.report_store import domain_key, list_reports, list_reports_for_domain, load_report
from web.scan_runner import approve_degraded_scan, cancel_scan, scan_status, start_scan
from web.scoring_store import backfill_reports, dashboard as scoring_dashboard

app = FastAPI(title="B3S — Brand Evidence Lab")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _brand_profile(domain: str) -> dict:
    reports = list_reports_for_domain(domain)
    current = reports[0] if reports else None
    normalized_domain = domain_key(domain) or domain

    components = list((current or {}).get("components") or [])
    detected = [component for component in components if component.get("status") == "scored"]
    proof_urls: list[str] = []
    for component in components:
        block = component.get("block") or {}
        for ref in block.get("refs") or []:
            url = ref.get("url")
            if url and url not in proof_urls:
                proof_urls.append(url)

    purpose = next((component for component in components if component.get("key") == "core_purpose"), {})
    value = next((component for component in components if component.get("key") == "value_proposition"), {})
    return {
        "domain": normalized_domain,
        "display_name": (current or {}).get("brand_name") or normalized_domain,
        "url": (current or {}).get("url") or f"https://{normalized_domain}",
        "current": current,
        "reports": reports,
        "summary": purpose.get("resumen") or value.get("resumen") or "",
        "outcome": value.get("veredicto") or purpose.get("veredicto") or "",
        "proof_urls": proof_urls[:6],
        "detected_count": len(detected),
        "component_count": len(components),
        "not_detected": (current or {}).get("not_detected") or [],
    }


def _api_health(*, run_checks: bool = False) -> dict[str, Any]:
    from src import config

    services = [
        _service_row(
            key="firecrawl_scrape",
            label="Firecrawl scrape",
            env_var="FIRECRAWL_API_KEY",
            secret=config.FIRECRAWL_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_firecrawl_scrape,
        ),
        _service_row(
            key="firecrawl_screenshot",
            label="Firecrawl screenshot",
            env_var="FIRECRAWL_API_KEY",
            secret=config.FIRECRAWL_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_firecrawl_screenshot,
        ),
        _service_row(
            key="exa",
            label="Exa search",
            env_var="EXA_API_KEY",
            secret=config.EXA_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_exa,
        ),
        _service_row(
            key="searchapi",
            label="SearchAPI fallback",
            env_var="SEARCHAPI_API_KEY",
            secret=config.SEARCHAPI_API_KEY,
            enabled=config.BRAND3_SEARCHAPI_VERTICAL_FALLBACK_ENABLED,
            run_checks=run_checks,
            check_fn=_check_searchapi,
        ),
        _service_row(
            key="llm",
            label="LLM provider",
            env_var="BRAND3_LLM_API_KEY",
            secret=config.BRAND3_LLM_API_KEY,
            enabled=True,
            run_checks=run_checks,
            check_fn=_check_llm,
            detail=f"configured model: {config.LLM_MODEL}",
        ),
        {
            "key": "github",
            "label": "GitHub proof",
            "env_var": "public GitHub API",
            "configured": True,
            "enabled": config.BRAND3_GITHUB_PROOF_ENABLED,
            "status": "configured" if config.BRAND3_GITHUB_PROOF_ENABLED else "disabled",
            "detail": "uses public repository metadata when owned links expose GitHub URLs",
            "elapsed_ms": None,
            "secret_suffix": "",
        },
    ]
    return {
        "status": _overall_api_status(services),
        "run_checks": run_checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }


def _service_row(
    *,
    key: str,
    label: str,
    env_var: str,
    secret: str,
    enabled: bool,
    run_checks: bool,
    check_fn,
    detail: str = "",
) -> dict[str, Any]:
    configured = bool(secret)
    status = "disabled" if not enabled else "configured" if configured else "missing"
    row = {
        "key": key,
        "label": label,
        "env_var": env_var,
        "configured": configured,
        "enabled": enabled,
        "status": status,
        "detail": detail,
        "elapsed_ms": None,
        "secret_suffix": secret[-4:] if secret else "",
    }
    if not run_checks or check_fn is None:
        return row
    if not enabled:
        row["status"] = "disabled"
        row["detail"] = "disabled by feature flag"
        return row
    if not configured:
        row["status"] = "missing"
        row["detail"] = f"{env_var} not set"
        return row
    start = perf_counter()
    try:
        detail = check_fn(secret)
    except Exception as exc:  # external checks must report, not break the health page
        row["status"] = "error"
        row["detail"] = str(exc)[:220]
    else:
        row["status"] = "ok"
        row["detail"] = detail
    row["elapsed_ms"] = int((perf_counter() - start) * 1000)
    return row


def _check_firecrawl_scrape(secret: str) -> str:
    from src.collectors.web_collector import WebCollector

    result = WebCollector(api_key=secret)._run_firecrawl("https://example.com")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return f"example.com scrape ok; markdown chars={len(result.get('content') or '')}"


def _check_firecrawl_screenshot(secret: str) -> str:
    from src.features.visual_analyzer import VisualAnalyzer

    result = VisualAnalyzer(api_key=secret).take_screenshot("https://example.com")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return "example.com screenshot ok"


def _check_exa(secret: str) -> str:
    from src.collectors.exa_collector import ExaCollector

    results = ExaCollector(api_key=secret).search("OpenAI official website", num_results=1)
    return f"search ok; results={len(results)}"


def _check_searchapi(secret: str) -> str:
    from src.collectors.searchapi_collector import SearchApiCollector

    data = SearchApiCollector(api_key=secret, limit=1, timeout_seconds=8).collect(
        "OpenAI",
        "https://openai.com",
        intents=("news",),
        queries_by_intent={"news": "OpenAI"},
    )
    if data.status == "error":
        failed = [item.error for item in data.intents.values() if item.error]
        raise RuntimeError("; ".join(failed) or "SearchAPI returned error")
    return f"search ok; status={data.status}; results={sum(item.result_count for item in data.intents.values())}"


def _check_llm(secret: str) -> str:
    from src import config
    from src.features.llm_analyzer import LLMAnalyzer

    analyzer = LLMAnalyzer(api_key=secret, model=config.LLM_MODEL)
    analyzer.use_cache = False
    response = analyzer._call(
        "You are a health check. Reply with exactly OK.",
        "Reply exactly OK.",
        max_tokens=128,
    )
    if response.strip() != "OK":
        detail = analyzer.last_failure_reason or "empty_or_unexpected_response"
        raise RuntimeError(f"LLM check failed: {detail}")
    return f"model {analyzer.model} responded ok"


def _overall_api_status(services: list[dict[str, Any]]) -> str:
    statuses = {service["status"] for service in services}
    if "error" in statuses or "missing" in statuses:
        return "degraded"
    return "ok"


@app.on_event("startup")
def _load_env() -> None:
    env_file = Path(".env")
    if env_file.is_file():
        from scripts.sv9_flow_shadow_run import _load_env_file

        _load_env_file(str(env_file))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/apis")
def api_health_api(run: bool = False):
    return JSONResponse(_api_health(run_checks=run))


@app.get("/health/apis")
def api_health_view(request: Request, run: bool = False):
    return templates.TemplateResponse(
        request,
        "api_health.html.j2",
        {"health": _api_health(run_checks=run)},
    )


@app.get("/api/scoring/lab")
def scoring_lab_api():
    backfill_reports()
    return JSONResponse(scoring_dashboard())


@app.get("/scoring/lab")
def scoring_lab_view(request: Request):
    imported = backfill_reports()
    return templates.TemplateResponse(
        request,
        "scoring_lab.html.j2",
        {"lab": scoring_dashboard(), "imported": imported},
    )


@app.get("/")
def index(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request,
        "index.html.j2",
        {"reports": list_reports(), "error": error},
    )


@app.get("/brand/{domain}")
def brand_view(request: Request, domain: str, lang: str = "es"):
    return templates.TemplateResponse(
        request,
        "brand.html.j2",
        {"brand": _brand_profile(domain), "lang": lang},
    )


@app.post("/scan")
def create_scan(
    url: str = Form(...),
    brand_name: str = Form(""),
    allow_degraded_fallback: bool = Form(False),
):
    try:
        scan_id = start_scan(
            url,
            brand_name,
            allow_degraded_fallback=allow_degraded_fallback,
        )
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


@app.post("/api/scan/{scan_id}/continue")
def scan_continue_api(scan_id: str):
    result = approve_degraded_scan(scan_id)
    if result is None:
        return JSONResponse({"state": "unknown", "id": scan_id}, status_code=404)
    status_code = 200 if result.get("approved") else 409
    return JSONResponse(result, status_code=status_code)


@app.post("/api/scan/{scan_id}/cancel")
def scan_cancel_api(scan_id: str):
    result = cancel_scan(scan_id)
    if result is None:
        return JSONResponse({"state": "unknown", "id": scan_id}, status_code=404)
    return JSONResponse(result)


@app.get("/report/{scan_id}")
def report_view(request: Request, scan_id: str):
    report = load_report(scan_id)
    if report is None:
        return RedirectResponse("/?error=Report not found", status_code=303)
    return templates.TemplateResponse(request, "report.html.j2", {"report": report})
