"""Incident responder: receives Grafana alerts, collects evidence, and runs a headless coding agent.

Flow for each firing alert:
1. Save the alert and the evidence (metrics, logs, traces, app logs) in incidents/<id>/.
2. Run `claude -p` headless with a tool allowlist.
3. Check the policy: the agent may only change files under app/ and tests/.
4. If the agent says FIXED and the tests pass, restart the app and check every order again.
   Otherwise write an escalation for a developer.
"""

import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request


ROOT = Path(__file__).resolve().parents[1]
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "900"))

# The agent may read the repo, edit code, and run the tests. Anything else is denied in headless mode.
AGENT_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "Bash(uv run --frozen pytest:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(git log:*)",
    "Bash(docker compose logs:*)",
]
EDITABLE_PREFIXES = ("app/", "tests/")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "incidents", "data"}
# The responder's own server log changes while the agent runs, so it is not an agent change.
SKIP_FILES = {"incident-response/responder.log"}

app = FastAPI(title="Order Tracker incident responder")
_active = set()
_active_lock = threading.Lock()


def now_utc():
    return datetime.now(timezone.utc)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def set_status(incident_dir, stage, **details):
    status_path = incident_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    status.update(stage=stage, updated_at=now_utc().isoformat(), **details)
    write_json(status_path, status)
    print(f"[{incident_dir.name}] {stage} {details or ''}".rstrip(), flush=True)


def collect_evidence(incident_dir, alert):
    evidence = incident_dir / "evidence"
    evidence.mkdir()
    end_ns = time.time_ns()
    start_ns = end_ns - 15 * 60 * 1_000_000_000

    with httpx.Client(timeout=10) as client:
        queries = {
            "metrics-requests": (
                f"{PROMETHEUS_URL}/api/v1/query",
                {"query": "sum by (http_route, http_response_status_code) (increase(http_server_requests_total[15m]))"},
            ),
            "logs": (
                f"{LOKI_URL}/loki/api/v1/query_range",
                {"query": '{service_name="order-tracker"}', "start": start_ns, "end": end_ns, "limit": 200},
            ),
            "traces-with-errors": (f"{TEMPO_URL}/api/search", {"q": "{ status = error }", "limit": 20}),
        }
        for name, (url, params) in queries.items():
            try:
                response = client.get(url, params=params)
                response.raise_for_status()
                write_json(evidence / f"{name}.json", response.json())
            except Exception as exc:
                # A missing signal is also evidence, so record it and keep going.
                write_json(evidence / f"{name}.error.json", {"error": str(exc)})

    logs = subprocess.run(
        ["docker", "compose", "logs", "app", "--tail", "300"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    (evidence / "app-logs.txt").write_text(logs.stdout + logs.stderr, encoding="utf-8")


def build_prompt(incident_dir, alert):
    return f"""You are the on-call coding agent for the Order Tracker app in this repository.
A Grafana alert fired. The alert is in {incident_dir / 'alert.json'} and the evidence is in {incident_dir / 'evidence'}.
Read the alert and the evidence first.

Alert:
{json.dumps(alert, indent=2)}

Rules:
- If the alert is a test (labels.test is "true") or the evidence shows no real problem, change nothing and say so.
- Otherwise find the root cause in app/ and tests/. Make the smallest fix that solves it. Add a regression test in tests/test_api.py.
- Only edit files under app/ and tests/. Do not edit compose.yaml, Dockerfile, observability/, or incident-response/. Do not commit or push.
- Run `uv run --frozen pytest -q` and make sure it passes before you finish.
- If you cannot find the cause or cannot fix it safely, do not guess. Escalate.

Write a short summary of what you found and did. The very last line of your answer must be exactly one of:
RESULT: NO_CHANGE
RESULT: FIXED
RESULT: ESCALATE
"""


def run_agent(incident_dir, alert):
    prompt = build_prompt(incident_dir, alert)
    command = [CLAUDE_BIN, "-p", "--permission-mode", "acceptEdits", "--allowedTools", *AGENT_TOOLS]
    proc = subprocess.run(
        command,
        input=prompt,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=AGENT_TIMEOUT_SECONDS,
    )
    (incident_dir / "agent-output.txt").write_text(
        proc.stdout + (f"\n--- stderr ---\n{proc.stderr}" if proc.stderr.strip() else ""),
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"agent exited with code {proc.returncode}")
    return proc.stdout


def parse_result(output):
    for line in reversed(output.strip().splitlines()):
        if line.startswith("RESULT:"):
            return line.removeprefix("RESULT:").strip()
    return "ESCALATE"


def snapshot():
    """Hash every repo file so we can tell what the agent changed."""
    hashes = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            rel = path.relative_to(ROOT).as_posix()
            if rel in SKIP_FILES:
                continue
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def changed_files(before, after):
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )


def run_checked(command):
    proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout + proc.stderr)[-2000:]


