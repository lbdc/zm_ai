"""
zm_ai.py

FastAPI application that:
- Mounts under /zm_ai (for reverse proxy / subpath setups)
- Launches and monitors background scripts (poll_zm_for_events, yolo8_analyze, email_notify)
- Serves thumbnails and detected frames
- Provides log viewing (tail + full log)
- Edits settings.ini through a web form
- Shows ZoneMinder monitor montage (MJPEG and snapshot-based)
"""

# =====================
# Standard library
# =====================
import os
import sys
import time
import json
import re
import configparser
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote, urlsplit
from contextlib import asynccontextmanager


# =====================
# Third-party packages
# =====================
import psutil
import requests
import urllib3
import uvicorn
from requests.auth import HTTPBasicAuth

from fastapi import FastAPI, Request, Query, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse

# =====================
# Global setup
# =====================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ACCESS_LOG = False
DEBUG_SUBPROCESS_OUTPUT = True

# Extra arguments passed to background scripts
COMMON_SUBPROCESS_ARGS = ["--loop"]

# Main app (mounted at /zm_ai by the outer `main_app` when run as __main__)
app = FastAPI(root_path="/zm_ai")

# Mount /temp to serve temporary files if needed
TEMP_DIR = Path(__file__).resolve().parent / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/temp", StaticFiles(directory=str(TEMP_DIR), html=False), name="temp")

# Jinja2 templates directory
templates = Jinja2Templates(directory="templates")

# Include router that provides events/export functionality
from zm_export import router as events_summary_router  # noqa: E402

app.include_router(events_summary_router)

# =====================
# Startup hook updated for on_event being deprecated
# =====================

async def lifespan(app: FastAPI):
    # -------- STARTUP --------
    print("🚀 Startup: launching background scripts")
    for script_name in TARGET_SCRIPTS:
        start_script_if_not_running(script_name)

    yield  # app runs while paused here

    # -------- SHUTDOWN --------
    print("🛑 Shutdown: terminating background scripts")
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            for part in cmdline:
                if os.path.basename(part).lower() in TARGET_SCRIPTS:
                    psutil.Process(proc.info["pid"]).terminate()
        except Exception:
            continue

# Main entrypoint app, so the whole thing can be mounted under /zm_ai
main_app = FastAPI(lifespan=lifespan)
main_app.mount("/zm_ai", app)

# Config globals (populated by load_config)
config = {}
ZM_AI_DETECTIONS_DIR = ""
DEFAULT_LOG_TAIL_LINES = 25
MON_CAMID = ""
EMAIL_CAMID = ""
ZM_HOST = ""
BAUTH_USER = ""
BAUTH_PWD = ""
GO2RTC_HOST = ""

# Background scripts we manage
TARGET_SCRIPTS = [
    "poll_zm_for_events.py",
    "yolo8_analyze.py",
    "email_notify.py",
]
# Normalize to lowercase for process detection
TARGET_SCRIPTS = [s.lower() for s in TARGET_SCRIPTS]

# =====================
# Path / base directory
# =====================

# Detect whether running from a frozen EXE (e.g. Nuitka) or from source
if getattr(sys, "frozen", False):
    BASE_PATH = os.path.dirname(sys.executable)
else:
    BASE_PATH = os.path.dirname(os.path.abspath(__file__))

# Map script -> log file
CONFIGURED_LOG_FILES = {
    script: os.path.join(BASE_PATH, script.replace(".py", ".log"))
    for script in TARGET_SCRIPTS
}


# =====================
# Configuration loading
# =====================

