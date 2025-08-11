from fastapi import FastAPI, Request, Form, UploadFile, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse
import subprocess, os, io, signal, psutil, time, json, re, configparser, sys
from urllib.parse import unquote
import logging
import requests
from requests.auth import HTTPBasicAuth
import threading
import urllib3
from datetime import datetime
import uvicorn

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ACCESS_LOG = False
DEBUG_SUBPROCESS_OUTPUT = True
common_args = ["--loop"]

app = FastAPI(root_path="/zm_ai")

templates = Jinja2Templates(directory="templates")

main_app = FastAPI()
main_app.mount("/zm_ai", app)


config = {}
target_scripts = ["poll_zm_for_events.py", "yolo8_analyze.py", "email_notify.py"]
target_scripts = [s.lower() for s in target_scripts]


@main_app.on_event("startup")
async def start_all_target_scripts():
    print("🚀 Startup: launching background scripts")
    for script in target_scripts:
        start_script_if_not_running(script)


# Detect EXE or script run
if getattr(sys, 'frozen', False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

# Dynamically create the log file mapping
configured_log_files = {
    script: os.path.join(base_path, script.replace(".py", ".log"))
    for script in target_scripts
}

def load_config(primary_file="settings.ini", secondary_file="email_settings.ini"):
    global config
    config = configparser.ConfigParser()

    primary_path = os.path.join(base_path, primary_file)
    secondary_path = os.path.join(base_path, secondary_file)

    loaded_files = []
    for path in (primary_path, secondary_path):
        if os.path.exists(path):
            config.read(path)
            loaded_files.append(path)
        else:
            print(f"⚠️ Config file not found: {path}")

    globals()["ZM_AI_DETECTIONS_DIR"] = os.path.abspath(os.path.join(base_path, config.get("paths", "ZM_AI_DETECTIONS_DIR", fallback="detected_frames")))
    globals()["DEFAULT_LOG_TAIL_LINES"] = config.getint("general", "DEFAULT_LOG_TAIL_LINES", fallback=25)
    globals()["MON_CAMID"] = config.get("general", "MON_CAMID", fallback="")
    globals()["EMAIL_CAMID"] = config.get("email", "EMAIL_CAMID", fallback="")
    ZM_HOST = config.get("general", "ZM_HOST", fallback="").rstrip("/")
    globals()["ZM_HOST"] = ZM_HOST
    globals()["BAUTH_USER"] = config.get("credentials", "BAUTH_USER", fallback="")
    globals()["BAUTH_PWD"] = config.get("credentials", "BAUTH_PWD", fallback="")

def get_saved_token():
    token_file = os.path.join(os.path.dirname(__file__), "zm_token.json")
    try:
        with open(token_file) as f:
            data = json.load(f)
            if data["expires"] > time.time():
                return data["token"]
    except Exception as e:
        print(f"⚠️ Could not load or parse token: {e}")
    return None

load_config()
os.makedirs(ZM_AI_DETECTIONS_DIR, exist_ok=True)

def get_request_scheme(request: Request) -> str:
    # Check 'x-forwarded-proto' header if the app is behind a reverse proxy (e.g., Apache)
    return request.headers.get("x-forwarded-proto", request.url.scheme)

def safe_redirect(request: Request, endpoint: str, query: str = ""):
    url = str(request.url_for(endpoint))
    if request.client.host not in ("127.0.0.1", "localhost"):
        url = url.replace("http://", "https://")
    return RedirectResponse(url + query, status_code=303)


def get_detector_status():
    # Normalize to lowercase for reliable comparison
    normalized_scripts = [s.lower() for s in target_scripts]
    expected = {script: None for script in normalized_scripts}

    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if not cmdline:
                continue

            for part in cmdline:
                script_name = os.path.basename(part).lower()
                if script_name in expected:
                    expected[script_name] = proc.info['pid']
        except Exception:
            continue

    return [
        {
            "script": name,
            "pid": pid or "–",
            "running": bool(pid),
            "last_checked": datetime.now().strftime("%H:%M:%S")
        }
        for name, pid in expected.items()
    ]


def linkify(text):
    return re.sub(r'\[link:([^\|]+)\|([^\]]+)\]', r'<a href="\1" target="_blank">\2</a>', text)

def start_script_if_not_running(script_name):
    if script_name not in target_scripts:
        return False

    for proc in psutil.process_iter(['cmdline']):
        try:
            cmdline = proc.info.get('cmdline') or []
            for part in cmdline:
                if os.path.basename(part).lower() == script_name.lower():
                    return False  # Already running
        except Exception:
            continue

    script_path = os.path.join(os.path.dirname(__file__), script_name)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", script_path] + common_args,
            stdout=subprocess.PIPE if DEBUG_SUBPROCESS_OUTPUT else subprocess.DEVNULL,
            stderr=subprocess.PIPE if DEBUG_SUBPROCESS_OUTPUT else subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',  # Replaces invalid chars instead of crashing
            start_new_session=True
        )


        if DEBUG_SUBPROCESS_OUTPUT:
            def stream_output(pipe, label):
                for line in iter(pipe.readline, ''):
                    print(f"[{script_name} {label}] {line.strip()}")
                pipe.close()

            threading.Thread(target=stream_output, args=(proc.stdout, 'stdout'), daemon=True).start()
            threading.Thread(target=stream_output, args=(proc.stderr, 'stderr'), daemon=True).start()

        print(f"✅ Started {script_name}")
        return True
    except Exception as e:
        print(f"⚠️ Failed to start {script_name}: {e}")
        return False


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    saved = request.query_params.get("saved")
    scheme = get_request_scheme(request)
    if os.path.exists(ZM_AI_DETECTIONS_DIR):
        thumbs = [
            {
                "filename": f,
                "url": f"{scheme}://{request.url.netloc}{request.url_for('serve_detected_frame', filename=f).path}"
            }
            for f in sorted(
                (f for f in os.listdir(ZM_AI_DETECTIONS_DIR)
                 if os.path.isfile(os.path.join(ZM_AI_DETECTIONS_DIR, f))),
                key=lambda f: os.path.getmtime(os.path.join(ZM_AI_DETECTIONS_DIR, f)),
                reverse=True
            )[:25]
        ]
    else:
        thumbs = []

    logs = {}

    for name in target_scripts:
        path = configured_log_files.get(name)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-DEFAULT_LOG_TAIL_LINES:]
                lines.reverse()
                content = ''.join(lines)

                full_log_url = f"{scheme}://{request.url.netloc}{request.url_for('log_full_by_name', script_name=name).path}"

                content += f'\n[link:{full_log_url}|🔍View Full Log]'
                logs[name] = linkify(content)
        else:
            logs[name] = "No log file found."

    detector_status = get_detector_status()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "thumbs": thumbs,
        "logs": logs,
        "detector_status": detector_status,
        "detected_dir": ZM_AI_DETECTIONS_DIR,
        "saved": saved,
        "zm_host": f"{scheme}://{request.url.netloc}",
        "ZM_HOST": ZM_HOST
    })

