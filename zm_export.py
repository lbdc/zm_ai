# zm_export.py
# -----------------------------------------------------------------------------
# Endpoints:
#   GET /events/summary              → camera list + earliest/latest event info
#   GET /events/videos               → list raw events (links + metadata)
#   GET /events/videos/export        → (A) download+trim  (B) optional concat
#   GET /events/download_counter     → simple polled counter for download/concat
#
# Notes:
#  - Frontend can poll /events/download_counter?job_id=... every ~1s.
#  - Pass &job_id=... to /events/videos/export to enable the counter.
#  - Counter is a tiny JSON file under ./temp; best-effort writes.
# -----------------------------------------------------------------------------

from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Dict, Any, List
from pathlib import Path
import requests
from requests.auth import HTTPBasicAuth
import json, re, subprocess, shutil
import time, math

router = APIRouter()

# =============================================================================
# Small helpers (IDs, counter/progress files)  
# =============================================================================
from pathlib import Path
from typing import Optional
import json, re, subprocess

def _safe_id(s: Optional[str]) -> str:
    """Make a filesystem-safe identifier from arbitrary text."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "")).strip("-")

def _default_temp_dir() -> Path:
    root = Path(__file__).resolve().parent
    t = root / "temp"
    t.mkdir(parents=True, exist_ok=True)
    return t

def _counter_path(job_id: str, temp_dir: Optional[Path] = None) -> Path:
    """
    Path to the JSON counter file for a given job_id.
    Accepts optional temp_dir; defaults to ./temp next to this file.
    """
    tdir = temp_dir or _default_temp_dir()
    return tdir / f"counter_{_safe_id(job_id)}.json"

def _progress_txt_path(job_id: Optional[str], temp_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Path to ffmpeg -progress text file for concat, or None if no job_id.
    Accepts optional temp_dir; defaults to ./temp next to this file.
    """
    if not job_id:
        return None
    tdir = temp_dir or _default_temp_dir()
    return tdir / f"concat_progress_{_safe_id(job_id)}.txt"

def _counter_write(
    job_id: Optional[str],
    data: dict,
    temp_dir: Optional[Path] = None,
    want_concat: Optional[bool] = None
) -> None:
    """
    Write a small JSON snapshot for download/concat counters.
    No-op if job_id is None. Best-effort (swallows errors).
    Atomic write (tmp + replace) to reduce partial-read issues.
    """
    if not job_id:
        return
    try:
        # Ensure required fields exist
        if "phase" not in data:
            data = {**data, "phase": "download"}
        if "status" not in data:
            data = {**data, "status": "running"}

        # Inject want_concat if caller provided it (so UI can be consistent)
        if want_concat is not None:
            data = {**data, "want_concat": bool(want_concat)}
        else:
            # preserve if already present; default False otherwise
            data = {**data, "want_concat": bool(data.get("want_concat", False))}

        # Add continuous overall fields
        data = _with_overall_fields(data)

        # Atomic write to avoid transient truncation reads
        p = _counter_path(job_id, temp_dir)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass  # non-fatal


def _counter_clear(job_id: Optional[str], temp_dir: Optional[Path] = None) -> None:
    """Remove the counter file when completely done (optional)."""
    if not job_id:
        return
    try:
        _counter_path(job_id, temp_dir).unlink()
    except Exception:
        pass

