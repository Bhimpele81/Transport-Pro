"""
Bus Route Optimizer for Elbow Lane Day Camp
Generates optimized routes with geographic clustering and produces a formatted Excel file.

Usage:
    python bus_route_optimizer.py \
        --csv students.csv \
        --vehicles "Vehicle A|7826 Loretto Ave, Philadelphia, PA|5,Vehicle B|12 Rachel Rd, Richboro, PA|13" \
        --output routes_output.xlsx

Or import and call generate_routes() directly.
"""

import argparse
import csv
import io
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side
)
from openpyxl.utils import get_column_letter


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

    @property
    def full_address(self) -> str:
        return f"{self.address}, {self.city}, PA {self.zip_code}"

    @property
    def display_name(self) -> str:
        return self.last


@dataclass
class Stop:
    address: str
    riders: list[Student] = field(default_factory=list)
    drive_time: str = ""        # e.g. "12 min" or "At start"

    @property
    def rider_count(self) -> int:
        return len(self.riders)

    @property
    def rider_names(self) -> str:
        names = []
        seen_last = {}
        for s in self.riders:
            seen_last[s.last] = seen_last.get(s.last, 0) + 1
        counters = {}
        for s in self.riders:
            if seen_last[s.last] > 1:
                counters[s.last] = counters.get(s.last, 0) + 1
                names.append(f"{s.last}{counters[s.last]}")
            else:
                names.append(s.last)
        return ", ".join(names)


@dataclass
class Vehicle:
    name: str               # "Vehicle A"
    start_address: str
    capacity: int
    stops: list[Stop] = field(default_factory=list)
    total_time: str = ""
    total_distance: str = ""
    time_source: str = "Estimated"   # "Google Maps" or "Estimated"

    @property
    def rider_count(self) -> int:
        return sum(s.rider_count for s in self.stops)

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def utilization_pct(self) -> int:
        if self.capacity == 0:
            return 0
        return round(self.rider_count / self.capacity * 100)

    @property
    def corridor(self) -> str:
        """Short human-readable description of route corridor."""
        cities = []
        seen = set()
        for stop in self.stops:
            city = stop.address.split(",")[1].strip() if "," in stop.address else ""
            if city and city not in seen:
                seen.add(city)
                cities.append(city)
        start_city = self.start_address.split(",")[1].strip() if "," in self.start_address else ""
        return f"{start_city} → " + " → ".join(cities[:3]) if cities else start_city


# ---------------------------------------------------------------------------
# Haversine distance (fallback when no API key)
# ---------------------------------------------------------------------------

# Approximate lat/lon for zip codes common to this dataset (Bucks/Montgomery County PA)
ZIP_COORDS: dict[str, tuple[float, float]] = {
    "18901": (40.3101, -75.1299),  # Doylestown
    "18902": (40.2815, -75.0951),  # Doylestown (east)
    "18914": (40.2868, -75.2066),  # Chalfont
    "18929": (40.2501, -75.0835),  # Jamison
    "18954": (40.2168, -74.9988),  # Richboro
    "18974": (40.2312, -75.0629),  # Warwick/Ivyland
    "18976": (40.2454, -75.1407),  # Warrington
    "19002": (40.1557, -75.2271),  # Ambler/Maple Glen
    "19025": (40.1390, -75.1771),  # Dresher
    "19040": (40.1807, -75.1063),  # Hatboro
    "19044": (40.1901, -75.1257),  # Horsham
    "19090": (40.1485, -75.1202),  # Willow Grove
    "19446": (40.2415, -75.2840),  # Lansdale
    "19446-4443": (40.2415, -75.2840),
    "19446-1677": (40.2415, -75.2840),
    "19025": (40.1390, -75.1771),
}

DEST_COORDS = (40.2454, -75.1407)   # 828 Elbow Lane, Warrington PA


def _zip5(z: str) -> str:
    return z[:5]


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def approx_dist_mi(zip1: str, zip2: str) -> float:
    c1 = ZIP_COORDS.get(_zip5(zip1))
    c2 = ZIP_COORDS.get(_zip5(zip2))
    if c1 and c2:
        return haversine_mi(*c1, *c2)
    return 999.0