@app.get("/get_logs") # from windows
async def get_logs(request: Request, lines: int = DEFAULT_LOG_TAIL_LINES):

    logs = {}
    for name, path in configured_log_files.items():
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines_data = f.readlines()[-lines:]
                lines_data.reverse()
                content = ''.join(lines_data)

                scheme = get_request_scheme(request)
                netloc = request.url.netloc
                full_url = f"{scheme}://{netloc}/zm_ai/log_full_by_name/{name}"
                content += f'\n[link:{full_url}|🔍View Full Log]'

                logs[name] = linkify(content)  # ✅ APPLY linkify
        else:
            logs[name] = "No log file found."
    return JSONResponse(logs)


@app.get("/log_full/{index}", response_class=PlainTextResponse)
async def log_full(index: int):
    script_keys = list(configured_log_files.keys())
    if index >= len(script_keys):
        return PlainTextResponse("Invalid script index", status_code=404)
    log_path = configured_log_files[script_keys[index]]
    if not os.path.exists(log_path):
        return PlainTextResponse("Log file not found", status_code=404)

    with open(log_path, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())

@app.get("/log_full_by_name/{script_name}", response_class=PlainTextResponse)
async def log_full_by_name(script_name: str):
    log_path = configured_log_files.get(script_name)
    if not log_path or not os.path.exists(log_path):
        return PlainTextResponse("Log file not found", status_code=404)

    with open(log_path, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())