def _ffprobe_duration_seconds(path: Path) -> Optional[float]:
    """Return duration (seconds) of a media file using ffprobe, or None."""
    try:
        res = subprocess.run(
            ["ffprobe","-v","error","-select_streams","v:0",
             "-show_entries","format=duration","-of","default=nw=1:nk=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if res.returncode == 0:
            s = (res.stdout or "").strip()
            return float(s) if s else None
    except Exception:
        pass
    return None

def _run_ffmpeg_with_progress(cmd: list[str],
                              effective_total_duration: float,
                              total_clips: int,
                              job_id: Optional[str],
                              temp_dir: Optional[Path],
                              logs: List[str], want_concat: bool) -> tuple[bool, str]:
    """
    Run ffmpeg with -progress pipe:1 and emit intermediate concat counters.
    `effective_total_duration` must be the *output* duration to expect
    (i.e., input_sum / speed when speed != 1).
    """
    cmd_with_prog = cmd + ["-progress", "pipe:1", "-nostats"]
    logs.append("FFMPEG " + " ".join(cmd_with_prog))

    try:
        proc = subprocess.Popen(
            cmd_with_prog,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
    except Exception as ex:
        return (False, f"spawn failed: {ex}")

    # Guards
    eff_total = float(effective_total_duration) if (effective_total_duration and effective_total_duration > 0) else 1.0
    total_clips = max(1, int(total_clips))

    last_update_ts = 0.0
    last_done = -1  # we’ll only write when done increases
    elapsed_sec = 0.0

    try:
        if proc.stdout:
            for line in proc.stdout:
                line = (line or "").strip()

                if line.startswith("out_time_ms="):
                    # ffmpeg reports OUTPUT timestamp here
                    try:
                        out_ms = float(line.split("=", 1)[1])
                        elapsed_sec = out_ms / 1_000_000.0
                    except Exception:
                        pass

                    now = time.time()
                    if now - last_update_ts > 0.25:  # throttle ~4/sec
                        frac = min(1.0, max(0.0, elapsed_sec / eff_total))
                        # While running, keep done in [0, total_clips-1]
                        done = int(math.floor(frac * total_clips))
                        if done >= total_clips:
                            done = total_clips - 1
                        if done > last_done:
                            _counter_write(job_id, {
                                "phase": "concat",
                                "status": "running",
                                "total": total_clips,
                                "done": done
                            }, temp_dir)
                            last_done = done
                        last_update_ts = now

        # finish
        _, err = proc.communicate()
        ok = (proc.returncode == 0)

        # Final snap: mark as 100% / total_clips
        _counter_write(job_id, {
            "phase": "concat",
            "status": "done" if ok else "error",
            "total": total_clips,
            "done": total_clips
        }, temp_dir, want_concat=want_concat)

        return (ok, (err or "").strip())

    except Exception as ex:
        try:
            proc.kill()
        except Exception:
            pass
        _counter_write(job_id, {
            "phase": "concat",
            "status": "error",
            "total": total_clips,
            "done": max(0, last_done)
        }, temp_dir, want_concat=want_concat)
        return (False, f"progress loop failed: {ex}")

def _with_overall_fields(data: dict) -> dict:
    """
    Adds continuous overall progress fields:
      - want_concat: bool
      - overall_percent: 0..100 (download=0..50, concat=50..100 if want_concat)
      - overall_status: running/done/error
      - overall_text: short human-friendly status
    """
    phase = str(data.get("phase") or "")
    status = str(data.get("status") or "")
    want_concat = bool(data.get("want_concat"))

    # safe numbers
    try:
        total = float(data.get("total") or 0)
    except Exception:
        total = 0.0
    try:
        done = float(data.get("done") or 0)
    except Exception:
        done = 0.0

    frac = (done / total) if total > 0 else 0.0
    frac = max(0.0, min(1.0, frac))

    # continuous overall percent
    if not want_concat:
        overall = frac
    else:
        if phase == "download":
            overall = 0.50 * frac
        elif phase == "concat":
            overall = 0.50 + 0.50 * frac
        else:
            overall = 0.0

    overall_percent = int(round(100.0 * max(0.0, min(1.0, overall))))

    # overall status + text
    if want_concat and phase == "download" and status == "done":
        # Download is complete but job is not complete yet
        overall_status = "running"
        overall_text = f"overall {overall_percent}% — download complete, starting concat…"
    elif phase == "concat" and status == "done":
        overall_status = "done"
        overall_text = "overall 100% — complete"
    elif status == "error":
        overall_status = "error"
        overall_text = f"overall {overall_percent}% — error"
    else:
        overall_status = status or "running"
        if phase == "download":
            overall_text = f"overall {overall_percent}% — downloading {int(done)}/{int(total)}"
        elif phase == "concat":
            mode = data.get("mode") or ""
            mode_txt = f" ({mode})" if mode else ""
            overall_text = f"overall {overall_percent}% — concatenating{mode_txt} {int(done)}/{int(total)}"
        else:
            overall_text = f"overall {overall_percent}% — {phase} {status}".strip()

    return {
        **data,
        "want_concat": want_concat,
        "overall_percent": overall_percent,
        "overall_status": overall_status,
        "overall_text": overall_text,
    }


# =============================================================================
# PUBLIC: Polled counter (simple JSON; no SSE)
# =============================================================================

@router.get("/events/download_counter")
def events_download_counter(job_id: str):
    p = _counter_path(job_id)  # temp_dir optional; default is ./temp
    if not p.exists():
        return {"job_id": job_id, "available": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data["job_id"] = job_id
        data["available"] = True
        return data
    except Exception:
        return {"job_id": job_id, "available": False, "error": "failed_to_read_counter"}



# =============================================================================
# PUBLIC: Camera/event summaries
# =============================================================================

@router.get("/events/summary")
def events_summary(
    ids: Optional[str] = Query(None, description="Comma-separated monitor IDs, e.g. 1,2,5"),
    debug: bool = Query(False, description="Include request log")
):
    """
    Returns cameras with width/height/FPS and earliest/latest finished events.
    """
    import zm_ai  # your module providing ZM_HOST, BAUTH_USER/PWD, token helpers

    zm_ai.load_config()

    logs: List[str] = []
    results: List[Dict[str, Any]] = []

    base = (zm_ai.ZM_HOST or "").rstrip("/")
    user, pwd = zm_ai.BAUTH_USER, zm_ai.BAUTH_PWD
    token = zm_ai.get_saved_token()
    auth = HTTPBasicAuth(user, pwd) if (user and pwd) else None

    if not base:
        payload = {"results": []}
        if debug: payload["debug"] = ["ERR: ZM_HOST not configured"]
        return payload

    def _to_float(x):
        try: return float(str(x).strip())
        except Exception: return None

    def _pick_event(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        evs = data.get("events") or []
        for w in evs:
            e = w.get("Event", {})
            end = str(e.get("EndTime", "")).strip().lower()
            if end and end not in ("null", "none", "0000-00-00 00:00:00"):
                return {"Id": int(e.get("Id", 0)), "StartTime": e.get("StartTime"), "EndTime": e.get("EndTime")}
        if evs:
            e = evs[0].get("Event", {})
            return {"Id": int(e.get("Id", 0)), "StartTime": e.get("StartTime"), "EndTime": e.get("EndTime")}
        return None

    def _fetch_json(url: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(url, auth=auth, verify=False, timeout=15)
            logs.append(f"{r.status_code} {url}")
            if r.ok:
                return r.json()
        except Exception as e:
            logs.append(f"ERR {url} -> {e}")
        return None

    def _parse_monitor_wrap(wrap: Dict[str, Any]) -> Dict[str, Any]:
        m  = wrap.get("Monitor", {}) or {}
        ms = wrap.get("Monitor_Status", {}) or {}
        width  = int(m.get("Width") or 0)
        height = int(m.get("Height") or 0)
        capture_fps  = _to_float(ms.get("CaptureFPS"))
        return {
            "Id": int(m.get("Id", 0)),
            "Name": m.get("Name"),
            "Width": width,
            "Height": height,
            "Resolution": f"{width}:{height}" if width and height else None,
            "CaptureFPS": capture_fps,
        }

    # Build monitor list (either selection or all)
    mon_info: Dict[int, Dict[str, Any]] = {}
    if ids:
        mids = [int(tok.strip()) for tok in ids.split(",") if tok.strip().isdigit()]
        monitors = []
        for mid in mids:
            murl = f"{base}/zm/api/monitors/{mid}.json"
            if token: murl += f"?token={token}"
            md = _fetch_json(murl) or {}
            if md.get("monitor"):
                info = _parse_monitor_wrap(md)
            else:
                info = _parse_monitor_wrap({"Monitor": (md.get("Monitor") or {}), "Monitor_Status": md.get("Monitor_Status") or {}})
            mon_info[mid] = info
            monitors.append({"Id": mid, "Name": info.get("Name")})
    else:
        mon_url = f"{base}/zm/api/monitors.json"
        if token: mon_url += f"?token={token}"
        mon_data = _fetch_json(mon_url) or {}
        wraps = mon_data.get("monitors", [])
        monitors = []
        for w in wraps:
            info = _parse_monitor_wrap(w)
            mon_info[info["Id"]] = info
            monitors.append({"Id": info["Id"], "Name": info.get("Name")})

    # For each monitor, get earliest & latest finished events
    for m in monitors:
        mid = m["Id"]
        asc  = f"{base}/zm/api/events/index/MonitorId:{mid}.json?sort=StartTime&direction=asc&limit=2"
        desc = f"{base}/zm/api/events/index/MonitorId:{mid}.json?sort=StartTime&direction=desc&limit=2"
        if token:
            asc  += f"&token={token}"
            desc += f"&token={token}"

        earliest = _pick_event(_fetch_json(asc) or {})
        latest   = _pick_event(_fetch_json(desc) or {})

        info = mon_info.get(mid, {})
        results.append({
            "Id": mid,
            "Name": m.get("Name"),
            "Width": info.get("Width"),
            "Height": info.get("Height"),
            "Resolution": info.get("Resolution"),
            "CaptureFPS": info.get("CaptureFPS"),
            "Earliest": earliest,
            "Latest": latest,
        })

    payload = {"results": results}
    if debug:
        payload["debug"] = logs
    return payload


# =============================================================================
# PUBLIC: List raw events (ascending)
# =============================================================================

@router.get("/events/videos")
def events_videos(
    monitor_id: int = Query(..., description="Monitor ID (e.g., 1)"),
    start: str = Query(..., description="Inclusive start time: 'YYYY-MM-DD HH:MM:SS' or ISO"),
    end: str = Query(..., description="Inclusive end time: 'YYYY-MM-DD HH:MM:SS' or ISO"),
    chunk: int = Query(200, description="Internal fetch size per page"),
    debug: bool = Query(False, description="Include request log"),
    debug_level: int = Query(1, ge=0, le=2, description="0=off, 1=summary, 2=verbose"),
):
    """
    Return ALL events for a monitor between start/end (inclusive),
    with simple video + JSON links. Internally loops pages until exhausted.
    """
    import zm_ai
    from urllib.parse import quote

    zm_ai.load_config()

    logs = []
    base = (zm_ai.ZM_HOST or "").rstrip("/")
    user, pwd = zm_ai.BAUTH_USER, zm_ai.BAUTH_PWD
    token = zm_ai.get_saved_token()
    auth = HTTPBasicAuth(user, pwd) if (user and pwd) else None

    if not base:
        payload = {"events": []}
        if debug: payload["debug"] = ["ERR: ZM_HOST not configured"]
        return payload

    s = start.replace("T", " ")
    e = end.replace("T", " ")
    s_enc = quote(s, safe="")
    e_enc = quote(e, safe="")

    events_out = []
    page = 1

    while True:
        url = (
            f"{base}/zm/api/events/index"
            f"/MonitorId:{monitor_id}"
            f"/StartTime >=:{s_enc}"
            f"/StartTime <=:{e_enc}.json"
            f"?sort=StartTime&direction=asc&limit={int(chunk)}&page={int(page)}"
        )
        if token:
            url += f"&token={token}"

        try:
            r = requests.get(url, auth=auth, verify=False, timeout=30)
            logs.append(f"{r.status_code} {url}")
            if not r.ok:
                break
            data = r.json() or {}
        except Exception as exc:
            logs.append(f"ERR fetching page {page}: {exc}")
            break

        wraps = data.get("events") or []
        if not wraps:
            break

        for wrap in wraps:
            ev = (wrap or {}).get("Event") or {}
            eid = int(ev.get("Id") or 0)
            if not eid:
                continue

            video_url = f"{base}/zm/index.php?view=video&eid={eid}"
            json_url  = f"{base}/zm/api/events/{eid}.json"
            if token:
                video_url += f"&token={token}"
                json_url  += f"?token={token}"

            events_out.append({
                "EventId": eid,
                "MonitorId": int(ev.get("MonitorId") or monitor_id),
                "StartTime": ev.get("StartTime"),
                "EndTime": ev.get("EndTime"),
                "Length": ev.get("Length"),
                "Frames": ev.get("Frames"),
                "Score": ev.get("MaxScore"),
                "VideoURL": video_url,
                "EventJSON": json_url,
            })

        p = (data.get("pagination") or data.get("Pagination") or {})
        page_count = int(p.get("pageCount") or 0)
        if page_count and page >= page_count:
            break
        if len(wraps) < int(chunk):
            break

        page += 1

    payload = {"events": events_out, "count": len(events_out)}
    if debug:
        payload["debug"] = logs
    return payload


# =============================================================================
# Internals for /events/videos/export
# =============================================================================

def _fmt_secs(x: float) -> str:
    """ffmpeg accepts fractional seconds; format robustly."""
    try:
        return f"{float(x):.3f}"
    except Exception:
        return "0.000"

def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    """Run ffmpeg and return (ok, stderr_excerpt)."""
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode == 0:
        return True, ""
    return False, (res.stderr.decode(errors="ignore")[:400] if res.stderr else "")

def _download_and_trim( *, TEMP_DIR: Path, monitor_id: int, events_out: list[dict], auth, download: bool, trim: bool, logs: list[str], job_id: Optional[str], want_concat: bool ) -> dict:
    """ 
    SECTION A: Download all clips (if download=True). Then trim first/last
    clips to precisely match requested window (if trim=True).

    Returns stats dict:
      attempted, downloaded, skipped_existing, failed, bytes, downloaded_now (list of EIDs in order)
    """
    import requests

    stats = {
        "attempted": 0, "downloaded": 0, "skipped_existing": 0,
        "failed": 0, "bytes": 0, "downloaded_now": []
    }

    if not download or not events_out:
        return stats

    total_to_download = len(events_out)
    _counter_write(job_id, {
        "phase": "download", "status": "starting", "monitor_id": monitor_id,
        "total": total_to_download, "done": 0, "bytes": 0, "current_file": None
    }, TEMP_DIR, want_concat=want_concat)

    for e in events_out:
        eid = e["EventId"]
        fmp4 = TEMP_DIR / f"{monitor_id}-{eid}.mp4"
        ftmp = fmp4.with_suffix(".part")

        _counter_write(job_id, {
            "phase": "download", "status": "downloading", "monitor_id": monitor_id,
            "total": total_to_download, "done": stats["downloaded"],
            "bytes": stats["bytes"], "current_file": fmp4.name
        }, TEMP_DIR, want_concat=want_concat)

        try:
            with requests.get(e["VideoURL"], auth=auth, verify=False, stream=True, timeout=180) as resp:
                stats["attempted"] += 1
                if resp.status_code != 200:
                    stats["failed"] += 1
                    _counter_write(job_id, {
                        "phase": "download", "status": "error", "monitor_id": monitor_id,
                        "total": total_to_download, "done": stats["downloaded"],
                        "bytes": stats["bytes"], "current_file": fmp4.name, "http": resp.status_code
                    }, TEMP_DIR, want_concat=want_concat)
                    continue

                with open(ftmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        stats["bytes"] += len(chunk)
                        _counter_write(job_id, {
                            "phase": "download", "status": "downloading", "monitor_id": monitor_id,
                            "total": total_to_download, "done": stats["downloaded"],
                            "bytes": stats["bytes"], "current_file": fmp4.name
                        }, TEMP_DIR, want_concat=want_concat)

            ftmp.replace(fmp4)
            stats["downloaded_now"].append(eid)
            stats["downloaded"] += 1

            _counter_write(job_id, {
                "phase": "download", "status": "file_done", "monitor_id": monitor_id,
                "total": total_to_download, "done": stats["downloaded"],
                "bytes": stats["bytes"], "current_file": fmp4.name
            }, TEMP_DIR, want_concat=want_concat)

        except Exception as ex:
            logs.append(f"ERR download eid={eid}: {ex}")
            stats["failed"] += 1
            try: ftmp.unlink()
            except Exception: pass
            _counter_write(job_id, {
                "phase": "download", "status": "error", "monitor_id": monitor_id,
                "total": total_to_download, "done": stats["downloaded"],
                "bytes": stats["bytes"], "current_file": fmp4.name, "error": str(ex)
            }, TEMP_DIR, want_concat=want_concat)

    # --- Optional first/last trim (only the files downloaded this run) ---
    if trim and stats["downloaded_now"]:
        has_ffmpeg = shutil.which("ffmpeg") is not None
        if not has_ffmpeg:
            logs.append("ERR: ffmpeg not found on PATH; trimming disabled")
        else:
            first_id = stats["downloaded_now"][0]
            last_id  = stats["downloaded_now"][-1]
            first_e  = next(e for e in events_out if e["EventId"] == first_id)
            last_e   = next(e for e in reversed(events_out) if e["EventId"] == last_id)

            first_fp = TEMP_DIR / f"{monitor_id}-{first_id}.mp4"
            last_fp  = TEMP_DIR / f"{monitor_id}-{last_id}.mp4"

            def _to_secs(x, default=0.0):
                try: return float(x)
                except Exception: return default

            first_off = _to_secs(first_e.get("OffsetSec"), 0.0)
            first_len = _to_secs(first_e.get("Length"), 0.0)
            first_dur = _to_secs(first_e.get("DurationSec"), max(0.0, first_len - first_off))
            last_len  = _to_secs(last_e.get("Length"), 0.0)
            last_dur  = _to_secs(last_e.get("DurationSec"), last_len)

            logs.append(f"INTENT first_eid={first_id} off={first_off:.3f}s dur={first_dur:.3f}s len={first_len:.3f}s")
            logs.append(f"INTENT last_eid={last_id}  keep_dur={last_dur:.3f}s len={last_len:.3f}s")

            if first_id == last_id and first_fp.exists():
                outp = first_fp.with_suffix(".part")
                ok, err = _run_ffmpeg([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", _fmt_secs(first_off), "-i", str(first_fp),
                    "-t",  _fmt_secs(first_dur),
                    "-c", "copy", "-map", "0",
                    "-fflags", "+genpts", "-movflags", "+faststart",
                    "-f", "mp4", str(outp),
                ])
                if ok: outp.replace(first_fp)
                else:  logs.append(f"TRIM_BOTH ERR: {err}")
            else:
                if first_fp.exists() and first_off > 0.01:
                    outp = first_fp.with_suffix(".part")
                    ok, err = _run_ffmpeg([
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", _fmt_secs(first_off), "-i", str(first_fp),
                        "-c", "copy", "-map", "0",
                        "-fflags", "+genpts", "-movflags", "+faststart",
                        "-f", "mp4", str(outp),
                    ])
                    if ok: outp.replace(first_fp)
                    else:  logs.append(f"TRIM_FIRST ERR: {err}")

                if last_fp.exists():
                    needs_tail_cut = (last_len - last_dur) > 0.25 and last_dur > 0.01
                    if needs_tail_cut:
                        outp = last_fp.with_suffix(".part")
                        ok, err = _run_ffmpeg([
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-i", str(last_fp),
                            "-t", _fmt_secs(last_dur),
                            "-c", "copy", "-map", "0",
                            "-fflags", "+genpts", "-movflags", "+faststart",
                            "-f", "mp4", str(outp),
                        ])
                        if ok: outp.replace(last_fp)
                        else:  logs.append(f"TRIM_LAST ERR: {err}")

    # mark the counter as done for the download phase
    _counter_write(job_id, {
        "phase": "download", "status": "done", "monitor_id": monitor_id,
        "total": len(events_out) if download else 0,
        "done": stats["downloaded"], "bytes": stats["bytes"], "current_file": None
    }, TEMP_DIR, want_concat=want_concat)
    return stats

def _concat_downloads(
    *, TEMP_DIR: Path, monitor_id: int, s_in: str, e_in: str,
    downloaded_now: List[int], speed: float, fps: Optional[int], size: Optional[str],
    use_gpu: bool, logs: List[str], job_id: Optional[str], want_concat: bool
) -> dict:
    """
    SECTION B: Concatenate the downloaded clips (copy or re-encode).
    Emits intermediate counter updates using ffmpeg -progress pipe:1.
    """
    import glob

    concat_info = {
        "enabled": False, "path": None, "bytes": 0,
        "mode": None, "list": None, "encoder": None, "device": None,
        "audio": {"present": None, "mode": None}
    }

    def _parse_size(s: Optional[str]):
        if not s: return None
        t = s.replace("x", ":").strip()
        parts = t.split(":")
        if len(parts) == 2:
            try:
                w = int(parts[0]); h = int(parts[1])
                if w > 0 and h > 0: return w, h
            except Exception:
                return None
        return None

    def _detect_gpu_encoder() -> tuple[str, Optional[str]]:
        try:
            res = subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-encoders"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            enc_list = res.stdout or ""
        except Exception:
            enc_list = ""
        if "h264_nvenc" in enc_list:
            return ("h264_nvenc", None)
        if "h264_vaapi" in enc_list:
            devs = sorted(glob.glob("/dev/dri/renderD*"))
            if devs:
                return ("h264_vaapi", devs[0])
        return ("libx264", None)

    def _ffprobe_has_audio(path: Path) -> bool:
        if shutil.which("ffprobe") is None or not path.exists():
            return False
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            out = (res.stdout or "").strip().lower()
            return ("audio" in out)
        except Exception:
            return False

    # ---- basic checks ----
    if not downloaded_now:
        logs.append("CONCAT: no local clips found; skipping")
        _counter_write(job_id, {
            "phase": "concat", "status": "skipped",
            "monitor_id": monitor_id, "total": 0, "done": 0
        }, TEMP_DIR)
        return concat_info

    if shutil.which("ffmpeg") is None:
        logs.append("ERR: ffmpeg not found on PATH; concat disabled")
        _counter_write(job_id, {
            "phase": "concat", "status": "error",
            "monitor_id": monitor_id, "total": len(downloaded_now), "done": 0,
            "error": "ffmpeg not found"
        }, TEMP_DIR)
        return concat_info

    # Build concat list in order of download
    clip_paths: List[Path] = []
    for eid in downloaded_now:
        p = TEMP_DIR / f"{monitor_id}-{eid}.mp4"
        if p.exists():
            clip_paths.append(p)
    if not clip_paths:
        logs.append("CONCAT: no local clips exist; skipping")
        _counter_write(job_id, {
            "phase": "concat", "status": "skipped",
            "monitor_id": monitor_id, "total": 0, "done": 0
        }, TEMP_DIR)
        return concat_info

    # Prepare list file
    list_name = f"concat_m{monitor_id}_{_safe_id(s_in)}_to_{_safe_id(e_in)}.txt"
    list_path = TEMP_DIR / list_name
    with list_path.open("w", encoding="utf-8") as lf:
        for p in clip_paths:
            lf.write(f"file '{p.as_posix()}'\n")
    out_path = TEMP_DIR / f"concat_m{monitor_id}_{_safe_id(s_in)}_to_{_safe_id(e_in)}.mp4"
    concat_info["list"] = str(list_path)
    concat_info["list_url"] = f"/zm_ai/temp/{list_path.name}"
    concat_info["path_url"] = f"/zm_ai/temp/{out_path.name}"

    # Mode decision
    want_speed = (abs(float(speed) - 1.0) > 1e-6)
    want_fps   = (fps is not None and int(fps) > 0)
    sz         = _parse_size(size)
    want_size  = (sz is not None)

    has_audio = _ffprobe_has_audio(clip_paths[0])
    concat_info["audio"]["present"] = bool(has_audio)

    # Compute total concatenated duration (sum of inputs)
    total_seconds = 0.0
    for p in clip_paths:
        d = _ffprobe_duration_seconds(p)
        if d:
            total_seconds += d

    # effective output duration accounts for speed (setpts)
    effective_total_seconds = total_seconds
    try:
        s = float(speed)
        if s > 0:
            effective_total_seconds = total_seconds / s
    except Exception:
        pass


    # Announce ffmpeg start (frontend shows mode + done/total)
    mode_label = "copy" if (not want_speed and not want_fps and not want_size) else "reencode"
    _counter_write(job_id, {
        "phase": "concat", "status": "running",
        "monitor_id": monitor_id,
        "total": len(clip_paths), "done": 0,
        "mode": mode_label,
        "total_seconds": int(total_seconds)
    }, TEMP_DIR)

    # Build the ffmpeg command (single shot)
    if not want_speed and not want_fps and not want_size:
        # COPY mode
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart",
            str(out_path),
        ]
        concat_info["mode"] = "copy"
        concat_info["encoder"] = "copy"
        concat_info["audio"]["mode"] = "copy" if has_audio else "none"
    else:
        # RE-ENCODE mode (optional GPU)
        encoder, device = ("libx264", None)
        if use_gpu:
            encoder, device = _detect_gpu_encoder()
        concat_info["mode"] = "reencode"
        concat_info["encoder"] = encoder
        concat_info["device"]  = device

        vfilters: List[str] = []
        if want_speed:
            vfilters.append(f"setpts=PTS/{float(speed):.6f}")
        if want_fps:
            vfilters.append(f"fps={int(fps)}")

        af_chain = ""
        if want_speed and has_audio:
            s = float(speed); chain = []
            if s > 1.0:
                while s > 2.0 + 1e-6: chain.append(2.0); s /= 2.0
                chain.append(s)
            else:
                while s < 0.5 - 1e-6: chain.append(0.5); s /= 0.5
                chain.append(s)
            af_chain = ",".join([f"atempo={x:.6f}" for x in chain])

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(list_path)]

        if encoder == "h264_nvenc":
            if want_size:
                w, h = sz; vfilters.append(f"scale={w}:{h}:flags=lanczos")
            vf = ",".join(vfilters) if vfilters else "null"
            cmd += ["-filter:v", vf, "-c:v", "h264_nvenc", "-preset", "p4"]
        elif encoder == "h264_vaapi" and concat_info["device"]:
            pre = ",".join(vfilters) if vfilters else None
            va = ["format=nv12", "hwupload"]
            if want_size:
                w, h = sz; va.append(f"scale_vaapi={w}:{h}")
            vf = ",".join(([pre] if pre else []) + va)
            cmd += ["-vaapi_device", concat_info["device"], "-filter:v", vf, "-c:v", "h264_vaapi", "-qp", "20"]
        else:
            if want_size:
                w, h = sz; vfilters.append(f"scale={w}:{h}:flags=lanczos")
            vf = ",".join(vfilters) if vfilters else "null"
            cmd += ["-filter:v", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

        if has_audio:
            if af_chain:
                cmd += ["-filter:a", af_chain, "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
                concat_info["audio"]["mode"] = "reencode_atempo"
            else:
                cmd += ["-c:a", "copy"]
                concat_info["audio"]["mode"] = "copy"
        else:
            cmd += ["-an"]; concat_info["audio"]["mode"] = "none"

        cmd += ["-movflags", "+faststart", str(out_path)]

    # Run ffmpeg with progress mapping → intermediate counter updates
    ok, err = _run_ffmpeg_with_progress(
        cmd,
        effective_total_duration=effective_total_seconds,
        total_clips=len(clip_paths),
        job_id=job_id,
        temp_dir=TEMP_DIR,
        logs=logs, want_concat=want_concat)

    if not ok:
        logs.append(f"CONCAT ERR({concat_info['mode']}): {err[:400]}")
        _counter_write(job_id, {
            "phase": "concat", "status": "error",
            "monitor_id": monitor_id, "total": len(clip_paths), "done": 0,
            "error": err[:200]
        }, TEMP_DIR)
        return concat_info

    # Success
    concat_info["enabled"] = True
    concat_info["path"] = str(out_path)
    try:
        concat_info["bytes"] = int(out_path.stat().st_size)
    except Exception:
        pass

    _counter_write(job_id, {
        "phase": "concat", "status": "done",
        "monitor_id": monitor_id, "total": len(clip_paths), "done": len(clip_paths),
        "mode": concat_info["mode"], "bytes": concat_info.get("bytes", 0)
    }, TEMP_DIR)

    # --- Auto-delete original clips + list after successful concatenation ---
    try:
        for p in clip_paths:
            if p.exists():
                p.unlink()
        if list_path.exists():
            list_path.unlink()
    except Exception as ex:
        logs.append(f"Cleanup error: {ex}")

    return concat_info


# =============================================================================
# PUBLIC: Export = (A) download/trim  + (B) optional concat
# =============================================================================

@router.get("/events/videos/export")
def events_videos_export(
    monitor_id: int = Query(..., description="Monitor ID (e.g., 1)"),
    start: str = Query(..., description="Inclusive start time: 'YYYY-MM-DD HH:MM:SS' or ISO"),
    end: str = Query(..., description="Inclusive end time: 'YYYY-MM-DD HH:MM:SS' or ISO"),
    chunk: int = Query(200, description="Internal fetch size per page"),
    download: bool = Query(False, description="Also download MP4s to ./temp/"),
    debug: bool = Query(False, description="Include request log"),
    debug_level: int = Query(1, ge=0, le=2, description="0=off, 1=summary, 2=verbose"),
    buffer: int = Query(2, description="Seconds of padding around start/end; also used as min overlap"),
    trim: bool = Query(True, description="Trim to [start,end] overlap using ffmpeg (stream copy)"),
    concat: bool = Query(False, description="Concatenate downloaded clips into one file"),
    speed: float = Query(1.0, description="Playback speed factor (1.0=normal, >1 faster)"),
    fps: Optional[int] = Query(None, description="Output FPS (re-encode)"),
    size: Optional[str] = Query(None, description="Output WxH, e.g. 1920:1080 or 1920x1080 (re-encode)"),
    use_gpu: bool = Query(True, description="Auto-use NVENC/VAAPI for re-encode if available"),
    job_id: str | None = Query(None),
):
    """
    Collect events that overlap the requested window.
    SECTION A: Download (and optionally trim first/last).
    SECTION B: (Optional) Concatenate the downloaded clips.
    """
    try:
        import zm_ai
        from urllib.parse import quote
        from datetime import datetime, timedelta

        def _parse_dt(s: str) -> datetime:
            s = (s or "").strip().replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    pass
            return datetime.fromisoformat(s.replace(" ", "T"))

        def _hms(total_seconds: float) -> str:
            total_seconds = int(max(0, total_seconds))
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            s = total_seconds % 60
            return f"{h:02d}:{m:02d}:{s:02d}"

        # --- config/auth ---
        zm_ai.load_config()
        logs: List[str] = []
        base = (zm_ai.ZM_HOST or "").rstrip("/")
        user, pwd = zm_ai.BAUTH_USER, zm_ai.BAUTH_PWD
        token = zm_ai.get_saved_token()
        auth = HTTPBasicAuth(user, pwd) if (user and pwd) else None

        if not base:
            payload = {
                "monitor_id": monitor_id,
                "requested": {"start": start, "end": end, "span_seconds": 0, "span_hms": "00:00:00"},
                "results": {"count": 0, "coverage": {"first_start": None, "last_end": None, "span_seconds": 0, "span_hms": "00:00:00"}},
                "saved": {"path": None, "bytes": 0},
                "videos": {"attempted": 0, "downloaded": 0, "skipped_existing": 0, "failed": 0, "bytes": 0, "dir": None}
            }
            if debug: payload["debug"] = ["ERR: ZM_HOST not configured"]
            return payload

        # normalize inputs
        s_in = start.replace("T", " ")
        e_in = end.replace("T", " ")
        s_enc = quote(s_in, safe=""); e_enc = quote(e_in, safe="")
        dt_start = _parse_dt(s_in); dt_end = _parse_dt(e_in)

        BUF = timedelta(seconds=int(max(0, buffer)))
        dt_start_adj = dt_start - BUF
        dt_end_adj   = dt_end + BUF

        # paths
        PROJECT_ROOT = Path(__file__).resolve().parent
        TEMP_DIR = PROJECT_ROOT / "temp"
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        want_concat = bool(download and concat)

        # --- Collect events (ascending) overlapping [start, end] (with buffer) ---
        events_out: List[dict] = []
        page = 1
        last_ev_start_in_page = None

        pages_hit = 0

        while True:
            url = (
                f"{base}/zm/api/events/index"
                f"/MonitorId:{monitor_id}"
                f"/StartTime <=:{e_enc}.json"
                f"?sort=StartTime&direction=asc&limit={int(chunk)}&page={int(page)}"
            )
            if token:
                url += f"&token={token}"

            try:
                r = requests.get(url, auth=auth, verify=False, timeout=30)
                if debug and debug_level >= 2:
                    logs.append(f"{r.status_code} {url}")
                if not r.ok:
                    break

                data = r.json() or {}
                pages_hit += 1
            except Exception as exc:
                logs.append(f"ERR fetching page {page}: {exc}")
                break

            wraps = data.get("events") or []
            if not wraps:
                break

            # restore per-wrap processing (collect overlapping events and compute clip cuts)
            for wrap in wraps:
                ev = (wrap or {}).get("Event") or {}
                eid = int(ev.get("Id") or 0)
                if not eid:
                    continue

                ev_start_raw = (ev.get("StartTime") or "").replace("T", " ")
                if not ev_start_raw:
                    continue
                ev_start = _parse_dt(ev_start_raw)
                last_ev_start_in_page = ev_start  # keep for early-stop logic

                ev_end_raw = (ev.get("EndTime") or "").strip()
                if not ev_end_raw:
                    continue  # skip ongoing/null EndTime
                ev_end = _parse_dt(ev_end_raw.replace("T", " "))

                # must overlap buffered window
                overlaps = (ev_start <= dt_end_adj) and (ev_end >= dt_start_adj)
                if not overlaps:
                    continue

                # require minimum actual intersection >= buffer seconds (filters tiny edge clips)
                overlap_start = max(ev_start, dt_start)
                overlap_end   = min(ev_end, dt_end)
                overlap_secs  = max(0.0, (overlap_end - overlap_start).total_seconds())
                if overlap_secs + 1e-6 < float(BUF.total_seconds()):
                    continue

                video_url = f"{base}/zm/index.php?view=view_video&eid={eid}"
                json_url  = f"{base}/zm/api/events/{eid}.json"
                if token:
                    video_url += f"&token={token}"
                    json_url  += f"?token={token}"

                # compute clip cut for first/last trimming
                clip_start_dt = max(ev_start, dt_start)
                clip_end_dt   = min(ev_end, dt_end)
                offset_secs   = max(0.0, (clip_start_dt - ev_start).total_seconds())
                duration_secs = max(0.0, (clip_end_dt - clip_start_dt).total_seconds())
                if duration_secs <= 0:
                    continue

                events_out.append({
                    "EventId": eid,
                    "MonitorId": int(ev.get("MonitorId") or monitor_id),
                    "StartTime": ev.get("StartTime"),
                    "EndTime": ev.get("EndTime"),
                    "Length": ev.get("Length"),
                    "Frames": ev.get("Frames"),
                    "Score": ev.get("MaxScore"),
                    "VideoURL": video_url,
                    "EventJSON": json_url,
                    "ClipStart": clip_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "ClipEnd":   clip_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "OffsetSec": round(offset_secs, 3),
                    "DurationSec": round(duration_secs, 3),
                })

            p = (data.get("pagination") or data.get("Pagination") or {})
            page_now   = int(p.get("page") or p.get("current") or page)
            page_count = int(p.get("pageCount") or 0)
            if last_ev_start_in_page and last_ev_start_in_page >= dt_end_adj:
                break
            if page_count and page_now >= page_count:
                break
            page += 1

        # after the while True loop:
        if debug and debug_level >= 1:
            logs.append(f"API pages fetched: {pages_hit}")

        # summaries
        def _hms_span(a, b): return _hms(max(0.0, (b - a).total_seconds()))
        count = len(events_out)
        req_span_hms = _hms_span(dt_start, dt_end)
        req_span_sec = max(0.0, (dt_end - dt_start).total_seconds())

        cov_first = cov_last = None
        cov_span_sec = 0.0
        if events_out:
            def _to_dt(zm_ts): 
                return _parse_dt((zm_ts or "").replace("T", " "))
            starts = [_to_dt(e.get("StartTime")) for e in events_out if e.get("StartTime")]
            ends   = [_to_dt(e.get("EndTime") or e.get("StartTime")) for e in events_out]
            if starts and ends:
                cov_first = min(starts); cov_last = max(ends)
                cov_span_sec = max(0.0, (cov_last - cov_first).total_seconds())
        cov_span_hms = _hms(cov_span_sec)

        # save full event list for reference/debug
        fname_json = f"events_m{monitor_id}_{_safe_id(s_in)}_to_{_safe_id(e_in)}.json"
        fpath_json = TEMP_DIR / fname_json
        fpath_json.write_text(json.dumps({"events": events_out, "count": count}, ensure_ascii=False, indent=2), encoding="utf-8")
        json_bytes = fpath_json.stat().st_size
        saved = {"path": str(fpath_json), "bytes": int(json_bytes), "path_url": f"/zm_ai/temp/{fpath_json.name}"}

        # ===== SECTION A: Download + optional first/last trim =====
        dl_stats = _download_and_trim(
            TEMP_DIR=TEMP_DIR, monitor_id=monitor_id, events_out=events_out,
            auth=auth, download=download, trim=trim, logs=logs, job_id=job_id
        , want_concat=want_concat)

        # ===== SECTION B: Concatenate (optional) =====
        concat_info = {
            "enabled": False, "path": None, "bytes": 0, "mode": None, "list": None,
            "encoder": None, "device": None, "audio": {"present": None, "mode": None}
        }
        if download and concat:
            concat_info = _concat_downloads(
                TEMP_DIR=TEMP_DIR, monitor_id=monitor_id, s_in=s_in, e_in=e_in,
                downloaded_now=dl_stats["downloaded_now"],
                speed=speed, fps=fps, size=size, use_gpu=use_gpu, logs=logs,
                job_id=job_id,
                want_concat=want_concat
            )

        payload = {
            "monitor_id": monitor_id,
            "requested": {
                "start": s_in, "end": e_in,
                "span_seconds": int(req_span_sec), "span_hms": req_span_hms,
            },
            "results": {
                "count": count,
                "coverage": {
                    "first_start": cov_first.strftime("%Y-%m-%d %H:%M:%S") if cov_first else None,
                    "last_end":   cov_last.strftime("%Y-%m-%d %H:%M:%S") if cov_last else None,
                    "span_seconds": int(cov_span_sec), "span_hms": cov_span_hms,
                },
            },
            "saved": saved,
            "videos": {
                "attempted": dl_stats["attempted"] if download else 0,
                "downloaded": dl_stats["downloaded"] if download else 0,
                "skipped_existing": dl_stats["skipped_existing"] if download else 0,
                "failed": dl_stats["failed"] if download else 0,
                "bytes": int(dl_stats["bytes"] if download else 0),
                "dir": str(TEMP_DIR),
                "enabled": bool(download),
                "concat": concat_info,
            }
        }   
        if debug:
            payload["debug"] = logs

        return payload

    finally:
        # rename counter JSON file, even on errors or early returns
        if job_id:
            try:
                TEMP_DIR = _default_temp_dir()
                (_counter_path(job_id, TEMP_DIR)).replace(
                    TEMP_DIR / f"counter_m{monitor_id}_{_safe_id(s_in)}_to_{_safe_id(e_in)}.json"
                )
            except Exception:
                pass

# =============================================================================
# PUBLIC: List finished concats (for UI table)
# =============================================================================
@router.get("/events/concat_index")
def events_concat_index():
    TEMP_DIR = _default_temp_dir()
    items: List[Dict[str, Any]] = []

    for mp4 in TEMP_DIR.glob("concat_*.mp4"):
        base = mp4.stem                             # e.g., concat_m3_2025-01-01_to_2025-01-02
        txt  = mp4.with_suffix(".txt")              # ffmpeg concat list
        js   = TEMP_DIR / (base.replace("concat_", "events_") + ".json")  # our saved events json (best-effort)
        log  = TEMP_DIR / (base + ".log")           # if you write concat logs here (optional)

        size_bytes = 0
        try: size_bytes = mp4.stat().st_size
        except Exception: pass

        dur_sec = _ffprobe_duration_seconds(mp4) or None

        items.append({
            "base_name": base,
            "mp4": str(mp4),
            "list": str(txt) if txt.exists() else None,
            "list_url": f"/zm_ai/temp/{txt.name}" if txt.exists() else None,
            "json": str(js) if js.exists() else None,
            "json_url": f"/zm_ai/temp/{js.name}" if js.exists() else None,  
            "log":  str(log) if log.exists() else None,
            "size_bytes": int(size_bytes),
            "length_sec": float(dur_sec) if dur_sec is not None else None,
            "status": "done",
        })

    # Sort by mtime desc
    items.sort(key=lambda it: Path(it["mp4"]).stat().st_mtime if it.get("mp4") else 0, reverse=True)
    return {"items": items}


# =============================================================================
# PUBLIC: Delete a concat set (mp4 + sidecars) by base name
# =============================================================================
@router.post("/events/files/delete")
def events_files_delete(base: str = Query(..., description="Base name without extension, e.g., concat_m10_...")):
    TEMP_DIR = _default_temp_dir()

    # sanitize and strip any accidental extension
    b = _safe_id(base).replace(".mp4", "").replace(".json", "")

    # expect "concat_<suffix>" → extract "<suffix>"
    suffix = b[len("concat_"):] if b.startswith("concat_") else b

    targets = [
        TEMP_DIR / f"concat_{suffix}.mp4",
        TEMP_DIR / f"counter_{suffix}.json",
        TEMP_DIR / f"events_{suffix}.json",
    ]

    deleted: list[str] = []
    for p in targets:
        try:
            if p.exists():
                p.unlink()
                deleted.append(str(p))
        except Exception:
            pass

    if not deleted:
        raise HTTPException(status_code=404, detail="Nothing deleted")
    return {"ok": True, "deleted": deleted}


