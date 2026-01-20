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

## Scripts

1. **01_import_excel_to_pg.py** - Import Excel data into PostgreSQL
2. **02_geocode_hybrid.py** - Geocode using Nominatim first, then Google fallback (coming soon)
3. **03_compute_distances.py** - Compute distances and assign status (coming soon)
4. **04_export_review_csv.py** - Export records needing review (coming soon)

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

