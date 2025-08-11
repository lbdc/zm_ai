#!/usr/bin/env python3
import os
import io
import sys
import requests
import urllib3
from requests.auth import HTTPBasicAuth
import time
import argparse
import configparser
import urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict
import json
import traceback

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
config = None

def load_config(config_file="settings.ini"):
    global config
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file_path = os.path.join(script_dir, config_file)

    if not os.path.exists(config_file_path):
        print(f"⚠️ Config file not found: {config_file_path}")
        return
        
    config = configparser.ConfigParser()
    config.read(config_file_path)

    # ==========================
    # CONFIGURATION SETTINGS FROM FILE
    # ==========================

    # === GENERAL ===
    globals()["MON_CAMID"] = config.get("general", "MON_CAMID", fallback="")
    ZM_HOST = config.get("general", "ZM_HOST", fallback="").rstrip("/")
    globals()["ZM_HOST"] = ZM_HOST
    globals()["LOG_ENABLE"] = config.getboolean("general", "LOG_ENABLE", fallback=True)

    # === PATHS ===
    globals()["ZM_ALARM_QUEUE"] = os.path.join(script_dir, config.get("paths", "ZM_ALARM_QUEUE", fallback="to_be_processed"))

    # === CREDENTIALS ===
    globals()["ZM_USER"] = config.get("credentials", "ZM_USER", fallback="")
    globals()["ZM_PASS"] = config.get("credentials", "ZM_PASS", fallback="")
    globals()["BAUTH_USER"] = config.get("credentials", "BAUTH_USER", fallback="")
    globals()["BAUTH_PWD"] = config.get("credentials", "BAUTH_PWD", fallback="")

    # === DETECTION ===
    globals()["THRESHOLD"] = config.getint("detection", "THRESHOLD", fallback=10)
    globals()["TIME_WINDOW"] = config.getint("detection", "TIME_WINDOW", fallback=60)

    # === LOGGING RETENTION (optional override) ===
    globals()["LOG_RETENTION_DAYS"] = config.getint("general", "LOG_RETENTION_DAYS", fallback=1)

# ==========================
# Globals for the script only
# ==========================

PROCESSED_IDS_FILE = "downloaded_ids.txt"
PENDING_CHECK_LOOKBACK_MINUTES = 5
CHECK_INTERVAL_SECONDS = 10
PROCESSED_RETENTION_HOURS = 1  # Limit how long to keep processed IDs
TOKEN_EXPIRY = 1
access_token = None
AUTH_DISABLED = False

# Auto-generate basepath from script name
if getattr(sys, 'frozen', False):
    # Running as a PyInstaller EXE
    base_path = os.path.dirname(sys.executable)
else:
    # Running as a .py script
    base_path = os.path.dirname(os.path.abspath(__file__))

script_basename = os.path.splitext(os.path.basename(sys.argv[0]))[0]
LOG_FILE = os.path.join(base_path, f"{script_basename}.log")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "zm_token.json")
file_timestamps = defaultdict(list)

# ==========================
# Auth
# ==========================

def login_bauth():
    global TOKEN_EXPIRY, access_token, AUTH_DISABLED
    current_time = time.time()

    if (access_token and current_time < TOKEN_EXPIRY) or AUTH_DISABLED:
        return

    url = f'{ZM_HOST}/zm/api/host/login.json'
    response = requests.post(
        url,
        auth=HTTPBasicAuth(BAUTH_USER, BAUTH_PWD),
        data={'user': ZM_USER, 'pass': ZM_PASS, 'stateful': '1'},
        verify=False
    )

    if response.status_code == 200:
        try:
            a = response.json()
        except Exception as e:
            printLog(f"❌ Invalid JSON in login response: {e}")
            return None

        # Normal case: token present
        token = a.get('access_token')
        if token:
            access_token = token
            TOKEN_EXPIRY = current_time + 3600
            try:
                with open(TOKEN_FILE, "w") as f:
                    json.dump({"token": access_token, "expires": TOKEN_EXPIRY}, f)
            except Exception as e:
                printLog(f"⚠️ Failed to write token file: {e}")
            return access_token

        # Auth disabled case: login.json returns version/apiversion instead of a token
        if "version" in a and "apiversion" in a:
            AUTH_DISABLED = True
            access_token = "AUTH_DISABLED"  # dummy value
            TOKEN_EXPIRY = time.time() + 10*365*24*3600  # 10 years in future

            try:
                with open(TOKEN_FILE, "w") as f:
                    json.dump({"token": access_token, "expires": TOKEN_EXPIRY}, f)
            except Exception as e:
                printLog(f"⚠️ Failed to write token file for AUTH_DISABLED: {e}")

            return access_token

        # Unexpected 200 payload
        printLog(f"❌ Login 200 but no token and no version/apiversion: {a}")
        return None

    # Non-200: failed login
    try:
        err = response.json()
    except Exception:
        err = response.text[:300]
    printLog(f"❌ Login failed ({response.status_code}): {err}")
    return None


