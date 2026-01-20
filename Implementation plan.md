# Chitalishta Geocoder — Implementation Plan (Excel → PostgreSQL/PostGIS + Hybrid Geocoding)

This plan describes a Python project that:

- imports your Excel file (~3600 records of Bulgarian community centers / читалища) into PostgreSQL (running locally in Docker),
- normalizes address strings into geocode-friendly queries,
- geocodes **Nominatim first**, then falls back to **Google Geocoding** when needed,
- stores **original**, **Nominatim**, and **Google** coordinates in the database (both as lat/lon and PostGIS geometry),
- computes distance differences and assigns a status (`ok`, `needs_review`, `mismatch`, `not_found`),
- supports slow execution with **1 req/sec** and response caching.

---

## 1) Repository layout

Recommended structure:

```
chitalishta-geocoder/
  config/
    config.example.yaml
  data/
    input.xlsx
    cache/
      nominatim_cache.sqlite
      google_cache.sqlite
  db/
    schema.sql
  scripts/
    01_import_excel_to_pg.py
    02_geocode_hybrid.py
    03_compute_distances.py
    04_export_review_csv.py   # optional
  requirements.txt
  README.md
```

Why SQLite caches?  
They prevent repeated API calls (fast iteration + safer for Nominatim usage).

---

## 2) Docker: PostgreSQL + PostGIS

Use a PostGIS-enabled image (recommended for geometry support).

Example `docker-compose.yml` (you can adapt):

```yaml
services:
  db:
    image: postgis/postgis:16-3.4
    container_name: chitalishta_maps_db
    environment:
      POSTGRES_DB: chitalishta_maps
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```

---

## 3) Database schema

Create a single main table: `community_centers`

### 3.1 Columns

**Source fields (from Excel)**
- `fid` (int, primary key)
- `name` (text)
- `address_raw` (text)
- `settlement` (text)
- `municipality` (text)
- `url` (text)
- `lon_src` (double precision)
- `lat_src` (double precision)
- `geom_src` (geometry(Point, 4326))

**Normalization**
- `address_query` (text) — normalized query string used for geocoding

**Nominatim result**
- `lon_nom` / `lat_nom` (double precision)
- `geom_nom` (geometry(Point, 4326))
- `nom_display_name` (text)
- `nom_osm_type` (text)
- `nom_osm_id` (bigint)
- `nom_importance` (double precision)
- `nom_class` (text)
- `nom_type` (text)
- `nom_confidence` (smallint 0–100, your computed score)
- `nom_raw_json` (jsonb)

**Google result**
- `lon_g` / `lat_g` (double precision)
- `geom_g` (geometry(Point, 4326))
- `g_formatted_address` (text)
- `g_place_id` (text)
- `g_location_type` (text)
- `g_types` (jsonb)
- `g_confidence` (smallint 0–100)
- `g_raw_json` (jsonb)

**Distances (meters)**
- `dist_src_nom_m` (double precision)
- `dist_src_g_m` (double precision)
- `dist_nom_g_m` (double precision)

**Decision**
- `best_provider` (text: `src` / `nominatim` / `google` / `none`)
- `best_lon` / `best_lat` (double precision)
- `best_geom` (geometry(Point, 4326))
- `status` (text: `ok` / `needs_review` / `mismatch` / `not_found`)
- `notes` (text)

**Timestamps**
- `created_at` (timestamptz default now())
- `updated_at` (timestamptz default now())

### 3.2 Indexes (recommended)
- `btree(fid)`
- `gist(geom_src)`, `gist(geom_nom)`, `gist(geom_g)`, `gist(best_geom)`
- `btree(status)`
- Optional: `btree(municipality)`, `btree(settlement)`

---

## 4) Config file

Store secrets + tuning parameters in YAML.

`config/config.yaml` (example):

```yaml
db:
  url: "postgresql+psycopg2://postgres:postgres@localhost:5432/chitalishta"

google:
  api_key: "YOUR_GOOGLE_KEY"

nominatim:
  base_url: "https://nominatim.openstreetmap.org/search"
  user_agent: "chitalishta-geocoder/1.0 (your@email.com)"
  rate_limit_seconds: 1.0

thresholds:
  ok_distance_m: 1000
  suspicious_distance_m: 5000
```

---

## 5) Script 01 — Import Excel → PostgreSQL (`01_import_excel_to_pg.py`)

### Input
Excel columns:
- `fid`
- `Име`
- `Адрес`
- `Населено място`
- `Община`
- `Връзка`
- `Longitude`
- `Latitude`

### Transform rules
- Convert decimal comma to dot:
  - `"25,0516609"` → `25.0516609`
  - `"41,43"` → `41.43`
- Strip whitespace
- Keep Cyrillic unchanged

### Output
Insert rows into `community_centers`.
Also compute and store an initial `address_query` (or leave for script 02).

✅ Insert should be **idempotent** (upsert on `fid`).

---

## 6) Address normalization (`address_query`)

Raw addresses often look like:

`община ЗЛАТОГРАД СЕЛО СТАРЦЕВО ул. Васил Левски 43, п.к. 4987`

Geocoding works better if we build a stable query string.

