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
- Queries **Nominatim** first with multiple fallback strategies:
  1. Full address (street + settlement + municipality)
  2. Settlement + municipality (if street address fails)
  3. Just settlement (if still no results)
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
- Selects the **best coordinates** based on:
  - Distance from source
  - Confidence scores
  - Intelligent scoring system
- Assigns **status** to each record:
  - `ok` - Within 1000m threshold, high confidence
  - `needs_review` - 1000-5000m range or low confidence
  - `mismatch` - >5000m from source (needs manual review)
  - `not_found` - No geocoding results available

**Results:**
- Distances stored in `dist_src_nom_m`, `dist_src_g_m`, `dist_nom_g_m`
- Best coordinates in `best_lon`, `best_lat`, `best_geom`
- Status in `status` column
- Decision notes in `notes` column

**Expected runtime:** ~1 second for all records (very fast!)

## Scripts

1. **01_import_excel_to_pg.py** - Import Excel data into PostgreSQL
2. **02_geocode_hybrid.py** - Geocode using Nominatim first, then Google fallback
3. **03_compute_distances.py** - Compute distances and assign status
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

# View sample results
docker exec chitalishta_maps_db psql -U postgres -d chitalishta_maps -c "
  SELECT id, name, settlement, 
         lon_nom, lat_nom, nom_confidence,
         lon_g, lat_g, g_confidence
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
    schema.sql              # Database schema
  scripts/
    01_import_excel_to_pg.py
    02_geocode_hybrid.py
    03_compute_distances.py
    04_export_review_csv.py
  docker-compose.yml        # PostgreSQL + PostGIS
  requirements.txt          # Python dependencies
  README.md
```

