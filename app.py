"""
Elbow Lane Day Camp - Bus Route Optimizer
Flask web application with fleet builder UI and in-app route viewer.
Run with: python app.py
"""

import os, uuid, threading, json, urllib.parse, urllib.request
from flask import Flask, request, jsonify, send_file, render_template_string, send_from_directory
from bus_route_optimizer import (
    generate_routes, parse_students_csv, parse_vehicles_text,
    cluster_and_route, Stop, Vehicle
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

@app.route("/healthz")
def healthz():
    return "OK", 200

jobs: dict = {}
jobs_lock = threading.Lock()

os.makedirs("uploads", exist_ok=True)
os.makedirs("outputs", exist_ok=True)

CAMP_COORDS = (40.2454, -75.1407)

# Serialise route data for JSON API

def vehicles_to_json(vehicles: list) -> list:
    out = []
    for v in vehicles:
        out.append({
            "name": v.name,
            "start_address": v.start_address,
            "capacity": v.capacity,
            "rider_count": v.rider_count,
            "stop_count": v.stop_count,
            "utilization_pct": v.utilization_pct,
            "total_time": v.total_time,
            "total_distance": v.total_distance,
            "under_threshold": v.under_threshold,
            "corridor": v.corridor,
            "start_lat": v.start_lat,
            "start_lon": v.start_lon,
            "camp_lat": getattr(v, "camp_lat", 40.2454),
            "camp_lon": getattr(v, "camp_lon", -75.1407),
            "stops": [
                {
                    "stop_num": i + 1,
                    "address": s.address,
                    "rider_names": s.rider_names,
                    "rider_count": s.rider_count,
                    "drive_time": s.drive_time,
                    "lat": s.lat,
                    "lon": s.lon,
                }
                for i, s in enumerate(v.stops)
            ],
        })
    return out

# Background worker

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
        vcfgs = parse_vehicles_text(vehicles_text)
        vehicles = cluster_and_route(students, vcfgs, progress,
                                     camp_address=camp_address,
                                     trip_direction=trip_direction)

        from openpyxl import Workbook
        from bus_route_optimizer import build_dashboard, build_vehicle_sheet
        wb = Workbook()
        build_dashboard(wb, vehicles, camp_address=camp_address, trip_direction=trip_direction)
        for veh in vehicles:
            build_vehicle_sheet(wb, veh, camp_address=camp_address, trip_direction=trip_direction)
        wb.save(output_path)
        progress("Excel saved")

        camp_lat_val = vehicles[0].camp_lat if vehicles else CAMP_COORDS[0]
        camp_lon_val = vehicles[0].camp_lon if vehicles else CAMP_COORDS[1]

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_path"] = output_path
            jobs[job_id]["route_data"] = vehicles_to_json(vehicles)
            jobs[job_id]["camp_address"] = camp_address
            jobs[job_id]["trip_direction"] = trip_direction
            jobs[job_id]["camp_lat"] = camp_lat_val
            jobs[job_id]["camp_lon"] = camp_lon_val

    except Exception as e:
        import traceback
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        progress(f"Error: {e}")

# API routes

@app.route("/")
def index():
    key = os.environ.get("GOOGLE_MAPS_KEY", "")
    return render_template_string(HTML.replace(
        '"{{ google_maps_key }}"', f'"{key}"'
    ))

@app.route("/logo.png")
def serve_logo():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "logo.png")

@app.route("/api/run", methods=["POST"])
def api_run():
    csv_file = request.files.get("csv_file")
    vehicles_text = request.form.get("vehicles_text", "").strip()

    if not csv_file:
        return jsonify({"error": "No CSV file uploaded"}), 400
    if not vehicles_text:
        return jsonify({"error": "No fleet configuration provided"}), 400

    csv_text = csv_file.read().decode("utf-8-sig", errors="replace")
    camp_address = request.form.get("camp_address", "").strip() or None
    trip_direction = request.form.get("trip_direction", "morning")

    try:
        students = parse_students_csv(csv_text)
        if not students:
            first_line = csv_text.strip().split("\n")[0].lower() if csv_text.strip() else ""
            has_name = "name" in first_line
            has_addr = "address" in first_line or "street" in first_line
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
            "status": "queued",
            "progress": [f"Loaded {len(students)} students, {len(vcfgs)} vehicles"],
            "output_path": None,
            "route_data": None,
            "error": None,
            "camp_address": camp_address,
            "trip_direction": trip_direction,
        }

    threading.Thread(
        target=run_job,
        args=(job_id, csv_text, vehicles_text, camp_address, trip_direction),
        daemon=True
    ).start()

    return jsonify({"job_id": job_id})

@app.route("/api/debug-coords")
def api_debug_coords():
    geocache_file = "geocache.json"
    if not os.path.exists(geocache_file):
        return jsonify({"error": "No geocache found - run routes first"}), 404

    with open(geocache_file) as f:
        cache = json.load(f)

    results = []
    for addr, coords in sorted(cache.items()):
        lat, lon = coords[0], coords[1]
        suspicious = abs(lat) < 0.001 or abs(lon) < 0.001
        camp_fallback = abs(lat - 40.2454) < 0.001 and abs(lon + 75.1407) < 0.001
        results.append({
            "address": addr, "lat": lat, "lon": lon,
            "suspicious": suspicious, "camp_fallback": camp_fallback,
        })

    bad = [r for r in results if r["suspicious"] or r["camp_fallback"]]
    return jsonify({"total": len(results), "bad_count": len(bad),
                    "bad_entries": bad, "all_entries": results})

@app.route("/api/clear-cache", methods=["POST"])
def api_clear_cache():
    cleared = []
    for f in ["geocache.json", "routecache.json"]:
        if os.path.exists(f):
            os.remove(f)
            cleared.append(f)
    return jsonify({"cleared": cleared,
                    "message": f"Cleared {len(cleared)} cache files. Next run will re-geocode all addresses using Google Maps."})

@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "error": job.get("error"),
        "route_data": job.get("route_data"),
    })

