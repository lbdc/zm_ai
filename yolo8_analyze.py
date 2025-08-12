#!/usr/bin/env python3
import sys
import cv2
import numpy as np
import argparse
import os
import io
import time
import sys
import glob
import configparser
from datetime import datetime, timedelta
from ultralytics import YOLO
import torch

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
    ZM_HOST = config.get("general", "ZM_HOST", fallback="").rstrip("/")
    globals()["ZM_HOST"] = ZM_HOST
    globals()["LOG_ENABLE"] = config.getboolean("general", "LOG_ENABLE", fallback=True)

    # === PATHS ===
    globals()["ZM_ALARM_QUEUE"] = os.path.join(script_dir, config.get("paths", "ZM_ALARM_QUEUE", fallback="to_be_processed"))
    globals()["ZM_AI_DETECTIONS_DIR"] = os.path.join(script_dir, config.get("paths", "ZM_AI_DETECTIONS_DIR", fallback="detected_frames"))
    globals()["YOLO_CONFIG_PATH"] = os.path.join(script_dir, config.get("paths", "YOLO_CONFIG_PATH", fallback="yolo"))

    # === DETECTION ===
    globals()["USE_GPU"] = config.getboolean("detection", "USE_GPU", fallback=True)
    globals()["USE_BOX"] = config.getboolean("detection", "USE_BOX", fallback=True)
    globals()["CONFIDENCE_THRESHOLD"] = config.getfloat("detection", "CONFIDENCE_THRESHOLD", fallback=0.7)
    globals()["OBJ_LIST"] = [item.strip() for item in config.get("detection", "OBJ_LIST", fallback="").split(",") if item.strip()]

    # === LOGGING RETENTION (optional override) ===
    globals()["LOG_RETENTION_DAYS"] = config.getint("general", "LOG_RETENTION_DAYS", fallback=1)

# ==========================
# CONFIGURATION SETTINGS FROM FILE
# ==========================

# Auto-generate basepath from script name
if getattr(sys, 'frozen', False):
    # Running as a PyInstaller EXE
    base_path = os.path.dirname(sys.executable)
else:
    # Running as a .py script
    base_path = os.path.dirname(os.path.abspath(__file__))

script_basename = os.path.splitext(os.path.basename(sys.argv[0]))[0]
LOG_FILE = os.path.join(base_path, f"{script_basename}.log")

def load_yolo():
    model_path = os.path.join(YOLO_CONFIG_PATH, "yolov8s.pt")
    model = YOLO(model_path)
    return model


# ==========================
# Process Object Detection
# ==========================
def detect_objects(frame, model):
    results = model.predict(frame, conf=CONFIDENCE_THRESHOLD, verbose=False)[0]

    detected_objects = {}

    for box in results.boxes:
        class_id = int(box.cls[0])
        confidence = float(box.conf[0])
        x1, y1, x2, y2 = map(int, box.xyxy[0])

        obj_name = model.names[class_id]
        if obj_name in OBJ_LIST:
            if obj_name not in detected_objects or confidence > detected_objects[obj_name]["confidence"]:
                detected_objects[obj_name] = {
                    "box": (x1, y1, x2 - x1, y2 - y1),
                    "confidence": confidence
                }

    return detected_objects


# ==========================
# Extract Event ID from Path
# ==========================
def extract_ids(video_path):
    filename = os.path.basename(video_path)
    name_part = os.path.splitext(filename)[0]  # Remove .mp4
    parts = name_part.split("-")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return parts[0], parts[1]  # camid, eventid
    return "unknown", "unknown"

# ==========================
# Process Video File
# ==========================
def process_video(video_path, model, camid, event_id):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    try:
        # handle NaN, None, or weird low values
        if not fps or fps != fps or fps < 1:
            fps = 1.0
    except Exception:
        fps = 1.0
        
    if fps <= 0:
        raise ValueError(f"Invalid FPS ({fps}) for {video_path}")
    frame_interval = max(1, int(round(fps)))  # Process 1 frame per second (adjust as needed)

    # Store the best detection for each object across all frames
    best_detections = {}

    frame_count = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1

        # Process only keyframes (I-frames)?
        if frame_count % frame_interval == 0:
            detected_objects = detect_objects(frame, model)

            for obj_name, data in detected_objects.items():
                x, y, w, h = data["box"]
                confidence = data["confidence"]

                # Track the highest confidence detection across all frames
                if obj_name not in best_detections or confidence > best_detections[obj_name]["confidence"]:
                    best_detections[obj_name] = {
                        "frame": frame.copy(),  # Store the frame for later saving
                        "box": (x, y, w, h),
                        "confidence": confidence
                    }

    cap.release()

    if not frame_count:
        printLog(f"⚠️ No frames processed from {video_path}", file=sys.stderr)
        return
        
    url = f"{ZM_HOST}/zm?view=event&eid={event_id}"
    event_link = f"[link:{url}|{event_id}]"

    if best_detections:
        details = ' '.join(f"{obj}: {data['confidence']:.2f}" for obj, data in best_detections.items())
        printLog(f"camId={camid} Event={event_link} ✅ Detected: {details}")
    else:
        printLog(f"camId={camid} Event={event_link} ☑️ Nothing Detected")