def parse_monitors():
    return [int(x.strip()) for x in MON_CAMID.split(",") if x.strip().isdigit()]

# ==========================
# Processed ID Handling
# ==========================

def load_processed_ids():
    if not os.path.exists(PROCESSED_IDS_FILE):
        print(f"⚠️ {PROCESSED_IDS_FILE} does not exist. Returning empty processed ID list.")
        return {}
    ids = {}
    cutoff = datetime.now() - timedelta(hours=PROCESSED_RETENTION_HOURS)
    with open(PROCESSED_IDS_FILE, "r") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            eid, ts = parts
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                if dt >= cutoff:
                    ids[eid] = ts
            except:
                continue
    return ids

def mark_id_as_processed(event_id, processed_ids):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    processed_ids[event_id] = ts

def cleanup_processed_ids(processed_ids):
    with open(PROCESSED_IDS_FILE, "w") as f:
        for eid, ts in processed_ids.items():
            f.write(f"{eid} {ts}\n")

# ==========================
# Event Functions
# ==========================

def get_events_in_range_by_start(start_dt, end_dt):
    login_bauth()

    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    start_encoded = urllib.parse.quote(f"StartTime >=:{start_str}")
    auth = HTTPBasicAuth(BAUTH_USER, BAUTH_PWD)
    allowed_monitors = parse_monitors()
    all_results = []
    page = 1
    limit = 100

    while True:
        url = (
            f"{ZM_HOST}/zm/api/events/index/{start_encoded}.json"
            f"?sort=StartTime&direction=desc&limit={limit}&page={page}&token={access_token}"
        )

        try:
            response = requests.get(url, auth=auth, verify=False)
            if response.status_code != 200:
                printLog(f"⚠️ Failed to retrieve events (page {page}): {response.status_code} - {response.text}")
                break

            data = response.json()
            raw_events = data.get("events", [])
#            printLog(f"📦 Page {page}: Retrieved {len(raw_events)} event(s)")

            for ev in raw_events:
                e = ev["Event"]
                if int(e["MonitorId"]) in allowed_monitors:
                    all_results.append({
                        "Id": str(e["Id"]),
                        "StartTime": e["StartTime"],
                        "StartDateTime": e.get("StartDateTime"),
                        "EndDateTime": e.get("EndDateTime"),
                        "MonitorId": str(e["MonitorId"])
                    })

            if len(raw_events) < limit:
                break  # Last page
            page += 1

        except Exception as e:
            printLog(f"⚠️ Error querying events page {page}: {e}")
            break

    return all_results

def get_event_by_id(event_id):
    login_bauth()
    if not access_token:
        return

    url = f"{ZM_HOST}/zm/api/events/view/{event_id}.json?token={access_token}"
    auth = HTTPBasicAuth(BAUTH_USER, BAUTH_PWD)

    try:
        response = requests.get(url, auth=auth, verify=False)
        if response.status_code != 200:
            return None
        ev = response.json().get("event")
        return {
            "Id": str(ev["Event"]["Id"]),
            "EndDateTime": ev["Event"].get("EndDateTime"),
            "MonitorId": str(ev["Event"]["MonitorId"])
        }
    except:
        return None

