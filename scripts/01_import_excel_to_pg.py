#!/usr/bin/env python3
"""
Script 01: Import Excel data to PostgreSQL

Reads the Excel file containing Bulgarian community centers (читалища)
and imports it into the PostgreSQL database with PostGIS support.

Usage:
    python scripts/01_import_excel_to_pg.py --xlsx data/input.xlsx
    python scripts/01_import_excel_to_pg.py --xlsx chitalishta_gps_coordinates.xlsx
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text
from tqdm import tqdm


def load_config(config_path="config/config.yaml"):
    """Load configuration from YAML file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def clean_coordinate(value):
    """
    Clean coordinate values by:
    - Converting comma to dot (e.g., "25,0516609" -> "25.0516609")
    - Stripping whitespace
    - Converting to float or None
    """
    if pd.isna(value):
        return None
    
    # Convert to string and clean
    value_str = str(value).strip()
    
    # Replace comma with dot
    value_str = value_str.replace(',', '.')
    
    # Try to convert to float
    try:
        return float(value_str)
    except (ValueError, TypeError):
        return None


def clean_text(value):
    """Clean text fields by stripping whitespace and handling None."""
    if pd.isna(value):
        return None
    return str(value).strip()


def normalize_address_query(row):
    """
    Build normalized address query string for geocoding.
    
    Format: "<street>, <settlement>, <municipality>, България"
    
    Example:
        "ул. Васил Левски 43, Старцево, Златоград, България"
    """
    import re
    
    parts = []
    
    # Get the raw address
    address = clean_text(row.get('address_raw'))
    
    # Extract street part (from "ул." onwards, but exclude administrative prefixes)
    street_part = None
    if address:
        # Find where the actual street address starts
        street_markers = ['ул.', 'бул.', 'пл.', 'кв.', 'жк.']
        for marker in street_markers:
            if marker in address.lower():
                # Find the position of the marker
                idx = address.lower().find(marker)
                # Extract from marker to end
                street_part = address[idx:].strip()
                
                # Remove postal code (п.к. XXXX)
                street_part = re.sub(r',?\s*п\.к\.\s*\d+', '', street_part)
                
                # Clean up extra spaces and commas
                street_part = re.sub(r'\s+', ' ', street_part).strip()
                break
    
    if street_part:
        parts.append(street_part)
    
    # Clean settlement name (remove СЕЛО/ГРАД prefix)
    settlement = clean_text(row.get('settlement'))
    if settlement:
        settlement_clean = settlement
        # Remove prefixes
        for prefix in ['СЕЛО ', 'ГРАД ', 'С. ', 'ГР. ']:
            if settlement_clean.startswith(prefix):
                settlement_clean = settlement_clean[len(prefix):].strip()
        
        if settlement_clean:
            parts.append(settlement_clean)
    
    # Clean municipality name (usually doesn't have prefixes)
    municipality = clean_text(row.get('municipality'))
    settlement_clean_upper = settlement_clean.upper() if settlement and settlement_clean else ''
    municipality_upper = municipality.upper() if municipality else ''
    
    # Only add municipality if different from settlement
    if municipality and municipality_upper != settlement_clean_upper:
        parts.append(municipality)
    
    # Add country
    parts.append('България')
    
    # Join with comma-space
    query = ', '.join(parts) if parts else None
    
    return query