@app.route("/api/recalculate/<job_id>", methods=["POST"])
def api_recalculate(job_id: str):
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
            route_leg_times, haversine_mi, CAMP_COORDS as CC,
            _sequence_stops_camp_directional
        )
        from openpyxl import Workbook

        camp_address = job.get("camp_address") or "828 Elbow Lane, Warrington, PA 18976"
        trip_direction = job.get("trip_direction") or "morning"

        class EditableStop:
            def __init__(self, d):
                self.address = d["address"]
                self.rider_names = d.get("rider_names", "")
                self.rider_count = int(d.get("rider_count", 0))
                self.lat = float(d.get("lat", 0) or 0)
                self.lon = float(d.get("lon", 0) or 0)
                self.drive_time = d.get("drive_time", "-")
                self.geocoded = True

        vehicles = []
        for vd in edited_vehicles:
            stops = [EditableStop(sd) for sd in vd.get("stops", [])
                     if sd.get("rider_names") and int(sd.get("rider_count", 0)) > 0]

            veh = Vehicle(
                name=vd["name"],
                start_address=vd["start_address"],
                capacity=vd["capacity"],
                stops=stops,
                total_time=vd.get("total_time", "-"),
                total_distance=vd.get("total_distance", "-"),
                under_threshold=vd.get("under_threshold", False),
                start_lat=vd.get("start_lat", 0.0),
                start_lon=vd.get("start_lon", 0.0),
                camp_lat=vd.get("camp_lat", CC[0]),
                camp_lon=vd.get("camp_lon", CC[1]),
            )

            if len(veh.stops) > 1:
                camp_lat = veh.camp_lat or CC[0]
                camp_lon = veh.camp_lon or CC[1]
                veh.stops = _sequence_stops_camp_directional(
                    veh.stops, camp_lat, camp_lon, trip_direction)

            if veh.stops and (veh.start_lat or veh.start_lon):
                camp_lat = veh.camp_lat or CC[0]
                camp_lon = veh.camp_lon or CC[1]
                coord_seq = ([(veh.start_lat, veh.start_lon)]
                             + [(s.lat, s.lon) for s in veh.stops]
                             + [(camp_lat, camp_lon)])
                legs = route_leg_times(coord_seq)

                for i, stop in enumerate(veh.stops):
                    mins = max(1, round(legs[i]))
                    stop.drive_time = f"{mins} min from start" if i == 0 else f"{mins} min"

                kids_mins = round(sum(legs[1:]))
                hrs, rem = divmod(kids_mins, 60)
                veh.total_time = (f"{hrs} hr {rem} min" if hrs and rem
                                  else (f"{hrs} hr" if hrs else f"{kids_mins} min"))

                total_mi = sum(
                    haversine_mi(coord_seq[i][0], coord_seq[i][1],
                                 coord_seq[i+1][0], coord_seq[i+1][1]) * 1.35
                    for i in range(len(coord_seq)-1))
                veh.total_distance = f"{round(total_mi, 1)} mi"

            cap = veh.capacity
            eff = 0.40 if cap <= 6 else (0.50 if cap <= 9 else 0.60)
            veh.under_threshold = (veh.rider_count / cap < eff) if cap else False

            vehicles.append(veh)

        output_path = os.path.join("outputs", f"routes_{job_id}_edited.xlsx")
        wb = Workbook()
        build_dashboard(wb, vehicles, camp_address=camp_address, trip_direction=trip_direction)
        for veh in vehicles:
            build_vehicle_sheet(wb, veh, camp_address=camp_address, trip_direction=trip_direction)
        wb.save(output_path)

        with jobs_lock:
            jobs[job_id]["output_path"] = output_path
            jobs[job_id]["route_data"] = vehicles_to_json(vehicles)

        return jsonify({"status": "ok", "route_data": jobs[job_id]["route_data"]})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

# Route polyline endpoint

@app.route("/api/route-polyline", methods=["POST"])
def api_route_polyline():
    data = {}
    try:
        data = request.get_json(force=True) or {}
        points = data.get("points", [])

        if len(points) < 2:
            return jsonify({"coords": [], "errors": ["Need at least 2 points"]})

        google_key = os.environ.get("GOOGLE_MAPS_KEY", "")
        if not google_key:
            return jsonify({
                "coords": [{"lat": p["lat"], "lng": p["lng"]} for p in points],
                "errors": ["No Google Maps key - using straight lines"]
            })

        origin = f"{points[0]['lat']},{points[0]['lng']}"
        destination = f"{points[-1]['lat']},{points[-1]['lng']}"
        waypoints_list = points[1:-1]

        all_coords = []
        errors = []

        if len(waypoints_list) <= 23:
            all_coords, err = _fetch_directions_polyline(
                origin, destination, waypoints_list, google_key)
            if err:
                errors.append(err)
        else:
            chunk_size = 23
            prev_point = points[0]
            remaining = points[1:]
            while remaining:
                chunk = remaining[:chunk_size + 1]
                chunk_origin = f"{prev_point['lat']},{prev_point['lng']}"
                chunk_dest = f"{chunk[-1]['lat']},{chunk[-1]['lng']}"
                chunk_wps = chunk[:-1]
                coords, err = _fetch_directions_polyline(
                    chunk_origin, chunk_dest, chunk_wps, google_key)
                if err:
                    errors.append(err)
                if coords:
                    if all_coords:
                        coords = coords[1:]
                    all_coords.extend(coords)
                prev_point = chunk[-1]
                remaining = remaining[chunk_size + 1:]

        if not all_coords:
            return jsonify({
                "coords": [{"lat": p["lat"], "lng": p["lng"]} for p in points],
                "errors": errors or ["Directions API returned no route"]
            })

        return jsonify({"coords": all_coords, "errors": errors})

    except Exception as e:
        return jsonify({
            "coords": [{"lat": p["lat"], "lng": p["lng"]} for p in data.get("points", [])],
            "errors": [str(e)]
        }), 200


def _fetch_directions_polyline(origin, destination, waypoints, google_key):
    params = {
        "origin": origin,
        "destination": destination,
        "mode": "driving",
        "key": google_key,
    }
    if waypoints:
        params["waypoints"] = "|".join(f"{p['lat']},{p['lng']}" for p in waypoints)

    url = ("https://maps.googleapis.com/maps/api/directions/json?"
           + urllib.parse.urlencode(params))

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ElbowLaneCampBusRouter/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

        if result.get("status") != "OK":
            return [], (f"Directions API status: {result.get('status')} - "
                        f"{result.get('error_message', '')}")

        coords = []
        for leg in result["routes"][0]["legs"]:
            for step in leg["steps"]:
                decoded = _decode_polyline(step["polyline"]["points"])
                if coords:
                    decoded = decoded[1:]
                coords.extend(decoded)

        return coords, None

    except Exception as e:
        return [], str(e)


