"""
Elbow Lane Day Camp — Bus Route Optimizer
Flask web application with fleet builder UI and in-app route viewer.
Run with: python app.py
"""

import os, uuid, threading, json
from flask import Flask, request, jsonify, send_file, render_template_string, send_from_directory
from bus_route_optimizer import (
    generate_routes, parse_students_csv, parse_vehicles_text,
    cluster_and_route, Stop, Vehicle
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

jobs: dict = {}
jobs_lock = threading.Lock()

os.makedirs("uploads", exist_ok=True)
os.makedirs("outputs", exist_ok=True)


# ── Serialise route data for JSON API ─────────────────────────────────────────

def vehicles_to_json(vehicles: list) -> list:
    out = []
    for v in vehicles:
        out.append({
            "name":            v.name,
            "start_address":   v.start_address,
            "capacity":        v.capacity,
            "rider_count":     v.rider_count,
            "stop_count":      v.stop_count,
            "utilization_pct": v.utilization_pct,
            "total_time":      v.total_time,
            "total_distance":  v.total_distance,
            "under_threshold": v.under_threshold,
            "corridor":        v.corridor,
            "start_lat":       v.start_lat,
            "start_lon":       v.start_lon,
            "camp_lat":        getattr(v, "camp_lat", 40.2454),
            "camp_lon":        getattr(v, "camp_lon", -75.1407),
            "stops": [
                {
                    "stop_num":    i + 1,
                    "address":     s.address,
                    "rider_names": s.rider_names,
                    "rider_count": s.rider_count,
                    "drive_time":  s.drive_time,
                    "lat":         s.lat,
                    "lon":         s.lon,
                }
                for i, s in enumerate(v.stops)
            ],
        })
    return out


# ── Background worker ──────────────────────────────────────────────────────────

def run_job(job_id: str, csv_text: str, vehicles_text: str,
           camp_address: str = None, trip_direction: str = "morning"):
    output_path = os.path.join("outputs", f"routes_{job_id}.xlsx")

    def progress(msg: str):
        with jobs_lock:
            jobs[job_id]["progress"].append(msg)

    try:
        with jobs_lock:
            jobs[job_id]["status"] = "running"

        students = parse_students_csv(csv_text)
        vcfgs    = parse_vehicles_text(vehicles_text)
        vehicles = cluster_and_route(students, vcfgs, progress,
                                        camp_address=camp_address,
                                        trip_direction=trip_direction)

        # Save Excel
        from openpyxl import Workbook
        from bus_route_optimizer import build_dashboard, build_vehicle_sheet
        wb = Workbook()
        build_dashboard(wb, vehicles, camp_address=camp_address, trip_direction=trip_direction)
        for veh in vehicles:
            build_vehicle_sheet(wb, veh, camp_address=camp_address, trip_direction=trip_direction)
        wb.save(output_path)

        progress("✅  Excel saved")

        camp_lat_val = vehicles[0].camp_lat if vehicles else CAMP_COORDS[0]
        camp_lon_val = vehicles[0].camp_lon if vehicles else CAMP_COORDS[1]
        with jobs_lock:
            jobs[job_id]["status"]        = "done"
            jobs[job_id]["output_path"]   = output_path
            jobs[job_id]["route_data"]    = vehicles_to_json(vehicles)
            jobs[job_id]["camp_address"]  = camp_address
            jobs[job_id]["trip_direction"]= trip_direction
            jobs[job_id]["camp_lat"]      = camp_lat_val
            jobs[job_id]["camp_lon"]      = camp_lon_val

    except Exception as e:
        import traceback
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"]  = str(e)
        progress(f"❌ Error: {e}")


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    key = os.environ.get("GOOGLE_MAPS_KEY", "")
    return render_template_string(HTML.replace(
        '"{{ google_maps_key }}"', f'"{key}"'
    ))

@app.route("/logo.png")
def serve_logo():
    """Serve the logo from the same directory as app.py."""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "logo.png")


@app.route("/api/run", methods=["POST"])
def api_run():
    csv_file      = request.files.get("csv_file")
    vehicles_text = request.form.get("vehicles_text", "").strip()

    if not csv_file:
        return jsonify({"error": "No CSV file uploaded"}), 400
    if not vehicles_text:
        return jsonify({"error": "No fleet configuration provided"}), 400

    csv_text      = csv_file.read().decode("utf-8-sig", errors="replace")
    camp_address  = request.form.get("camp_address", "").strip() or None
    trip_direction = request.form.get("trip_direction", "morning")

    try:
        students = parse_students_csv(csv_text)
        if not students:
            # Check if it's a missing-header issue vs empty file
            first_line = csv_text.strip().split("\n")[0].lower() if csv_text.strip() else ""
            has_name   = "name" in first_line
            has_addr   = "address" in first_line or "street" in first_line
            has_zip    = "zip" in first_line or "postal" in first_line
            if not (has_name and has_addr):
                return jsonify({"error":
                    "Could not find required columns. Make sure your CSV has a header row "
                    "with column names like: Last name, First name, Address, City, Zip. "
                    "The first row must contain column headers, not student data."}), 400
            return jsonify({"error": "No students found in CSV. The file may be empty."}), 400
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {e}"}), 400

    try:
        vcfgs = parse_vehicles_text(vehicles_text)
        if not vcfgs:
            return jsonify({"error": "No vehicles parsed. Check fleet configuration."}), 400
    except Exception as e:
        return jsonify({"error": f"Fleet config error: {e}"}), 400

    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "status":       "queued",
            "progress":     [f"✓ Loaded {len(students)} students, {len(vcfgs)} vehicles"],
            "output_path":  None,
            "route_data":   None,
            "error":        None,
            "camp_address": camp_address,
            "trip_direction": trip_direction,
        }

    threading.Thread(
        target=run_job,
        args=(job_id, csv_text, vehicles_text, camp_address, trip_direction),
        daemon=True
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/clear-cache", methods=["POST"])
def api_clear_cache():
    """Delete geocache and routecache so all addresses are re-geocoded fresh."""
    import os
    cleared = []
    for f in ["geocache.json", "routecache.json"]:
        if os.path.exists(f):
            os.remove(f)
            cleared.append(f)
    return jsonify({"cleared": cleared, "message": f"Cleared {len(cleared)} cache files. Next run will re-geocode all addresses using Google Maps."})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":     job["status"],
        "progress":   job["progress"],
        "error":      job.get("error"),
        "route_data": job.get("route_data"),
    })