def dist_to_dest(zip_code: str) -> float:
    c = ZIP_COORDS.get(_zip5(zip_code))
    if c:
        return haversine_mi(*c, *DEST_COORDS)
    return 999.0


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_students_csv(csv_text: str) -> list[Student]:
    """Parse CSV text (or file path) into Student objects."""
    # Accept file path or raw CSV text
    if "\n" not in csv_text and len(csv_text) < 300:
        try:
            with open(csv_text, newline="", encoding="utf-8-sig") as f:
                csv_text = f.read()
        except FileNotFoundError:
            pass

    students = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        # Normalize column names (handle BOM, extra quotes, etc.)
        row = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items()}
        idx_key = next((k for k in row if k in ("", "idx", "#")), "")
        try:
            idx = int(row.get(idx_key, 0))
        except ValueError:
            idx = 0

        last = row.get("Last name", row.get("last_name", ""))
        first = row.get("First name", row.get("first_name", ""))
        addr = row.get("Primary family address 1", row.get("address", ""))
        city = row.get("Primary family city", row.get("city", ""))
        zip_code = row.get("Primary family zip", row.get("zip", ""))

        if last and addr:
            students.append(Student(idx, last, first, addr, city, zip_code))

    return students


# ---------------------------------------------------------------------------
# Vehicle config parsing
# ---------------------------------------------------------------------------

def parse_vehicles_text(text: str) -> list[dict]:
    """
    Parse a block like:
        Vehicle A: Start: 7826 Loretto Ave, Philadelphia, PA - Capacity: 5 riders
        Vehicle B: Start: 12 Rachel Rd, Richboro, PA - Capacity: 13 riders

    Returns list of dicts: {name, start, capacity}
    """
    vehicles = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            # Split on first colon to get name
            if ":" not in line:
                continue
            name_part, rest = line.split(":", 1)
            name = name_part.strip()
            rest = rest.strip()

            # Extract start address
            start = ""
            if "Start:" in rest:
                start_part = rest.split("Start:")[1]
                if " - Capacity" in start_part:
                    start = start_part.split(" - Capacity")[0].strip()
                elif "Capacity" in start_part:
                    start = start_part.split("Capacity")[0].strip(" -").strip()
                else:
                    start = start_part.strip()

            # Extract capacity
            capacity = 13
            if "Capacity:" in rest:
                cap_str = rest.split("Capacity:")[1].strip().split()[0]
                capacity = int("".join(c for c in cap_str if c.isdigit()) or "13")
            elif "capacity" in rest.lower():
                import re
                m = re.search(r"(\d+)\s*riders?", rest, re.I)
                if m:
                    capacity = int(m.group(1))

            if name and start:
                vehicles.append({"name": name, "start": start, "capacity": capacity})
        except Exception:
            continue

    return vehicles


# ---------------------------------------------------------------------------
# Core routing algorithm
# ---------------------------------------------------------------------------

def _extract_zip_from_address(addr: str) -> str:
    """Extract 5-digit ZIP from a full address string."""
    import re
    m = re.search(r"\b(\d{5})\b", addr)
    return m.group(1) if m else ""


def _vehicle_start_zip(veh: "Vehicle") -> str:
    return _extract_zip_from_address(veh.start_address)


def _stop_zip(stop: "Stop") -> str:
    return _extract_zip_from_address(stop.address)


