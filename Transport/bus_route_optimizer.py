"""
Bus Route Optimizer for Elbow Lane Day Camp
============================================
Uses OpenStreetMap Nominatim (free, no API key) to geocode every individual
street address to exact lat/lon, then clusters students by true address-level
proximity — completely ignoring ZIP codes.

Dependencies:
    pip install openpyxl geopy requests

Usage (CLI):
    python bus_route_optimizer.py --csv students.csv --vehicles vehicles.txt --output routes.xlsx

Usage (import):
    from bus_route_optimizer import generate_routes
    generate_routes(csv_text, vehicles_text, "output.xlsx")
"""

import argparse
import csv
import io
import json
import math
import time
import re
import os
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMP_ADDRESS  = "828 Elbow Lane, Warrington, PA 18976"
CAMP_COORDS   = (40.2454, -75.1407)    # fallback if geocode fails
GEOCACHE_FILE = "geocache.json"         # persists between runs to avoid re-geocoding
NEIGHBOR_MI   = 1.5                     # addresses within 1.5 mi = candidate same-van

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Student:
    idx: int
    last: str
    first: str
    address: str
    city: str
    zip_code: str
    lat: float = 0.0
    lon: float = 0.0
    geocoded: bool = False

    @property
    def full_address(self) -> str:
        return f"{self.address}, {self.city}, PA {self.zip_code}"


@dataclass
class Stop:
    address: str
    riders: list = field(default_factory=list)
    drive_time: str = ""
    lat: float = 0.0
    lon: float = 0.0

    @property
    def rider_count(self) -> int:
        return len(self.riders)

    @property
    def rider_names(self) -> str:
        seen: dict[str, int] = {}
        for s in self.riders:
            seen[s.last] = seen.get(s.last, 0) + 1
        counters: dict[str, int] = {}
        names = []
        for s in self.riders:
            if seen[s.last] > 1:
                counters[s.last] = counters.get(s.last, 0) + 1
                names.append(f"{s.last}{counters[s.last]}")
            else:
                names.append(s.last)
        return ", ".join(names)


@dataclass
class Vehicle:
    name: str
    start_address: str
    capacity: int
    stops: list = field(default_factory=list)
    total_time: str = ""
    total_distance: str = ""
    time_source: str = "Estimated"
    start_lat: float = 0.0
    start_lon: float = 0.0

    @property
    def rider_count(self) -> int:
        return sum(s.rider_count for s in self.stops)

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def utilization_pct(self) -> int:
        return round(self.rider_count / self.capacity * 100) if self.capacity else 0

    @property
    def corridor(self) -> str:
        cities, seen = [], set()
        for stop in self.stops:
            parts = stop.address.split(",")
            city = parts[1].strip() if len(parts) >= 3 else ""
            if city and city not in seen:
                seen.add(city)
                cities.append(city)
        start_city = self.start_address.split(",")[1].strip() if "," in self.start_address else ""
        return f"{start_city} → " + " → ".join(cities[:3]) if cities else start_city


# ---------------------------------------------------------------------------
# Haversine — real distance between two GPS coordinates
# ---------------------------------------------------------------------------

def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Geocoding — OpenStreetMap Nominatim (free, no API key required)
# ---------------------------------------------------------------------------