@app.route("/api/recalculate/<job_id>", methods=["POST"])
def api_recalculate(job_id: str):
    """
    Accept manually edited route data, recompute drive times via Google,
    regenerate the Excel file, and return updated route_data + download.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    edited_vehicles = request.json.get("vehicles", [])
    if not edited_vehicles:
        return jsonify({"error": "No vehicle data provided"}), 400

    try:
        from bus_route_optimizer import (
            build_dashboard, build_vehicle_sheet,
            route_leg_times, haversine_mi, CAMP_COORDS
        )
        from openpyxl import Workbook
        import dataclasses

        camp_address  = job.get("camp_address")  or "828 Elbow Lane, Warrington, PA 18976"
        trip_direction = job.get("trip_direction") or "morning"

        # Simple stop class for recalculate (avoids Stop dataclass property conflicts)
        class EditableStop:
            def __init__(self, d):
                self.address     = d["address"]
                self.rider_names = d.get("rider_names", "")
                self.rider_count = int(d.get("rider_count", 0))
                self.lat         = float(d.get("lat", 0) or 0)
                self.lon         = float(d.get("lon", 0) or 0)
                self.drive_time  = d.get("drive_time", "—")
                self.geocoded    = True

        # Reconstruct Vehicle objects from edited JSON
        vehicles = []
        for vd in edited_vehicles:
            stops = [EditableStop(sd) for sd in vd.get("stops", [])
                     if sd.get("rider_names") and int(sd.get("rider_count", 0)) > 0]

            from bus_route_optimizer import Vehicle, CAMP_COORDS as CC
            veh = Vehicle(
                name          = vd["name"],
                start_address = vd["start_address"],
                capacity      = vd["capacity"],
                stops         = stops,
                total_time    = vd.get("total_time", "—"),
                total_distance= vd.get("total_distance", "—"),
                under_threshold = vd.get("under_threshold", False),
                start_lat     = vd.get("start_lat", 0.0),
                start_lon     = vd.get("start_lon", 0.0),
                camp_lat      = vd.get("camp_lat", CC[0]),
                camp_lon      = vd.get("camp_lon", CC[1]),
            )

            # Resequence stops geographically (nearest-neighbour toward camp)
            if len(veh.stops) > 1:
                camp_lat = veh.camp_lat or CC[0]
                camp_lon = veh.camp_lon or CC[1]
                unvisited = list(veh.stops)
                is_afternoon = trip_direction == "afternoon"
                first = (min if is_afternoon else max)(
                    unvisited,
                    key=lambda s: haversine_mi(s.lat, s.lon, camp_lat, camp_lon)
                )
                ordered = [first]
                unvisited.remove(first)
                while unvisited:
                    last = ordered[-1]
                    cur_d2c = haversine_mi(last.lat, last.lon, camp_lat, camp_lon)
                    best, best_score = None, float("inf")
                    for s in unvisited:
                        geo = haversine_mi(last.lat, last.lon, s.lat, s.lon)
                        d2c = haversine_mi(s.lat, s.lon, camp_lat, camp_lon)
                        pen = max(0, d2c - cur_d2c) * 0.5 if not is_afternoon else max(0, cur_d2c - d2c) * 0.5
                        if geo + pen < best_score:
                            best_score, best = geo + pen, s
                    ordered.append(best)
                    unvisited.remove(best)
                veh.stops = ordered

            # Recalculate drive times
            if veh.stops and (veh.start_lat or veh.start_lon):
                coord_seq = ([(veh.start_lat, veh.start_lon)]
                             + [(s.lat, s.lon) for s in veh.stops]
                             + [(veh.camp_lat or CC[0], veh.camp_lon or CC[1])])
                legs = route_leg_times(coord_seq)
                for i, stop in enumerate(veh.stops):
                    mins = max(1, round(legs[i]))
                    stop.drive_time = f"{mins} min from start" if i == 0 else f"{mins} min"
                total_mins = round(sum(legs))
                hrs, rem = divmod(total_mins, 60)
                veh.total_time = f"{hrs} hr {rem} min" if hrs and rem else (f"{hrs} hr" if hrs else f"{total_mins} min")
                total_mi = sum(
                    haversine_mi(coord_seq[i][0], coord_seq[i][1],
                                 coord_seq[i+1][0], coord_seq[i+1][1]) * 1.35
                    for i in range(len(coord_seq)-1)
                )
                veh.total_distance = f"{round(total_mi, 1)} mi"

            cap = veh.capacity
            eff = 0.40 if cap <= 6 else (0.50 if cap <= 9 else 0.60)
            veh.under_threshold = (veh.rider_count / cap < eff) if cap else False
            vehicles.append(veh)

        # Regenerate Excel
        output_path = os.path.join("outputs", f"routes_{job_id}_edited.xlsx")
        wb = Workbook()
        build_dashboard(wb, vehicles, camp_address=camp_address, trip_direction=trip_direction)
        for veh in vehicles:
            build_vehicle_sheet(wb, veh, camp_address=camp_address, trip_direction=trip_direction)
        wb.save(output_path)

        # Update job with new data
        with jobs_lock:
            jobs[job_id]["output_path"] = output_path
            jobs[job_id]["route_data"]  = vehicles_to_json(vehicles)

        return jsonify({
            "status":     "ok",
            "route_data": jobs[job_id]["route_data"],
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/download/<job_id>")
def api_download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(
        job["output_path"],
        as_attachment=True,
        download_name="elbow_lane_routes.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚌</text></svg>">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Elbow Lane — Bus Route Optimizer</title>
<script>
// Google Maps API key injected from server
window.GOOGLE_MAPS_KEY = "{{ google_maps_key }}";
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Roboto+Slab:wght@600;700;800&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root {
  --brand:      #6D1F2F;
  --brand-dark: #4a1520;
  --brand-mid:  #9e3347;
  --brand-light:#f5e6e9;
  --gold:       #c9a84c;
  --gold-lt:    #f0d98a;
  --ink:        #1a1018;
  --mist:       #f8f4f5;
  --border:     #e8dde0;
  --success:    #2d6a4f;
  --warn:       #b36a00;
  --r:          12px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',sans-serif;background:var(--mist);color:var(--ink);min-height:100vh}

/* header */
header{background:var(--brand);color:#fff;padding:0 2rem;display:flex;align-items:center;gap:1.25rem;height:80px;box-shadow:0 2px 16px rgba(109,31,47,.35);position:sticky;top:0;z-index:200}
.h-logo{width:60px;height:60px;flex-shrink:0;border-radius:50%;background-image:url("data:image/png;base64,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NOyeJ29G8ZBX1WFaburqGoEMzj5NIeOfmH6X7VmgIcA5pDgeIIOQULpY3kbqnnmuKCKUQ7QoUqEAUqFKAhFKhASoUogChEQBSoRASoRSgIRFKAhERAFKKEBKIiAhEUoAiKEBKhSiAIiIAoUogIRSiAhFKICFKIgIUoiAhSiICEUogCIiAIiICFKIgChSiAhSigkNaXOIAHEknACA+dVURUtO+eZwaxgySsCu1dJcK1078hvJjc53R3Lmajuzq+cwxOIpozw4+mfpfsXUIVPVL/p5dHD3V8wiIhEBERAEREAREQBERAEREAREQBERAFkOlrv1LxQ1T8RO/e3k+ie4+H/Xqx5EN9vcTt6inA2aeHAqFjOmb1gNoqx/hHIT/RKyZC52t1C5htw/0SoREOkKVCIAiIhglQiIZCIiAIiIAiIgCIiAcEREMBERDIREQBERAEREBKjsREBKIiAIoRASoREBKhEQEooRASiKEBKIiAIoRASoUqEBKhSiAhSoRASiKEBPM8FieqLwZnuoaV46ppxI9p9M9w8PrX01Les71FRycOUr29vgPtWNIVzVNS2s0aT739AiIhXwiIgCIiAIiIAiIgCIiAIiIAiIgCIiAIiIAsl07fMblHWu4cmSk8vA/tWNIh0W1zUtp7cP9mzcIsTsF+dCW0tc8uiwGskPNnge8fUsraQ5oc0ggjII7QhcbW7p3MNqHigpUIh1EqE7UQEqFKhASihEBKKEQEooRASihEBKKEQEooRASihEBKKEQEooRASihEBKIoQEooUoAihEBKKEQEooRASihEBKKFKAIoRASihEBKKEQEooRxDWlziA0DJJ5AICVjGo75nfo6J3Dk+UHn3gftXxv9+dMXUtC8tiwWvkHN/gO4fWsfQrmpaptZpUX3v7BERCvhERAEREAREQBERAEREAREQBERAEREAREQBERAEREAXb2O9zULmwzF0lNyx2s9X7F1CIbaNadGe3B4ZsimnhqYWywSNkjPJwX1WvrXcam3zb8LvNJ89h5OWZ2q6U1xiLojuPHpRuPEftCFssdShcrZe6XV9jmoilCSIUoiAIoRASihEBKIiAhSiICEREAREQEqFKjsQBSoUoAoUqEBKhSoQEoiIAiIgIU5REBClEQBQpRAERQgJREQBERAERQgJRFwbtdKa3RgynfkPoxtPE/sCHipUjSi5T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");background-size:90%;background-position:center;background-repeat:no-repeat;background-color:var(--brand-dark)}
.h-title{font-family:'Roboto Slab',serif;font-size:1.25rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase}
.h-sub{font-size:.72rem;opacity:.75;font-weight:400;margin-top:2px;letter-spacing:.08em;text-transform:uppercase}
.h-badge{margin-left:auto;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);color:#fff;font-size:.68rem;font-family:'Roboto Slab',serif;font-weight:500;letter-spacing:.12em;text-transform:uppercase;padding:.35rem .9rem;border-radius:20px;white-space:nowrap}

/* tabs */
.tab-bar{display:flex;background:#fff;border-bottom:2px solid var(--border);position:sticky;top:80px;z-index:100}
.tab{padding:.85rem 1.75rem;font-size:.82rem;font-weight:500;font-family:'Roboto Slab',serif;letter-spacing:.07em;text-transform:uppercase;color:#999;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;transition:color .15s,border-color .15s;white-space:nowrap;display:flex;align-items:center;gap:.5rem}
.tab:hover{color:var(--brand-mid)}
.tab.active{color:var(--brand);border-bottom-color:var(--brand)}
.tab-badge{background:var(--brand);color:#fff;font-size:.65rem;font-weight:700;padding:.15rem .45rem;border-radius:10px;min-width:18px;text-align:center}

/* container */
.container{max-width:960px;margin:0 auto;padding:2rem 1.5rem 4rem}
.tab-panel{display:none}.tab-panel.active{display:block}

/* cards */
.card{background:#fff;border:1px solid var(--border);border-radius:var(--r);padding:1.5rem 1.75rem;margin-bottom:1.1rem;box-shadow:0 1px 4px rgba(0,0,0,.04);transition:box-shadow .2s}
.card:hover{box-shadow:0 3px 12px rgba(109,31,47,.07)}
.card-hd{display:flex;align-items:center;gap:.7rem;margin-bottom:1.1rem}
.card-num{width:26px;height:26px;background:var(--brand);color:#fff;border-radius:50%;font-size:.75rem;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.card-title{font-family:'Roboto Slab',serif;font-size:1.05rem;font-weight:700;color:var(--brand-dark);letter-spacing:.01em;text-transform:uppercase}
.card-hint{font-size:.75rem;color:#999;margin-top:.15rem;font-weight:300}
label.lbl{display:block;font-size:.75rem;font-weight:600;color:var(--brand-dark);letter-spacing:.04em;text-transform:uppercase;margin-bottom:.4rem}

/* drop zone */
.drop-zone{border:2px dashed var(--border);border-radius:var(--r);padding:1.75rem;text-align:center;cursor:pointer;transition:all .2s;background:var(--mist);position:relative}
.drop-zone:hover,.drop-zone.drag-over{border-color:var(--brand-mid);background:var(--brand-light)}
.drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-icon{font-size:2rem;margin-bottom:.4rem}
.drop-text{font-size:.88rem;color:#666}.drop-text strong{color:var(--brand)}
.drop-meta{font-size:.72rem;color:#bbb;margin-top:.3rem}
.file-chosen{display:none;align-items:center;gap:.7rem;padding:.65rem .9rem;background:#edfaf3;border:1px solid #a3d9b8;border-radius:8px;margin-top:.6rem;font-size:.83rem;color:var(--success);font-weight:500}
.file-chosen.visible{display:flex}
.file-chosen .rm{margin-left:auto;cursor:pointer;font-size:.9rem;color:#999;background:none;border:none;padding:0 .2rem}

/* fleet builder */
.fleet-builder{display:flex;flex-direction:column;gap:.6rem}
.fleet-row{display:grid;grid-template-columns:110px 1fr 120px auto;gap:.6rem;align-items:center;background:var(--mist);border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;transition:background .15s}
.fleet-row:hover{background:var(--brand-light)}
.fleet-row select,.fleet-row input{border:1.5px solid var(--border);border-radius:6px;padding:.45rem .6rem;font-size:.83rem;font-family:'DM Sans',sans-serif;color:var(--ink);background:#fff;transition:border-color .15s;width:100%}
.fleet-row select:focus,.fleet-row input:focus{outline:none;border-color:var(--brand-mid);background:#fff}
.fleet-row .rm-row{background:none;border:none;cursor:pointer;color:#bbb;font-size:1.1rem;padding:.2rem;line-height:1;transition:color .15s;flex-shrink:0}
.fleet-row .rm-row:hover{color:var(--brand)}
.fleet-col-label{font-size:.7rem;font-weight:600;color:#999;letter-spacing:.05em;text-transform:uppercase;margin-bottom:.3rem;display:grid;grid-template-columns:110px 1fr 120px 32px;gap:.6rem;padding:0 .8rem}
.add-vehicle-btn{display:flex;align-items:center;gap:.5rem;padding:.6rem 1.1rem;background:none;border:1.5px dashed var(--border);border-radius:8px;color:var(--brand-mid);font-size:.83rem;font-weight:600;cursor:pointer;transition:all .15s;width:100%;justify-content:center;margin-top:.2rem}
.add-vehicle-btn:hover{border-color:var(--brand-mid);background:var(--brand-light)}
.fleet-summary{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:.75rem}
.fleet-chip{background:var(--brand-light);border:1px solid #d4a0aa;border-radius:20px;padding:.3rem .85rem;font-size:.75rem;font-weight:500;color:var(--brand-dark)}

/* run button */
.trip-btn{padding:.5rem 1.25rem;border:1.5px solid var(--border);border-radius:8px;background:#fff;color:#888;font-family:'Roboto Slab',serif;font-size:.78rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;cursor:pointer;transition:all .15s;white-space:nowrap}
.trip-btn.active{background:var(--brand);border-color:var(--brand);color:#fff}
.trip-btn:hover:not(.active){border-color:var(--brand-mid);color:var(--brand-mid)}
.run-btn{width:100%;padding:.95rem 2rem;background:var(--brand);color:#fff;border:none;border-radius:var(--r);font-family:'Roboto Slab',serif;font-size:1.05rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:.65rem;transition:background .18s,transform .1s,box-shadow .18s;box-shadow:0 4px 14px rgba(109,31,47,.3);margin-top:1.25rem}
.run-btn:hover:not(:disabled){background:var(--brand-dark);box-shadow:0 6px 20px rgba(109,31,47,.4);transform:translateY(-1px)}
.run-btn:disabled{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}

/* progress */
#prog-panel{display:none;background:#1a1018;border-radius:var(--r);padding:1.1rem 1.4rem;margin-top:1.1rem;border:1px solid #2d1e24}
#prog-panel.visible{display:block}
.prog-hd{display:flex;align-items:center;gap:.65rem;margin-bottom:.75rem;padding-bottom:.65rem;border-bottom:1px solid #2d1e24}
.prog-title{font-size:.82rem;font-weight:600;color:#e0d4d8;letter-spacing:.06em;text-transform:uppercase}
.spinner{width:15px;height:15px;border:2px solid rgba(255,255,255,.15);border-top-color:var(--gold);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.pbar-wrap{background:rgba(255,255,255,.08);border-radius:4px;height:3px;margin-bottom:.65rem;overflow:hidden}
.pbar{height:100%;background:linear-gradient(90deg,var(--brand-mid),var(--gold));width:0%;transition:width .4s ease}
#log{font-family:monospace;font-size:.76rem;line-height:1.65;color:#c4b5bb;max-height:220px;overflow-y:auto}
#log .ok{color:#6fcf97}#log .warn{color:#f2c94c}#log .err{color:#eb5757}

/* action bar */
.action-bar{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1.1rem}
.dl-btn{display:inline-flex;align-items:center;gap:.55rem;padding:.75rem 1.5rem;background:var(--gold);color:#1a1018;border-radius:8px;text-decoration:none;font-weight:700;font-size:.9rem;transition:background .15s,transform .1s;box-shadow:0 3px 10px rgba(201,168,76,.35);border:none;cursor:pointer}
.dl-btn:hover{background:var(--gold-lt);transform:translateY(-1px)}
.view-btn{display:inline-flex;align-items:center;gap:.55rem;padding:.75rem 1.5rem;background:var(--brand);color:#fff;border-radius:8px;font-weight:600;font-size:.9rem;border:none;cursor:pointer;transition:background .15s}
.view-btn:hover{background:var(--brand-dark)}

/* error */
#error-card{display:none;background:#2d0d13;border:1px solid #6d1f2f;border-radius:var(--r);padding:1.1rem 1.4rem;margin-top:1.1rem;color:#f5c2cb;font-size:.85rem}
#error-card.visible{display:block}
#error-card strong{display:block;margin-bottom:.35rem;font-size:.95rem}

/* ── RESULTS TAB ── */
.results-empty{text-align:center;padding:4rem 2rem;color:#bbb}
.unassigned-tray{background:#fff8e6;border:1.5px dashed var(--gold);border-radius:var(--r);padding:1rem 1.25rem;margin-bottom:1rem;display:none}
.unassigned-tray.visible{display:block}
.unassigned-title{font-family:'Roboto Slab',serif;font-size:.85rem;font-weight:700;color:#7a4f00;margin-bottom:.65rem;text-transform:uppercase;letter-spacing:.04em}
.unassigned-list{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:.75rem}
.unassigned-chip{display:flex;align-items:center;gap:.4rem;background:#fff3cd;border:1px solid #f0c060;border-radius:20px;padding:.3rem .75rem;font-size:.78rem;font-weight:500;color:#7a4f00}
.unassigned-chip select{border:none;background:transparent;font-size:.75rem;color:#7a4f00;cursor:pointer;padding:0 .2rem;font-weight:600}
.reassign-btn{padding:.25rem .65rem;background:var(--brand);color:#fff;border:none;border-radius:6px;font-size:.72rem;font-weight:600;cursor:pointer;transition:background .15s}
.reassign-btn:hover{background:var(--brand-dark)}
.rider-remove{background:none;border:none;cursor:pointer;color:#bbb;font-size:.8rem;padding:0 .1rem;line-height:1;transition:color .15s;margin-left:.15rem}
.rider-remove:hover{color:var(--brand)}
.recalc-bar{display:none;align-items:center;gap:.75rem;padding:.65rem 1rem;background:#edfaf3;border:1px solid #a3d9b8;border-radius:8px;margin-bottom:.75rem;font-size:.82rem;color:var(--success)}
.recalc-bar.visible{display:flex}
.recalc-btn{padding:.4rem 1rem;background:var(--success);color:#fff;border:none;border-radius:6px;font-size:.8rem;font-weight:600;cursor:pointer;margin-left:auto;transition:background .15s}
.recalc-btn:hover{background:#1a4f38}
.recalc-btn:disabled{opacity:.5;cursor:not-allowed}
.results-empty .empty-icon{font-size:3rem;margin-bottom:1rem}
.results-empty p{font-size:.9rem;line-height:1.6}

/* dashboard summary table */
.summary-table{width:100%;border-collapse:collapse;font-size:.83rem}
.summary-table th{background:var(--brand);color:#fff;padding:.6rem .9rem;text-align:left;font-family:'Roboto Slab',serif;font-weight:500;font-size:.82rem;letter-spacing:.08em;text-transform:uppercase}
.summary-table th:first-child{border-radius:6px 0 0 0}
.summary-table th:last-child{border-radius:0 6px 0 0}
.summary-table td{padding:.6rem .9rem;border-bottom:1px solid var(--border);vertical-align:middle}
.summary-table tr:last-child td{border-bottom:none}
.summary-table tr:nth-child(even) td{background:var(--mist)}
.summary-table tr.warn td{background:#fff8e6}
.util-bar-wrap{background:#eee;border-radius:4px;height:8px;width:80px;overflow:hidden;display:inline-block;vertical-align:middle;margin-right:.4rem}
.util-bar{height:100%;border-radius:4px}
.util-ok{background:#2d6a4f}
.util-warn{background:#b36a00}
.badge{display:inline-block;padding:.15rem .55rem;border-radius:10px;font-size:.7rem;font-weight:600}
.badge-ok{background:#edfaf3;color:var(--success)}
.badge-warn{background:#fff3cd;color:var(--warn)}

/* vehicle accordion */
.veh-list{display:flex;flex-direction:column;gap:.6rem;margin-top:1rem}
.veh-card{background:#fff;border:1px solid var(--border);border-radius:var(--r);overflow:hidden;transition:box-shadow .2s}
.veh-card:hover{box-shadow:0 3px 12px rgba(109,31,47,.08)}
.veh-card.warn-card{border-color:#f0c060}
.veh-header{display:flex;align-items:center;gap:1rem;padding:.9rem 1.1rem;cursor:pointer;user-select:none}
.veh-header:hover{background:var(--mist)}
.veh-name{font-family:'Roboto Slab',serif;font-size:1rem;font-weight:600;color:var(--brand-dark);min-width:90px;letter-spacing:.04em;text-transform:uppercase}
.veh-corridor{font-size:.78rem;color:#888;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.veh-stats{display:flex;align-items:center;gap:.75rem;margin-left:auto;flex-shrink:0}
.veh-stat{font-size:.78rem;color:#666;white-space:nowrap}
.veh-stat strong{color:var(--ink)}
.veh-chevron{color:#bbb;transition:transform .2s;font-size:.85rem;flex-shrink:0}
.veh-card.open .veh-chevron{transform:rotate(180deg)}
.veh-body{display:none;padding:0 1.1rem 1.1rem;border-top:1px solid var(--border)}
.veh-card.open .veh-body{display:block}
.veh-map{width:100%;height:280px;border-radius:8px;margin-bottom:.9rem;border:1px solid var(--border);background:var(--mist);position:relative;z-index:1}
.map-loading{display:flex;align-items:center;justify-content:center;height:100%;font-size:.82rem;color:#aaa;gap:.5rem}

/* stop list */
.stop-table{width:100%;border-collapse:collapse;font-size:.8rem;margin-top:.75rem}
.stop-table th{font-size:.7rem;font-weight:600;color:#999;letter-spacing:.05em;text-transform:uppercase;padding:.4rem .6rem;border-bottom:2px solid var(--border);text-align:left}
.stop-table td{padding:.55rem .6rem;border-bottom:1px solid #f0eaec;vertical-align:top}
.stop-table tr:last-child td{border-bottom:none}
.stop-table tr:hover td{background:var(--mist)}
.stop-num{font-weight:700;color:var(--brand);width:40px}
.stop-addr{font-weight:500;color:var(--ink)}
.stop-city{font-size:.72rem;color:#999;margin-top:.1rem}
.stop-riders{color:#555}
.stop-time{color:#888;white-space:nowrap}
.stop-row-start td,.stop-row-arrive td{background:var(--brand-light)!important}
.rider-pill{display:inline-flex;align-items:center;gap:.25rem;background:var(--brand-light);color:var(--brand-dark);border-radius:10px;padding:.1rem .5rem;font-size:.72rem;font-weight:500;margin:.1rem .15rem .1rem 0}
.rider-remove{background:none;border:none;cursor:pointer;color:var(--brand-mid);font-size:.75rem;line-height:1;padding:0;opacity:.6;transition:opacity .15s}
.rider-remove:hover{opacity:1}


.unassigned-pill{display:inline-flex;align-items:center;gap:.4rem;background:#fff3cd;border:1px solid #f0c060;border-radius:8px;padding:.3rem .7rem;font-size:.78rem;font-weight:500;color:#7a4f00}
.unassigned-pill select{border:none;background:transparent;font-size:.75rem;color:#7a4f00;cursor:pointer;outline:none;font-family:'DM Sans',sans-serif}
.unassigned-pill .assign-btn{background:var(--brand);color:#fff;border:none;border-radius:6px;padding:.2rem .55rem;font-size:.7rem;font-weight:600;cursor:pointer;transition:background .15s}
.unassigned-pill .assign-btn:hover{background:var(--brand-dark)}
.recalc-btn{display:inline-flex;align-items:center;gap:.5rem;padding:.6rem 1.25rem;background:#2d6a4f;color:#fff;border:none;border-radius:8px;font-family:'Roboto Slab',serif;font-size:.82rem;font-weight:600;cursor:pointer;transition:background .15s;letter-spacing:.03em;text-transform:uppercase}
.recalc-btn:hover{background:#1e4f3a}
.recalc-btn:disabled{opacity:.5;cursor:not-allowed}
.edit-bar{display:flex;align-items:center;gap:.75rem;margin-bottom:.75rem;flex-wrap:wrap}
.edit-hint{font-size:.75rem;color:#999;font-style:italic}
.stop-row-start .stop-num{color:var(--brand-mid)}
.stop-row-arrive .stop-num{color:var(--success);font-weight:700}
.stop-row-arrive .stop-time{color:var(--success);font-weight:600}
.rider-pill{display:inline-block;background:var(--brand-light);color:var(--brand-dark);border-radius:10px;padding:.1rem .5rem;font-size:.72rem;font-weight:500;margin:.1rem .15rem .1rem 0}

/* totals row */
.summary-totals{background:var(--brand)!important;color:#fff}
.summary-totals td{color:#fff!important;font-weight:700;border-bottom:none!important}

/* responsive */
@media(max-width:640px){
  .fleet-row{grid-template-columns:1fr 1fr;grid-template-rows:auto auto}
  .fleet-col-label{display:none}
  .veh-stats{display:none}
  .tab span:not(.tab-badge){display:none}
  header{padding:0 1rem;gap:.75rem;height:64px}
  .h-logo{width:46px;height:46px}
  .h-title{font-size:1rem}
  .h-sub{display:none}
  .h-badge{display:none}
  .container{padding:1rem .75rem 3rem}
  .card{padding:1.1rem 1rem}
  .trip-settings-grid{grid-template-columns:1fr !important;gap:.75rem}
  .trip-btn{padding:.6rem .9rem;font-size:.75rem}
  .run-btn{font-size:.95rem}
  .summary-table{font-size:.72rem}
  .summary-table th,.summary-table td{padding:.4rem .5rem}
  .veh-header{flex-wrap:wrap;gap:.5rem}
  .veh-corridor{display:none}
}
</style>
</head>
<body>

<header>
  <div class="h-logo" role="img" aria-label="Elbow Lane Day Camp"></div>
  <div>
    <div class="h-title">Elbow Lane Day Camp</div>
    <div class="h-sub">Bus Route Optimizer</div>
  </div>
  <span class="h-badge">Route Planner</span>
</header>

<div class="tab-bar">
  <div class="tab active" data-tab="setup">⚙️ <span>Setup</span></div>
  <div class="tab" data-tab="results">📊 <span>Results</span> <span class="tab-badge" id="results-badge" style="display:none">0</span></div>
</div>

<div class="container">

<!-- ══════════════ SETUP TAB ══════════════ -->
<div class="tab-panel active" id="tab-setup">

  <!-- Step 0: Camp location + trip direction -->
  <div class="card">
    <div class="card-hd">
      <span class="card-num" style="background:var(--gold);color:#1a1018">★</span>
      <div>
        <div class="card-title">Trip Settings</div>
        <div class="card-hint">Set the camp location and whether this is a morning or afternoon run</div>
      </div>
    </div>

    <div class="trip-settings-grid" style="display:grid;grid-template-columns:1fr auto;gap:1rem;align-items:start">
      <div>
        <label class="lbl" for="camp-address">Camp Address</label>
        <input type="text" id="camp-address"
               value="828 Elbow Lane, Warrington, PA 18976"
               placeholder="828 Elbow Lane, Warrington, PA 18976"
               style="width:100%;padding:.6rem .85rem;border:1.5px solid var(--border);border-radius:8px;font-family:'DM Sans',sans-serif;font-size:.85rem;color:var(--ink);background:var(--mist);transition:border-color .15s;box-sizing:border-box"
               onfocus="this.style.borderColor='var(--brand-mid)';this.style.background='#fff'"
               onblur="this.style.borderColor='var(--border)';this.style.background='var(--mist)'">
        <div style="font-size:.72rem;color:#aaa;margin-top:.35rem">
          This is the destination for morning routes and the starting point for afternoon routes
        </div>
      </div>
      <div>
        <label class="lbl">Trip Direction</label>
        <div style="display:flex;gap:.5rem">
          <button class="trip-btn active" id="btn-morning" onclick="setTrip('morning')">
            Morning
          </button>
          <button class="trip-btn" id="btn-afternoon" onclick="setTrip('afternoon')">
            Afternoon
          </button>
        </div>
        <div id="trip-hint" style="font-size:.72rem;color:#aaa;margin-top:.35rem;max-width:160px">
          Students travel <strong>to camp</strong>
        </div>
      </div>
    </div>
  </div>

  <!-- Step 1: CSV -->
  <div class="card">
    <div class="card-hd">
      <span class="card-num">1</span>
      <div>
        <div class="card-title" id="roster-title">Morning Roster</div>
        <div class="card-hint">Upload the CSV exported from your camp management software</div>
      </div>
    </div>
    <div class="drop-zone" id="drop-zone">
      <input type="file" id="csv-file" accept=".csv">
      <div class="drop-icon">📋</div>
      <div class="drop-text"><strong>Click to choose</strong> or drag & drop your CSV file</div>
      <div class="drop-meta">Required columns: Last name  |  First name  |  Address  |  City  |  Zip</div>
    </div>
    <div class="file-chosen" id="file-chosen">
      <span>✅</span>
      <span id="file-name">—</span>
      <span id="file-rows" style="color:#888;font-weight:400"></span>
      <button class="rm" id="remove-file">✕</button>
    </div>
  </div>

  <!-- Step 2: Fleet builder -->
  <div class="card">
    <div class="card-hd">
      <span class="card-num">2</span>
      <div>
        <div class="card-title">Fleet Configuration</div>
        <div class="card-hint">Add each vehicle, its starting address, and capacity</div>
      </div>
    </div>

    <div class="fleet-col-label">
      <span>Vehicle</span><span>Starting Address</span><span>Capacity</span><span></span>
    </div>

    <div class="fleet-builder" id="fleet-builder"></div>

    <button class="add-vehicle-btn" id="add-vehicle-btn">
      ＋ Add Vehicle
    </button>

    <div class="fleet-summary" id="fleet-summary"></div>
    <div id="capacity-warning" style="display:none;margin-top:.75rem;background:#fff3cd;border:1px solid #f0c060;border-radius:8px;padding:.75rem 1rem;font-size:.84rem;color:#7a4f00;display:flex;align-items:center;gap:.6rem">
      <span style="font-size:1.1rem">⚠️</span>
      <span id="capacity-warning-msg"></span>
    </div>
  </div>

  <!-- Run -->
  <button class="run-btn" id="run-btn" disabled>
    <span id="run-icon">🗺️</span>
    <span id="run-label">Generate Route Plan</span>
  </button>

  <!-- Progress -->
  <div id="prog-panel">
    <div class="prog-hd">
      <div class="spinner" id="spinner"></div>
      <span class="prog-title" id="prog-title">Optimizing routes…</span>
    </div>
    <div class="pbar-wrap"><div class="pbar" id="pbar"></div></div>
    <div id="log"></div>
  </div>

  <!-- Actions (shown after done) -->
  <div class="action-bar" id="action-bar" style="display:none">
    <a class="dl-btn" id="dl-link" href="#" download>⬇ Download Excel</a>
    <button class="view-btn" id="view-results-btn">📊 View Results</button>
  </div>

  <!-- Cache warning -->
  <div id="cache-notice" style="display:none;margin-top:.6rem;background:#fff3cd;border:1px solid #f0c060;border-radius:8px;padding:.6rem 1rem;font-size:.78rem;color:#7a4f00">
    ✅ Cache cleared — next run will re-geocode all addresses with Google Maps.
  </div>
  <div style="text-align:right;margin-top:.4rem">
    <button onclick="clearCache()" style="background:none;border:none;color:#aaa;font-size:.72rem;cursor:pointer;text-decoration:underline">
      Clear geocache (fixes wrong map locations)
    </button>
  </div>

  <!-- Error -->
  <div id="error-card">
    <strong>Something went wrong</strong>
    <span id="error-msg"></span>
  </div>

</div><!-- /setup tab -->


<!-- ══════════════ RESULTS TAB ══════════════ -->
<div class="tab-panel" id="tab-results">

  <div id="results-empty" class="results-empty">
    <div class="empty-icon">🗺️</div>
    <p>No routes generated yet.<br>Go to <strong>Setup</strong> and click <em>Generate Route Plan</em>.</p>
  </div>
  <div id="results-stale" style="display:none">
    <div style="background:var(--brand-light);border:1px solid #d4a0aa;border-radius:8px;padding:.6rem 1rem;font-size:.78rem;color:var(--brand-dark);margin-bottom:1rem;display:flex;align-items:center;gap:.5rem">
      <span>📋</span>
      <span>Showing results from your last run on <strong id="last-run-date"></strong> — generate a new plan to update</span>
    </div>
  </div>

  <div id="results-content" style="display:none">

    <!-- Summary dashboard -->
    <div class="card" id="summary-card">
      <div class="card-hd">
        <span style="font-size:1.3rem">📊</span>
        <div>
          <div class="card-title">Route Summary</div>
          <div class="card-hint" id="summary-hint"></div>
        </div>
        <a class="dl-btn" id="dl-link-2" href="#" download style="margin-left:auto;padding:.5rem 1rem;font-size:.8rem">⬇ Excel</a>
      </div>
      <div style="overflow-x:auto">
        <table class="summary-table" id="summary-table">
          <thead>
            <tr>
              <th>Vehicle</th>
              <th>Route Corridor</th>
              <th>Riders</th>
              <th>Utilization</th>
              <th>Stops</th>
              <th>Drive Time</th>
              <th>Distance</th>
            </tr>
          </thead>
          <tbody id="summary-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- Recalculate bar -->
    <div class="recalc-bar" id="recalc-bar">
      <span>✏️ You have unsaved changes — recalculate to update times and Excel</span>
      <button class="recalc-btn" id="recalc-btn" onclick="recalculate()">Recalculate Routes</button>
    </div>

    <!-- Unassigned students tray -->
    <div class="unassigned-tray" id="unassigned-tray">
      <div class="unassigned-title">⚠ Unassigned Students</div>
      <div class="unassigned-list" id="unassigned-list"></div>
    </div>

    <!-- Vehicle route cards -->
    <div class="veh-list" id="veh-list"></div>

  </div>

</div><!-- /results tab -->

</div><!-- /container -->

<script>
// ── Constants ──────────────────────────────────────────────────────────────
const VEHICLE_NAMES = ['Vehicle A','Vehicle B','Vehicle C','Vehicle D','Vehicle E',
  'Vehicle F','Vehicle G','Vehicle H','Vehicle I','Vehicle J','Vehicle K','Vehicle L'];
const CAPACITIES = [3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,24,26,28,30,40,50];
const DEFAULT_FLEET = [
  {name:'Vehicle A', address:'', capacity:5},
  {name:'Vehicle B', address:'', capacity:13},
];

// ── State ──────────────────────────────────────────────────────────────────
let csvFile = null;
let currentJobId = null;
let pollTimer = null;
let lastLineCount = 0;
let routeData = null;

// ── Tabs ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});

// ── Fleet Builder ──────────────────────────────────────────────────────────
const builder = document.getElementById('fleet-builder');
let fleet = JSON.parse(JSON.stringify(DEFAULT_FLEET));

function renderFleet() {
  builder.innerHTML = '';
  fleet.forEach((veh, i) => {
    const row = document.createElement('div');
    row.className = 'fleet-row';
    row.innerHTML = `
      <select data-idx="${i}" data-field="name">
        ${VEHICLE_NAMES.map(n => `<option value="${n}" ${n===veh.name?'selected':''}>${n}</option>`).join('')}
      </select>
      <input type="text" placeholder="e.g. 828 Elbow Lane, Warrington, PA"
             value="${veh.address}" data-idx="${i}" data-field="address">
      <select data-idx="${i}" data-field="capacity">
        ${CAPACITIES.map(c => `<option value="${c}" ${c===veh.capacity?'selected':''}>${c} riders</option>`).join('')}
      </select>
      <button class="rm-row" data-idx="${i}" title="Remove">✕</button>
    `;
    builder.appendChild(row);
  });
  updateFleetSummary();
  updateRunBtn();
}

builder.addEventListener('change', e => {
  const idx = +e.target.dataset.idx;
  const field = e.target.dataset.field;
  if (field === 'capacity') {
    fleet[idx][field] = parseInt(e.target.value);
  } else {
    fleet[idx][field] = e.target.value;
  }
  updateFleetSummary();
  updateRunBtn();
});

builder.addEventListener('input', e => {
  const idx = +e.target.dataset.idx;
  const field = e.target.dataset.field;
  if (field === 'address') {
    fleet[idx].address = e.target.value;
    updateRunBtn();
  }
});

builder.addEventListener('click', e => {
  if (e.target.classList.contains('rm-row')) {
    const idx = +e.target.dataset.idx;
    if (fleet.length > 1) {
      fleet.splice(idx, 1);
      renderFleet();
    }
  }
});

document.getElementById('add-vehicle-btn').addEventListener('click', () => {
  // Pick next unused name
  const used = new Set(fleet.map(v => v.name));
  const next = VEHICLE_NAMES.find(n => !used.has(n)) || `Vehicle ${fleet.length + 1}`;
  fleet.push({name: next, address: '', capacity: 13});
  renderFleet();
});

function updateFleetSummary() {
  const summary = document.getElementById('fleet-summary');
  const total = fleet.reduce((s, v) => s + v.capacity, 0);
  const filled = fleet.filter(v => v.address.trim()).length;
  const seatsOk = studentCount === 0 || total >= studentCount;
  summary.innerHTML = `
    <span class="fleet-chip">🚌 ${fleet.length} vehicles</span>
    <span class="fleet-chip" style="${!seatsOk ? 'background:#fde8e8;border-color:#e07070;color:#7a1f1f' : ''}">
      💺 ${total} total seats${studentCount > 0 ? ' / ' + studentCount + ' needed' : ''}
    </span>
    <span class="fleet-chip" style="${filled < fleet.length ? 'background:#fff3cd;border-color:#f0c060' : ''}">
      📍 ${filled}/${fleet.length} addresses entered
    </span>
  `;
  checkCapacity();
}

function checkCapacity() {
  const total = fleet.reduce((s, v) => s + v.capacity, 0);
  const warning = document.getElementById('capacity-warning');
  const msg = document.getElementById('capacity-warning-msg');
  if (studentCount > 0 && total < studentCount) {
    const needed = studentCount - total;
    msg.textContent = `Not enough seats — you have ${total} seats for ${studentCount} students. Add ${needed} more seat${needed !== 1 ? 's' : ''} by increasing vehicle capacities or adding another vehicle.`;
    warning.style.display = 'flex';
  } else {
    warning.style.display = 'none';
  }
  updateRunBtn();
}

// ── Fleet → text format for backend ───────────────────────────────────────
function fleetToText() {
  return fleet.map(v =>
    `${v.name}: Start: ${v.address || '828 Elbow Lane, Warrington, PA'} - Capacity: ${v.capacity} riders`
  ).join('\n');
}

// ── CSV upload ─────────────────────────────────────────────────────────────
const dropZone   = document.getElementById('drop-zone');
const csvInput   = document.getElementById('csv-file');
const fileChosen = document.getElementById('file-chosen');

let studentCount = 0;
let tripDirection = "morning";

function setTrip(dir) {
  tripDirection = dir;
  document.getElementById('btn-morning').classList.toggle('active', dir === 'morning');
  document.getElementById('btn-afternoon').classList.toggle('active', dir === 'afternoon');
  const hint = document.getElementById('trip-hint');
  const title = document.getElementById('roster-title');
  if (dir === 'afternoon') {
    hint.innerHTML = 'Students travel <strong>home from camp</strong>';
    title.textContent = 'Afternoon Roster';
  } else {
    hint.innerHTML = 'Students travel <strong>to camp</strong>';
    title.textContent = 'Morning Roster';
  }
}


function setFile(file) {
  csvFile = file;
  document.getElementById('file-name').textContent = file.name;
  const reader = new FileReader();
  reader.onload = e => {
    const lines = e.target.result.split('\n').filter(l => l.trim()).length;
    studentCount = lines - 1;
    document.getElementById('file-rows').textContent = `· ${studentCount} students`;
    checkCapacity();
  };
  reader.readAsText(file);
  fileChosen.classList.add('visible');
  dropZone.querySelector('.drop-icon').textContent = '✅';
  updateRunBtn();
}

csvInput.addEventListener('change', e => { if (e.target.files[0]) setFile(e.target.files[0]); });
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && f.name.endsWith('.csv')) setFile(f);
});
document.getElementById('remove-file').addEventListener('click', e => {
  e.stopPropagation(); csvFile = null; csvInput.value = '';
  studentCount = 0;
  fileChosen.classList.remove('visible');
  dropZone.querySelector('.drop-icon').textContent = '📋';
  document.getElementById('capacity-warning').style.display = 'none';
  updateRunBtn();
});

// ── Validation ─────────────────────────────────────────────────────────────
function updateRunBtn() {
  const hasCSV = !!csvFile;
  const hasAddresses = fleet.some(v => v.address.trim().length > 5);
  const totalSeats = fleet.reduce((s, v) => s + v.capacity, 0);
  const hasEnoughSeats = studentCount === 0 || totalSeats >= studentCount;
  const btn = document.getElementById('run-btn');
  const label = document.getElementById('run-label');
  btn.disabled = !(hasCSV && hasAddresses && hasEnoughSeats);
  if (hasCSV && hasAddresses && !hasEnoughSeats) {
    label.textContent = `Not enough seats (${totalSeats} / ${studentCount} needed)`;
  } else {
    label.textContent = 'Generate Route Plan';
  }
}

// ── Run ────────────────────────────────────────────────────────────────────
document.getElementById('run-btn').addEventListener('click', async () => {
  // reset
  document.getElementById('action-bar').style.display = 'none';
  document.getElementById('error-card').classList.remove('visible');
  document.getElementById('log').innerHTML = '';
  setPbar(0); lastLineCount = 0;

  setRunning(true);
  document.getElementById('prog-panel').classList.add('visible');

  const fd = new FormData();
  fd.append('csv_file', csvFile);
  fd.append('vehicles_text', fleetToText());
  fd.append('camp_address', document.getElementById('camp-address').value.trim());
  fd.append('trip_direction', tripDirection);

  try {
    const res = await fetch('/api/run', {method:'POST', body:fd});
    const data = await res.json();
    if (!res.ok || data.error) { showError(data.error || 'Server error'); return; }
    currentJobId = data.job_id;
    appendLog(`Job started — ID: ${currentJobId}`);
    pollStatus();
  } catch(err) {
    showError('Could not connect: ' + err.message);
  }
});

// ── Polling ────────────────────────────────────────────────────────────────
function pollStatus() {
  pollTimer = setInterval(async () => {
    try {
      const res  = await fetch(`/api/status/${currentJobId}`);
      const data = await res.json();
      const lines = data.progress || [];
      for (let i = lastLineCount; i < lines.length; i++) appendLog(lines[i]);
      lastLineCount = lines.length;
      setPbar(estimatePct(lines));

      if (data.status === 'done') {
        clearInterval(pollTimer);
        setPbar(100);
        document.getElementById('spinner').style.display = 'none';
        document.getElementById('prog-title').textContent = '✅ Routes generated';
        routeData = data.route_data;
        showDone(currentJobId, lines);
      } else if (data.status === 'error') {
        clearInterval(pollTimer);
        document.getElementById('spinner').style.display = 'none';
        showError(data.error || 'An error occurred');
      }
    } catch(_) {}
  }, 1200);
}

// ── Done ───────────────────────────────────────────────────────────────────
function showDone(jobId, lines) {
  setRunning(false);
  const dlUrl = `/api/download/${jobId}`;
  document.getElementById('dl-link').href = dlUrl;
  document.getElementById('dl-link-2').href = dlUrl;
  document.getElementById('action-bar').style.display = 'flex';

  if (routeData) {
    buildResultsTab(routeData, jobId);
    const badge = document.getElementById('results-badge');
    badge.textContent = routeData.length;
    badge.style.display = 'inline-block';

    // Save to localStorage for next session
    try {
      const campAddr = document.getElementById('camp-address').value.trim();
      localStorage.setItem('elbow_last_routes', JSON.stringify({
        vehicles:   routeData,
        savedAt:    new Date().toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'}),
        tripDir:    tripDirection,
        campAddr:   campAddr,
      }));
    } catch(e) { /* localStorage not available */ }
  }
}

document.getElementById('view-results-btn').addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelector('[data-tab="results"]').classList.add('active');
  document.getElementById('tab-results').classList.add('active');
});

// ── Build results tab ──────────────────────────────────────────────────────
function buildResultsTab(vehicles, jobId, initEditable=true) {
  document.getElementById('results-empty').style.display = 'none';
  document.getElementById('results-content').style.display = 'block';
  if (jobId) document.getElementById('results-stale').style.display = 'none';
  // Initialise editable copy on fresh load (not on rebuild after edit)
  if (initEditable) initEditableRoutes(vehicles);

  const totalRiders = vehicles.reduce((s, v) => s + v.rider_count, 0);
  const totalCap    = vehicles.reduce((s, v) => s + v.capacity, 0);
  const campAddr    = document.getElementById('camp-address').value.trim() || '828 Elbow Lane, Warrington, PA';
  const dirLabel    = tripDirection === 'afternoon' ? 'All routes depart from' : 'All routes end at';
  const tripLabel   = tripDirection === 'afternoon' ? '🌇 Afternoon run' : '🌅 Morning run';
  document.getElementById('summary-hint').textContent =
    `${tripLabel} · ${totalRiders} riders · ${totalCap} seats · ${vehicles.length} vehicles · ${dirLabel} ${campAddr}`;

  // Summary table
  const tbody = document.getElementById('summary-tbody');
  tbody.innerHTML = '';
  vehicles.forEach(v => {
    const warn = v.under_threshold;
    const pct  = v.utilization_pct;
    const barColor = warn ? 'util-warn' : 'util-ok';
    const badgeCls = warn ? 'badge-warn' : 'badge-ok';
    const tr = document.createElement('tr');
    if (warn) tr.className = 'warn';
    tr.innerHTML = `
      <td><strong>${v.name}</strong></td>
      <td style="font-size:.75rem;color:#888;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${v.corridor || v.start_address}</td>
      <td><strong>${v.rider_count}</strong> / ${v.capacity}</td>
      <td>
        <span class="util-bar-wrap"><span class="util-bar ${barColor}" style="width:${pct}%"></span></span>
        <span class="badge ${badgeCls}">${pct}%${warn?' ⚠':''}</span>
      </td>
      <td>${v.stop_count}</td>
      <td>${v.total_time}</td>
      <td>${v.total_distance}</td>
    `;
    tbody.appendChild(tr);
  });
  // Totals
  const totTr = document.createElement('tr');
  totTr.className = 'summary-totals';
  totTr.innerHTML = `
    <td colspan="2"><strong>TOTAL</strong></td>
    <td><strong>${totalRiders} / ${totalCap}</strong></td>
    <td><strong>${Math.round(totalRiders/totalCap*100)}%</strong></td>
    <td><strong>${vehicles.reduce((s,v)=>s+v.stop_count,0)}</strong></td>
    <td>—</td><td>—</td>
  `;
  tbody.appendChild(totTr);

  // Reset unassigned tray on fresh build
  const unassignedTray = document.getElementById('unassigned-tray');
  if (unassignedTray) {
    unassignedTray.classList.remove('visible');
    const list = document.getElementById('unassigned-list');
    if (list) list.innerHTML = '';
  }

  // Vehicle accordion cards
  const vehList = document.getElementById('veh-list');
  vehList.innerHTML = '';
  vehicles.forEach(v => {
    const card = document.createElement('div');
    card.className = 'veh-card' + (v.under_threshold ? ' warn-card' : '');

    const riders = v.stops.map(s => s.rider_names).filter(Boolean);

    const mapId = `map-${v.name.replace(/\s+/g,'-')}`;
    const campAddr = document.getElementById('camp-address').value.trim() || '828 Elbow Lane, Warrington, PA';

    card.innerHTML = `
      <div class="veh-header">
        <span class="veh-name">${v.name}</span>
        <span class="veh-corridor">${v.corridor || v.start_address}</span>
        <div class="veh-stats">
          <span class="veh-stat"><strong>${v.rider_count}</strong>/${v.capacity} riders</span>
          <span class="veh-stat"><strong>${v.utilization_pct}%</strong></span>
          <span class="veh-stat">${v.total_time}</span>
          <span class="veh-stat">${v.total_distance}</span>
        </div>
        <span class="veh-chevron">▼</span>
      </div>
      <div class="veh-body">
        ${v.under_threshold ? `<div style="background:#fff3cd;border:1px solid #f0c060;border-radius:6px;padding:.6rem .9rem;font-size:.78rem;color:#7a4f00;margin-bottom:.75rem">⚠ This vehicle is below 60% capacity — it serves a geographically isolated area that cannot be merged without splitting neighbour groups.</div>` : ''}
        <div class="veh-map" id="${mapId}">
          <div class="map-loading">⏳ Loading map…</div>
        </div>
        <div class="edit-bar">
          <button class="recalc-btn" id="recalc-${v.name.replace(/\s+/g,'-')}"
                  onclick="recalculate()" disabled>
            ↻ Recalculate Routes
          </button>
          <span class="edit-hint">Click ✕ on a rider to remove them from this route</span>
        </div>

        <table class="stop-table" id="stop-table-${v.name.replace(/\s+/g,'-')}">
          <thead><tr><th>#</th><th>Address</th><th>Riders</th><th>Drive Time</th></tr></thead>
          <tbody>
            <tr class="stop-row-start">
              <td class="stop-num">▶</td>
              <td class="stop-addr" colspan="2">${v.start_address}<div class="stop-city">Departure point</div></td>
              <td class="stop-time">—</td>
            </tr>
            ${v.stops.map((s, si) => {
              const addrParts = s.address.split(',');
              const street = addrParts[0] || s.address;
              const cityState = addrParts.slice(1).join(',').trim();
              const riderPills = s.rider_names.split(', ')
                .filter(r => r.trim())
                .map(r => `<span class="rider-pill" data-rider="${r}" data-vehicle="${v.name}" data-address="${s.address}">
                  ${r}
                  <button class="rider-remove" title="Remove ${r}" onclick="removeRider(this, '${v.name}', ${si}, '${r}')">✕</button>
                </span>`).join('');
              return `<tr data-address="${s.address}" data-vehicle="${v.name}">
                <td class="stop-num">${s.stop_num}</td>
                <td class="stop-addr">${street}<div class="stop-city">${cityState}</div></td>
                <td class="stop-riders">${riderPills}<br><span class="stop-rider-count" style="font-size:.7rem;color:#aaa">${s.rider_count} rider${s.rider_count!==1?'s':''}</span></td>
                <td class="stop-time">${s.drive_time}</td>
              </tr>`;
            }).join('')}
            <tr class="stop-row-arrive">
              <td class="stop-num">⛳</td>
              <td class="stop-addr" colspan="2">${campAddr}<div class="stop-city">Camp — destination</div></td>
              <td class="stop-time">→ ARRIVE</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;

    // Track whether map has been initialised for this card
    let mapInitialised = false;

    card.querySelector('.veh-header').addEventListener('click', () => {
      card.classList.toggle('open');
      if (card.classList.contains('open') && !mapInitialised) {
        mapInitialised = true;
        setTimeout(() => initVehicleMap(mapId, v), 200);
      }
    });

    vehList.appendChild(card);
  });
}

// ── Vehicle map (Google Maps) ───────────────────────────────────────────────
const initializedMaps = {};
let googleMapsLoaded  = false;
let googleMapsLoading = false;
const mapQueue = [];

function loadGoogleMapsAPI() {
  if (googleMapsLoaded || googleMapsLoading) return;
  googleMapsLoading = true;
  const script = document.createElement('script');
  script.src = `https://maps.googleapis.com/maps/api/js?key=${window.GOOGLE_MAPS_KEY}&libraries=geometry,marker&callback=onGoogleMapsLoaded&v=beta`;
  script.async = true;
  document.head.appendChild(script);
}

window.onGoogleMapsLoaded = function() {
  googleMapsLoaded  = true;
  googleMapsLoading = false;
  mapQueue.forEach(fn => fn());
  mapQueue.length = 0;
};

function initVehicleMap(mapId, vehicle) {
  const el = document.getElementById(mapId);
  if (!el || initializedMaps[mapId]) return;

  const campAddr = document.getElementById('camp-address').value.trim() || '828 Elbow Lane, Warrington, PA 18976';
  const allPoints = [];

  // Map shows stop 1 → stop N → camp only (no garage-to-first-stop leg)
  vehicle.stops.forEach((s, i) => {
    const lat = parseFloat(s.lat);
    const lng = parseFloat(s.lon);
    if (!isNaN(lat) && !isNaN(lng) && Math.abs(lat) > 0.001 && Math.abs(lng) > 0.001) {
      allPoints.push({lat, lng, label: String(i+1), type: 'stop',
                      riders: s.rider_names, address: s.address.split(',')[0]});
    }
  });
  // Camp destination
  const campLat = vehicle.camp_lat || 40.2454;
  const campLng = vehicle.camp_lon || -75.1407;
  allPoints.push({lat: campLat, lng: campLng, type: 'camp'});

  if (allPoints.length < 2) {
    el.innerHTML = '<div class="map-loading">No coordinates available</div>';
    return;
  }

  if (!googleMapsLoaded) {
    loadGoogleMapsAPI();
    mapQueue.push(() => renderGoogleMap(el, mapId, allPoints, vehicle, campAddr));
    return;
  }
  renderGoogleMap(el, mapId, allPoints, vehicle, campAddr);
}

async function renderGoogleMap(el, mapId, allPoints, vehicle, campAddr) {
  el.innerHTML = '';
  initializedMaps[mapId] = true;

  const BRAND = '#6D1F2F';
  const GOLD  = '#c9a84c';
  const GREEN = '#2d6a4f';

  const avgLat = allPoints.reduce((s,p) => s + p.lat, 0) / allPoints.length;
  const avgLng = allPoints.reduce((s,p) => s + p.lng, 0) / allPoints.length;

  const map = new google.maps.Map(el, {
    center: {lat: avgLat, lng: avgLng},
    zoom: 11,
    mapId: 'elbow_lane_route_map',
    zoomControl: true,
    streetViewControl: false,
    mapTypeControl: false,
    fullscreenControl: true,
    mapTypeId: 'roadmap',
  });

  function makeIcon(label, bg, fg, size) {
    return {
      url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">
          <circle cx="${size/2}" cy="${size/2}" r="${size/2-1}" fill="${bg}" stroke="white" stroke-width="2"/>
          <text x="${size/2}" y="${size/2+4}" text-anchor="middle" font-size="${size<=30?11:13}"
            font-weight="700" fill="${fg}" font-family="Arial">${label}</text>
        </svg>`
      )}`,
      scaledSize: new google.maps.Size(size, size),
      anchor: new google.maps.Point(size/2, size/2),
    };
  }

  const infoWindow = new google.maps.InfoWindow();

  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
  allPoints.forEach((pt, i) => {
    let pinEl, popupContent;
    const size = pt.type === 'start' || pt.type === 'camp' ? 32 : 28;
    const bg   = pt.type === 'start' ? BRAND : pt.type === 'camp' ? GREEN : GOLD;
    const fg   = pt.type === 'camp' || pt.type === 'start' ? '#fff' : '#1a1018';
    const lbl  = pt.type === 'start' ? '▶' : pt.type === 'camp' ? '⛳' : pt.label;

    pinEl = document.createElement('div');
    pinEl.style.cssText = `width:${size}px;height:${size}px;background:${bg};border:2px solid #fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:${size<=28?'11':'13'}px;font-weight:700;color:${fg};font-family:Arial;box-shadow:0 2px 6px rgba(0,0,0,.3);cursor:pointer`;
    pinEl.textContent = lbl;

    popupContent = pt.type === 'start'
      ? `<strong>Start</strong><br>${vehicle.start_address}`
      : pt.type === 'camp'
      ? `<strong>Camp</strong><br>${campAddr}`
      : `<strong>Stop ${pt.label}</strong><br>${pt.address}<br><em>${pt.riders}</em>`;

    const marker = new AdvancedMarkerElement({
      position: {lat: pt.lat, lng: pt.lng},
      map,
      content: pinEl,
    });
    marker.addListener('click', () => {
      infoWindow.setContent(popupContent);
      infoWindow.open(map, marker);
    });
  });

  // Draw route with Google Directions Service
  const directionsService = new google.maps.DirectionsService();

  // Split route into chunks of max 8 waypoints each to stay within Google limits
  // Pattern: [stop0 → stop1..8 → stop9] [stop9 → stop10..17 → stop18] ... [lastStop → camp]
  const MAX_WP = 8;
  const stopPoints = allPoints; // stops only (camp already appended as last point)
  const segments = [];

  for (let i = 0; i < stopPoints.length - 1; i += MAX_WP) {
    const chunk = stopPoints.slice(i, Math.min(i + MAX_WP + 1, stopPoints.length));
    if (chunk.length >= 2) segments.push(chunk);
  }

  const bounds = new google.maps.LatLngBounds();
  allPoints.forEach(p => bounds.extend({lat: p.lat, lng: p.lng}));

  let successCount = 0;

  const drawSegment = (seg, idx) => {
    const origin = seg[0];
    const dest   = seg[seg.length - 1];
    const wps    = seg.slice(1, -1).map(p => ({
      location: new google.maps.LatLng(p.lat, p.lng),
      stopover: true,
    }));

    directionsService.route({
      origin:            new google.maps.LatLng(origin.lat, origin.lng),
      destination:       new google.maps.LatLng(dest.lat,   dest.lng),
      waypoints:         wps,
      travelMode:        google.maps.TravelMode.DRIVING,
      optimizeWaypoints: false,
    }, (result, status) => {
      if (status === 'OK') {
        new google.maps.DirectionsRenderer({
          map,
          suppressMarkers:  true,
          preserveViewport: true,
          polylineOptions:  {strokeColor: BRAND, strokeWeight: 4, strokeOpacity: .85}
        }).setDirections(result);
        successCount++;
      } else {
        // Fallback: draw a simple polyline for this segment
        new google.maps.Polyline({
          path:          seg.map(p => ({lat: p.lat, lng: p.lng})),
          map,
          strokeColor:   BRAND,
          strokeWeight:  3,
          strokeOpacity: .6,
          icons: [{icon:{path:'M 0,-1 0,1',strokeOpacity:1,scale:3},offset:'0',repeat:'12px'}],
        });
      }
      // Fit bounds after last segment
      if (idx === segments.length - 1) {
        map.fitBounds(bounds, {top: 40, right: 40, bottom: 40, left: 40});
      }
    });
  };

  if (segments.length > 0) {
    segments.forEach((seg, idx) => drawSegment(seg, idx));
  } else {
    map.fitBounds(bounds, {top: 40, right: 40, bottom: 40, left: 40});
  }
}

// ── Manual editing ─────────────────────────────────────────────────────────

// In-memory editable copy of route data
let editableRoutes = null;
let hasEdits = false;

function initEditableRoutes(vehicles) {
  // Deep clone so we can edit without touching the original
  editableRoutes = JSON.parse(JSON.stringify(vehicles));
  hasEdits = false;
}

let unassignedRiders = [];  // [{name, fromVehicle, stopAddress, lat, lon}]


function removeRider(btn, vehicleName, stopIdx, riderName) {
  if (!editableRoutes) return;

  const veh = editableRoutes.find(v => v.name === vehicleName);
  if (!veh) return;

  const stop = veh.stops[stopIdx];
  if (!stop) return;

  // Remove rider from stop
  const riderList = stop.rider_names.split(', ').filter(r => r.trim() && r !== riderName);
  
  if (riderList.length === 0) {
    // Last rider at this stop — remove the whole stop
    veh.stops.splice(stopIdx, 1);
  } else {
    stop.rider_names = riderList.join(', ');
    stop.rider_count = riderList.length;
  }

  // Add to unassigned tray
  unassignedRiders.push({
    name: riderName,
    fromVehicle: vehicleName,
    stopAddress: stop.address,
    lat: stop.lat,
    lon: stop.lon,
  });

  // Show recalculate bar
  document.getElementById('recalc-bar').classList.add('visible');

  // Rebuild results display
  buildResultsTab(editableRoutes, currentJobId, false);
  updateUnassignedTray();
}

function updateUnassignedTray() {
  const tray = document.getElementById('unassigned-tray');
  const list = document.getElementById('unassigned-list');

  if (unassignedRiders.length === 0) {
    tray.classList.remove('visible');
    return;
  }

  tray.classList.add('visible');

  // Build vehicle options for dropdown
  const vehOptions = (editableRoutes || [])
    .map(v => `<option value="${v.name}">${v.name}</option>`)
    .join('');

  list.innerHTML = unassignedRiders.map((r, i) => `
    <div class="unassigned-chip">
      <span>${r.name}</span>
      <span style="color:#aaa;font-size:.7rem">from ${r.fromVehicle}</span>
      <select id="assign-select-${i}">
        <option value="">Assign to...</option>
        ${vehOptions}
      </select>
      <button class="reassign-btn" onclick="assignRider(${i})">Move</button>
    </div>
  `).join('');
}

function assignRider(idx) {
  const select = document.getElementById(`assign-select-${idx}`);
  const targetVehicleName = select.value;
  if (!targetVehicleName || !editableRoutes) return;

  const rider = unassignedRiders[idx];
  const targetVeh = editableRoutes.find(v => v.name === targetVehicleName);
  if (!targetVeh) return;

  // Check capacity
  const currentRiders = targetVeh.stops.reduce((s, st) => s + (st.rider_count || 1), 0);
  if (currentRiders >= targetVeh.capacity) {
    alert(`${targetVehicleName} is already at full capacity (${targetVeh.capacity} riders)`);
    return;
  }

  // Find if there's already a stop at this address on the target vehicle
  const existingStop = targetVeh.stops.find(s => s.address === rider.stopAddress);
  if (existingStop) {
    existingStop.rider_names = existingStop.rider_names
      ? existingStop.rider_names + ', ' + rider.name
      : rider.name;
    existingStop.rider_count = (existingStop.rider_count || 0) + 1;
  } else {
    // Add as new stop
    targetVeh.stops.push({
      stop_num:    targetVeh.stops.length + 1,
      address:     rider.stopAddress,
      rider_names: rider.name,
      rider_count: 1,
      drive_time:  '— recalculate',
      lat:         rider.lat,
      lon:         rider.lon,
    });
  }

  // Remove from unassigned
  unassignedRiders.splice(idx, 1);

  // Rebuild display
  buildResultsTab(editableRoutes, currentJobId, false);
  updateUnassignedTray();
  document.getElementById('recalc-bar').classList.add('visible');
}

async function recalculate() {
  if (!editableRoutes || !currentJobId) return;

  const btn = document.getElementById('recalc-btn');
  btn.disabled = true;
  btn.textContent = 'Recalculating…';

  try {
    const resp = await fetch(`/api/recalculate/${currentJobId}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({vehicles: editableRoutes}),
    });
    const data = await resp.json();

    if (data.error) {
      alert('Recalculation failed: ' + data.error);
      return;
    }

    // Update with fresh data from server
    routeData = data.route_data;
    editableRoutes = JSON.parse(JSON.stringify(routeData));
    unassignedRiders = [];

    buildResultsTab(editableRoutes, currentJobId, false);
    updateUnassignedTray();

    document.getElementById('recalc-bar').classList.remove('visible');
    document.getElementById('recalc-bar').innerHTML = `
      <span>✅ Routes recalculated successfully</span>
      <button class="recalc-btn" id="recalc-btn" onclick="recalculate()">Recalculate Again</button>
    `;
    document.getElementById('recalc-bar').classList.add('visible');
    setTimeout(() => document.getElementById('recalc-bar').classList.remove('visible'), 3000);

    // Save to localStorage
    try {
      const campAddr = document.getElementById('camp-address').value.trim();
      localStorage.setItem('elbow_last_routes', JSON.stringify({
        vehicles: routeData,
        savedAt: new Date().toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'}),
        tripDir:  tripDirection,
        campAddr: campAddr,
      }));
    } catch(e) {}

  } catch(e) {
    alert('Network error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Recalculate Routes';
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────
function setRunning(on) {
  const btn = document.getElementById('run-btn');
  btn.disabled = on;
  document.getElementById('run-icon').textContent = on ? '⏳' : '🗺️';
  document.getElementById('run-label').textContent = on ? 'Generating…' : 'Generate Route Plan';
  document.getElementById('spinner').style.display = on ? 'block' : 'none';
}

function appendLog(line) {
  const div = document.createElement('div');
  if (line.startsWith('✅')||line.startsWith('✓')||line.startsWith('  ✓')) div.className='ok';
  else if (line.startsWith('⚠')||line.includes('Purged')||line.includes('warn')) div.className='warn';
  else if (line.startsWith('❌')) div.className='err';
  div.textContent = line;
  const log = document.getElementById('log');
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function setPbar(pct) { document.getElementById('pbar').style.width = Math.min(100,pct)+'%'; }

function estimatePct(lines) {
  if (!lines.length) return 5;
  const last = lines[lines.length-1]||'';
  if (last.includes('Saved')||last.includes('✅')) return 100;
  if (last.includes('Sequence')||last.includes('stop')) return 85;
  if (last.includes('Active')) return 75;
  if (last.includes('Formed')||last.includes('cluster')) return 50;
  if (last.includes('Geocoding')||last.includes('Geocoded')) return 20;
  if (last.includes('Loaded')) return 10;
  return Math.min(90, 10 + lines.length * 1.5);
}

function showError(msg) {
  document.getElementById('error-card').classList.add('visible');
  document.getElementById('error-msg').textContent = msg;
  setRunning(false);
}

async function clearCache() {
  try {
    const resp = await fetch('/api/clear-cache', {method: 'POST'});
    const data = await resp.json();
    const notice = document.getElementById('cache-notice');
    notice.style.display = 'block';
    setTimeout(() => notice.style.display = 'none', 5000);
  } catch(e) {
    alert('Could not clear cache: ' + e.message);
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
renderFleet();

// Load previous results from localStorage if available
try {
  const saved = localStorage.getItem('elbow_last_routes');
  if (saved) {
    const parsed = JSON.parse(saved);
    if (parsed.vehicles && parsed.vehicles.length > 0) {
      // Restore camp address and trip direction
      if (parsed.campAddr) document.getElementById('camp-address').value = parsed.campAddr;
      if (parsed.tripDir)  setTrip(parsed.tripDir);

      // Show stale banner with date
      document.getElementById('last-run-date').textContent = parsed.savedAt;
      document.getElementById('results-stale').style.display = 'block';

      // Build results tab with saved data
      buildResultsTab(parsed.vehicles, null);

      // Show badge
      const badge = document.getElementById('results-badge');
      badge.textContent = parsed.vehicles.length;
      badge.style.display = 'inline-block';
    }
  }
} catch(e) { /* localStorage not available or corrupted */ }
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
