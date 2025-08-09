import time
import os
import io
import sys
import smtplib
import threading
from datetime import datetime, timedelta
import configparser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

config = None

def load_config():
    global config
    config = configparser.ConfigParser()

    # Determine base path depending on whether running as script or PyInstaller .exe
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    settings_path = os.path.join(base_path, "settings.ini")
    email_path = os.path.join(base_path, "email_settings.ini")

    loaded_files = config.read([email_path, settings_path])

    if not loaded_files:
        printLog("❌ No config files loaded.")
        return

    # === GENERAL (from Settings.ini) ===
    globals()["LOG_ENABLE"] = config.getboolean("general", "LOG_ENABLE", fallback=True)
    globals()["LOG_RETENTION_DAYS"] = config.getint("general", "LOG_RETENTION_DAYS", fallback=1)

    # === PATHS (from Settings.ini) ===
    globals()["ZM_AI_DETECTIONS_DIR"] = os.path.join(base_path, config.get("paths", "ZM_AI_DETECTIONS_DIR", fallback="detected_frames"))

    # === OTHER (from Email_settings.ini) ===
    globals()["EMAIL_CAMID"] = config.get("email", "EMAIL_CAMID", fallback="")
    globals()["BATCH_INTERVAL"] = config.getint("email", "BATCH_INTERVAL", fallback=60)
    globals()["EMAIL_SENDER"] = config.get("credentials", "EMAIL_SENDER", fallback="")
    globals()["EMAIL_RECEIVER"] = config.get("credentials", "EMAIL_RECEIVER", fallback="")
    globals()["EMAIL_PASSWORD"] = config.get("credentials", "EMAIL_PASSWORD", fallback="")
    globals()["SMTP_SERVER"] = config.get("credentials", "SMTP_SERVER", fallback="smtp.gmail.com")
    globals()["SMTP_PORT"] = config.getint("credentials", "SMTP_PORT", fallback=587)

# Auto-generate basepath from script name
if getattr(sys, 'frozen', False):
    # Running as a PyInstaller EXE
    base_path = os.path.dirname(sys.executable)
else:
    # Running as a .py script
    base_path = os.path.dirname(os.path.abspath(__file__))

script_basename = os.path.splitext(os.path.basename(sys.argv[0]))[0]
LOG_FILE = os.path.join(base_path, f"{script_basename}.log")

# Store new files in this list before sending
new_files = []
lock = threading.Lock()

# Function to send an email with multiple attachments
def send_email():
    global new_files
    while True:
        time.sleep(int(BATCH_INTERVAL))  # Wait for the batch interval

        with lock:
            if not new_files:  # Skip if no new files
                continue
            files_to_send = new_files[:]
            new_files.clear()  # Reset the list **only after copying**

        subject = f"Batch File Update ({len(files_to_send)} files)"
        body = f"The following new files were created:\n\n" + "\n".join(os.path.basename(f) for f in files_to_send)

        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # Attach each file
        for file_path in files_to_send:
            file_name = os.path.basename(file_path)
            try:
                with open(file_path, "rb") as attachment:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(attachment.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={file_name}")
                    msg.attach(part)
            except Exception as e:
                printLog(f"❌ Error attaching {file_name}: {e}")

        # Send the email
        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()  # Secure the connection
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
                printLog(f"✅ Batch email sent with {len(files_to_send)} files")
        except Exception as e:
            printLog(f"❌ Error sending email: {e}")

# Define event handler for monitoring folder
class WatcherHandler(FileSystemEventHandler):
    def __init__(self, script_start_time):
        self.script_start_time = script_start_time

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = event.src_path
        file_name = os.path.basename(file_path)
        file_creation_time = os.path.getctime(file_path)
#        printLog(f"🔍 New file detected: {file_name}")
        # Only add new files created after the script started that match any CAMID prefix
        if file_creation_time >= self.script_start_time and any(file_name.startswith(prefix) for prefix in CAMID_LIST):
            with lock:
                new_files.append(file_path)
            printLog(f"🆕 File added to batch: {file_name}")

# Start the observer and email sender thread
def start_monitoring():
    script_start_time = time.time()  # Capture script start time
    event_handler = WatcherHandler(script_start_time)
    observer = Observer()
    observer.schedule(event_handler, ZM_AI_DETECTIONS_DIR, recursive=False)

    # Start the batch email sender thread
    email_thread = threading.Thread(target=send_email, daemon=True)
    email_thread.start()

    # Start monitoring
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()

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

# Run the script
if __name__ == "__main__":
    load_config()
    CAMID_LIST = [prefix.strip() + "_" for prefix in EMAIL_CAMID.split(",")]
    printLog(f"🔍 📧 {os.path.basename(__file__)} Cam Id's {EMAIL_CAMID} | Batch setting {BATCH_INTERVAL} sec")
    printLog(f"📂 Watching directory: {ZM_AI_DETECTIONS_DIR}")
    start_monitoring()
