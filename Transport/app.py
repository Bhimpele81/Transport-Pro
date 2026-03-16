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

def run_job(job_id: str, csv_text: str, vehicles_text: str):
    output_path = os.path.join("outputs", f"routes_{job_id}.xlsx")

    def progress(msg: str):
        with jobs_lock:
            jobs[job_id]["progress"].append(msg)

    try:
        with jobs_lock:
            jobs[job_id]["status"] = "running"

        students = parse_students_csv(csv_text)
        vcfgs    = parse_vehicles_text(vehicles_text)
        vehicles = cluster_and_route(students, vcfgs, progress)

        # Save Excel
        from openpyxl import Workbook
        from bus_route_optimizer import build_dashboard, build_vehicle_sheet
        wb = Workbook()
        build_dashboard(wb, vehicles)
        for veh in vehicles:
            build_vehicle_sheet(wb, veh)
        wb.save(output_path)

        progress("✅  Excel saved")

        with jobs_lock:
            jobs[job_id]["status"]    = "done"
            jobs[job_id]["output_path"] = output_path
            jobs[job_id]["route_data"]  = vehicles_to_json(vehicles)

    except Exception as e:
        import traceback
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"]  = str(e)
        progress(f"❌ Error: {e}")


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

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

    csv_text = csv_file.read().decode("utf-8-sig", errors="replace")

    try:
        students = parse_students_csv(csv_text)
        if not students:
            return jsonify({"error": "No students found in CSV. Check column names."}), 400
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
            "status":      "queued",
            "progress":    [f"✓ Loaded {len(students)} students, {len(vcfgs)} vehicles"],
            "output_path": None,
            "route_data":  None,
            "error":       None,
        }

    threading.Thread(
        target=run_job, args=(job_id, csv_text, vehicles_text), daemon=True
    ).start()

    return jsonify({"job_id": job_id})


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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Elbow Lane — Bus Route Optimizer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
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
.h-logo{width:58px;height:58px;flex-shrink:0;object-fit:cover;object-position:center center;display:block}
.h-title{font-family:'Oswald',sans-serif;font-size:1.3rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.h-sub{font-size:.72rem;opacity:.75;font-weight:400;margin-top:2px;letter-spacing:.08em;text-transform:uppercase}
.h-badge{margin-left:auto;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);color:#fff;font-size:.68rem;font-family:'Oswald',sans-serif;font-weight:500;letter-spacing:.12em;text-transform:uppercase;padding:.35rem .9rem;border-radius:20px;white-space:nowrap}

/* tabs */
.tab-bar{display:flex;background:#fff;border-bottom:2px solid var(--border);position:sticky;top:80px;z-index:100}
.tab{padding:.85rem 1.75rem;font-size:.82rem;font-weight:500;font-family:'Oswald',sans-serif;letter-spacing:.07em;text-transform:uppercase;color:#999;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;transition:color .15s,border-color .15s;white-space:nowrap;display:flex;align-items:center;gap:.5rem}
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
.card-title{font-family:'Oswald',sans-serif;font-size:1.1rem;font-weight:600;color:var(--brand-dark);letter-spacing:.04em;text-transform:uppercase}
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
.run-btn{width:100%;padding:.95rem 2rem;background:var(--brand);color:#fff;border:none;border-radius:var(--r);font-family:'Oswald',sans-serif;font-size:1.1rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:.65rem;transition:background .18s,transform .1s,box-shadow .18s;box-shadow:0 4px 14px rgba(109,31,47,.3);margin-top:1.25rem}
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
.results-empty .empty-icon{font-size:3rem;margin-bottom:1rem}
.results-empty p{font-size:.9rem;line-height:1.6}

/* dashboard summary table */
.summary-table{width:100%;border-collapse:collapse;font-size:.83rem}
.summary-table th{background:var(--brand);color:#fff;padding:.6rem .9rem;text-align:left;font-family:'Oswald',sans-serif;font-weight:500;font-size:.82rem;letter-spacing:.08em;text-transform:uppercase}
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
.veh-name{font-family:'Oswald',sans-serif;font-size:1rem;font-weight:600;color:var(--brand-dark);min-width:90px;letter-spacing:.04em;text-transform:uppercase}
.veh-corridor{font-size:.78rem;color:#888;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.veh-stats{display:flex;align-items:center;gap:.75rem;margin-left:auto;flex-shrink:0}
.veh-stat{font-size:.78rem;color:#666;white-space:nowrap}
.veh-stat strong{color:var(--ink)}
.veh-chevron{color:#bbb;transition:transform .2s;font-size:.85rem;flex-shrink:0}
.veh-card.open .veh-chevron{transform:rotate(180deg)}
.veh-body{display:none;padding:0 1.1rem 1.1rem;border-top:1px solid var(--border)}
.veh-card.open .veh-body{display:block}

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
.stop-row-start .stop-num{color:var(--brand-mid)}
.stop-row-arrive .stop-num{color:var(--success);font-weight:700}
.stop-row-arrive .stop-time{color:var(--success);font-weight:600}
.rider-pill{display:inline-block;background:var(--brand-light);color:var(--brand-dark);border-radius:10px;padding:.1rem .5rem;font-size:.72rem;font-weight:500;margin:.1rem .15rem .1rem 0}

/* totals row */
.summary-totals{background:var(--brand)!important;color:#fff}
.summary-totals td{color:#fff!important;font-weight:700;border-bottom:none!important}

/* responsive */
@media(max-width:640px){.fleet-row{grid-template-columns:1fr 1fr;grid-template-rows:auto auto}.fleet-col-label{display:none}.veh-stats{display:none}.tab span:not(.tab-badge){display:none}}
</style>
</head>
<body>

<header>
  <img src="/logo.png" alt="Elbow Lane Day Camp" class="h-logo">
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

  <!-- Step 1: CSV -->
  <div class="card">
    <div class="card-hd">
      <span class="card-num">1</span>
      <div>
        <div class="card-title">Student Roster</div>
        <div class="card-hint">Upload the CSV exported from your camp management software</div>
      </div>
    </div>
    <div class="drop-zone" id="drop-zone">
      <input type="file" id="csv-file" accept=".csv">
      <div class="drop-icon">📋</div>
      <div class="drop-text"><strong>Click to choose</strong> or drag & drop your CSV file</div>
      <div class="drop-meta">Columns needed: Last name · First name · Primary family address 1 · Primary family city · Primary family zip</div>
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
    // Badge
    const badge = document.getElementById('results-badge');
    badge.textContent = routeData.length;
    badge.style.display = 'inline-block';
  }
}

document.getElementById('view-results-btn').addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelector('[data-tab="results"]').classList.add('active');
  document.getElementById('tab-results').classList.add('active');
});

// ── Build results tab ──────────────────────────────────────────────────────
function buildResultsTab(vehicles, jobId) {
  document.getElementById('results-empty').style.display = 'none';
  document.getElementById('results-content').style.display = 'block';

  const totalRiders = vehicles.reduce((s, v) => s + v.rider_count, 0);
  const totalCap    = vehicles.reduce((s, v) => s + v.capacity, 0);
  document.getElementById('summary-hint').textContent =
    `${totalRiders} riders · ${totalCap} seats · ${vehicles.length} vehicles · All routes end at 828 Elbow Lane, Warrington, PA`;

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

  // Vehicle accordion cards
  const vehList = document.getElementById('veh-list');
  vehList.innerHTML = '';
  vehicles.forEach(v => {
    const card = document.createElement('div');
    card.className = 'veh-card' + (v.under_threshold ? ' warn-card' : '');

    const riders = v.stops.map(s => s.rider_names).filter(Boolean);

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
        ${v.under_threshold ? `<div style="background:#fff3cd;border:1px solid #f0c060;border-radius:6px;padding:.6rem .9rem;font-size:.78rem;color:#7a4f00;margin-bottom:.75rem">⚠ This vehicle is below 75% capacity — it serves a geographically isolated area that cannot be merged without splitting neighbour groups.</div>` : ''}
        <table class="stop-table">
          <thead><tr><th>#</th><th>Address</th><th>Riders</th><th>Drive Time</th></tr></thead>
          <tbody>
            <tr class="stop-row-start">
              <td class="stop-num">▶</td>
              <td class="stop-addr" colspan="2">${v.start_address}<div class="stop-city">Departure point</div></td>
              <td class="stop-time">—</td>
            </tr>
            ${v.stops.map(s => {
              const addrParts = s.address.split(',');
              const street = addrParts[0] || s.address;
              const cityState = addrParts.slice(1).join(',').trim();
              const riderPills = s.rider_names.split(', ')
                .map(r => `<span class="rider-pill">${r}</span>`).join('');
              return `<tr>
                <td class="stop-num">${s.stop_num}</td>
                <td class="stop-addr">${street}<div class="stop-city">${cityState}</div></td>
                <td class="stop-riders">${riderPills}<br><span style="font-size:.7rem;color:#aaa">${s.rider_count} rider${s.rider_count!==1?'s':''}</span></td>
                <td class="stop-time">${s.drive_time}</td>
              </tr>`;
            }).join('')}
            <tr class="stop-row-arrive">
              <td class="stop-num">⛳</td>
              <td class="stop-addr" colspan="2">828 Elbow Lane, Warrington, PA<div class="stop-city">Camp — destination</div></td>
              <td class="stop-time">→ ARRIVE</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;

    card.querySelector('.veh-header').addEventListener('click', () => {
      card.classList.toggle('open');
    });

    vehList.appendChild(card);
  });
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

// ── Init ───────────────────────────────────────────────────────────────────
renderFleet();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