def load_config(primary_file: str = "settings.ini",
                secondary_file: str = "email_settings.ini") -> None:
    """
    Load settings from the primary settings.ini and secondary email_settings.ini.

    Populates global configuration values and paths used by the app.
    """
    global config, ZM_AI_DETECTIONS_DIR, DEFAULT_LOG_TAIL_LINES
    global MON_CAMID, EMAIL_CAMID, ZM_HOST, BAUTH_USER, BAUTH_PWD
    global GO2RTC_HOST

    config = configparser.ConfigParser()

    primary_path = os.path.join(BASE_PATH, primary_file)
    secondary_path = os.path.join(BASE_PATH, secondary_file)

    for path in (primary_path, secondary_path):
        if os.path.exists(path):
            config.read(path)
        else:
            print(f"⚠️ Config file not found: {path}")

    ZM_AI_DETECTIONS_DIR = os.path.abspath(
        os.path.join(
            BASE_PATH,
            config.get("paths", "ZM_AI_DETECTIONS_DIR", fallback="detected_frames"),
        )
    )
    DEFAULT_LOG_TAIL_LINES = config.getint(
        "general", "DEFAULT_LOG_TAIL_LINES", fallback=25
    )
    MON_CAMID = config.get("general", "MON_CAMID", fallback="")
    EMAIL_CAMID = config.get("email", "EMAIL_CAMID", fallback="")

    ZM_HOST = config.get("general", "ZM_HOST", fallback="").rstrip("/")
    BAUTH_USER = config.get("credentials", "BAUTH_USER", fallback="")
    BAUTH_PWD = config.get("credentials", "BAUTH_PWD", fallback="")
    GO2RTC_HOST = config.get("general", "GO2RTC_HOST", fallback="").rstrip("/")

def get_saved_token() -> str | None:
    """
    Return a still-valid ZoneMinder API token if available, else None.

    Token is stored in zm_token.json with fields: {"token": "...", "expires": <epoch>}.
    """
    token_file = os.path.join(os.path.dirname(__file__), "zm_token.json")
    try:
        with open(token_file, encoding="utf-8") as f:
            data = json.load(f)
        if data["expires"] > time.time():
            return data["token"]
    except Exception as e:
        print(f"⚠️ Could not load or parse token: {e}")
    return None


# Load config immediately on import
load_config()
os.makedirs(ZM_AI_DETECTIONS_DIR, exist_ok=True)


# =====================
# Helpers
# =====================

def get_request_scheme(request: Request) -> str:
    """
    Determine the requested scheme (http/https), respecting reverse proxies.

    Apache / Nginx often pass X-Forwarded-Proto when terminating TLS.
    """
    return request.headers.get("x-forwarded-proto", request.url.scheme)


def safe_redirect(request: Request, endpoint: str, query: str = "") -> RedirectResponse:
    """
    Redirect helper that forces HTTPS for non-localhost clients.

    Used after POST actions (start/stop) to redirect back to index.
    """
    url = str(request.url_for(endpoint))
    if request.client.host not in ("127.0.0.1", "localhost"):
        url = url.replace("http://", "https://")
    return RedirectResponse(url + query, status_code=303)


def get_detector_status() -> list[dict]:
    """
    Return a list of background script status objects:
        [
          {"script": "poll_zm_for_events.py", "pid": 1234, "running": True, ...},
          ...
        ]
    """
    expected = {script: None for script in TARGET_SCRIPTS}

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
            if not cmdline:
                continue

            for part in cmdline:
                script_name = os.path.basename(part).lower()
                if script_name in expected:
                    expected[script_name] = proc.info["pid"]
        except Exception:
            # Ignore processes we can't introspect
            continue

    now_str = datetime.now().strftime("%H:%M:%S")
    return [
        {
            "script": name,
            "pid": pid or "–",
            "running": bool(pid),
            "last_checked": now_str,
        }
        for name, pid in expected.items()
    ]


def linkify(text: str) -> str:
    """
    Convert [link:url|label] markup into HTML <a> tags.

    This is used in logs to append a "View Full Log" link.
    """
    return re.sub(
        r"\[link:([^\|]+)\|([^\]]+)\]",
        r'<a href="\1" target="_blank">\2</a>',
        text,
    )


