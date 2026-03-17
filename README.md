# 🚌 Camp Bus Route Optimizer

Automated, Google Maps-powered bus route optimization for day camps. Upload a student roster, configure your fleet, and get optimized routes with real drive times in under 2 minutes.

---

## Features

- **Smart geographic clustering** — groups students by actual driving distance, not ZIP codes. Neighbors always ride together.
- **Google Maps integration** — geocoding, drive times, and interactive route maps all powered by the Google Maps API.
- **Fleet builder UI** — configure vehicles with custom starting addresses and capacities. Real-time capacity warnings prevent over-assignment.
- **Morning & afternoon runs** — separate rosters and fleet configs for each direction. Routes automatically reverse for the ride home.
- **Manual override** — remove any rider with one click, reassign to a different vehicle, and recalculate drive times instantly.
- **Results dashboard** — summary table with utilization bars, interactive Google Maps for each route, stop-by-stop details, and Excel download.
- **Fully brandable** — your logo, colors, and camp name throughout every route sheet and export.

---

## Project Structure

```
├── app.py                  # Flask web application (UI + API endpoints)
├── bus_route_optimizer.py  # Core routing engine (geocoding, clustering, Excel)
├── requirements.txt        # Python dependencies
├── .replit                 # Replit run configuration
├── coord_overrides.json    # Manual GPS coordinate fixes for problem addresses
└── static/
    └── logo.png            # Camp logo (served at /logo.png)
```

**Auto-generated at runtime (excluded from Git):**
```
├── geocache.json           # Cached geocoding results (Google Maps API)
├── routecache.json         # Cached drive time calculations
├── uploads/                # Temporary CSV uploads
└── outputs/                # Generated Excel files
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3, Flask |
| Routing engine | Custom nearest-neighbor TSP with geographic clustering |
| Geocoding | Google Maps Geocoding API (Nominatim fallback) |
| Drive times | Google Maps Directions API (OSRM fallback) |
| Maps | Google Maps JavaScript API |
| Excel output | openpyxl |
| Hosting | Replit (connected to GitHub) |

---

## How It Works

### Routing Algorithm

The optimizer runs a 7-step pipeline on every generation:

1. **Geocode** — every student address and vehicle start is geocoded using Google Maps (with Nominatim as fallback). Results are cached to `geocache.json`.
2. **Group families** — students at the same address are grouped as a unit and always kept together.
3. **Cluster neighbors** — family units within **1.5 miles** of each other (by actual coordinates, not ZIP code) are grouped into geographic clusters.
4. **Assign clusters to vehicles** — whole clusters are assigned to the nearest vehicle start. Small vehicles get a priority bonus so they fill before large ones.
5. **Consolidate** — under-filled vehicles are merged into nearby vehicles. A 5-mile maximum scatter distance prevents students from geographically different areas being mixed.
6. **Sequence stops** — nearest-neighbor TSP with directional bias:
   - Morning: starts farthest from camp, works toward camp (no backtracking)
   - Afternoon: starts nearest to camp, works away from camp
7. **Calculate drive times** — Google Maps Directions API calculates real drive times for each leg. Results are cached to `routecache.json`.

### Utilization Thresholds

| Vehicle Size | Minimum Utilization |
|-------------|-------------------|
| ≤ 6 seats | 40% |
| 7–9 seats | 50% |
| 10+ seats | 60% |

Vehicles below threshold are flagged with a warning rather than forcibly merged if merging would violate geographic clustering rules.

### Caching

Both geocoding results and drive times are cached on disk. **The same address is never looked up twice.** On subsequent runs with the same roster, zero API calls are made — results load instantly from cache.

The cache is self-healing: any cached coordinate that fails Pennsylvania bounds validation or ZIP centroid proximity check is automatically purged and re-geocoded on the next run.

---

## CSV Format

The student roster CSV must have a header row. Column names are flexible:

| Data | Accepted Column Names |
|------|-----------------------|
| Last name | `Last name`, `last_name`, `Last Name` |
| First name | `First name`, `first_name`, `First Name` |
| Address | `Primary family address 1`, `Address`, `address`, `Street` |
| City | `Primary family city`, `City`, `city` |
| ZIP | `Primary family zip`, `Zip`, `ZIP`, `Postal Code` |

Column order does not matter. The first row must contain headers.

**Example:**
```csv
Last name,First name,Address,City,Zip
Smith,John,123 Main St,Doylestown,18901
Jones,Sarah,45 Oak Ave,Warrington,18976
```

---

## Fleet Configuration

Each vehicle needs a name, starting address, and capacity. The UI fleet builder handles this — no text formatting required.

Vehicles are named A through L. The starting address is where the vehicle begins its route (typically a garage or depot). Capacity is the maximum number of riders.

For vehicles that start at camp (common for afternoon runs or camp-based vans), use the camp address as the starting address.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Serves the web application |
| `POST` | `/api/run` | Starts a route generation job |
| `GET` | `/api/status/<job_id>` | Polls job status and progress |
| `GET` | `/api/download/<job_id>` | Downloads the generated Excel file |
| `POST` | `/api/recalculate/<job_id>` | Recalculates routes after manual edits |
| `GET` | `/logo.png` | Serves the camp logo |

### POST /api/run

**Form data:**
- `csv_file` — the student roster CSV
- `vehicles_text` — fleet configuration string
- `camp_address` — destination address (defaults to 828 Elbow Lane, Warrington, PA)
- `trip_direction` — `morning` or `afternoon`

**Response:**
```json
{ "job_id": "a1b2c3d4" }
```

### GET /api/status/<job_id>

**Response:**
```json
{
  "status": "done",
  "progress": ["✓ Loaded 77 students...", "Geocoded 77/77 addresses", "..."],
  "route_data": [ ... ],
  "error": null
}
```

---

## Coordinate Overrides

`coord_overrides.json` allows permanent GPS fixes for addresses that geocoding services get wrong. Keys are lowercase address strings, values are `[lat, lon]`.

```json
{
  "103 indian lake cir, lansdale, pa 19446": [40.2400, -75.2825],
  "828 elbow lane, warrington, pa 18976": [40.2454, -75.1407]
}
```

Overrides take priority over both the geocache and any API results. This file **should be committed** to the repository — it contains permanent corrections, not cached data.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_MAPS_KEY` | Yes | Google Maps API key |
| `PORT` | No | Port to run on (default: 5000) |

---

## GitHub → Replit Workflow

This project uses GitHub as the source of truth with Replit for hosting:

1. Make changes here or in this conversation
2. Update files in GitHub (pencil icon → paste → commit)
3. In Replit, pull from GitHub
4. Hit Run — Replit installs dependencies and starts the server

**Files that should NOT be committed** (handled by `.gitignore`):
- `geocache.json` — rebuilds automatically
- `routecache.json` — rebuilds automatically
- `outputs/` — generated Excel files
- `uploads/` — temporary CSV uploads

**Files that SHOULD be committed:**
- `coord_overrides.json` — permanent coordinate fixes

---

## Routing Rules

These rules are enforced on every generation and cannot be overridden:

1. **Geographic neighbors stay together** — students within 1.5 miles of each other ride the same bus, regardless of ZIP code boundaries.
2. **No drive-bys** — a route cannot pass a student's address without stopping.
3. **Zero backtracking** — routes flow in one direction toward camp (morning) or away from camp (afternoon).
4. **Families always together** — siblings or students at the same address always ride the same bus.
5. **No geographic mixing** — students from areas more than 5 miles apart are not placed on the same vehicle during consolidation.

---

## License

Private — Elbow Lane Day Camp. All rights reserved.