def cluster_students(students: list[Student], vehicles: list[dict]) -> list[Vehicle]:
    """
    Assign students to vehicles using geographic clustering:
    1. Families (same address) stay together
    2. Addresses sharing same ZIP code (or ZIPs within 1.5mi) cluster together
    3. Clusters assigned to vehicle whose start is geographically closest
    4. Stops ordered furthest-from-camp first (no backtracking)
    """
    # --- Step 1: Group by exact address ---
    addr_groups: dict[str, list[Student]] = {}
    for s in students:
        key = s.address.lower().strip()
        addr_groups.setdefault(key, []).append(s)
    family_units = list(addr_groups.values())

    def unit_zip(unit: list[Student]) -> str:
        return _zip5(unit[0].zip_code)

    # --- Step 2: Cluster family units by geographic proximity ---
    NEIGHBOR_THRESHOLD_MI = 2.0  # slightly larger for zip-centroid approximation
    clusters: list[list[list[Student]]] = []

    for unit in family_units:
        z = unit_zip(unit)
        assigned = False
        for cluster in clusters:
            for existing_unit in cluster:
                ez = unit_zip(existing_unit)
                # Same zip = definitely neighbors
                if ez == z or approx_dist_mi(z, ez) <= NEIGHBOR_THRESHOLD_MI:
                    cluster.append(unit)
                    assigned = True
                    break
            if assigned:
                break
        if not assigned:
            clusters.append([unit])

    # --- Step 2.5: Split oversized clusters so each fits in at least one vehicle ---
    max_vehicle_capacity = max(v["capacity"] for v in vehicles)
    split_clusters: list[list[list[Student]]] = []
    for cluster in clusters:
        if sum(len(u) for u in cluster) <= max_vehicle_capacity:
            split_clusters.append(cluster)
        else:
            current: list[list[Student]] = []
            current_count = 0
            for unit in sorted(cluster, key=lambda u: unit_zip(u)):
                unit_size = len(unit)
                if current_count + unit_size > max_vehicle_capacity and current:
                    split_clusters.append(current)
                    current = []
                    current_count = 0
                current.append(unit)
                current_count += unit_size
            if current:
                split_clusters.append(current)
    clusters = split_clusters

    # --- Step 3: Build Vehicle objects ---
    veh_objects: list[Vehicle] = [
        Vehicle(name=v["name"], start_address=v["start"], capacity=v["capacity"])
        for v in vehicles
    ]

    # Sort clusters by distance to destination (farthest first)
    def cluster_avg_dist_to_dest(cluster: list[list[Student]]) -> float:
        zips = [unit_zip(u) for u in cluster]
        dists = [dist_to_dest(z) for z in zips if z]
        return sum(dists) / len(dists) if dists else 0.0

    sorted_clusters = sorted(clusters, key=cluster_avg_dist_to_dest, reverse=True)

    # --- Step 4: Assign clusters to vehicles (proximity + capacity) ---
    vehicle_assignments: list[list] = [[] for _ in veh_objects]
    vehicle_counts = [0] * len(veh_objects)

    # First pass: try to respect capacity hard limits
    unassigned: list = []
    for cluster in sorted_clusters:
        cluster_zip = unit_zip(cluster[0])
        cluster_size = sum(len(u) for u in cluster)

        best_vi = None
        best_score = float("inf")
        for vi, veh in enumerate(veh_objects):
            remaining = veh.capacity - vehicle_counts[vi]
            if remaining < cluster_size:
                continue  # skip — not enough room
            vz = _vehicle_start_zip(veh)
            geo_dist = approx_dist_mi(cluster_zip, vz) if vz and cluster_zip else 10.0
            if geo_dist < best_score:
                best_score = geo_dist
                best_vi = vi

        if best_vi is None:
            # Try partial fit (first vehicle with any space)
            for vi, veh in enumerate(veh_objects):
                if vehicle_counts[vi] < veh.capacity:
                    best_vi = vi
                    break

        if best_vi is None:
            unassigned.append(cluster)
            continue

        vehicle_assignments[best_vi].append(cluster)
        vehicle_counts[best_vi] += cluster_size

    # Second pass: place any overflow in least-full vehicle
    for cluster in unassigned:
        best_vi = min(range(len(veh_objects)), key=lambda i: vehicle_counts[i])
        vehicle_assignments[best_vi].append(cluster)
        vehicle_counts[best_vi] += sum(len(u) for u in cluster)

    # --- Step 5: Build stops and sequence them ---
    for vi, veh in enumerate(veh_objects):
        assigned_clusters = vehicle_assignments[vi]
        if not assigned_clusters:
            veh.total_time = "—"
            veh.total_distance = "—"
            continue

        # Aggregate family units into address-keyed stops
        addr_stop: dict[str, Stop] = {}
        for cluster in assigned_clusters:
            for unit in cluster:
                rep = unit[0]
                key = rep.address.lower().strip()
                if key not in addr_stop:
                    addr_stop[key] = Stop(address=rep.full_address)
                addr_stop[key].riders.extend(unit)

        # Sort: furthest from destination first
        sorted_stops = sorted(
            addr_stop.values(),
            key=lambda s: dist_to_dest(_stop_zip(s)),
            reverse=True
        )

        # Compute sequential leg drive times
        prev_zip = _vehicle_start_zip(veh)
        for i, stop in enumerate(sorted_stops):
            sz = _stop_zip(stop)
            d = approx_dist_mi(prev_zip, sz) if (prev_zip and sz) else 5.0
            mins = max(2, round(d * 3.0))  # ~3 min/mile suburban PA
            stop.drive_time = f"{mins} min from start" if i == 0 else f"{mins} min"
            prev_zip = sz or prev_zip

        veh.stops = sorted_stops

        # Total route time (sum of legs + final leg to camp)
        total_mins = 0
        for stop in sorted_stops:
            t = stop.drive_time.replace(" min from start", "").replace(" min", "").strip()
            if t.isdigit():
                total_mins += int(t)

        last_zip = _stop_zip(sorted_stops[-1]) if sorted_stops else ""
        final_leg = max(3, round(approx_dist_mi(last_zip, "18976") * 3.0)) if last_zip else 12
        total_mins += final_leg

        if total_mins >= 60:
            hrs, mins_rem = divmod(total_mins, 60)
            veh.total_time = f"{hrs} hr {mins_rem} min *" if mins_rem else f"{hrs} hr *"
        else:
            veh.total_time = f"{total_mins} min *"

        # Total distance
        total_dist = 0.0
        pz = _vehicle_start_zip(veh)
        for stop in sorted_stops:
            sz = _stop_zip(stop)
            if pz and sz:
                total_dist += approx_dist_mi(pz, sz)
            pz = sz or pz
        if last_zip:
            total_dist += approx_dist_mi(last_zip, "18976")
        veh.total_distance = f"{round(total_dist, 1)} mi"
        veh.time_source = "Estimated"

    return veh_objects