def start_script_if_not_running(script_name: str) -> bool:
    """
    Start a background script if it's not already running.

    Returns:
        True  if the script was started
        False if it was already running or failed to start
    """
    script_name = script_name.lower()
    if script_name not in TARGET_SCRIPTS:
        return False

    # Check if already running
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            for part in cmdline:
                if os.path.basename(part).lower() == script_name:
                    return False
        except Exception:
            continue

    script_path = os.path.join(os.path.dirname(__file__), script_name)
    try:
        proc = psutil.Popen(  # type: ignore[attr-defined]
            [sys.executable, "-u", script_path] + COMMON_SUBPROCESS_ARGS,
            stdout=(subprocess.PIPE if DEBUG_SUBPROCESS_OUTPUT else subprocess.DEVNULL),  # type: ignore[name-defined]
            stderr=(subprocess.PIPE if DEBUG_SUBPROCESS_OUTPUT else subprocess.DEVNULL),  # type: ignore[name-defined]
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )

        if DEBUG_SUBPROCESS_OUTPUT:
            # Stream stdout/stderr to the main console for easier debugging
            def stream_output(pipe, label: str) -> None:
                for line in iter(pipe.readline, ""):
                    print(f"[{script_name} {label}] {line.strip()}")
                pipe.close()

            threading.Thread(
                target=stream_output, args=(proc.stdout, "stdout"), daemon=True
            ).start()
            threading.Thread(
                target=stream_output, args=(proc.stderr, "stderr"), daemon=True
            ).start()

        print(f"✅ Started {script_name}")
        return True
    except Exception as e:
        print(f"⚠️ Failed to start {script_name}: {e}")
        return False


def get_monitors(token: str) -> list[dict]:
    """
    Fetch the list of ZoneMinder monitors via the ZM API.

    Only returns monitors that are actually decoding.
    """
    if not ZM_HOST:
        print("❌ ZM_HOST is not configured")
        return []

    url = f"{ZM_HOST}/zm/api/monitors.json?token={token}"
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(BAUTH_USER, BAUTH_PWD),
            verify=False,
            timeout=10,
        )
        if response.ok:
            monitors = response.json().get("monitors", [])
            # Keep only monitors that are decoding
            return [
                m
                for m in monitors
                if m.get("Monitor", {}).get("Decoding") != "None"
            ]
        print(f"❌ Failed to fetch monitors: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Error contacting ZM API: {e}")
    return []

def zm_button_url(request: Request) -> str:
    """
    ZM button target:
      - If user is browsing this app on localhost/127.0.0.1 -> use ZM_HOST from settings.ini
      - Otherwise -> use the public request host (proxy-aware), then append /zm
    """
    # scheme (proxy-aware)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)

    # host:port (proxy-aware)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
        or ""
    ).lower()

    # local/direct access => use configured ZM host
    if host.startswith("localhost") or host.startswith("127.0.0.1"):
        base = (ZM_HOST or "").rstrip("/")
    else:
        base = f"{scheme}://{host}".rstrip("/")

    # ZoneMinder web root path (adjust if yours isn't /zm)
    return f"{base}/zm"


# =====================
# Routes: UI pages
# =====================

