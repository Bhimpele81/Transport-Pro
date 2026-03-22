"""
Elbow Lane Day Camp — Bus Route Optimizer
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

# ── Serialise route data for JSON API ─────────────────────────────────────────

def vehicles_to_json(vehicles: list) -> list:
    out = []
    for v in vehicles:
        out.append({
            "name": v.name,
            "start_address": v.start_address,
            "capacity