# ---------------------------------------------------------------------------
# Excel generation
# ---------------------------------------------------------------------------

CAMP_BLUE = "1F497D"
HEADER_GOLD = "C6EFCE"
LIGHT_BLUE = "DDEEFF"
LIGHT_GRAY = "F2F2F2"
GREEN_FILL = "E2EFDA"
YELLOW_FILL = "FFEB9C"
WHITE = "FFFFFF"
DARK_TEXT = "1A1A1A"
MEDIUM_BORDER = Side(style="medium", color="1F497D")
THIN_BORDER = Side(style="thin", color="AAAAAA")


def _border(top=None, bottom=None, left=None, right=None):
    return Border(top=top, bottom=bottom, left=left, right=right)


def _fill(hex_color: str):
    return PatternFill("solid", start_color=hex_color, fgColor=hex_color)


def _font(bold=False, size=11, color=DARK_TEXT, italic=False):
    return Font(name="Arial", bold=bold, size=size, color=color, italic=italic)


def _align(h="left", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def _set_row(ws, row, values, fonts=None, fills=None, aligns=None, heights=None):
    for ci, val in enumerate(values, 1):
        cell = ws.cell(row=row, column=ci, value=val)
        if fonts and ci <= len(fonts) and fonts[ci - 1]:
            cell.font = fonts[ci - 1]
        if fills and ci <= len(fills) and fills[ci - 1]:
            cell.fill = fills[ci - 1]
        if aligns and ci <= len(aligns) and aligns[ci - 1]:
            cell.alignment = aligns[ci - 1]


def build_dashboard(wb: Workbook, vehicles: list[Vehicle]):
    ws = wb.active
    ws.title = "Route Summary"

    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 52
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 9
    ws.column_dimensions["F"].width = 14
    ws.column_dimensions["G"].width = 13
    ws.column_dimensions["H"].width = 14

    # Row 1: Title
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value = "🚌  Elbow Lane Day Camp — Vehicle Route Plan"
    c.font = _font(bold=True, size=16, color=WHITE)
    c.fill = _fill(CAMP_BLUE)
    c.alignment = _align("center")
    ws.row_dimensions[1].height = 32

    # Row 2: Subtitle
    ws.merge_cells("A2:H2")
    c = ws["A2"]
    c.value = (
        "All vehicles finish at: 828 Elbow Lane, Warrington, PA  |  "
        "Times marked * are estimates based on road network knowledge; "
        "others confirmed via Google Maps"
    )
    c.font = _font(size=9, italic=True, color="444444")
    c.fill = _fill(LIGHT_BLUE)
    c.alignment = _align("center")
    ws.row_dimensions[2].height = 18

    # Row 3: Headers
    headers = ["Vehicle", "Starting Point / Route Corridor", "Capacity", "Riders", "Stops", "Est. Time", "Distance", "Source"]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=ci, value=h)
        cell.font = _font(bold=True, size=10, color=WHITE)
        cell.fill = _fill(CAMP_BLUE)
        cell.alignment = _align("center")
        cell.border = _border(
            bottom=MEDIUM_BORDER, top=MEDIUM_BORDER,
            left=THIN_BORDER, right=THIN_BORDER
        )
    ws.row_dimensions[3].height = 18

    # Rows 4+: Vehicle data
    total_capacity = 0
    total_riders = 0
    for ri, veh in enumerate(vehicles):
        row = 4 + ri
        fill = _fill(WHITE) if ri % 2 == 0 else _fill(LIGHT_GRAY)
        is_confirmed = veh.time_source == "Google Maps"
        time_fill = _fill(GREEN_FILL) if is_confirmed else _fill(YELLOW_FILL)

        corridor = f"{veh.start_address}  |  {veh.corridor}"

        cells_vals = [
            veh.name,
            corridor,
            veh.capacity,
            f"=SUM({get_column_letter(4)}{row}:{get_column_letter(4)}{row})",
            veh.stop_count,
            veh.total_time,
            veh.total_distance,
            veh.time_source,
        ]
        # Write raw data (not formula for riders - just count)
        cells_vals[3] = veh.rider_count

        for ci, val in enumerate(cells_vals, 1):
            cell = ws.cell(row=row, column=ci, value=val)
            cell.font = _font(size=10)
            cell.fill = time_fill if ci == 6 else fill
            cell.alignment = _align("center" if ci != 2 else "left")
            cell.border = _border(bottom=Side(style="thin", color="CCCCCC"))

        total_capacity += veh.capacity
        total_riders += veh.rider_count
        ws.row_dimensions[row].height = 15

    # Total row
    total_row = 4 + len(vehicles)
    ws.merge_cells(f"A{total_row}:B{total_row}")
    label_cell = ws.cell(
        row=total_row, column=1,
        value=f"TOTAL  ({total_riders} riders / {total_capacity} capacity / {len(vehicles)} vehicles)"
    )
    label_cell.font = _font(bold=True, size=10, color=WHITE)
    label_cell.fill = _fill(CAMP_BLUE)
    label_cell.alignment = _align("center")

    cap_cell = ws.cell(row=total_row, column=3, value=total_capacity)
    cap_cell.font = _font(bold=True, color=WHITE)
    cap_cell.fill = _fill(CAMP_BLUE)
    cap_cell.alignment = _align("center")

    rider_cell = ws.cell(row=total_row, column=4, value=f"=SUM(D4:D{total_row - 1})")
    rider_cell.font = _font(bold=True, color=WHITE)
    rider_cell.fill = _fill(CAMP_BLUE)
    rider_cell.alignment = _align("center")

    stop_cell = ws.cell(row=total_row, column=5, value=f"=SUM(E4:E{total_row - 1})")
    stop_cell.font = _font(bold=True, color=WHITE)
    stop_cell.fill = _fill(CAMP_BLUE)
    stop_cell.alignment = _align("center")

    for ci in [6, 7, 8]:
        cell = ws.cell(row=total_row, column=ci, value="—")
        cell.font = _font(bold=True, color=WHITE)
        cell.fill = _fill(CAMP_BLUE)
        cell.alignment = _align("center")

    ws.row_dimensions[total_row].height = 18

    # Legend
    legend_row = total_row + 2
    ws.merge_cells(f"A{legend_row}:H{legend_row}")
    leg_cell = ws[f"A{legend_row}"]
    leg_cell.value = (
        "LEGEND:   🔵 Blue time = Confirmed via Google Maps live traffic   |   "
        "🟡 Yellow time * = Estimated based on road network knowledge of Bucks/Montgomery County"
    )
    leg_cell.font = _font(size=9, italic=True, color="555555")
    leg_cell.alignment = _align("left")
    ws.row_dimensions[legend_row].height = 16