@app.get("/zm_export", response_class=HTMLResponse, name="zm_export")
async def zm_export_page(request: Request):
    """Simple wrapper to render the ZoneMinder export page."""
    return templates.TemplateResponse("zm_export.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """
    Main dashboard:
    - Shows latest detection thumbnails
    - Shows tail of each script log + link to full log
    - Shows status of background scripts
    """
    saved = request.query_params.get("saved")
    scheme = get_request_scheme(request)

    # Build thumbnail list (latest 25)
    if os.path.exists(ZM_AI_DETECTIONS_DIR):
        thumbs = [
            {
                "filename": f,
                "url": f"{scheme}://{request.url.netloc}"
                f"{request.url_for('serve_detected_frame', filename=f).path}",
            }
            for f in sorted(
                (
                    f
                    for f in os.listdir(ZM_AI_DETECTIONS_DIR)
                    if os.path.isfile(os.path.join(ZM_AI_DETECTIONS_DIR, f))
                ),
                key=lambda f: os.path.getmtime(
                    os.path.join(ZM_AI_DETECTIONS_DIR, f)
                ),
                reverse=True,
            )[:25]
        ]
    else:
        thumbs = []

    # Tail logs for each script
    logs: dict[str, str] = {}
    for name in TARGET_SCRIPTS:
        path = CONFIGURED_LOG_FILES.get(name)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-DEFAULT_LOG_TAIL_LINES:]
            lines.reverse()
            content = "".join(lines)

            full_log_url = (
                f"{scheme}://{request.url.netloc}"
                f"{request.url_for('log_full_by_name', script_name=name).path}"
            )

            content += f"\n[link:{full_log_url}|🔍View Full Log]"
            logs[name] = linkify(content)
        else:
            logs[name] = "No log file found."

    detector_status = get_detector_status()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "thumbs": thumbs,
            "logs": logs,
            "detector_status": detector_status,
            "detected_dir": ZM_AI_DETECTIONS_DIR,
            "saved": saved,
            "zm_host": f"{scheme}://{request.url.netloc}",
            "ZM_HOST": ZM_HOST,
            "zm_button_url": zm_button_url(request),
        },
    )


@app.get("/gallery", response_class=HTMLResponse, name="gallery")
async def gallery(request: Request):
    """Simple gallery view (front-end handles image loading)."""
    return templates.TemplateResponse("gallery.html", {"request": request})


@app.get("/edit_settings", response_class=HTMLResponse, name="edit_settings")
async def edit_settings_get(request: Request):
    """
    Render settings.ini in a simple editable form.

    Each input name uses section__key convention (e.g. general__ZM_HOST).
    """
    config_path = os.path.join(BASE_PATH, "settings.ini")
    parser = configparser.ConfigParser()
    parser.read(config_path)
    settings = {section: dict(parser[section]) for section in parser.sections()}
    return templates.TemplateResponse(
        "edit_settings.html", {"request": request, "config": settings}
    )


@app.post("/edit_settings", name="edit_settings_post")
async def edit_settings_post(request: Request):
    """
    Accept posted form data and write back to settings.ini, then reload config.

    Form key format: section__key = value
    """
    form_data = await request.form()
    config_path = os.path.join(BASE_PATH, "settings.ini")
    parser = configparser.ConfigParser()
    parser.read(config_path)

    for full_key, value in form_data.items():
        if "__" in full_key:
            section, key = full_key.split("__", 1)
            if section not in parser:
                parser.add_section(section)
            parser[section][key] = value

    with open(config_path, "w", encoding="utf-8") as f:
        parser.write(f)

    # Reload globals
    load_config()

    return safe_redirect(request, "index", "?saved=1")


# =====================
# Routes: Logs
# =====================

@app.get("/get_logs")
async def get_logs(request: Request, lines: int = DEFAULT_LOG_TAIL_LINES):
    """
    Return a JSON dict: {script_name: "tail of log (HTML linkified)"}.

    Designed for the frontend to periodically poll and update log panels.
    """
    scheme = get_request_scheme(request)
    netloc = request.url.netloc

    logs: dict[str, str] = {}
    for name, path in CONFIGURED_LOG_FILES.items():
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines_data = f.readlines()[-lines:]
            lines_data.reverse()
            content = "".join(lines_data)

            full_url = f"{scheme}://{netloc}/zm_ai/log_full_by_name/{name}"
            content += f"\n[link:{full_url}|🔍View Full Log]"
            logs[name] = linkify(content)
        else:
            logs[name] = "No log file found."
    return JSONResponse(logs)


@app.get("/log_full/{index}", response_class=PlainTextResponse)
async def log_full(index: int):
    """
    Return full log contents by index into CONFIGURED_LOG_FILES keys list.
    """
    script_keys = list(CONFIGURED_LOG_FILES.keys())
    if index >= len(script_keys):
        return PlainTextResponse("Invalid script index", status_code=404)

    log_path = CONFIGURED_LOG_FILES[script_keys[index]]
    if not os.path.exists(log_path):
        return PlainTextResponse("Log file not found", status_code=404)

    with open(log_path, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())


@app.get("/log_full_by_name/{script_name}", response_class=PlainTextResponse)
async def log_full_by_name(script_name: str):
    """
    Return full log contents for a script by file name.
    """
    log_path = CONFIGURED_LOG_FILES.get(script_name)
    if not log_path or not os.path.exists(log_path):
        return PlainTextResponse("Log file not found", status_code=404)

    with open(log_path, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())


# =====================
# Routes: Background script control
# =====================

@app.post("/start", name="start")
async def start_all_scripts(request: Request):
    """Start all target scripts."""
    for script in TARGET_SCRIPTS:
        start_script_if_not_running(script)
    return safe_redirect(request, "index")


@app.post("/stop", name="stop")
async def stop_all_scripts(request: Request):
    """Stop all target scripts by scanning processes and terminating matches."""
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline")
            if not cmdline:
                continue

            for part in cmdline:
                part_basename = os.path.basename(part).lower()
                for target in TARGET_SCRIPTS:
                    if part_basename == target:
                        psutil.Process(proc.info["pid"]).terminate()
                        break
        except Exception as e:
            print(f"⚠️ Error stopping process: {e}")
    return safe_redirect(request, "index")


@app.post("/start/{script_name}")
async def start_script(script_name: str, request: Request):
    """Start a single script by name (if valid and not already running)."""
    if script_name not in TARGET_SCRIPTS:
        return PlainTextResponse("Invalid script", status_code=400)

    start_script_if_not_running(script_name)
    return safe_redirect(request, "index")


@app.post("/stop/{script_name}")
async def stop_script(script_name: str, request: Request):
    """Stop a single script by name."""
    if script_name not in TARGET_SCRIPTS:
        return PlainTextResponse("Invalid script", status_code=400)

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            for part in cmdline:
                if os.path.basename(part).lower() == script_name.lower():
                    psutil.Process(proc.info["pid"]).terminate()
                    break
        except Exception as e:
            print(f"⚠️ Error stopping {script_name}: {e}")

    return safe_redirect(request, "index")


# =====================
# Routes: Detected images
# =====================

@app.get("/get_images", name="get_images")
async def get_images(request: Request):
    """
    Return a list of detected frame URLs sorted newest-first.

    Front-end uses this for thumbnail grid updates.
    """
    try:
        if not os.path.exists(ZM_AI_DETECTIONS_DIR):
            return JSONResponse([])

        files = [
            f
            for f in os.listdir(ZM_AI_DETECTIONS_DIR)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif"))
        ]
        files.sort(
            key=lambda f: os.path.getmtime(os.path.join(ZM_AI_DETECTIONS_DIR, f)),
            reverse=True,
        )

        scheme = get_request_scheme(request)
        urls = [
            f"{scheme}://{request.url.netloc}"
            f"{request.url_for('serve_detected_frame', filename=f).path}"
            for f in files
        ]

        return JSONResponse(urls)
    except BrokenPipeError:
        # Client closed connection mid-response
        print("⚠️ Broken pipe in get_images — client likely disconnected early")
        return PlainTextResponse("Client disconnected", status_code=499)


@app.post("/delete_images", name="delete_images")
async def delete_images(request: Request):
    """
    Delete a list of images by their URLs.

    Body format:
      {"urls": ["http://.../detected_frames/file1.jpg", ...]}
    """
    data = await request.json()
    urls = data.get("urls", [])
    deleted = []

    for url in urls:
        filename = unquote(url.split("/")[-1])
        abs_path = os.path.join(ZM_AI_DETECTIONS_DIR, filename)

        try:
            os.remove(abs_path)
            deleted.append(url)
        except Exception as e:
            print(f"Failed to delete {url}: {e}")

    return JSONResponse({"deleted": deleted})


@app.get("/detected_frames/{filename}", name="serve_detected_frame")
async def serve_detected_frame(filename: str):
    """Serve a detected frame file by filename."""
    abs_path = os.path.join(ZM_AI_DETECTIONS_DIR, filename)
    if not os.path.exists(abs_path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(abs_path)


# =====================
# Routes: Status & debug
# =====================

@app.get("/get_status")
async def get_status():
    """Return JSON status for all background scripts."""
    return JSONResponse(get_detector_status())


@app.get("/debug_headers")
async def debug_headers(request: Request):
    """
    Debug helper: returns request headers as JSON.

    Useful while tuning reverse proxy / TLS / forwarding configuration.
    """
    return JSONResponse(dict(request.headers))


# =====================
# Routes: Montage / MJPEG proxy
# =====================

@app.get("/montage_snapshot", response_class=HTMLResponse)
async def camera_montage_snapshot(request: Request):
    """
    Show snapshot-based montage (no MJPEG) for lower bandwidth use.

    Uses ZoneMinder monitor snapshots instead of continuous MJPEG streams.
    """
    token = get_saved_token()
    if not token:
        return HTMLResponse("❌ No valid access token found", status_code=503)

    scheme = get_request_scheme(request)
    all_monitors = get_monitors(token)

    cameras = []
    for m in all_monitors:
        mon = m["Monitor"]
        cameras.append(
            {
                "cam_id": int(mon["Id"]),
                "name": mon["Name"],
                "decoding": True,
            }
        )

    return templates.TemplateResponse(
        "montage_snapshot.html",
        {
            "request": request,
            "cameras": cameras,
            "token": token,
            "zm_host": f"{scheme}://{request.url.netloc}",
            "ZM_HOST": ZM_HOST,
            "GO2RTC_HOST": GO2RTC_HOST,
        },
    )

@app.get("/montage/snapshot/{cam_id}", name="montage_snapshot_proxy")
def montage_snapshot_proxy(cam_id: int, scale: int = Query(100)):
    token = get_saved_token()
    if not token:
        return HTMLResponse("❌ No valid token", status_code=503)

    if not ZM_HOST:
        return HTMLResponse("❌ ZM_HOST not configured", status_code=503)

    zm_url = (
        f"{ZM_HOST}/zm/cgi-bin/nph-zms"
        f"?mode=single"
        f"&monitor={cam_id}"
        f"&scale={scale}"
        f"&token={token}"
    )

    r = requests.get(
        zm_url,
        auth=HTTPBasicAuth(BAUTH_USER, BAUTH_PWD),
        verify=False,
        timeout=10,
        headers={"User-Agent": "FastAPI Snapshot Proxy"},
    )

    if not r.ok:
        return HTMLResponse("❌ Snapshot failed", status_code=502)

    return Response(
        content=r.content,
        media_type=r.headers.get("Content-Type", "image/jpeg"),
    )


# =====================
# Routes: Login helper
# =====================

@app.get("/login")
async def login(request: Request):
    """
    Minimal login redirect.

    Currently just redirects to the base host (useful when behind /zm_ai subpath).
    """
    scheme = get_request_scheme(request)
    next_url = f"{scheme}://{request.url.netloc}"
    return RedirectResponse(url=next_url)


# =====================
# Local dev entrypoint
# =====================

if __name__ == "__main__":
    print("✅ Started at http://localhost:8001/zm_ai")
    uvicorn.run(
        "zm_ai:main_app",
        host="0.0.0.0",
        port=8001,
        access_log=ACCESS_LOG,
    )