#        print(f"camId={camid} Event={event_link} ☑️ Nothing Detected")
        
    # Optionally save the best frame for each detected object class
    for obj_name, data in best_detections.items():
        frame = data["frame"]
        x, y, w, h = data["box"]
        confidence = data["confidence"]

        if int(USE_BOX):
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
            label = f"{obj_name}: {int(confidence * 100)}%"
            cv2.putText(frame, label, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        save_filename = f"{camid}_{event_id}_{obj_name}.jpg"
        save_path = os.path.join(ZM_AI_DETECTIONS_DIR, save_filename)
        cv2.imwrite(save_path, frame)

def make_folder(path):
    if not os.path.exists(path):
        parent = os.path.dirname(path)
        parent_stat = os.stat(parent)

        # Ensure the parent directory has the setgid bit set
        if not (parent_stat.st_mode & 0o2000):  # Check if setgid is not set
            os.chmod(parent, parent_stat.st_mode | 0o2000)  # Set setgid if missing

        # Set the umask to control permissions (e.g., 770)
        current_umask = os.umask(0o007)
        try:
            os.makedirs(path, exist_ok=True)  # Create the folder
        finally:
            os.umask(current_umask)  # Restore the original umask

        # The folder will inherit the parent group because of the setgid bit
        # apply it chmod g+s /var/www/html/zm137_Detect
        
# ==========================
# Print to Log
# ==========================

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


def get_oldest_video(folder):
    videos = glob.glob(os.path.join(folder, "*.mp4"))  # Adjust extension as needed
    if not videos:
        return None
    return min(videos, key=os.path.getmtime)

def watchdog_loop():
    model = load_yolo()

    # ✅ Optional warm-up to trigger CUDA + model initialization
    if USE_GPU and torch.cuda.is_available():
        printLog("⚙️ Warming up model on GPU...")
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        _ = model.predict(dummy, device="cuda", verbose=False)

    while True:
        video_path = get_oldest_video(ZM_ALARM_QUEUE)
        if not video_path:
            time.sleep(5)
            continue

        camid, event_id = extract_ids(video_path)
#        printLog(f"⏳ Processing {video_path}")
        try:
            process_video(video_path, model, camid, event_id)
        except Exception as e:
            printLog(f"⚠️ Error processing {video_path}: {e}", file=sys.stderr)
        finally:
            try:
                os.remove(video_path)
#                printLog(f"🗑️ Deleted {video_path}")
            except Exception as del_err:
                printLog(f"❌ Failed to delete {video_path}: {del_err}", file=sys.stderr)



# ==========================
# Main Execution
# ==========================
if __name__ == "__main__":
    
    load_config() # Loads configuration from settings.conf
    
    parser = argparse.ArgumentParser(description="YOLOv8 Object Detection on Video or Folder Watchdog")
    parser.add_argument("--loop", action="store_true", help="Watch folder and process videos in a loop")
    parser.add_argument("video_path", nargs="?", type=str, help="Path to the video file (optional if using --loop)")
    parser.add_argument("--confidence", type=float, default=CONFIDENCE_THRESHOLD, help="Minimum confidence threshold (0.0 - 1.0)")

    args = parser.parse_args()
    CONFIDENCE_THRESHOLD = args.confidence
    make_folder(ZM_AI_DETECTIONS_DIR)

    if USE_GPU and not torch.cuda.is_available():
        printLog("⚠️ GPU requested but not available — falling back to CPU", file=sys.stderr)

    if args.loop:
        printLog(f"❤️  Starting Yolo on : {ZM_ALARM_QUEUE}")
        watchdog_loop()
    else:
        if not args.video_path or not os.path.isfile(args.video_path):
            printLog(f" ❌ Error: Provide a valid video_path or use --loop mode", file=sys.stderr)
            sys.exit(1)

        model = load_yolo()
        camid, event_id = extract_ids(args.video_path)
        process_video(args.video_path, model, camid, event_id)