def build_vehicle_sheet(wb: Workbook, veh: Vehicle):
    ws = wb.create_sheet(title=veh.name)

    ws.column_dimensions["A"].width = 9
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 22

    # Row 1: Title
    ws.merge_cells("A1:E1")
    c = ws["A1"]
    c.value = f"🚌  {veh.name}  —  Route Sheet"
    c.font = _font(bold=True, size=14, color=WHITE)
    c.fill = _fill(CAMP_BLUE)
    c.alignment = _align("center")
    ws.row_dimensions[1].height = 28

    # Row 2: Summary
    ws.merge_cells("A2:E2")
    confirmed = veh.time_source == "Google Maps"
    src_tag = "✓ Google Maps" if confirmed else "* Estimated"
    summary = (
        f"Start: {veh.start_address}   |   "
        f"Cap: {veh.capacity}   |   "
        f"Riders: {veh.rider_count} ({veh.utilization_pct}%)   |   "
        f"Total Route: {veh.total_time}, {veh.total_distance}   [{src_tag}]"
    )
    c = ws["A2"]
    c.value = summary
    c.font = _font(size=9, italic=True)
    c.fill = _fill(GREEN_FILL if confirmed else YELLOW_FILL)
    c.alignment = _align("left")
    ws.row_dimensions[2].height = 16

    # Row 3: Column headers
    headers = ["Stop #", "Address", "Riders", "# Riders", "Drive Time"]
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=ci, value=h)
        cell.font = _font(bold=True, size=10, color=WHITE)
        cell.fill = _fill(CAMP_BLUE)
        cell.alignment = _align("center")
        cell.border = _border(bottom=MEDIUM_BORDER)
    ws.row_dimensions[3].height = 16

    # Row 4: START row
    ws.cell(row=4, column=1, value=" START").font = _font(bold=True, color="444444")
    ws.cell(row=4, column=2, value=veh.start_address).font = _font(italic=True, color="444444")
    ws.cell(row=4, column=3, value="Departure point").font = _font(italic=True, color="888888")
    for ci in range(1, 6):
        ws.cell(row=4, column=ci).fill = _fill(LIGHT_GRAY)
        ws.cell(row=4, column=ci).alignment = _align("center" if ci != 2 else "left")
    ws.row_dimensions[4].height = 14

    # Stop rows
    data_start_row = 5
    for si, stop in enumerate(veh.stops):
        row = data_start_row + si
        fill = _fill(WHITE) if si % 2 == 0 else _fill(LIGHT_GRAY)

        ws.cell(row=row, column=1, value=si + 1).font = _font(bold=True)
        ws.cell(row=row, column=2, value=stop.address).font = _font(size=10)
        ws.cell(row=row, column=3, value=stop.rider_names).font = _font(size=10)
        ws.cell(row=row, column=4, value=stop.rider_count).font = _font(size=10)
        ws.cell(row=row, column=5, value=stop.drive_time).font = _font(size=10)

        for ci in range(1, 6):
            ws.cell(row=row, column=ci).fill = fill
            ws.cell(row=row, column=ci).alignment = _align(
                "center" if ci in (1, 4) else "left"
            )
            ws.cell(row=row, column=ci).border = _border(
                bottom=Side(style="thin", color="CCCCCC")
            )
        ws.row_dimensions[row].height = 14

    # ARRIVE row
    arrive_row = data_start_row + len(veh.stops)

    # Calculate final leg time
    if veh.stops:
        last_zip = _stop_zip(veh.stops[-1])
        d = approx_dist_mi(last_zip, "18976") if last_zip else 5.0
        final_mins = max(3, round(d * 2.8))
        arrive_time = f"{final_mins} min → ARRIVE"
    else:
        arrive_time = "— → ARRIVE"

    ws.cell(row=arrive_row, column=1, value="ARRIVE").font = _font(bold=True, color="006400")
    ws.cell(row=arrive_row, column=2, value="828 Elbow Lane, Warrington, PA").font = _font(bold=True, color="006400")
    ws.cell(row=arrive_row, column=3, value="—").font = _font(color="006400")
    ws.cell(row=arrive_row, column=4, value="—").font = _font(color="006400")
    ws.cell(row=arrive_row, column=5, value=arrive_time).font = _font(bold=True, color="006400")
    for ci in range(1, 6):
        ws.cell(row=arrive_row, column=ci).fill = _fill(GREEN_FILL)
        ws.cell(row=arrive_row, column=ci).alignment = _align(
            "center" if ci in (1, 3, 4) else "left"
        )
        ws.cell(row=arrive_row, column=ci).border = _border(
            top=MEDIUM_BORDER, bottom=MEDIUM_BORDER
        )
    ws.row_dimensions[arrive_row].height = 16

    # Total riders formula
    total_row = arrive_row + 1
    ws.cell(row=total_row, column=4, value="Total Riders:").font = _font(bold=True)
    ws.cell(row=total_row, column=4).alignment = _align("right")
    formula_cell = ws.cell(
        row=total_row, column=5,
        value=f"=SUM(D{data_start_row}:D{arrive_row - 1})"
    )
    formula_cell.font = _font(bold=True)
    formula_cell.alignment = _align("center")

    # Source note
    note_row = total_row + 1
    ws.merge_cells(f"A{note_row}:E{note_row}")
    note = ws[f"A{note_row}"]
    if confirmed:
        note.value = "✓ Drive time confirmed via Google Maps"
        note.font = _font(size=9, color="006400", italic=True)
    else:
        note.value = "* Drive time is an estimate"
        note.font = _font(size=9, color="996600", italic=True)
    note.alignment = _align("left")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_routes(
    csv_text: str,
    vehicles_text: str,
    output_path: str = "bus_routes_output.xlsx",
    route_data: Optional[list] = None,
) -> str:
    """
    Generate bus routes Excel file.

    Args:
        csv_text:       Raw CSV text (or file path)
        vehicles_text:  Multi-line fleet config text
        output_path:    Where to save the .xlsx
        route_data:     Optional pre-computed route data from Claude AI analysis
                        (list of dicts with keys: vehicle_name, stops, total_time, etc.)

    Returns:
        output_path on success
    """
    students = parse_students_csv(csv_text)
    if not students:
        raise ValueError("No students parsed from CSV. Check format.")

    vehicle_configs = parse_vehicles_text(vehicles_text)
    if not vehicle_configs:
        raise ValueError("No vehicles parsed. Check fleet configuration format.")

    if route_data:
        # Use Claude AI-provided route data
        vehicles = _apply_ai_route_data(students, vehicle_configs, route_data)
    else:
        # Fall back to algorithmic clustering
        vehicles = cluster_students(students, vehicle_configs)

    wb = Workbook()
    build_dashboard(wb, vehicles)
    for veh in vehicles:
        build_vehicle_sheet(wb, veh)

    wb.save(output_path)
    return output_path