def verify_fix(incident_dir):
    """Run the tests, restart the app, and check every order again."""
    code, output = run_checked(["uv", "run", "--frozen", "pytest", "-q"])
    if code != 0:
        return False, f"pytest failed:\n{output}"

    code, output = run_checked(["docker", "compose", "up", "--build", "-d", "--wait", "app"])
    if code != 0:
        return False, f"app restart failed:\n{output}"

    checks = {}
    with httpx.Client(timeout=10) as client:
        checks["GET /healthz"] = client.get(f"{APP_URL}/healthz").status_code
        for order in client.get(f"{APP_URL}/api/orders").json():
            checks[f"GET /api/orders/{order['id']}"] = client.get(f"{APP_URL}/api/orders/{order['id']}").status_code

    write_json(incident_dir / "verification.json", checks)
    failed = {request: status for request, status in checks.items() if status >= 400}
    if failed:
        return False, f"requests still failing: {failed}"
    return True, "pytest passed, app restarted, all order lookups returned non-error responses"


def investigate(incident_dir, alert, key):
    try:
        set_status(incident_dir, "collecting evidence")
        collect_evidence(incident_dir, alert)

        set_status(incident_dir, "agent running")
        before = snapshot()
        output = run_agent(incident_dir, alert)
        result = parse_result(output)
        changes = changed_files(before, snapshot())

        outside = [path for path in changes if not path.startswith(EDITABLE_PREFIXES)]
        if outside:
            set_status(incident_dir, "escalated", reason="agent changed files outside app/ and tests/", files=outside)
            return

        if result == "NO_CHANGE":
            set_status(incident_dir, "no action needed", agent_result=result)
        elif result == "FIXED" and changes:
            ok, message = verify_fix(incident_dir)
            if ok:
                set_status(incident_dir, "fixed and verified", agent_result=result, files=changes, verification=message)
            else:
                set_status(incident_dir, "escalated", agent_result=result, files=changes, reason=message)
        else:
            reason = "agent escalated" if result == "ESCALATE" else "agent said FIXED but changed no files"
            set_status(incident_dir, "escalated", agent_result=result, reason=reason)
            (incident_dir / "escalation.md").write_text(
                f"# Escalation\n\nThe coding agent could not fix this alert automatically.\n\n"
                f"See agent-output.txt and evidence/ in this folder.\n",
                encoding="utf-8",
            )
    except subprocess.TimeoutExpired:
        set_status(incident_dir, "escalated", reason=f"agent timed out after {AGENT_TIMEOUT_SECONDS}s")
    except Exception as exc:
        set_status(incident_dir, "escalated", reason=f"responder error: {exc}")
    finally:
        with _active_lock:
            _active.discard(key)


@app.post("/alerts", status_code=202)
async def receive_alerts(request: Request):
    payload = await request.json()
    started, skipped = [], []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        labels = alert.get("labels", {})
        key = f"{labels.get('alertname', 'unknown')}|{labels.get('http_route', '')}"
        with _active_lock:
            if key in _active:
                skipped.append(key)
                continue
            _active.add(key)

        slug = "".join(c if c.isalnum() else "-" for c in labels.get("alertname", "alert")).strip("-")
        incident_id = f"{now_utc():%Y%m%d-%H%M%S}-{slug}-{uuid4().hex[:6]}"
        incident_dir = INCIDENTS_DIR / incident_id
        incident_dir.mkdir(parents=True)
        write_json(incident_dir / "alert.json", alert)
        set_status(incident_dir, "received")
        threading.Thread(target=investigate, args=(incident_dir, alert, key), daemon=True).start()
        started.append(incident_id)
    return {"started": started, "skipped_already_running": skipped}


@app.get("/incidents")
def list_incidents():
    if not INCIDENTS_DIR.exists():
        return []
    return sorted((d.name for d in INCIDENTS_DIR.iterdir() if d.is_dir()), reverse=True)


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    incident_dir = INCIDENTS_DIR / incident_id
    if not incident_dir.is_dir():
        raise HTTPException(404, "Incident not found")
    status_path = incident_dir / "status.json"
    agent_path = incident_dir / "agent-output.txt"
    return {
        "status": json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {},
        "agent_output": agent_path.read_text(encoding="utf-8") if agent_path.exists() else None,
    }
