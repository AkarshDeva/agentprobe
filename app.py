import os
import threading
import time
import traceback
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import graph as lab
from config import CATEGORIES, TARGET_URL
from guards import MAX_PER_CATEGORY, MAX_ROUNDS_ALLOWED, check_run_request, redact_pii

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AgentProbe", version="1.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

RUNS = {}  # run_id -> everything we know about that run
MAX_STORED_RUNS = 20
ACTIVE = {"id": None}  # only ONE run at a time keeps the free API limits safe
ACTIVE_LOCK = threading.Lock()


class RunRequest(BaseModel):
    target_url: str | None = None
    per_category: int = 1
    rounds: int = 2
    categories: list[str] | None = None


# ---------- helpers ----------
def _clean_result(item):
    test, reply, verdict = item["test"], item["reply"], item["verdict"]
    return {
        "id": test["id"],
        "category": test["category"],
        "round": test.get("round", 1),
        "message": redact_pii(test["message"])[:300],
        "reply": redact_pii(reply.get("text") or reply.get("error") or "")[:600],
        "status": reply.get("status_code"),
        "latency": reply.get("latency_seconds"),
        "passed": verdict["passed"],
        "severity": verdict["severity"],
        "score": verdict["score"],
        "reason": redact_pii(verdict["reason"]),
        "judged_by": verdict["judged_by"],
    }


def _build_payload(final):
    summary = final["report"]["summary"]
    keep = (
        "core", "deep_probe_tests", "total", "graded", "passed", "failed",
        "ungraded", "pass_rate", "by_category", "average_latency",
    )
    return {
        "summary": {k: summary[k] for k in keep},
        "results": [_clean_result(r) for r in final["results"]],
        "notes": final.get("notes", []),
        "suggestions": redact_pii(final["report"]["suggestions"]),
        "markdown": redact_pii(final["report"]["markdown"]),
    }


def _friendly(exc):
    # Our own errors are written to be read by people. Anything else stays in the log.
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    return "The run failed. Check the server window for details."


def _worker(run, settings):
    def listener(event):
        event["i"] = len(run["events"])
        run["events"].append(event)

    lab.LISTENERS[run["id"]] = listener
    try:
        final = lab.run_lab(
            label=f"web-{run['id']}",
            categories=settings["categories"],
            per_category=settings["per_category"],
            target_url=settings["url"],
            max_rounds=settings["rounds"],
            run_id=run["id"],
        )
        run["payload"] = _build_payload(final)
        run["status"] = "done"
        listener({"kind": "done"})
    except Exception as exc:
        traceback.print_exc()
        run["error"] = _friendly(exc)
        run["status"] = "error"
        listener({"kind": "error", "message": run["error"]})
    finally:
        lab.LISTENERS.pop(run["id"], None)
        with ACTIVE_LOCK:
            ACTIVE["id"] = None


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/config")
async def get_config():
    return {
        "default_target": TARGET_URL,
        "categories": CATEGORIES,
        "limits": {"per_category": MAX_PER_CATEGORY, "rounds": MAX_ROUNDS_ALLOWED},
    }


@app.post("/api/runs")
async def start_run(payload: RunRequest):
    try:
        url, per, rounds, cats = check_run_request(
            payload.target_url, payload.per_category, payload.rounds, payload.categories
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    with ACTIVE_LOCK:
        if ACTIVE["id"]:
            return JSONResponse({"error": "A run is already in progress. Wait for it to finish."}, status_code=409)
        run_id = uuid.uuid4().hex[:10]
        ACTIVE["id"] = run_id

    run = {"id": run_id, "status": "running", "events": [], "error": "", "payload": None, "created": time.time()}
    RUNS[run_id] = run
    for old in sorted(RUNS, key=lambda k: RUNS[k]["created"])[:-MAX_STORED_RUNS]:
        RUNS.pop(old, None)

    settings = {"url": url, "per_category": per, "rounds": rounds, "categories": cats}
    threading.Thread(target=_worker, args=(run, settings), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, since: int = 0):
    run = RUNS.get(run_id)
    if not run:
        return JSONResponse({"error": "Unknown run."}, status_code=404)
    events = run["events"][since:]
    body = {
        "status": run["status"],
        "events": events,
        "next": since + len(events),
        "error": run["error"],
    }
    if run["status"] == "done":
        body["result"] = run["payload"]
    return body


@app.get("/api/runs/{run_id}/report.md")
async def download_report(run_id: str):
    run = RUNS.get(run_id)
    if not run or not run["payload"]:
        return PlainTextResponse("Report not ready.", status_code=404)
    return PlainTextResponse(
        run["payload"]["markdown"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="agentprobe-{run_id}.md"'},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8100"))
    uvicorn.run(app, host="127.0.0.1", port=port)