def download_event_video(event_id, monitor_id):
    login_bauth()
    if not access_token:
        return
    
    video_url = f"{ZM_HOST}/zm/index.php?view=view_video&eid={event_id}&token={access_token}"
    filename = f"{monitor_id}-{event_id}.mp4"
    output_path = os.path.join(ZM_ALARM_QUEUE, filename)

    os.makedirs(ZM_ALARM_QUEUE, exist_ok=True)

    try:
        r = requests.get(video_url, auth=HTTPBasicAuth(BAUTH_USER, BAUTH_PWD), verify=False, stream=True)
        if r.status_code == 200:
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
#            printLog(f"Downloaded event {event_id} video to {output_path}")
            return True
        else:
            printLog(f"⚠️ Failed to download video for event {event_id}: {r.status_code}")
    except Exception as e:
        printLog(f"❌ Error downloading video for event {event_id}: {e}")
    
    return False    

# ==========================
# Monitor Loop
# ==========================

from collections import defaultdict

file_timestamps = defaultdict(list)

def loop_monitor():
    printLog(f"❤️  Starting polling Zoneminder: {ZM_HOST}")
    processed_ids = load_processed_ids()
    pending_events = {}

    while True:
        try:
            now = datetime.now()
            lookback = now - timedelta(minutes=PENDING_CHECK_LOOKBACK_MINUTES)

            events = get_events_in_range_by_start(lookback, now)
#            print(f"Fetched {len(events)} events")

            for ev in events:

            # make sure it is a good event
                required_keys = {"Id", "MonitorId", "StartDateTime"}
                if not required_keys.issubset(ev.keys()):
                    printLog(f"⚠️ Skipping event missing required fields: {ev}")
                    continue

                eid = ev["Id"]
                cam_id = str(ev["MonitorId"])
                if eid in processed_ids:
                    continue

                if ev["EndDateTime"]:

                    event_time = datetime.strptime(ev["StartDateTime"], "%Y-%m-%d %H:%M:%S")

                    # ✅ Use event_time for sliding window
                    window_start = event_time - timedelta(seconds=int(TIME_WINDOW))
                    file_timestamps[cam_id] = [t for t in file_timestamps[cam_id] if t >= window_start]
                    file_timestamps[cam_id].append(event_time)

                    if len(file_timestamps[cam_id]) > int(THRESHOLD):
                        url = f'{ZM_HOST}/zm?view=event&eid={eid}'
                        printLog(f"📌 Rate Limit Exceeded Skipped! {event_time} camId={cam_id} Event=[link:{url}|{eid}]")
                        processed_ids[eid] = event_time.strftime("%Y-%m-%d %H:%M:%S")
                        continue

                    processed_ids[eid] = event_time.strftime("%Y-%m-%d %H:%M:%S")
                    url = f"{ZM_HOST}/zm?view=event&eid={eid}"
                    printLog(f"🆕 {ev['StartDateTime']} camId={cam_id} Event=[link:{url}|{eid}]")
                    pending_events.pop(eid, None)
                    download_event_video(eid, cam_id)
                else:
                    pending_events[eid] = ev

            for eid in list(pending_events):
                if eid in processed_ids:
                    pending_events.pop(eid, None)
                    continue

                ev_updated = get_event_by_id(eid)
                ev_original = pending_events[eid]
                
                if not ev_updated or not {"Id", "MonitorId"}.issubset(ev_updated.keys()):
                    printLog(f"⚠️ Skipping pending event {eid}: missing required fields on retry: {ev_updated}")
                    pending_events.pop(eid, None)
                    continue

                if not ev_updated.get("EndDateTime"):
                    continue  # Still not ended

                if "StartDateTime" not in ev_original:
                    printLog(f"⚠️ Skipping pending event {eid}: missing StartDateTime in original: {ev_original}")
                    pending_events.pop(eid, None)
                    continue
                    
                cam_id = str(ev_updated["MonitorId"])
                event_time = datetime.strptime(ev["StartDateTime"], "%Y-%m-%d %H:%M:%S")
                window_start = event_time - timedelta(seconds=int(TIME_WINDOW))
                file_timestamps[cam_id] = [t for t in file_timestamps[cam_id] if t >= window_start]
                file_timestamps[cam_id].append(event_time)

                if len(file_timestamps[cam_id]) > int(THRESHOLD):
                    url = f'{ZM_HOST}/zm?view=event&eid={eid}'
                    printLog(f"📌 Rate Limit Exceeded Skipped! {event_time} camId={cam_id} Event=[link:{url}|{eid}]")
                    processed_ids[eid] = event_time.strftime("%Y-%m-%d %H:%M:%S")
                    pending_events.pop(eid)
                    continue


                processed_ids[eid] = event_time.strftime("%Y-%m-%d %H:%M:%S")
                url = f"{ZM_HOST}/zm?view=event&eid={eid}"
                printLog(f"🆕 {event_time} camId={cam_id} Event=[link:{url}|{eid}]")
                pending_events.pop(eid)
                download_event_video(eid, cam_id)

            cleanup_processed_ids(processed_ids)

        except Exception as e:
            tb_str = traceback.format_exc()
            if isinstance(e, BrokenPipeError):
                printLog(f"❌ BrokenPipeError in main loop (errno 32): {e}")
                printLog(f"🧵 Traceback:\n{tb_str}")
            else:
                printLog(f"❌ Exception in main loop: {e}")
                printLog(f"🧵 Traceback:\n{tb_str}")

        time.sleep(CHECK_INTERVAL_SECONDS)