@app.post("/start", name="start")
async def start_all_scripts(request: Request):
    for script in target_scripts:
        start_script_if_not_running(script)
    return safe_redirect(request, "index")



@app.post("/stop", name="stop")
async def stop_all_scripts(request: Request):
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline')
            if not cmdline:
                continue

            for part in cmdline:
                part_basename = os.path.basename(part).lower()
                for target in target_scripts:
                    if part_basename == target.lower():
                        psutil.Process(proc.info['pid']).terminate()
                        break  # stop checking other targets for this process
        except Exception as e:
            print(f"⚠️ Error stopping process: {e}")
    return safe_redirect(request, "index")



@app.post("/start/{script_name}")
async def start_script(script_name: str, request: Request):
    if script_name not in target_scripts:
        return PlainTextResponse("Invalid script", status_code=400)

    start_script_if_not_running(script_name)
    return safe_redirect(request, "index")



@app.post("/stop/{script_name}")
async def stop_script(script_name: str, request: Request):
    if script_name not in target_scripts:
        return PlainTextResponse("Invalid script", status_code=400)

    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline') or []
            for part in cmdline:
                if os.path.basename(part).lower() == script_name.lower():
                    psutil.Process(proc.info['pid']).terminate()
                    break  # Stop checking other parts for this process
        except Exception as e:
            print(f"⚠️ Error stopping {script_name}: {e}")

    return safe_redirect(request, "index")

    
@app.get("/gallery", response_class=HTMLResponse, name="gallery")
async def gallery(request: Request):
    return templates.TemplateResponse("gallery.html", {"request": request})

@app.get("/edit_settings", response_class=HTMLResponse, name="edit_settings")
async def edit_settings_get(request: Request):
    config_path = os.path.join(base_path, "settings.ini")
    parser = configparser.ConfigParser()
    parser.read(config_path)
    settings = {section: dict(parser[section]) for section in parser.sections()}
    return templates.TemplateResponse("edit_settings.html", {"request": request, "config": settings})

@app.post("/edit_settings", name="edit_settings_post")
async def edit_settings_post(request: Request):
    form_data = await request.form()
    config_path = os.path.join(base_path, "settings.ini")
    parser = configparser.ConfigParser()
    parser.read(config_path)

    for full_key, value in form_data.items():
        if "__" in full_key:
            section, key = full_key.split("__", 1)
            if section not in parser:
                parser.add_section(section)
            parser[section][key] = value

    with open(config_path, "w") as f:
        parser.write(f)
    
    load_config()
    
    return safe_redirect(request, "index", "?saved=1")
    

@app.get("/get_images", name="get_images")
async def get_images(request: Request):
    try:
        if not os.path.exists(ZM_AI_DETECTIONS_DIR):
            return JSONResponse([])

        files = [
            f for f in os.listdir(ZM_AI_DETECTIONS_DIR)
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))
        ]

        files.sort(key=lambda f: os.path.getmtime(os.path.join(ZM_AI_DETECTIONS_DIR, f)), reverse=True)

        scheme = get_request_scheme(request)
        urls = [
            f"{scheme}://{request.url.netloc}{request.url_for('serve_detected_frame', filename=f).path}"
            for f in files
        ]

        return JSONResponse(urls)

    except BrokenPipeError:
        print("⚠️ Broken pipe in get_images — client likely disconnected early")
        return PlainTextResponse("Client disconnected", status_code=499)



@app.get("/debug_headers")
async def debug_headers(request: Request):
    return JSONResponse(dict(request.headers))