### Recommended output format
`"<street_part>, <settlement>, <municipality>, България <postcode_optional>"`

Example:
- `ул. Васил Левски 43, Старцево, Златоград, България 4987`
- `ул. Петър Богдан 77, Раковски, България 4150`

### Suggested cleaning rules (simple + reliable)
- Remove leading administrative tokens:
  - `община`, `град`, `село`, `жк`, etc. (keep the actual names)
- Extract:
  - street part: substring starting at `ул.` / `бул.` / `пл.` / `кв.` etc.
  - postal code: detect `п.к.` + 4 digits
- Use Excel fields (`settlement`, `municipality`) as authoritative for locality naming

You don’t need perfect parsing — consistency matters more.

---

## 7) Script 02 — Hybrid geocoder (`02_geocode_hybrid.py`)

### Scope
Only process rows where:
- `lat_nom/lon_nom IS NULL` **OR**
- `lat_g/lon_g IS NULL`

(i.e. do not re-geocode filled records)

### Workflow per record
1. Build the `address_query` if missing
2. **Try Nominatim**
3. Compute Nominatim confidence score
4. If `not_found` or `low_confidence` → **call Google**
5. Store results + raw JSON in DB

### 7.1 Nominatim request
Query params:
- `q = address_query`
- `format=jsonv2`
- `countrycodes=bg`
- `limit=1`
- `addressdetails=1`

Rate limit:
- 1 request/sec
- required `User-Agent`

Caching:
- SQLite cache keyed by `address_query`

### 7.2 Nominatim confidence scoring (example heuristic)
Return 0–100 score based on:
- result exists: +40
- `class/type` indicates street or building (not just municipality): +20
- importance >= threshold: +10
- settlement match (if returned address contains expected settlement): +20
- otherwise penalty

If confidence < e.g. 60 → fallback to Google.

### 7.3 Google Geocoding fallback
Send `address_query` to Google.

Compute confidence using:
- `location_type == ROOFTOP` → 95
- `RANGE_INTERPOLATED` → 80
- `GEOMETRIC_CENTER` → 60
- `APPROXIMATE` → 40

Also store:
- `place_id`
- `formatted_address`
- `types[]`

Caching:
- SQLite cache keyed by `address_query`

---

## 8) Script 03 — Compute distances + status (`03_compute_distances.py`)

### Distances to compute
Using Haversine (meters):
- `dist_src_nom_m`
- `dist_src_g_m`
- `dist_nom_g_m`

### Status assignment (initial rules)
Using thresholds from config:

- `ok`:
  - best available provider result within `ok_distance_m` (default 1000m) from source coords **OR**
  - high confidence even if source missing
- `needs_review`:
  - in the suspicious range (1000–5000m) or low confidence result
- `mismatch`:
  - > `suspicious_distance_m` (default 5000m) between source and best result
- `not_found`:
  - neither Nominatim nor Google returned a usable point

### Best coordinate selection
If source coords exist:
- prefer Nominatim if `dist_src_nom_m <= ok_distance_m` and `nom_confidence >= 60`
- else prefer Google if `dist_src_g_m <= ok_distance_m` and `g_confidence >= 60`
- else pick whichever has higher confidence and mark `needs_review` or `mismatch`

If source coords missing:
- choose highest-confidence provider result

Store:
- `best_provider`, `best_lon`, `best_lat`, `best_geom`

---

## 9) Script 04 — Export CSV for manual review (optional)

Export `status IN ('needs_review', 'mismatch', 'not_found')` to CSV:

Include columns:
- `fid, name, address_query, settlement, municipality`
- `lat_src, lon_src`
- `lat_nom, lon_nom, nom_confidence, dist_src_nom_m`
- `lat_g, lon_g, g_confidence, dist_src_g_m`
- `best_provider, status, url`

This makes spot-checking easy.

---

## 10) Runtime notes

### Processing speed
- With 1 req/sec and caching, expect a single run to take roughly:
  - up to ~3600 seconds (~1 hour) for Nominatim-only,
  - plus Google calls only for the “bad” cases.

### Safety / compliance
- Respect Nominatim usage limits (you are OK with 1 req/sec).
- Always set a proper User-Agent.

---

## 11) Dependencies (`requirements.txt`)
Recommended:

- `pandas`
- `openpyxl` (read Excel)
- `sqlalchemy`
- `psycopg2-binary`
- `requests`
- `pyyaml`
- `tqdm`
- `tenacity` (retries)
- `geopy` (optional) OR implement your own haversine
- `shapely` (optional, only if you want local geometry ops; PostGIS handles geometry anyway)

---

## 12) Execution order

1. Start PostGIS (Docker)
2. Run `db/schema.sql`
3. `python scripts/01_import_excel_to_pg.py --xlsx data/input.xlsx`
4. `python scripts/02_geocode_hybrid.py`
5. `python scripts/03_compute_distances.py`
6. Optional: `python scripts/04_export_review_csv.py`

---

## 13) Acceptance checklist

A record is considered “good” when:

- `status == ok`
- `best_provider in ('nominatim','google')`
- `best_geom IS NOT NULL`
- distance differences are within thresholds

You will refine distance thresholds after sampling results.

---