def _decode_polyline(encoded):
    coords = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        result = 0
        shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        lat += ~(result >> 1) if (result & 1) else (result >> 1)

        result = 0
        shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        lng += ~(result >> 1) if (result & 1) else (result >> 1)

        coords.append({"lat": lat / 1e5, "lng": lng / 1e5})

    return coords

# Download

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

# HTML

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1F68C;</text></svg>">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Elbow Lane - Bus Route Optimizer</title>
<script>
window.GOOGLE_MAPS_KEY = "{{ google_maps_key }}";
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Roboto+Slab:wght@600;700;800&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root {
--brand: #6D1F2F;
--brand-dark: #4a1520;
--brand-mid: #9e3347;
--brand-light:#f5e6e9;
--gold: #c9a84c;
--gold-lt: #f0d98a;
--ink: #1a1018;
--mist: #f8f4f5;
--border: #e8dde0;
--success: #2d6a4f;
--warn: #b36a00;
--r: 12px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',sans-serif;background:var(--mist);color:var(--ink);min-height:100vh}
header{background:var(--brand);color:#fff;padding:0 2rem;display:flex;align-items:center;gap:1.25rem;height:80px;box-shadow:0 2px 16px rgba(109,31,47,.35);position:sticky;top:0;z-index:200}
.h-title{font-family:'Roboto Slab',serif;font-size:1.25rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase}
.h-sub{font-size:.72rem;opacity:.75;font-weight:400;margin-top:2px;letter-spacing:.08em;text-transform:uppercase}
.h-badge{margin-left:auto;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);color:#fff;font-size:.68rem;font-family:'Roboto Slab',serif;font-weight:500;letter-spacing:.12em;text-transform:uppercase;padding:.35rem .9rem;border-radius:20px;white-space:nowrap}
.tab-bar{display:flex;background:#fff;border-bottom:2px solid var(--border);position:sticky;top:80px;z-index:100}
.tab{padding:.85rem 1.75rem;font-size:.82rem;font-weight:500;font-family:'Roboto Slab',serif;letter-spacing:.07em;text-transform:uppercase;color:#999;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;transition:color .15s,border-color .15s;white-space:nowrap;display:flex;align-items:center;gap:.5rem}
.tab:hover{color:var(--brand-mid)}
.tab.active{color:var(--brand);border-bottom-color:var(--brand)}
.tab-badge{background:var(--brand);color:#fff;font-size:.65rem;font-weight:700;padding:.15rem .45rem;border-radius:10px;min-width:18px;text-align:center}
.container{max-width:960px;margin:0 auto;padding:2rem 1.5rem 4rem}
.tab-panel{display:none}.tab-panel.active{display:block}
.card{background:#fff;border:1px solid var(--border);border-radius:var(--r);padding:1.5rem 1.75rem;margin-bottom:1.1rem;box-shadow:0 1px 4px rgba(0,0,0,.04);transition:box-shadow .2s}
.card:hover{box-shadow:0 3px 12px rgba(109,31,47,.07)}
.card-hd{display:flex;align-items:center;gap:.7rem;margin-bottom:1.1rem}
.card-num{width:26px;height:26px;background:var(--brand);color:#fff;border-radius:50%;font-size:.75rem;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.card-title{font-family:'Roboto Slab',serif;font-size:1.05rem;font-weight:700;color:var(--brand-dark);letter-spacing:.01em;text-transform:uppercase}
.card-hint{font-size:.75rem;color:#999;margin-top:.15rem;font-weight:300}
label.lbl{display:block;font-size:.75rem;font-weight:600;color:var(--brand-dark);letter-spacing:.04em;text-transform:uppercase;margin-bottom:.4rem}
.drop-zone{border:2px dashed var(--border);border-radius:var(--r);padding:1.75rem;text-align:center;cursor:pointer;transition:all .2s;background:var(--mist);position:relative}
.drop-zone:hover,.drop-zone.drag-over{border-color:var(--brand-mid);background:var(--brand-light)}
.drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-icon{font-size:2rem;margin-bottom:.4rem}
.drop-text{font-size:.88rem;color:#666}.drop-text strong{color:var(--brand)}
.drop-meta{font-size:.72rem;color:#bbb;margin-top:.3rem}
.file-chosen{display:none;align-items:center;gap:.7rem;padding:.65rem .9rem;background:#edfaf3;border:1px solid #a3d9b8;border-radius:8px;margin-top:.6rem;font-size:.83rem;color:var(--success);font-weight:500}
.file-chosen.visible{display:flex}
.file-chosen .rm{margin-left:auto;cursor:pointer;font-size:.9rem;color:#999;background:none;border:none;padding:0 .2rem}
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
.trip-btn{padding:.5rem 1.25rem;border:1.5px solid var(--border);border-radius:8px;background:#fff;color:#888;font-family:'Roboto Slab',serif;font-size:.78rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;cursor:pointer;transition:all .15s;white-space:nowrap}
.trip-btn.active{background:var(--brand);border-color:var(--brand);color:#fff}
.trip-btn:hover:not(.active){border-color:var(--brand-mid);color:var(--brand-mid)}
.run-btn{width:100%;padding:.95rem 2rem;background:var(--brand);color:#fff;border:none;border-radius:var(--r);font-family:'Roboto Slab',serif;font-size:1.05rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:.65rem;transition:background .18s,transform .1s,box-shadow .18s;box-shadow:0 4px 14px rgba(109,31,47,.3);margin-top:1.25rem}
.run-btn:hover:not(:disabled){background:var(--brand-dark);box-shadow:0 6px 20px rgba(109,31,47,.4);transform:translateY(-1px)}
.run-btn:disabled{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}
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
.action-bar{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1.1rem}
.dl-btn{display:inline-flex;align-items:center;gap:.55rem;padding:.75rem 1.5rem;background:var(--gold);color:#1a1018;border-radius:8px;text-decoration:none;font-weight:700;font-size:.9rem;transition:background .15s,transform .1s;box-shadow:0 3px 10px rgba(201,168,76,.35);border:none;cursor:pointer}
.dl-btn:hover{background:var(--gold-lt);transform:translateY(-1px)}
.view-btn{display:inline-flex;align-items:center;gap:.55rem;padding:.75rem 1.5rem;background:var(--brand);color:#fff;border-radius:8px;font-weight:600;font-size:.9rem;border:none;cursor:pointer;transition:background .15s}
.view-btn:hover{background:var(--brand-dark)}
#error-card{display:none;background:#2d0d13;border:1px solid #6d1f2f;border-radius:var(--r);padding:1.1rem 1.4rem;margin-top:1.1rem;color:#f5c2cb;font-size:.85rem}
#error-card.visible{display:block}
#error-card strong{display:block;margin-bottom:.35rem;font-size:.95rem}
.results-empty{text-align:center;padding:4rem 2rem;color:#bbb}
.unassigned-tray{background:#fff8e6;border:1.5px dashed var(--gold);border-radius:var(--r);padding:1rem 1.25rem;margin-bottom:1rem;display:none}
.unassigned-tray.visible{display:block}
.unassigned-title{font-family:'Roboto Slab',serif;font-size:.85rem;font-weight:700;color:#7a4f00;margin-bottom:.65rem;text-transform:uppercase;letter-spacing:.04em}
.unassigned-list{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:.75rem}
.unassigned-chip{display:flex;align-items:center;gap:.4rem;background:#fff3cd;border:1px solid #f0c060;border-radius:20px;padding:.3rem .75rem;font-size:.78rem;font-weight:500;color:#7a4f00}
.unassigned-chip select{border:none;background:transparent;font-size:.75rem;color:#7a4f00;cursor:pointer;padding:0 .2rem;font-weight:600}
.reassign-btn{padding:.25rem .65rem;background:var(--brand);color:#fff;border:none;border-radius:6px;font-size:.72rem;font-weight:600;cursor:pointer;transition:background .15s}
.reassign-btn:hover{background:var(--brand-dark)}
.recalc-bar{display:none;align-items:center;gap:.75rem;padding:.65rem 1rem;background:#edfaf3;border:1px solid #a3d9b8;border-radius:8px;margin-bottom:.75rem;font-size:.82rem;color:var(--success)}
.recalc-bar.visible{display:flex}
.results-empty .empty-icon{font-size:3rem;margin-bottom:1rem}
.results-empty p{font-size:.9rem;line-height:1.6}
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
.summary-totals{background:var(--brand)!important;color:#fff}
.summary-totals td{color:#fff!important;font-weight:700;border-bottom:none!important}
@media(max-width:640px){
.fleet-row{grid-template-columns:1fr 1fr;grid-template-rows:auto auto}
.fleet-col-label{display:none}
.veh-stats{display:none}
.tab span:not(.tab-badge){display:none}
header{padding:0 1rem;gap:.75rem;height:64px}
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
<div>
<div class="h-title">Elbow Lane Day Camp</div>
<div class="h-sub">Bus Route Optimizer</div>
</div>
<span class="h-badge">Route Planner</span>
</header>
<div class="tab-bar">
<div class="tab active" data-tab="setup">Setup</div>
<div class="tab" data-tab="results">Results <span class="tab-badge" id="results-badge" style="display:none">0</span></div>
</div>
<div class="container">
<div class="tab-panel active" id="tab-setup">
<div class="card">
<div class="card-hd">
<span class="card-num" style="background:var(--gold);color:#1a1018">*</span>
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
<div style="font-size:.72rem;color:#aaa;margin-top:.35rem">This is the destination for morning routes and the starting point for afternoon routes</div>
</div>
<div>
<label class="lbl">Trip Direction</label>
<div style="display:flex;gap:.5rem">
<button class="trip-btn active" id="btn-morning" onclick="setTrip('morning')">Morning</button>
<button class="trip-btn" id="btn-afternoon" onclick="setTrip('afternoon')">Afternoon</button>
</div>
<div id="trip-hint" style="font-size:.72rem;color:#aaa;margin-top:.35rem;max-width:160px">Students travel <strong>to camp</strong></div>
</div>
</div>
</div>
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
<div class="drop-icon">&#x1F4CB;</div>
<div class="drop-text"><strong>Click to choose</strong> or drag and drop your CSV file</div>
<div class="drop-meta">Required columns: Last name | First name | Address | City | Zip</div>
</div>
<div class="file-chosen" id="file-chosen">
<span>OK</span>
<span id="file-name">-</span>
<span id="file-rows" style="color:#888;font-weight:400"></span>
<button class="rm" id="remove-file">X</button>
</div>
</div>
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
<button class="add-vehicle-btn" id="add-vehicle-btn">+ Add Vehicle</button>
<div class="fleet-summary" id="fleet-summary"></div>
<div id="capacity-warning" style="display:none;margin-top:.75rem;background:#fff3cd;border:1px solid #f0c060;border-radius:8px;padding:.75rem 1rem;font-size:.84rem;color:#7a4f00;align-items:center;gap:.6rem">
<span style="font-size:1.1rem">!</span>
<span id="capacity-warning-msg"></span>
</div>
</div>
<button class="run-btn" id="run-btn" disabled>
<span id="run-icon">Map</span>
<span id="run-label">Generate Route Plan</span>
</button>
<div id="prog-panel">
<div class="prog-hd">
<div class="spinner" id="spinner"></div>
<span class="prog-title" id="prog-title">Optimizing routes...</span>
</div>
<div class="pbar-wrap"><div class="pbar" id="pbar"></div></div>
<div id="log"></div>
</div>
<div class="action-bar" id="action-bar" style="display:none">
<a class="dl-btn" id="dl-link" href="#" download>Download Excel</a>
<button class="view-btn" id="view-results-btn">View Results</button>
</div>
<div id="cache-notice" style="display:none;margin-top:.6rem;background:#fff3cd;border:1px solid #f0c060;border-radius:8px;padding:.6rem 1rem;font-size:.78rem;color:#7a4f00">
Cache cleared - next run will re-geocode all addresses with Google Maps.
</div>
<div style="text-align:right;margin-top:.4rem">
<button onclick="clearCache()" style="background:none;border:none;color:#aaa;font-size:.72rem;cursor:pointer;text-decoration:underline">Clear geocache (fixes wrong map locations)</button>
</div>
<div id="error-card">
<strong>Something went wrong</strong>
<span id="error-msg"></span>
</div>
</div>

<div class="tab-panel" id="tab-results">
<div id="results-empty" class="results-empty">
<div class="empty-icon">Map</div>
<p>No routes generated yet.<br>Go to <strong>Setup</strong> and click <em>Generate Route Plan</em>.</p>
</div>
<div id="results-stale" style="display:none">
<div style="background:var(--brand-light);border:1px solid #d4a0aa;border-radius:8px;padding:.6rem 1rem;font-size:.78rem;color:var(--brand-dark);margin-bottom:1rem;display:flex;align-items:center;gap:.5rem">
<span>Results</span>
<span>Showing results from your last run on <strong id="last-run-date"></strong> - generate a new plan to update</span>
</div>
</div>
<div id="results-content" style="display:none">
<div class="card" id="summary-card">
<div class="card-hd">
<span style="font-size:1.3rem">Summary</span>
<div>
<div class="card-title">Route Summary</div>
<div class="card-hint" id="summary-hint"></div>
</div>
<a class="dl-btn" id="dl-link-2" href="#" download style="margin-left:auto;padding:.5rem 1rem;font-size:.8rem">Excel</a>
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
<th>Kids Ride Time</th>
<th>Distance</th>
</tr>
</thead>
<tbody id="summary-tbody"></tbody>
</table>
</div>
</div>
<div class="recalc-bar" id="recalc-bar">
<span>You have unsaved changes - recalculate to update times and Excel</span>
<button class="recalc-btn" id="recalc-btn" onclick="recalculate()">Recalculate Routes</button>
</div>
<div class="unassigned-tray" id="unassigned-tray">
<div class="unassigned-title">Unassigned Students</div>
<div class="unassigned-list" id="unassigned-list"></div>
</div>
<div class="veh-list" id="veh-list"></div>
</div>
</div>
</div>

<script>
const VEHICLE_NAMES = ['Vehicle A','Vehicle B','Vehicle C','Vehicle D','Vehicle E',
'Vehicle F','Vehicle G','Vehicle H','Vehicle I','Vehicle J','Vehicle K','Vehicle L'];
const CAPACITIES = [3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,24,26,28,30,40,50];
const DEFAULT_FLEET = [
  {name:'Vehicle A', address:'', capacity:5},
  {name:'Vehicle B', address:'', capacity:13},
];

let csvFile = null;
let currentJobId = null;
let pollTimer = null;
let lastLineCount = 0;
let routeData = null;

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});

const builder = document.getElementById('fleet-builder');
let fleet = JSON.parse(JSON.stringify(DEFAULT_FLEET));

function renderFleet() {
  builder.innerHTML = '';
  fleet.forEach((veh, i) => {
    const row = document.createElement('div');
    row.className = 'fleet-row';
    row.innerHTML =
      '<select data-idx="' + i + '" data-field="name">' +
      VEHICLE_NAMES.map(n => '<option value="' + n + '"' + (n===veh.name?' selected':'') + '>' + n + '</option>').join('') +
      '</select>' +
      '<input type="text" placeholder="e.g. 828 Elbow Lane, Warrington, PA" value="' + veh.address + '" data-idx="' + i + '" data-field="address">' +
      '<select data-idx="' + i + '" data-field="capacity">' +
      CAPACITIES.map(c => '<option value="' + c + '"' + (c===veh.capacity?' selected':'') + '>' + c + ' riders</option>').join('') +
      '</select>' +
      '<button class="rm-row" data-idx="' + i + '" title="Remove">X</button>';
    builder.appendChild(row);
  });
  updateFleetSummary();
  updateRunBtn();
}

builder.addEventListener('change', e => {
  const idx = +e.target.dataset.idx;
  const field = e.target.dataset.field;
  fleet[idx][field] = field === 'capacity' ? parseInt(e.target.value) : e.target.value;
  updateFleetSummary();
  updateRunBtn();
});

builder.addEventListener('input', e => {
  const idx = +e.target.dataset.idx;
  const field = e.target.dataset.field;
  if (field === 'address') { fleet[idx].address = e.target.value; updateRunBtn(); }
});

builder.addEventListener('click', e => {
  if (e.target.classList.contains('rm-row')) {
    const idx = +e.target.dataset.idx;
    if (fleet.length > 1) { fleet.splice(idx, 1); renderFleet(); }
  }
});

document.getElementById('add-vehicle-btn').addEventListener('click', () => {
  const used = new Set(fleet.map(v => v.name));
  const next = VEHICLE_NAMES.find(n => !used.has(n)) || ('Vehicle ' + (fleet.length + 1));
  fleet.push({name: next, address: '', capacity: 13});
  renderFleet();
});

function updateFleetSummary() {
  const summary = document.getElementById('fleet-summary');
  const total = fleet.reduce((s, v) => s + v.capacity, 0);
  const filled = fleet.filter(v => v.address.trim()).length;
  const seatsOk = studentCount === 0 || total >= studentCount;
  summary.innerHTML =
    '<span class="fleet-chip">Vehicles: ' + fleet.length + '</span>' +
    '<span class="fleet-chip" style="' + (!seatsOk ? 'background:#fde8e8;border-color:#e07070;color:#7a1f1f' : '') + '">' +
    'Seats: ' + total + (studentCount > 0 ? ' / ' + studentCount + ' needed' : '') +
    '</span>' +
    '<span class="fleet-chip" style="' + (filled < fleet.length ? 'background:#fff3cd;border-color:#f0c060' : '') + '">' +
    'Addresses: ' + filled + '/' + fleet.length +
    '</span>';
  checkCapacity();
}

function checkCapacity() {
  const total = fleet.reduce((s, v) => s + v.capacity, 0);
  const warning = document.getElementById('capacity-warning');
  const msg = document.getElementById('capacity-warning-msg');
  if (studentCount > 0 && total < studentCount) {
    const needed = studentCount - total;
    msg.textContent = 'Not enough seats - you have ' + total + ' seats for ' + studentCount + ' students. Add ' + needed + ' more seat' + (needed !== 1 ? 's' : '') + ' by increasing vehicle capacities or adding another vehicle.';
    warning.style.display = 'flex';
  } else {
    warning.style.display = 'none';
  }
  updateRunBtn();
}

function fleetToText() {
  return fleet.map(v =>
    v.name + ': Start: ' + (v.address || '828 Elbow Lane, Warrington, PA') + ' - Capacity: ' + v.capacity + ' riders'
  ).join('\n');
}

const dropZone = document.getElementById('drop-zone');
const csvInput = document.getElementById('csv-file');
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
    document.getElementById('file-rows').textContent = studentCount + ' students';
    checkCapacity();
  };
  reader.readAsText(file);
  fileChosen.classList.add('visible');
  dropZone.querySelector('.drop-icon').textContent = 'OK';
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
  dropZone.querySelector('.drop-icon').textContent = 'CSV';
  document.getElementById('capacity-warning').style.display = 'none';
  updateRunBtn();
});

function updateRunBtn() {
  const hasCSV = !!csvFile;
  const hasAddresses = fleet.some(v => v.address.trim().length > 5);
  const totalSeats = fleet.reduce((s, v) => s + v.capacity, 0);
  const hasEnoughSeats = studentCount === 0 || totalSeats >= studentCount;
  const btn = document.getElementById('run-btn');
  const label = document.getElementById('run-label');
  btn.disabled = !(hasCSV && hasAddresses && hasEnoughSeats);
  if (hasCSV && hasAddresses && !hasEnoughSeats) {
    label.textContent = 'Not enough seats (' + totalSeats + ' / ' + studentCount + ' needed)';
  } else {
    label.textContent = 'Generate Route Plan';
  }
}

document.getElementById('run-btn').addEventListener('click', async () => {
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
    appendLog('Job started - ID: ' + currentJobId);
    pollStatus();
  } catch(err) {
    showError('Could not connect: ' + err.message);
  }
});

function pollStatus() {
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/status/' + currentJobId);
      const data = await res.json();
      const lines = data.progress || [];
      for (let i = lastLineCount; i < lines.length; i++) appendLog(lines[i]);
      lastLineCount = lines.length;
      setPbar(estimatePct(lines));
      if (data.status === 'done') {
        clearInterval(pollTimer);
        setPbar(100);
        document.getElementById('spinner').style.display = 'none';
        document.getElementById('prog-title').textContent = 'Routes generated';
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

function showDone(jobId, lines) {
  setRunning(false);
  const dlUrl = '/api/download/' + jobId;
  document.getElementById('dl-link').href = dlUrl;
  document.getElementById('dl-link-2').href = dlUrl;
  document.getElementById('action-bar').style.display = 'flex';
  if (routeData) {
    buildResultsTab(routeData, jobId);
    const badge = document.getElementById('results-badge');
    badge.textContent = routeData.length;
    badge.style.display = 'inline-block';
    try {
      const campAddr = document.getElementById('camp-address').value.trim();
      localStorage.setItem('elbow_last_routes', JSON.stringify({
        vehicles: routeData,
        savedAt: new Date().toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'}),
        tripDir: tripDirection,
        campAddr: campAddr,
      }));
    } catch(e) {}
  }
}

document.getElementById('view-results-btn').addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelector('[data-tab="results"]').classList.add('active');
  document.getElementById('tab-results').classList.add('active');
});

function buildResultsTab(vehicles, jobId, initEditable=true) {
  document.getElementById('results-empty').style.display = 'none';
  document.getElementById('results-content').style.display = 'block';
  if (jobId) document.getElementById('results-stale').style.display = 'none';
  if (initEditable) initEditableRoutes(vehicles);

  const totalRiders = vehicles.reduce((s, v) => s + v.rider_count, 0);
  const totalCap = vehicles.reduce((s, v) => s + v.capacity, 0);
  const campAddr = document.getElementById('camp-address').value.trim() || '828 Elbow Lane, Warrington, PA';
  const dirLabel = tripDirection === 'afternoon' ? 'All routes depart from' : 'All routes end at';
  const tripLabel = tripDirection === 'afternoon' ? 'Afternoon run' : 'Morning run';
  document.getElementById('summary-hint').textContent =
    tripLabel + ' - ' + totalRiders + ' riders - ' + totalCap + ' seats - ' + vehicles.length + ' vehicles - ' + dirLabel + ' ' + campAddr;

  const tbody = document.getElementById('summary-tbody');
  tbody.innerHTML = '';
  vehicles.forEach(v => {
    const warn = v.under_threshold;
    const pct = v.utilization_pct;
    const barColor = warn ? 'util-warn' : 'util-ok';
    const badgeCls = warn ? 'badge-warn' : 'badge-ok';
    const tr = document.createElement('tr');
    if (warn) tr.className = 'warn';
    tr.innerHTML =
      '<td><strong>' + v.name + '</strong></td>' +
      '<td style="font-size:.75rem;color:#888;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (v.corridor || v.start_address) + '</td>' +
      '<td><strong>' + v.rider_count + '</strong> / ' + v.capacity + '</td>' +
      '<td><span class="util-bar-wrap"><span class="util-bar ' + barColor + '" style="width:' + pct + '%"></span></span><span class="badge ' + badgeCls + '">' + pct + '%' + (warn?' !':'') + '</span></td>' +
      '<td>' + v.stop_count + '</td>' +
      '<td>' + v.total_time + '</td>' +
      '<td>' + v.total_distance + '</td>';
    tbody.appendChild(tr);
  });

  const totTr = document.createElement('tr');
  totTr.className = 'summary-totals';
  const totalRidersAll = vehicles.reduce((s,v)=>s+v.rider_count,0);
  const totalCapAll = vehicles.reduce((s,v)=>s+v.capacity,0);
  totTr.innerHTML =
    '<td colspan="2"><strong>TOTAL</strong></td>' +
    '<td><strong>' + totalRidersAll + ' / ' + totalCapAll + '</strong></td>' +
    '<td><strong>' + Math.round(totalRidersAll/totalCapAll*100) + '%</strong></td>' +
    '<td><strong>' + vehicles.reduce((s,v)=>s+v.stop_count,0) + '</strong></td>' +
    '<td>-</td><td>-</td>';
  tbody.appendChild(totTr);

  const unassignedTray = document.getElementById('unassigned-tray');
  if (unassignedTray) {
    unassignedTray.classList.remove('visible');
    const list = document.getElementById('unassigned-list');
    if (list) list.innerHTML = '';
  }

  const vehList = document.getElementById('veh-list');
  vehList.innerHTML = '';
  vehicles.forEach(v => {
    const card = document.createElement('div');
    card.className = 'veh-card' + (v.under_threshold ? ' warn-card' : '');
    const mapId = 'map-' + v.name.replace(/\s+/g,'-');
    const campAddr = document.getElementById('camp-address').value.trim() || '828 Elbow Lane, Warrington, PA';

    const stopsHtml = v.stops.map((s, si) => {
      const addrParts = s.address.split(',');
      const street = addrParts[0] || s.address;
      const cityState = addrParts.slice(1).join(',').trim();
      const riderPills = s.rider_names.split(', ')
        .filter(r => r.trim())
        .map(r => '<span class="rider-pill" data-rider="' + r + '" data-vehicle="' + v.name + '" data-address="' + s.address + '">' +
          r + '<button class="rider-remove" title="Remove ' + r + '" onclick="removeRider(this, \'' + v.name + '\', ' + si + ', \'' + r + '\')">X</button></span>').join('');
      return '<tr data-address="' + s.address + '" data-vehicle="' + v.name + '">' +
        '<td class="stop-num">' + s.stop_num + '</td>' +
        '<td class="stop-addr">' + street + '<div class="stop-city">' + cityState + '</div></td>' +
        '<td class="stop-riders">' + riderPills + '<br><span class="stop-rider-count" style="font-size:.7rem;color:#aaa">' + s.rider_count + ' rider' + (s.rider_count!==1?'s':'') + '</span></td>' +
        '<td class="stop-time">' + s.drive_time + '</td></tr>';
    }).join('');

    card.innerHTML =
      '<div class="veh-header">' +
      '<span class="veh-name">' + v.name + '</span>' +
      '<span class="veh-corridor">' + (v.corridor || v.start_address) + '</span>' +
      '<div class="veh-stats">' +
      '<span class="veh-stat"><strong>' + v.rider_count + '</strong>/' + v.capacity + ' riders</span>' +
      '<span class="veh-stat"><strong>' + v.utilization_pct + '%</strong></span>' +
      '<span class="veh-stat">' + v.total_time + '</span>' +
      '<span class="veh-stat">' + v.total_distance + '</span>' +
      '</div>' +
      '<span class="veh-chevron">v</span>' +
      '</div>' +
      '<div class="veh-body">' +
      (v.under_threshold ? '<div style="background:#fff3cd;border:1px solid #f0c060;border-radius:6px;padding:.6rem .9rem;font-size:.78rem;color:#7a4f00;margin-bottom:.75rem">This vehicle is below 60% capacity.</div>' : '') +
      '<div class="veh-map" id="' + mapId + '"><div class="map-loading">Loading map...</div></div>' +
      '<div class="edit-bar">' +
      '<button class="recalc-btn" id="recalc-' + v.name.replace(/\s+/g,'-') + '" onclick="recalculate()" disabled>Recalculate Routes</button>' +
      '<span class="edit-hint">Click X on a rider to remove them from this route</span>' +
      '</div>' +
      '<table class="stop-table" id="stop-table-' + v.name.replace(/\s+/g,'-') + '">' +
      '<thead><tr><th>#</th><th>Address</th><th>Riders</th><th>Drive Time</th></tr></thead>' +
      '<tbody>' +
      '<tr class="stop-row-start"><td class="stop-num">Start</td><td class="stop-addr" colspan="2">' + v.start_address + '<div class="stop-city">Departure point</div></td><td class="stop-time">-</td></tr>' +
      stopsHtml +
      '<tr class="stop-row-arrive"><td class="stop-num">End</td><td class="stop-addr" colspan="2">' + campAddr + '<div class="stop-city">Camp - destination</div></td><td class="stop-time">ARRIVE</td></tr>' +
      '</tbody></table></div>';

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

const initializedMaps = {};
let googleMapsLoaded = false;
let googleMapsLoading = false;
const mapQueue = [];

function loadGoogleMapsAPI() {
  if (googleMapsLoaded || googleMapsLoading) return;
  googleMapsLoading = true;
  const script = document.createElement('script');
  script.src = 'https://maps.googleapis.com/maps/api/js?key=' + window.GOOGLE_MAPS_KEY + '&libraries=geometry,marker&callback=onGoogleMapsLoaded&v=beta';
  script.async = true;
  document.head.appendChild(script);
}

window.onGoogleMapsLoaded = function() {
  googleMapsLoaded = true;
  googleMapsLoading = false;
  mapQueue.forEach(fn => fn());
  mapQueue.length = 0;
};

function initVehicleMap(mapId, vehicle) {
  const el = document.getElementById(mapId);
  if (!el || initializedMaps[mapId]) return;
  const campAddr = document.getElementById('camp-address').value.trim() || '828 Elbow Lane, Warrington, PA 18976';
  const allPoints = [];

  vehicle.stops.forEach((s, i) => {
    const lat = parseFloat(s.lat);
    const lng = parseFloat(s.lon);
    if (!isNaN(lat) && !isNaN(lng) && Math.abs(lat) > 0.001 && Math.abs(lng) > 0.001) {
      allPoints.push({lat, lng, label: String(i+1), type: 'stop', riders: s.rider_names, address: s.address.split(',')[0]});
    }
  });

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
  const GOLD = '#c9a84c';
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

  const infoWindow = new google.maps.InfoWindow();
  const bounds = new google.maps.LatLngBounds();
  allPoints.forEach(p => bounds.extend({lat: p.lat, lng: p.lng}));

  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");

  function drawMarkers() {
    allPoints.forEach(pt => {
      const size = pt.type === 'camp' ? 32 : 28;
      const bg = pt.type === 'camp' ? GREEN : GOLD;
      const fg = pt.type === 'camp' ? '#fff' : '#1a1018';
      const lbl = pt.type === 'camp' ? 'Camp' : pt.label;
      const pinEl = document.createElement('div');
      pinEl.style.cssText = 'width:' + size + 'px;height:' + size + 'px;background:' + bg + ';border:2.5px solid #fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:' + (size<=28?'11':'13') + 'px;font-weight:700;color:' + fg + ';font-family:Arial;box-shadow:0 2px 8px rgba(0,0,0,.5);cursor:pointer';
      pinEl.textContent = lbl;
      const popup = pt.type === 'camp' ? '<strong>Camp</strong><br>' + campAddr : '<strong>Stop ' + pt.label + '</strong><br>' + pt.address + '<br><em>' + pt.riders + '</em>';
      const m = new AdvancedMarkerElement({position:{lat:pt.lat,lng:pt.lng}, map, content:pinEl, zIndex:200});
      m.addListener('click', () => { infoWindow.setContent(popup); infoWindow.open(map, m); });
    });
    map.fitBounds(bounds, {top:40, right:40, bottom:40, left:40});
  }

  try {
    const resp = await fetch('/api/route-polyline', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({points: allPoints.map(p => ({lat: p.lat, lng: p.lng}))}),
    });
    const data = await resp.json();
    if (data.coords && data.coords.length > 1) {
      new google.maps.Polyline({path: data.coords, map, strokeColor: BRAND, strokeWeight: 4, strokeOpacity: .85});
    } else {
      new google.maps.Polyline({path: allPoints.map(p => ({lat:p.lat, lng:p.lng})), map, strokeColor: BRAND, strokeWeight: 3, strokeOpacity: .6});
    }
  } catch(e) {
    new google.maps.Polyline({path: allPoints.map(p => ({lat:p.lat, lng:p.lng})), map, strokeColor: BRAND, strokeWeight: 3, strokeOpacity: .6});
  }

  drawMarkers();
}

let editableRoutes = null;
let unassignedRiders = [];

function initEditableRoutes(vehicles) {
  editableRoutes = JSON.parse(JSON.stringify(vehicles));
}

function removeRider(btn, vehicleName, stopIdx, riderName) {
  if (!editableRoutes) return;
  const veh = editableRoutes.find(v => v.name === vehicleName);
  if (!veh) return;
  const stop = veh.stops[stopIdx];
  if (!stop) return;
  const riderList = stop.rider_names.split(', ').filter(r => r.trim() && r !== riderName);
  if (riderList.length === 0) {
    veh.stops.splice(stopIdx, 1);
  } else {
    stop.rider_names = riderList.join(', ');
    stop.rider_count = riderList.length;
  }
  unassignedRiders.push({name: riderName, fromVehicle: vehicleName, stopAddress: stop.address, lat: stop.lat, lon: stop.lon});
  document.getElementById('recalc-bar').classList.add('visible');
  buildResultsTab(editableRoutes, currentJobId, false);
  updateUnassignedTray();
}

function updateUnassignedTray() {
  const tray = document.getElementById('unassigned-tray');
  const list = document.getElementById('unassigned-list');
  if (unassignedRiders.length === 0) { tray.classList.remove('visible'); return; }
  tray.classList.add('visible');
  const vehOptions = (editableRoutes || []).map(v => '<option value="' + v.name + '">' + v.name + '</option>').join('');
  list.innerHTML = unassignedRiders.map((r, i) =>
    '<div class="unassigned-chip">' +
    '<span>' + r.name + '</span>' +
    '<span style="color:#aaa;font-size:.7rem">from ' + r.fromVehicle + '</span>' +
    '<select id="assign-select-' + i + '"><option value="">Assign to...</option>' + vehOptions + '</select>' +
    '<button class="reassign-btn" onclick="assignRider(' + i + ')">Move</button>' +
    '</div>'
  ).join('');
}

function assignRider(idx) {
  const select = document.getElementById('assign-select-' + idx);
  const targetVehicleName = select.value;
  if (!targetVehicleName || !editableRoutes) return;
  const rider = unassignedRiders[idx];
  const targetVeh = editableRoutes.find(v => v.name === targetVehicleName);
  if (!targetVeh) return;
  const currentRiders = targetVeh.stops.reduce((s, st) => s + (st.rider_count || 1), 0);
  if (currentRiders >= targetVeh.capacity) { alert(targetVehicleName + ' is already at full capacity (' + targetVeh.capacity + ' riders)'); return; }
  const existingStop = targetVeh.stops.find(s => s.address === rider.stopAddress);
  if (existingStop) {
    existingStop.rider_names = existingStop.rider_names ? existingStop.rider_names + ', ' + rider.name : rider.name;
    existingStop.rider_count = (existingStop.rider_count || 0) + 1;
  } else {
    targetVeh.stops.push({stop_num: targetVeh.stops.length + 1, address: rider.stopAddress, rider_names: rider.name, rider_count: 1, drive_time: '- recalculate', lat: rider.lat, lon: rider.lon});
  }
  unassignedRiders.splice(idx, 1);
  buildResultsTab(editableRoutes, currentJobId, false);
  updateUnassignedTray();
  document.getElementById('recalc-bar').classList.add('visible');
}

async function recalculate() {
  if (!editableRoutes || !currentJobId) return;
  const btn = document.getElementById('recalc-btn');
  btn.disabled = true;
  btn.textContent = 'Recalculating...';
  try {
    const resp = await fetch('/api/recalculate/' + currentJobId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({vehicles: editableRoutes}),
    });
    const data = await resp.json();
    if (data.error) { alert('Recalculation failed: ' + data.error); return; }
    routeData = data.route_data;
    editableRoutes = JSON.parse(JSON.stringify(routeData));
    unassignedRiders = [];
    buildResultsTab(editableRoutes, currentJobId, false);
    updateUnassignedTray();
    document.getElementById('recalc-bar').classList.remove('visible');
    document.getElementById('recalc-bar').innerHTML = '<span>Routes recalculated successfully</span><button class="recalc-btn" id="recalc-btn" onclick="recalculate()">Recalculate Again</button>';
    document.getElementById('recalc-bar').classList.add('visible');
    setTimeout(() => document.getElementById('recalc-bar').classList.remove('visible'), 3000);
    try {
      const campAddr = document.getElementById('camp-address').value.trim();
      localStorage.setItem('elbow_last_routes', JSON.stringify({vehicles: routeData, savedAt: new Date().toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'}), tripDir: tripDirection, campAddr: campAddr}));
    } catch(e) {}
  } catch(e) {
    alert('Network error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Recalculate Routes';
  }
}

function setRunning(on) {
  const btn = document.getElementById('run-btn');
  btn.disabled = on;
  document.getElementById('run-icon').textContent = on ? 'Loading' : 'Map';
  document.getElementById('run-label').textContent = on ? 'Generating...' : 'Generate Route Plan';
  document.getElementById('spinner').style.display = on ? 'block' : 'none';
}

function appendLog(line) {
  const div = document.createElement('div');
  if (line.includes('saved') || line.includes('Loaded')) div.className='ok';
  else if (line.includes('warn')) div.className='warn';
  else if (line.includes('Error')) div.className='err';
  div.textContent = line;
  const log = document.getElementById('log');
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function setPbar(pct) { document.getElementById('pbar').style.width = Math.min(100,pct)+'%'; }

function estimatePct(lines) {
  if (!lines.length) return 5;
  const last = lines[lines.length-1]||'';
  if (last.includes('Saved')||last.includes('saved')) return 100;
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

renderFleet();

try {
  const saved = localStorage.getItem('elbow_last_routes');
  if (saved) {
    const parsed = JSON.parse(saved);
    if (parsed.vehicles && parsed.vehicles.length > 0) {
      if (parsed.campAddr) document.getElementById('camp-address').value = parsed.campAddr;
      if (parsed.tripDir) setTrip(parsed.tripDir);
      document.getElementById('last-run-date').textContent = parsed.savedAt;
      document.getElementById('results-stale').style.display = 'block';
      buildResultsTab(parsed.vehicles, null);
      const badge = document.getElementById('results-badge');
      badge.textContent = parsed.vehicles.length;
      badge.style.display = 'inline-block';
    }
  }
} catch(e) {}
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)