def _apply_ai_route_data(
    students: list[Student],
    vehicle_configs: list[dict],
    route_data: list[dict]
) -> list[Vehicle]:
    """
    Build Vehicle objects from Claude AI route analysis results.

    route_data format (list of dicts):
    [
      {
        "vehicle_name": "Vehicle A",
        "start_address": "...",
        "capacity": 5,
        "total_time": "1 hr 14 min",
        "total_distance": "26.1 mi",
        "time_source": "Google Maps",  # or "Estimated"
        "stops": [
          {
            "address": "562 Coach Rd, Horsham, PA",
            "rider_names": ["Boyd"],
            "rider_count": 1,
            "drive_time": "6 min from start"
          },
          ...
        ]
      },
      ...
    ]
    """
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
        for stop_data in rd.get("stops", []):
            stop = Stop(address=stop_data.get("address", ""))
            # Create placeholder Student objects for display
            for name in stop_data.get("rider_names", []):
                s = Student(0, name, "", "", "", "")
                stop.riders.append(s)
            stop.drive_time = stop_data.get("drive_time", "")
            veh.stops.append(stop)
        vehicles.append(veh)
    return vehicles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bus Route Optimizer")
    parser.add_argument("--csv", required=True, help="Path to student CSV file")
    parser.add_argument("--vehicles", required=True,
                        help="Fleet config text (newline-separated, or pass a file path)")
    parser.add_argument("--output", default="bus_routes_output.xlsx",
                        help="Output Excel file path")
    parser.add_argument("--route-json", default=None,
                        help="Optional JSON file with pre-computed AI route data")
    args = parser.parse_args()

    with open(args.csv, encoding="utf-8-sig") as f:
        csv_text = f.read()

    # vehicles arg can be a text block or a file
    try:
        with open(args.vehicles) as f:
            vehicles_text = f.read()
    except (FileNotFoundError, IsADirectoryError):
        vehicles_text = args.vehicles.replace("\\n", "\n")

    route_data = None
    if args.route_json:
        with open(args.route_json) as f:
            route_data = json.load(f)

    out = generate_routes(csv_text, vehicles_text, args.output, route_data)
    print(f"✅  Routes saved to: {out}")


if __name__ == "__main__":
    main()