def _load_geocache() -> dict:
    if os.path.exists(GEOCACHE_FILE):
        try:
            with open(GEOCACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_geocache(cache: dict) -> None:
    try:
        with open(GEOCACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def geocode_address(address: str, cache: dict, progress_cb=None) -> tuple:
    """
    Geocode a single address to (lat, lon) using OpenStreetMap Nominatim.
    - Free, no API key needed
    - Results cached to disk so the same address is never looked up twice
    - Nominatim requires max 1 request/second — enforced automatically
    """
    key = address.strip().lower()
    if key in cache:
        return tuple(cache[key])

    if progress_cb:
        progress_cb(f"  Geocoding: {address}")

    try:
        params = urllib.parse.urlencode({
            "q": address,
            "format": "json",
            "limit": 1,
            "addressdetails": 0,
        })
        url = f"https://nominatim.openstreetmap.org/search?{params}"
        req = urllib.request.Request(url, headers={
            # Nominatim requires a descriptive User-Agent
            "User-Agent": "ElbowLaneCampBusRouter/1.0"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode())

        if results:
            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            cache[key] = [lat, lon]
            _save_geocache(cache)
            time.sleep(1.1)   # Respect Nominatim rate limit: 1 req/sec
            return lat, lon

    except Exception as e:
        if progress_cb:
            progress_cb(f"  ⚠ Geocode failed for '{address}': {e}")

    # Fallback: return camp coords so student doesn't crash the algorithm
    return CAMP_COORDS


def geocode_all_addresses(addresses: list, progress_cb=None) -> dict:
    """
    Geocode a list of unique addresses.
    Returns dict: address_string -> (lat, lon)
    Only calls the API for addresses not already in the cache.
    """
    cache = _load_geocache()
    uncached = [a for a in addresses if a.strip().lower() not in cache]

    if uncached and progress_cb:
        progress_cb(f"Geocoding {len(uncached)} addresses via OpenStreetMap "
                    f"(cached: {len(addresses) - len(uncached)}, "
                    f"~{len(uncached)}s remaining)...")

    result = {}
    for addr in addresses:
        result[addr] = geocode_address(addr, cache, progress_cb)

    return result


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_students_csv(csv_text: str) -> list:
    if "\n" not in csv_text and len(csv_text) < 300:
        try:
            with open(csv_text, newline="", encoding="utf-8-sig") as f:
                csv_text = f.read()
        except FileNotFoundError:
            pass

    students = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        row = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items()}
        idx_key = next((k for k in row if k in ("", "idx", "#")), "")
        try:
            idx = int(row.get(idx_key, 0))
        except ValueError:
            idx = 0
        last     = row.get("Last name",               row.get("last_name",  ""))
        first    = row.get("First name",              row.get("first_name", ""))
        addr     = row.get("Primary family address 1", row.get("address",   ""))
        city     = row.get("Primary family city",     row.get("city",       ""))
        zip_code = row.get("Primary family zip",      row.get("zip",        ""))
        if last and addr:
            students.append(Student(idx, last, first, addr, city, zip_code))
    return students


# ---------------------------------------------------------------------------
# Vehicle config parsing
# ---------------------------------------------------------------------------

def parse_vehicles_text(text: str) -> list:
    """
    Handles many real-world formats:
        Vehicle A: Start: 7826 Loretto Ave, Philadelphia, PA - Capacity: 5 riders
        Vehicle B Start: 12 Rachel Rd, Richboro, PA Capacity: up to 13 riders
        Vehicles D
        Start: 1045 N West End Blvd, Quakertown PA - Capacity: up to 13 riders
        E-H (5 vehicles)Start & End: 828 Elbow Lane - Capacity: up to 13 riders each
    """
    VEHICLE_START_RE = re.compile(
        r"""^(?:
            (?:vehicles?\s+[A-Z0-9][-\s,A-Z0-9]*)
          | (?:[A-Z][-][A-Z]\s*(?:\(|$))
          | (?:[A-Z]\s*(?:\(|:|\s+Start))
          | (?:Van\s+[A-Z0-9])
        )""", re.VERBOSE | re.IGNORECASE,
    )

    # Merge continuation lines
    merged = []
    for raw in text.strip().splitlines():
        s = raw.strip()
        if not s:
            continue
        if merged and not VEHICLE_START_RE.match(s):
            merged[-1] += " " + s
        else:
            merged.append(s)

    # Expand letter ranges (e.g. "E-H")
    RANGE_RE = re.compile(
        r"^(?:vehicles?\s*)?([A-Z])[-]([A-Z])(?:\s*\(\d+\s*vehicles?\))?",
        re.IGNORECASE,
    )
    expanded = []
    for line in merged:
        m = RANGE_RE.match(line)
        if m:
            sl, el = m.group(1).upper(), m.group(2).upper()
            remainder = line[m.end():].strip().lstrip(":")
            for c in range(ord(sl), ord(el) + 1):
                expanded.append(f"Vehicle {chr(c)}: {remainder}")
        else:
            expanded.append(line)

    vehicles = []
    for line in expanded:
        line = line.strip()
        if not line:
            continue

        name_m = re.match(r"^((?:Vehicles?|Van)\s+[A-Z0-9]+)", line, re.IGNORECASE)
        if not name_m:
            name_m = re.match(r"^([A-Z])(?:\s*:|\s+Start)", line, re.IGNORECASE)
        if not name_m:
            continue

        raw_name = name_m.group(1).strip()
        name = re.sub(r"^Vehicles\b", "Vehicle", raw_name, flags=re.IGNORECASE)
        rest = line[name_m.end():].strip().lstrip(":").strip()

        start_m = re.search(r"Start(?:\s*[&]\s*End)?\s*:?\s*", rest, re.IGNORECASE)
        if not start_m:
            continue
        after = rest[start_m.end():]

        addr_end = re.search(r"\s*[-]?\s*Capacity\b", after, re.IGNORECASE)
        start_addr = (after[:addr_end.start()].strip() if addr_end else after.strip()).rstrip(" -,")

        cap_m = re.search(r"Capacity\s*:?\s*(?:up\s+to\s+)?(\d+)\s*riders?", rest, re.IGNORECASE)
        capacity = int(cap_m.group(1)) if cap_m else 13

        if name and start_addr:
            vehicles.append({"name": name, "start": start_addr, "capacity": capacity})

    return vehicles


# ---------------------------------------------------------------------------
# Core routing — real address geocoding, ZIP codes never used
# ---------------------------------------------------------------------------

def cluster_and_route(students: list, vehicles: list, progress_cb=None) -> list:
    """
    Full routing pipeline:
    1. Geocode every unique street address to exact lat/lon (OpenStreetMap)
    2. Cluster students by TRUE address proximity (<1.5 mi between houses)
       ZIP codes are completely ignored
    3. Assign clusters to vehicles by nearest start location (strict capacity)
    4. Sequence stops furthest-from-camp first within each vehicle
    """

    # ── 1. Geocode all addresses ──────────────────────────────────────────
    unique_student_addrs = list({s.full_address for s in students})
    vehicle_start_addrs  = [v["start"] for v in vehicles]
    all_addrs = list({*unique_student_addrs, *vehicle_start_addrs, CAMP_ADDRESS})

    coords = geocode_all_addresses(all_addrs, progress_cb)

    camp_lat, camp_lon = coords.get(CAMP_ADDRESS, CAMP_COORDS)

    # Attach real coordinates to each student
    for s in students:
        lat, lon = coords.get(s.full_address, CAMP_COORDS)
        s.lat, s.lon = lat, lon
        s.geocoded = (lat, lon) != CAMP_COORDS

    geocoded_count = sum(1 for s in students if s.geocoded)
    if progress_cb:
        progress_cb(f"Geocoded {geocoded_count}/{len(students)} student addresses successfully")

    # ── 2. Group exact-same-address students (families) ──────────────────
    addr_groups: dict = {}
    for s in students:
        addr_groups.setdefault(s.address.lower().strip(), []).append(s)
    family_units = list(addr_groups.values())

    def unit_coords(unit):
        return unit[0].lat, unit[0].lon

    def dist_to_camp(unit):
        lat, lon = unit_coords(unit)
        return haversine_mi(lat, lon, camp_lat, camp_lon)

    # ── 3. Cluster by true address-level proximity (NO ZIP logic) ────────
    #
    # For every family unit, find the existing cluster that contains an
    # address within NEIGHBOR_MI miles. If none found, start a new cluster.
    # This means two houses 0.4 mi apart in different ZIPs → same cluster.
    # Two houses 3 mi apart in the same ZIP → different clusters.
    #
    clusters: list = []
    for unit in family_units:
        ulat, ulon = unit_coords(unit)
        best_ci   = None
        best_dist = float("inf")

        for ci, cluster in enumerate(clusters):
            for existing_unit in cluster:
                elat, elon = unit_coords(existing_unit)
                d = haversine_mi(ulat, ulon, elat, elon)
                if d <= NEIGHBOR_MI and d < best_dist:
                    best_dist = d
                    best_ci   = ci

        if best_ci is not None:
            clusters[best_ci].append(unit)
        else:
            clusters.append([unit])

    if progress_cb:
        progress_cb(f"Formed {len(clusters)} geographic clusters "
                    f"(threshold: {NEIGHBOR_MI} mi between actual addresses)")

    # ── 4. Build Vehicle objects with geocoded start coords ───────────────
    veh_objects = []
    for v in vehicles:
        lat, lon = coords.get(v["start"], CAMP_COORDS)
        veh_objects.append(Vehicle(
            name=v["name"], start_address=v["start"], capacity=v["capacity"],
            start_lat=lat, start_lon=lon,
        ))

    # ── 5. Assign family units to vehicles ────────────────────────────────
    # Sort farthest-from-camp first so distant students get priority placement
    all_units = sorted(family_units, key=dist_to_camp, reverse=True)

    assignments: list = [[] for _ in veh_objects]
    counts      = [0]  * len(veh_objects)

    def score(unit, vi) -> float:
        """Score vehicle vi for this unit. Lower = better. inf = full."""
        remaining = veh_objects[vi].capacity - counts[vi]
        if remaining < len(unit):
            return float("inf")   # HARD CAP — never exceed capacity
        ulat, ulon = unit_coords(unit)
        # Base score: distance from this address to vehicle's start
        geo = haversine_mi(ulat, ulon, veh_objects[vi].start_lat, veh_objects[vi].start_lon)
        # Clustering bonus: reward keeping nearby students on the same van
        if assignments[vi]:
            nearest = min(
                haversine_mi(ulat, ulon, *unit_coords(eu))
                for eu in assignments[vi]
            )
            geo -= min(2.0, max(0.0, 2.0 - nearest))
        return geo

    unplaced = []
    for unit in all_units:
        best = min(range(len(veh_objects)), key=lambda vi: score(unit, vi))
        if score(unit, best) < float("inf"):
            assignments[best].append(unit)
            counts[best] += len(unit)
        else:
            unplaced.append(unit)

    # Second pass: place stragglers in vehicle with most remaining space
    for unit in unplaced:
        size = len(unit)
        candidates = [
            (veh_objects[vi].capacity - counts[vi], vi)
            for vi in range(len(veh_objects))
            if veh_objects[vi].capacity - counts[vi] >= size
        ]
        vi = max(candidates)[1] if candidates else min(
            range(len(veh_objects)), key=lambda i: counts[i]
        )
        assignments[vi].append(unit)
        counts[vi] += size

    # ── 6. Sequence stops: furthest from camp first (no backtracking) ─────
    for vi, veh in enumerate(veh_objects):
        if not assignments[vi]:
            veh.total_time = "—"
            veh.total_distance = "—"
            continue

        # Build address-level stops
        addr_stop: dict = {}
        for unit in assignments[vi]:
            rep = unit[0]
            key = rep.address.lower().strip()
            if key not in addr_stop:
                addr_stop[key] = Stop(address=rep.full_address, lat=rep.lat, lon=rep.lon)
            addr_stop[key].riders.extend(unit)

        # Order stops: farthest house from camp goes first
        sorted_stops = sorted(
            addr_stop.values(),
            key=lambda s: haversine_mi(s.lat, s.lon, camp_lat, camp_lon),
            reverse=True,
        )

        # Drive times between consecutive stops (using real coordinates)
        prev_lat, prev_lon = veh.start_lat, veh.start_lon
        for i, stop in enumerate(sorted_stops):
            d    = haversine_mi(prev_lat, prev_lon, stop.lat, stop.lon)
            mins = max(2, round(d * 3.0))   # ~3 min/mile for suburban PA roads
            stop.drive_time = f"{mins} min from start" if i == 0 else f"{mins} min"
            prev_lat, prev_lon = stop.lat, stop.lon

        veh.stops = sorted_stops

        # Total route time
        total_mins = sum(
            int(s.drive_time.replace(" min from start", "").replace(" min", ""))
            for s in sorted_stops
            if s.drive_time.replace(" min from start", "").replace(" min", "").isdigit()
        )
        last = sorted_stops[-1]
        total_mins += max(3, round(haversine_mi(last.lat, last.lon, camp_lat, camp_lon) * 3.0))

        if total_mins >= 60:
            hrs, rem = divmod(total_mins, 60)
            veh.total_time = f"{hrs} hr {rem} min *" if rem else f"{hrs} hr *"
        else:
            veh.total_time = f"{total_mins} min *"

        # Total distance
        dist = 0.0
        plat, plon = veh.start_lat, veh.start_lon
        for stop in sorted_stops:
            dist += haversine_mi(plat, plon, stop.lat, stop.lon)
            plat, plon = stop.lat, stop.lon
        dist += haversine_mi(plat, plon, camp_lat, camp_lon)
        veh.total_distance = f"{round(dist, 1)} mi"
        veh.time_source    = "Estimated"

    return veh_objects


# ---------------------------------------------------------------------------
# Excel generation
# ---------------------------------------------------------------------------

CAMP_BLUE   = "1F497D"
LIGHT_BLUE  = "DDEEFF"
LIGHT_GRAY  = "F2F2F2"
GREEN_FILL  = "E2EFDA"
YELLOW_FILL = "FFEB9C"
WHITE       = "FFFFFF"
DARK_TEXT   = "1A1A1A"
MED_SIDE    = Side(style="medium", color="1F497D")
THIN_SIDE   = Side(style="thin",   color="AAAAAA")


def _fill(c):  return PatternFill("solid", start_color=c, fgColor=c)
def _font(bold=False, size=11, color=DARK_TEXT, italic=False):
    return Font(name="Arial", bold=bold, size=size, color=color, italic=italic)
def _align(h="left", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)
def _border(**kw): return Border(**kw)


def build_dashboard(wb: Workbook, vehicles: list):
    ws = wb.active
    ws.title = "Route Summary"
    for col, w in zip("ABCDEFGH", [18, 54, 10, 10, 9, 14, 13, 14]):
        ws.column_dimensions[col].width = w

    # Row 1: title
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value     = "🚌  Elbow Lane Day Camp — Vehicle Route Plan"
    c.font      = _font(bold=True, size=16, color=WHITE)
    c.fill      = _fill(CAMP_BLUE)
    c.alignment = _align("center")
    ws.row_dimensions[1].height = 32

    # Row 2: subtitle
    ws.merge_cells("A2:H2")
    c = ws["A2"]
    c.value = ("All vehicles finish at: 828 Elbow Lane, Warrington, PA  |  "
               "Clustered by real street-address proximity (OpenStreetMap)  |  "
               "Times marked * are estimates")
    c.font      = _font(size=9, italic=True, color="444444")
    c.fill      = _fill(LIGHT_BLUE)
    c.alignment = _align("center")
    ws.row_dimensions[2].height = 18

    # Row 3: headers
    hdrs = ["Vehicle","Starting Point / Route Corridor",
            "Capacity","Riders","Stops","Est. Time","Distance","Source"]
    for ci, h in enumerate(hdrs, 1):
        cell = ws.cell(row=3, column=ci, value=h)
        cell.font      = _font(bold=True, size=10, color=WHITE)
        cell.fill      = _fill(CAMP_BLUE)
        cell.alignment = _align("center")
        cell.border    = _border(bottom=MED_SIDE, top=MED_SIDE,
                                 left=THIN_SIDE,  right=THIN_SIDE)
    ws.row_dimensions[3].height = 18

    total_cap = total_riders = 0
    for ri, veh in enumerate(vehicles):
        row = 4 + ri
        bg   = _fill(WHITE) if ri % 2 == 0 else _fill(LIGHT_GRAY)
        tfill = _fill(GREEN_FILL if veh.time_source == "Google Maps" else YELLOW_FILL)
        vals = [veh.name,
                f"{veh.start_address}  |  {veh.corridor}",
                veh.capacity, veh.rider_count, veh.stop_count,
                veh.total_time, veh.total_distance, veh.time_source]
        for ci, val in enumerate(vals, 1):
            cell = ws.cell(row=row, column=ci, value=val)
            cell.font      = _font(size=10)
            cell.fill      = tfill if ci == 6 else bg
            cell.alignment = _align("left" if ci == 2 else "center")
            cell.border    = _border(bottom=Side(style="thin", color="CCCCCC"))
        total_cap    += veh.capacity
        total_riders += veh.rider_count
        ws.row_dimensions[row].height = 15

    # Totals row
    tr = 4 + len(vehicles)
    ws.merge_cells(f"A{tr}:B{tr}")
    c = ws[f"A{tr}"]
    c.value = (f"TOTAL  ({total_riders} riders / {total_cap} capacity "
               f"/ {len(vehicles)} vehicles)")
    c.font = _font(bold=True, size=10, color=WHITE)
    c.fill = _fill(CAMP_BLUE)
    c.alignment = _align("center")
    for ci, val in [(3, total_cap), (4, f"=SUM(D4:D{tr-1})"),
                    (5, f"=SUM(E4:E{tr-1})"), (6,"—"),(7,"—"),(8,"—")]:
        cell = ws.cell(row=tr, column=ci, value=val)
        cell.font = _font(bold=True, color=WHITE)
        cell.fill = _fill(CAMP_BLUE)
        cell.alignment = _align("center")
    ws.row_dimensions[tr].height = 18

    # Legend
    lr = tr + 2
    ws.merge_cells(f"A{lr}:H{lr}")
    c = ws[f"A{lr}"]
    c.value = ("LEGEND:   🟡 Yellow * = Estimated time  |  "
               "Clustering uses real geocoded street addresses (OpenStreetMap) — ZIP codes ignored  |  "
               "🔵 Blue = Google Maps verified")
    c.font      = _font(size=9, italic=True, color="555555")
    c.alignment = _align("left")
    ws.row_dimensions[lr].height = 16


def build_vehicle_sheet(wb: Workbook, veh: Vehicle):
    ws = wb.create_sheet(title=veh.name)
    for col, w in zip("ABCDE", [9, 40, 28, 12, 22]):
        ws.column_dimensions[col].width = w

    # Row 1
    ws.merge_cells("A1:E1")
    c = ws["A1"]
    c.value     = f"🚌  {veh.name}  —  Route Sheet"
    c.font      = _font(bold=True, size=14, color=WHITE)
    c.fill      = _fill(CAMP_BLUE)
    c.alignment = _align("center")
    ws.row_dimensions[1].height = 28

    # Row 2: summary
    ws.merge_cells("A2:E2")
    confirmed = veh.time_source == "Google Maps"
    c = ws["A2"]
    c.value = (f"Start: {veh.start_address}   |   Cap: {veh.capacity}   |   "
               f"Riders: {veh.rider_count} ({veh.utilization_pct}%)   |   "
               f"Total Route: {veh.total_time}, {veh.total_distance}   "
               f"[{'✓ Google Maps' if confirmed else '* Estimated'}]")
    c.font      = _font(size=9, italic=True)
    c.fill      = _fill(GREEN_FILL if confirmed else YELLOW_FILL)
    c.alignment = _align("left")
    ws.row_dimensions[2].height = 16

    # Row 3: column headers
    for ci, h in enumerate(["Stop #","Address","Riders","# Riders","Drive Time"], 1):
        cell = ws.cell(row=3, column=ci, value=h)
        cell.font      = _font(bold=True, size=10, color=WHITE)
        cell.fill      = _fill(CAMP_BLUE)
        cell.alignment = _align("center")
        cell.border    = _border(bottom=MED_SIDE)
    ws.row_dimensions[3].height = 16

    # Row 4: START
    for ci, val in enumerate([" START", veh.start_address, "Departure point", None, None], 1):
        cell = ws.cell(row=4, column=ci, value=val)
        cell.font      = _font(bold=(ci==1), italic=(ci==3), color="444444")
        cell.fill      = _fill(LIGHT_GRAY)
        cell.alignment = _align("left" if ci == 2 else "center")
    ws.row_dimensions[4].height = 14

    # Stop rows
    ds = 5
    for si, stop in enumerate(veh.stops):
        row = ds + si
        bg  = _fill(WHITE) if si % 2 == 0 else _fill(LIGHT_GRAY)
        for ci, val in enumerate([si+1, stop.address, stop.rider_names,
                                   stop.rider_count, stop.drive_time], 1):
            cell = ws.cell(row=row, column=ci, value=val)
            cell.font      = _font(bold=(ci==1), size=10)
            cell.fill      = bg
            cell.alignment = _align("center" if ci in (1,4) else "left")
            cell.border    = _border(bottom=Side(style="thin", color="CCCCCC"))
        ws.row_dimensions[row].height = 14

    # ARRIVE row
    arrive = ds + len(veh.stops)
    if veh.stops:
        last = veh.stops[-1]
        final = max(3, round(haversine_mi(last.lat, last.lon, *CAMP_COORDS) * 3.0))
        arrive_time = f"{final} min → ARRIVE"
    else:
        arrive_time = "— → ARRIVE"

    for ci, val in enumerate(["ARRIVE","828 Elbow Lane, Warrington, PA",
                               "—","—", arrive_time], 1):
        cell = ws.cell(row=arrive, column=ci, value=val)
        cell.font      = _font(bold=True, color="006400")
        cell.fill      = _fill(GREEN_FILL)
        cell.alignment = _align("left" if ci == 2 else "center")
        cell.border    = _border(top=MED_SIDE, bottom=MED_SIDE)
    ws.row_dimensions[arrive].height = 16

    # Totals
    tr = arrive + 1
    ws.cell(row=tr, column=4, value="Total Riders:").font = _font(bold=True)
    ws.cell(row=tr, column=4).alignment = _align("right")
    cell = ws.cell(row=tr, column=5, value=f"=SUM(D{ds}:D{arrive-1})")
    cell.font = _font(bold=True)
    cell.alignment = _align("center")

    # Note
    nr = tr + 1
    ws.merge_cells(f"A{nr}:E{nr}")
    note = ws[f"A{nr}"]
    if confirmed:
        note.value = "✓ Drive time confirmed via Google Maps"
        note.font  = _font(size=9, color="006400", italic=True)
    else:
        note.value = ("* Drive time estimated (straight-line × 3 min/mi)  |  "
                      "Stop order based on real geocoded coordinates — ZIP codes not used")
        note.font  = _font(size=9, color="996600", italic=True)
    note.alignment = _align("left")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_routes(
    csv_text: str,
    vehicles_text: str,
    output_path: str = "bus_routes_output.xlsx",
    route_data: Optional[list] = None,
    progress_cb=None,
) -> str:
    """
    Full pipeline: CSV + fleet config → geocode → cluster by real proximity → Excel.

    Geocoding results are cached in geocache.json so re-runs are fast.
    First run with new addresses takes ~1 second per unique address.
    """
    students = parse_students_csv(csv_text)
    if not students:
        raise ValueError("No students parsed from CSV. Check format.")

    vehicle_configs = parse_vehicles_text(vehicles_text)
    if not vehicle_configs:
        raise ValueError("No vehicles parsed. Check fleet configuration format.")

    if progress_cb:
        progress_cb(f"Loaded {len(students)} students across "
                    f"{len({s.full_address for s in students})} unique addresses, "
                    f"{len(vehicle_configs)} vehicles")

    if route_data:
        vehicles = _apply_ai_route_data(students, vehicle_configs, route_data)
    else:
        vehicles = cluster_and_route(students, vehicle_configs, progress_cb)

    wb = Workbook()
    build_dashboard(wb, vehicles)
    for veh in vehicles:
        build_vehicle_sheet(wb, veh)

    wb.save(output_path)
    if progress_cb:
        progress_cb(f"✅ Saved: {output_path}")
    return output_path


def _apply_ai_route_data(students, vehicle_configs, route_data) -> list:
    vehicles = []
    for rd in route_data:
        veh = Vehicle(
            name=rd.get("vehicle_name", "Vehicle ?"),
            start_address=rd.get("start_address", ""),
            capacity=rd.get("capacity", 13),
            total_time=rd.get("total_time", "—"),
            total_distance=rd.get("total_distance", "—"),
            time_source=rd.get("time_source", "Estimated"),
        )
        for sd in rd.get("stops", []):
            stop = Stop(address=sd.get("address", ""))
            for name in sd.get("rider_names", []):
                stop.riders.append(Student(0, name, "", "", "", ""))
            stop.drive_time = sd.get("drive_time", "")
            veh.stops.append(stop)
        vehicles.append(veh)
    return vehicles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bus Route Optimizer — Elbow Lane Day Camp")
    parser.add_argument("--csv",        required=True)
    parser.add_argument("--vehicles",   required=True)
    parser.add_argument("--output",     default="bus_routes_output.xlsx")
    parser.add_argument("--route-json", default=None)
    args = parser.parse_args()

    with open(args.csv, encoding="utf-8-sig") as f:
        csv_text = f.read()
    try:
        with open(args.vehicles) as f:
            vehicles_text = f.read()
    except (FileNotFoundError, IsADirectoryError):
        vehicles_text = args.vehicles.replace("\\n", "\n")

    route_data = None
    if args.route_json:
        with open(args.route_json) as f:
            route_data = json.load(f)

    generate_routes(csv_text, vehicles_text, args.output, route_data, print)


if __name__ == "__main__":
    main()