def printLog(*args, **kwargs):
    now = datetime.now()
    timestamp = now.strftime('%Y-%m-%d %H:%M:%S')

    # Combine all args into one string
    raw_entry = ' '.join(str(arg) for arg in args)
    log_entry = f"[{timestamp}] {raw_entry}"

    # Print to console safely
    try:
        print(log_entry, **kwargs)
    except BrokenPipeError:
        pass

    if not int(LOG_ENABLE):
        return

    try:
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        # Retain only lines within log retention period
        cutoff = now - timedelta(days=LOG_RETENTION_DAYS)
        kept_lines = []
        for line in lines:
            try:
                if line.startswith("["):
                    ts_str = line.split("]", 1)[0].strip("[]")
                    entry_time = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                    if entry_time >= cutoff:
                        kept_lines.append(line)
            except Exception:
                kept_lines.append(line)

        kept_lines.append(log_entry + "\n")

        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.writelines(kept_lines)

    except Exception as e:
        try:
            print(f"[Logging error] {e}", file=sys.stderr)
        except BrokenPipeError:
            pass


# ==========================
# Entrypoint
# ==========================

if __name__ == "__main__":
    load_config()
    parser = argparse.ArgumentParser(description="Fetch ZoneMinder events in a time range or monitor in a loop.")
    parser.add_argument("--start", help="Start time in format YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", help="End time in format YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--last", type=int, help="Number of seconds to look back from now")
    parser.add_argument("--id", type=int, help="Fetch a single event by ID")
    parser.add_argument("--download-id", type=int, help="Download video for a specific event ID (not implemented)")
    parser.add_argument("--loop", action="store_true", help="Run monitoring loop")

    args = parser.parse_args()
    login_bauth()  # 🔐 always acquire token once

    if args.loop:
        loop_monitor()
    elif args.id:
        ev = get_event_by_id(args.id)
        if ev:
            print(ev)
        else:
            print(f"Event {args.id} not found or failed to fetch.")
    elif args.last:
        now = datetime.now()
        start = now - timedelta(seconds=args.last)
        events = get_events_in_range_by_start(start, now)
        print(events)
        print(f"✅ Retrieved {len(events)} event(s)")
    elif args.start and args.end:
        try:
            start = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(args.end, "%Y-%m-%d %H:%M:%S")
            events = get_events_in_range_by_start(start, end)
            print(events)
        except ValueError:
            print("Invalid date format. Use YYYY-MM-DD HH:MM:SS")
    else:
        parser.print_help()
