# Chitalishta Geocoder

Python project to geocode Bulgarian community centers (читалища) using Nominatim and Google Geocoding APIs.

## Setup

### 1. Database (PostgreSQL + PostGIS)

Start the database using Docker:

```bash
docker-compose up -d
```

Run the schema to create tables:

```bash
docker exec -i chitalishta_maps_db psql -U postgres -d chitalishta_maps < db/schema.sql
```

**Existing databases:** If you already have the database and need to add new columns, run the migrations in order:

```bash
docker exec -i chitalishta_maps_db psql -U postgres -d chitalishta_maps < db/migrations/001_add_nominatim_address_columns.sql
docker exec -i chitalishta_maps_db psql -U postgres -d chitalishta_maps < db/migrations/002_add_nom_query_used.sql
```

### 2. Python Environment

Create and activate a virtual environment:

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### 3. Configuration

Edit `config/config.yaml` and add:
- Your Google API key
- Your email address for Nominatim user-agent

### 4. Import Data

Import the Excel file:

```bash
python scripts/01_import_excel_to_pg.py --xlsx chitalishta_gps_coordinates.xlsx
```

This will:
- Import all 3,601 records from Excel
- Clean coordinates (convert comma to dot)
- Build normalized address queries
- Store data in PostgreSQL

### 5. Geocode Addresses

Geocode addresses using the hybrid approach (Nominatim first, Google fallback):

**Test with a small sample first (recommended):**

```bash
# Test with 5 records
python scripts/02_geocode_hybrid.py --limit 5

# Test with 50 records
python scripts/02_geocode_hybrid.py --limit 50

# Test with specific municipality (filter by name)
python scripts/02_geocode_hybrid.py --municipality_limit "ВАРНА" --limit 10

# Process all records from a specific municipality
python scripts/02_geocode_hybrid.py --municipality_limit "ПЛОВДИВ"
```

**Note for Windows users with Cyrillic:** If you encounter encoding issues in PowerShell, use CMD instead:
```cmd
cmd /c python scripts/02_geocode_hybrid.py --municipality_limit "ВАРНА" --limit 10
```

**Process all records:**

```bash
python scripts/02_geocode_hybrid.py
```

**What this script does:**
- Queries **Nominatim** first, using trusted Excel data (Населено място = settlement, Община = municipality) to disambiguate when the same settlement name exists in different municipalities:
  1. **Structured search** (when both settlement and a **normalized** municipality are present): the Excel "Община" column often contains long text (e.g. "община БУРГАС СЕЛО ИЗВОР Михаи"); we normalize it to a short name (e.g. "БУРГАС") and send `city` + `county` + `country` to Nominatim so the result is from the correct municipality. Without this normalization, structured search would get no match and we’d fall back to free-form (wrong region).
  2. **Free-form fallbacks** if structured returns no result: full address query, then "settlement, municipality, България", then "settlement, България".
- Cache key includes municipality when present so the same address in different municipalities (e.g. "Извор" in Бургас vs Радомир) does not share a cached result.
- Extracts and stores **nom_settlement**, **nom_municipality**, **nom_region** from the Nominatim response `address` object (from `nom_raw_json`), and **nom_query_used** (the query actually sent: structured or free-form string).
- Calls **Google Geocoding API** as fallback when:
  - Nominatim returns no results, OR
  - Nominatim confidence is low (< 60)
- Stores **all results** in the database (including failed attempts)
- Uses **SQLite caching** to avoid duplicate API calls
- Respects **rate limits**: 1 request/second for Nominatim