@app.post("/delete_images", name="delete_images")
async def delete_images(request: Request):
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
    abs_path = os.path.join(ZM_AI_DETECTIONS_DIR, filename)
    if not os.path.exists(abs_path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(abs_path)

@app.get("/get_status")
async def get_status():
    return JSONResponse(get_detector_status())

def get_monitors(token, scheme="http"):
    url = f"{ZM_HOST}/zm/api/monitors.json?token={token}"
    try:
        response = requests.get(url, auth=HTTPBasicAuth(BAUTH_USER, BAUTH_PWD), verify=False)
        if response.ok:
            monitors = response.json().get("monitors", [])
            # Only return monitors that are decoding
            return [m for m in monitors if m.get("Monitor", {}).get("Decoding") != "None"]
        else:
            print(f"❌ Failed to fetch monitors: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Error contacting ZM API: {e}")
    return []


@app.get("/montage", response_class=HTMLResponse, name="camera_montage")
async def camera_montage(request: Request):
    token = get_saved_token()
    if not token:
        return HTMLResponse("❌ No valid access token found. Ensure polling script is running.", status_code=503)
    scheme = get_request_scheme(request)
    all_monitors = get_monitors(token, scheme)
    cam_ids = set(cid.strip() for cid in MON_CAMID.split(",") if cid.strip().isdigit())
    email_ids = set(cid.strip() for cid in EMAIL_CAMID.split(",") if cid.strip().isdigit())

    cameras = []
    for m in all_monitors:
        mon = m["Monitor"]
        cam_id = str(mon["Id"])
        cameras.append({
            "cam_id": int(cam_id),
            "name": mon["Name"],
            "decoding": True,  # Always decoding now
            "analysing": cam_id in cam_ids,
            "email_enabled": cam_id in email_ids
        })

    return templates.TemplateResponse("montage.html", {
        "request": request,
        "cameras": cameras,
        "token": token,
        "zm_host": f"{scheme}://{request.url.netloc}"
    })


@app.get("/montage_snapshot", response_class=HTMLResponse)
async def camera_montage_snapshot(request: Request):
    token = get_saved_token()
    if not token:
        return HTMLResponse("❌ No valid access token found", status_code=503)

    scheme = get_request_scheme(request)
    all_monitors = get_monitors(token, scheme)

    cameras = []
    for m in all_monitors:
        mon = m["Monitor"]
        cameras.append({
            "cam_id": int(mon["Id"]),
            "name": mon["Name"],
            "decoding": True  # Always decoding now
        })
    scheme = get_request_scheme(request)
    return templates.TemplateResponse("montage_snapshot.html", {
        "request": request,
        "cameras": cameras,
        "token": token,
        "zm_host": f"{scheme}://{request.url.netloc}",
        "ZM_HOST": ZM_HOST
    })

@app.get("/montage/mjpeg/{cam_id}")
def mjpeg_proxy(request: Request, cam_id: int, scale: int = Query(50)):
    token = get_saved_token()
    if not token:
        return HTMLResponse("❌ No valid token", status_code=503)

    scheme = get_request_scheme(request)
    zm_url = (
        f"{ZM_HOST}/zm/cgi-bin/nph-zms"
        f"?mode=jpeg"
        f"&monitor={cam_id}"
        f"&scale={scale}"
        f"&maxfps=1"
        f"&buffer=1"
        f"&token={token}"
    )

    try:
        r = requests.get(
            zm_url,
            stream=True,
            auth=HTTPBasicAuth(BAUTH_USER, BAUTH_PWD),
            verify=False,
            timeout=10,
            headers={"User-Agent": "FastAPI MJPEG Proxy"}
        )

        content_type = r.headers.get("Content-Type", "multipart/x-mixed-replace")

        if not r.ok or not content_type.startswith("multipart"):
            print(f"❌ Unexpected ZM response: {r.status_code}, {content_type}")
            return HTMLResponse("❌ ZM stream failed", status_code=502)

        # ✅ This flushes each frame to the browser immediately
        def iter_mjpeg():
            try:
                for chunk in r.iter_content(chunk_size=1024):
                    if chunk:
                        yield chunk
            except Exception as e:
                print("❌ MJPEG stream closed or broken:", e)

        return StreamingResponse(iter_mjpeg(), media_type=content_type)

    except Exception as e:
        print("❌ Proxy MJPEG error:", e)
        return HTMLResponse("❌ Internal error", status_code=500)


@app.get("/login")
async def login(request: Request):
    scheme = get_request_scheme(request)
    next_url = f"{scheme}://{request.url.netloc}"
    return RedirectResponse(url=next_url)


if __name__ == "__main__":
    print(f"✅ Started at http://localhost:8001/zm_ai")
    uvicorn.run("zm_ai:main_app", host="0.0.0.0", port=8001, access_log=ACCESS_LOG)