def import_excel_to_db(xlsx_path, config):
    """
    Import Excel file into PostgreSQL database.
    
    Args:
        xlsx_path: Path to Excel file
        config: Configuration dictionary
    """
    print(f"[*] Reading Excel file: {xlsx_path}")
    
    # Read Excel file
    try:
        df = pd.read_excel(xlsx_path, engine='openpyxl')
    except Exception as e:
        print(f"[ERROR] Error reading Excel file: {e}")
        sys.exit(1)
    
    print(f"[OK] Loaded {len(df)} rows")
    print(f"[*] Columns: {list(df.columns)}")
    
    # Map Excel columns to database columns
    # Expected Excel columns (Cyrillic names)
    column_mapping = {
        'fid': 'fid',
        'Име': 'name',
        'Адрес': 'address_raw',
        'Населено място': 'settlement',
        'Община': 'municipality',
        'Връзка': 'url',
        'Longitude': 'lon_src',
        'Latitude': 'lat_src'
    }
    
    # Check if expected columns exist
    missing_cols = [col for col in column_mapping.keys() if col not in df.columns]
    if missing_cols:
        print(f"[WARNING] Missing expected columns: {missing_cols}")
        print(f"Available columns: {list(df.columns)}")
        
        # If columns are different, ask user or try to auto-detect
        # For now, we'll proceed with what we have
    
    # Rename columns
    df_mapped = df.rename(columns=column_mapping)
    
    # Clean data
    print("[*] Cleaning data...")
    
    # Clean coordinates
    df_mapped['lon_src'] = df_mapped['lon_src'].apply(clean_coordinate)
    df_mapped['lat_src'] = df_mapped['lat_src'].apply(clean_coordinate)
    
    # Clean text fields
    for col in ['name', 'address_raw', 'settlement', 'municipality', 'url']:
        if col in df_mapped.columns:
            df_mapped[col] = df_mapped[col].apply(clean_text)
    
    # Build normalized address query
    print("[*] Building normalized address queries...")
    df_mapped['address_query'] = df_mapped.apply(normalize_address_query, axis=1)
    
    # Connect to database
    print("[*] Connecting to database...")
    db_url = config['db']['url']
    engine = create_engine(db_url)
    
    # Insert data
    print("[*] Inserting data into database...")
    
    inserted_count = 0
    updated_count = 0
    error_count = 0
    
    with engine.connect() as conn:
        for idx, row in tqdm(df_mapped.iterrows(), total=len(df_mapped), desc="Importing"):
            try:
                # Build geometry from coordinates (if available)
                geom_wkt = None
                if row['lon_src'] is not None and row['lat_src'] is not None:
                    geom_wkt = f"SRID=4326;POINT({row['lon_src']} {row['lat_src']})"
                
                # Insert query (id will auto-increment)
                query = text("""
                    INSERT INTO community_centers (
                        fid, name, address_raw, settlement, municipality, url,
                        lon_src, lat_src, geom_src, address_query
                    ) VALUES (
                        :fid, :name, :address_raw, :settlement, :municipality, :url,
                        :lon_src, :lat_src, ST_GeomFromEWKT(:geom_src), :address_query
                    )
                """)
                
                result = conn.execute(query, {
                    'fid': int(row['fid']) if pd.notna(row['fid']) else None,
                    'name': row.get('name'),
                    'address_raw': row.get('address_raw'),
                    'settlement': row.get('settlement'),
                    'municipality': row.get('municipality'),
                    'url': row.get('url'),
                    'lon_src': row.get('lon_src'),
                    'lat_src': row.get('lat_src'),
                    'geom_src': geom_wkt,
                    'address_query': row.get('address_query')
                })
                
                inserted_count += 1
                
            except Exception as e:
                error_count += 1
                print(f"\n[WARNING] Error inserting row {idx} (fid={row.get('fid')}): {e}")
                if error_count > 10:
                    print("[ERROR] Too many errors, stopping import")
                    break
        
        # Commit transaction
        conn.commit()
    
    print(f"\n[OK] Import completed!")
    print(f"   Total rows: {len(df_mapped)}")
    print(f"   Successfully inserted: {inserted_count}")
    print(f"   Errors: {error_count}")
    
    # Show sample data
    print("\n[*] Sample of imported data:")
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, fid, name, settlement, municipality, 
                   lon_src, lat_src, address_query
            FROM community_centers
            ORDER BY id
            LIMIT 5
        """))
        
        for row in result:
            print(f"   ID {row.id}, FID {row.fid}: {row.name} - {row.settlement}")
            print(f"      Coords: ({row.lon_src}, {row.lat_src})")
            print(f"      Query: {row.address_query}")
            print()
    
    # Show statistics
    print("[*] Database statistics:")
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT 
                COUNT(*) as total,
                COUNT(DISTINCT fid) as unique_fids,
                COUNT(lon_src) as with_coordinates,
                COUNT(*) - COUNT(lon_src) as without_coordinates
            FROM community_centers
        """))
        stats = result.fetchone()
        print(f"   Total records: {stats.total}")
        print(f"   Unique FIDs: {stats.unique_fids}")
        print(f"   With coordinates: {stats.with_coordinates}")
        print(f"   Without coordinates: {stats.without_coordinates}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Import Excel data into PostgreSQL database'
    )
    parser.add_argument(
        '--xlsx',
        required=True,
        help='Path to Excel file'
    )
    parser.add_argument(
        '--config',
        default='config/config.yaml',
        help='Path to config file (default: config/config.yaml)'
    )
    
    args = parser.parse_args()
    
    # Check if files exist
    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"[ERROR] Excel file not found: {xlsx_path}")
        sys.exit(1)
    
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        sys.exit(1)
    
    # Load config
    config = load_config(args.config)
    
    # Import data
    import_excel_to_db(xlsx_path, config)


if __name__ == '__main__':
    main()