**Expected runtime:**
- With 3,601 records and rate limiting: ~1-2 hours
- The script will show a progress bar
- You can stop and restart anytime (it won't re-query already processed records)

**Results:**
- All Nominatim results stored in `lon_nom`, `lat_nom`, `nom_*` columns
- **nom_settlement**, **nom_municipality**, **nom_region** — extracted from Nominatim’s structured `address` (e.g. Нивянин, Радомир, Перник)
- **nom_query_used** — the query actually sent to Nominatim (see [How to tell structured vs free-form](#nominatim-structured-vs-free-form-request) below)
- All Google results stored in `lon_g`, `lat_g`, `g_*` columns
- Raw API responses stored in `nom_raw_json` and `g_raw_json`

### 6. Compute Distances and Assign Status

After geocoding, compute distances between coordinates and assign status:

```bash
# Test with 10 records
python scripts/03_compute_distances.py --limit 10

# Process all records
python scripts/03_compute_distances.py
```

**What this script does:**
- Computes distances (in meters) between:
  - Source coordinates ↔ Nominatim
  - Source coordinates ↔ Google
  - Nominatim ↔ Google
- Validates **settlement match**: when source coordinates exist, a geocoder result is only accepted if the expected settlement (from Excel) appears in the geocoder’s address (nom_display_name or g_formatted_address). Stored **nom_settlement**, **nom_municipality**, **nom_region** are available for reporting and future municipality-level validation.
- Selects the **best coordinates** based on:
  - Distance from source
  - Confidence scores
  - Settlement match (wrong or missing settlement filters out that provider)
- Assigns **status** to each record:
  - `ok` - Within 1000m threshold, high confidence, settlement/municipality match
  - `needs_review` - 1000-5000m range, low confidence, or no settlement validation possible
  - `mismatch` - >5000m from source (needs manual review)
  - `not_found` - No geocoding results available

**Results:**
- Distances stored in `dist_src_nom_m`, `dist_src_g_m`, `dist_nom_g_m`
- Best coordinates in `best_lon`, `best_lat`, `best_geom`
- Status in `status` column
- Decision notes in `notes` column

**Expected runtime:** ~1 second for all records (very fast!)

## Nominatim: structured vs free-form request

The column **nom_query_used** stores the query actually sent to Nominatim so you can tell whether the result came from the trusted structured search or from a free-form fallback.

| **nom_query_used** value | Meaning |
|-------------------------|--------|
| Starts with `structured:` | Result from **structured** search (city + county + country). Uses trusted settlement and municipality from Excel; best for disambiguating same settlement in different municipalities. |
| Any other string | Result from **free-form** fallback (the exact `q=` string sent to Nominatim). |
| NULL | Nominatim returned no result, or row was geocoded before this column existed. |

**Examples:**

- `structured:Извор,Бургас,Bulgaria` → structured (correct municipality requested).
- `ИЗВОР, община БУРГАС СЕЛО ИЗВОР Михаил Герджиков 3, България` → free-form (full address).
- `Извор, Бургас, България` → free-form (settlement + municipality string).
- `Извор, България` → free-form (settlement-only fallback).

In SQL you can count structured vs free-form:

```sql
SELECT 
  CASE 
    WHEN nom_query_used LIKE 'structured:%' THEN 'structured'
    WHEN nom_query_used IS NOT NULL THEN 'freeform'
    ELSE 'no_result'
  END AS request_type,
  COUNT(*) AS count
FROM community_centers
WHERE nom_queried_at IS NOT NULL
GROUP BY 1;
```

## Scripts

1. **01_import_excel_to_pg.py** - Import Excel data into PostgreSQL
2. **02_geocode_hybrid.py** - Geocode using Nominatim first (structured by settlement+municipality when available), then Google fallback
3. **03_compute_distances.py** - Compute distances, validate settlement/municipality, and assign status
4. **04_export_review_csv.py** - Export records needing review (coming soon)

## Cache Management

The geocoding scripts cache all API responses to avoid duplicate requests:
- `data/cache/nominatim_cache.sqlite` - Nominatim responses
- `data/cache/google_cache.sqlite` - Google responses

To clear the cache (if you want to re-geocode):

```bash
# Windows
Remove-Item -Path "data\cache\*.sqlite" -Force

# Linux/Mac
rm -f data/cache/*.sqlite
```

## Checking Results

After geocoding, you can check the results in DBeaver or using SQL:

```bash
# View geocoding statistics
docker exec chitalishta_maps_db psql -U postgres -d chitalishta_maps -c "
  SELECT 
    COUNT(*) as total,
    COUNT(nom_queried_at) as nominatim_queried,
    COUNT(CASE WHEN lon_nom IS NOT NULL THEN 1 END) as nominatim_found,
    COUNT(g_queried_at) as google_queried,
    COUNT(CASE WHEN lon_g IS NOT NULL THEN 1 END) as google_found
  FROM community_centers;
"

# View sample results (including which Nominatim request was used)
docker exec chitalishta_maps_db psql -U postgres -d chitalishta_maps -c "
  SELECT id, name, settlement, municipality,
         lon_nom, lat_nom, nom_confidence, nom_query_used,
         nom_settlement, nom_municipality, nom_region
  FROM community_centers
  WHERE nom_queried_at IS NOT NULL
  LIMIT 10;
"

# View status distribution
docker exec chitalishta_maps_db psql -U postgres -d chitalishta_maps -c "
  SELECT status, COUNT(*) as count
  FROM community_centers
  GROUP BY status
  ORDER BY count DESC;
"

# View records needing review
docker exec chitalishta_maps_db psql -U postgres -d chitalishta_maps -c "
  SELECT id, name, settlement, dist_src_nom_m::int, dist_src_g_m::int, 
         best_provider, status, notes
  FROM community_centers
  WHERE status IN ('needs_review', 'mismatch')
  LIMIT 10;
"
```

## Database Connection

- **Host:** localhost
- **Port:** 5436
- **Database:** chitalishta_maps
- **Username:** postgres
- **Password:** postgres

## Project Structure

```
chitalishta-karta-gps-improvement/
  config/
    config.yaml              # Your configuration (not in git)
    config.example.yaml      # Example configuration
  data/
    cache/                   # SQLite caches for API responses
    chitalishta_gps_coordinates.xlsx  # Input data
  db/
    schema.sql               # Database schema
    migrations/              # ALTER scripts for existing DBs
      001_add_nominatim_address_columns.sql  # nom_settlement, nom_municipality, nom_region
      002_add_nom_query_used.sql            # nom_query_used
  scripts/
    01_import_excel_to_pg.py
    02_geocode_hybrid.py
    03_compute_distances.py
    04_export_review_csv.py
  docker-compose.yml        # PostgreSQL + PostGIS
  requirements.txt          # Python dependencies
  README.md
```

