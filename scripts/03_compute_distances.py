#!/usr/bin/env python3
"""
Script 03: Compute Distances and Assign Status

Computes distances between source, Nominatim, and Google coordinates,
then assigns status and selects the best coordinate for each record.

Usage:
    python scripts/03_compute_distances.py
    python scripts/03_compute_distances.py --limit 10  # Test with 10 rows
"""

import argparse
import sys
from math import radians, cos, sin, asin, sqrt
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text
from tqdm import tqdm


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great circle distance in meters between two points 
    on the earth (specified in decimal degrees).
    
    Returns:
        Distance in meters
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    
    # Convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    
    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    
    # Radius of earth in meters
    r = 6371000
    
    return c * r


def load_config(config_path="config/config.yaml"):
    """Load configuration from YAML file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def compute_distances_and_status(config, limit=None):
    """
    Compute distances between coordinates and assign status.
    
    Args:
        config: Configuration dictionary
        limit: Maximum number of records to process (None for all)
    """
    # Get thresholds from config
    ok_distance_m = config['thresholds'].get('ok_distance_m', 1000)
    suspicious_distance_m = config['thresholds'].get('suspicious_distance_m', 5000)
    min_confidence = config['thresholds'].get('min_confidence', 60)
    
    print(f"[*] Using thresholds:")
    print(f"    - OK distance: {ok_distance_m}m")
    print(f"    - Suspicious distance: {suspicious_distance_m}m")
    print(f"    - Min confidence: {min_confidence}")
    
    # Connect to database
    db_url = config['db']['url']
    engine = create_engine(db_url)
    
    # Get records to process
    print("\n[*] Finding records to process...")
    
    with engine.connect() as conn:
        query = text("""
            SELECT id, 
                   lon_src, lat_src,
                   lon_nom, lat_nom, nom_confidence,
                   lon_g, lat_g, g_confidence
            FROM community_centers
            ORDER BY id
        """)
        
        if limit:
            query = text("""
                SELECT id, 
                       lon_src, lat_src,
                       lon_nom, lat_nom, nom_confidence,
                       lon_g, lat_g, g_confidence
                FROM community_centers
                ORDER BY id
                LIMIT :limit
            """)
            result = conn.execute(query, {'limit': limit})
        else:
            result = conn.execute(query)
        
        records = result.fetchall()
    
    print(f"[*] Processing {len(records)} records...")
    
    # Statistics
    stats = {
        'ok': 0,
        'needs_review': 0,
        'mismatch': 0,
        'not_found': 0,
        'best_src': 0,
        'best_nominatim': 0,
        'best_google': 0
    }
    
    # Process each record
    for record in tqdm(records, desc="Computing distances"):
        record_id = record.id
        
        # Extract coordinates
        lon_src, lat_src = record.lon_src, record.lat_src
        lon_nom, lat_nom = record.lon_nom, record.lat_nom
        lon_g, lat_g = record.lon_g, record.lat_g
        nom_confidence = record.nom_confidence or 0
        g_confidence = record.g_confidence or 0
        
        # Compute distances
        dist_src_nom_m = haversine_distance(lat_src, lon_src, lat_nom, lon_nom) if all([lat_src, lon_src, lat_nom, lon_nom]) else None
        dist_src_g_m = haversine_distance(lat_src, lon_src, lat_g, lon_g) if all([lat_src, lon_src, lat_g, lon_g]) else None
        dist_nom_g_m = haversine_distance(lat_nom, lon_nom, lat_g, lon_g) if all([lat_nom, lon_nom, lat_g, lon_g]) else None
        
        # Decide best coordinates and status
        best_provider = None
        best_lon = None
        best_lat = None
        status = None
        notes = []
        
        # Check if we have any geocoded results
        has_nom = lon_nom is not None and lat_nom is not None
        has_google = lon_g is not None and lat_g is not None
        has_src = lon_src is not None and lat_src is not None
        
        if not has_nom and not has_google:
            # No geocoding results at all
            status = 'not_found'
            notes.append('No geocoding results from Nominatim or Google')
            if has_src:
                # Use source coordinates as fallback
                best_provider = 'src'
                best_lon = lon_src
                best_lat = lat_src
                notes.append('Using source coordinates as fallback')
        
        elif has_src:
            # We have source coordinates - use them to validate
            
            # Check Nominatim first
            nom_valid = has_nom and dist_src_nom_m is not None and dist_src_nom_m <= ok_distance_m and nom_confidence >= min_confidence
            
            # Check Google
            g_valid = has_google and dist_src_g_m is not None and dist_src_g_m <= ok_distance_m and g_confidence >= min_confidence
            
            if nom_valid:
                # Nominatim is good
                best_provider = 'nominatim'
                best_lon = lon_nom
                best_lat = lat_nom
                status = 'ok'
                notes.append(f'Nominatim within {dist_src_nom_m:.0f}m of source, confidence {nom_confidence}')
            
            elif g_valid:
                # Google is good
                best_provider = 'google'
                best_lon = lon_g
                best_lat = lat_g
                status = 'ok'
                notes.append(f'Google within {dist_src_g_m:.0f}m of source, confidence {g_confidence}')
            
            else:
                # Neither is clearly good - pick the best we have
                
                # Calculate which one is better
                nom_score = 0
                g_score = 0
                
                if has_nom:
                    nom_score = nom_confidence
                    if dist_src_nom_m is not None:
                        # Penalty for distance
                        if dist_src_nom_m > suspicious_distance_m:
                            nom_score -= 30
                        elif dist_src_nom_m > ok_distance_m:
                            nom_score -= 15
                
                if has_google:
                    g_score = g_confidence
                    if dist_src_g_m is not None:
                        # Penalty for distance
                        if dist_src_g_m > suspicious_distance_m:
                            g_score -= 30
                        elif dist_src_g_m > ok_distance_m:
                            g_score -= 15
                
                # Pick the better one
                if g_score > nom_score:
                    best_provider = 'google'
                    best_lon = lon_g
                    best_lat = lat_g
                    if dist_src_g_m is not None and dist_src_g_m > suspicious_distance_m:
                        status = 'mismatch'
                        notes.append(f'Google {dist_src_g_m:.0f}m from source (>5km)')
                    else:
                        status = 'needs_review'
                        notes.append(f'Google {dist_src_g_m:.0f}m from source' if dist_src_g_m else 'Google result, distance unknown')
                
                elif nom_score > 0 and has_nom:
                    best_provider = 'nominatim'
                    best_lon = lon_nom
                    best_lat = lat_nom
                    if dist_src_nom_m is not None and dist_src_nom_m > suspicious_distance_m:
                        status = 'mismatch'
                        notes.append(f'Nominatim {dist_src_nom_m:.0f}m from source (>5km)')
                    else:
                        status = 'needs_review'
                        notes.append(f'Nominatim {dist_src_nom_m:.0f}m from source' if dist_src_nom_m else 'Nominatim result, distance unknown')
                
                else:
                    # Fallback to source
                    best_provider = 'src'
                    best_lon = lon_src
                    best_lat = lat_src
                    status = 'needs_review'
                    notes.append('Using source coordinates - geocoding results not reliable')
        
        else:
            # No source coordinates - just pick the best geocoded result
            if has_google and g_confidence >= min_confidence:
                best_provider = 'google'
                best_lon = lon_g
                best_lat = lat_g
                status = 'ok'
                notes.append(f'No source coords - using Google (confidence {g_confidence})')
            
            elif has_nom and nom_confidence >= min_confidence:
                best_provider = 'nominatim'
                best_lon = lon_nom
                best_lat = lat_nom
                status = 'ok'
                notes.append(f'No source coords - using Nominatim (confidence {nom_confidence})')
            
            elif has_google:
                best_provider = 'google'
                best_lon = lon_g
                best_lat = lat_g
                status = 'needs_review'
                notes.append(f'No source coords - using Google (low confidence {g_confidence})')
            
            elif has_nom:
                best_provider = 'nominatim'
                best_lon = lon_nom
                best_lat = lat_nom
                status = 'needs_review'
                notes.append(f'No source coords - using Nominatim (low confidence {nom_confidence})')
            
            else:
                # Should not reach here
                status = 'not_found'
                notes.append('No coordinates available')
        
        # Update statistics
        if status:
            stats[status] += 1
        if best_provider:
            stats[f'best_{best_provider}'] += 1
        
        # Prepare notes text
        notes_text = '; '.join(notes) if notes else None
        
        # Update database
        with engine.connect() as conn:
            update_query = text("""
                UPDATE community_centers
                SET 
                    dist_src_nom_m = :dist_src_nom_m,
                    dist_src_g_m = :dist_src_g_m,
                    dist_nom_g_m = :dist_nom_g_m,
                    best_provider = :best_provider,
                    best_lon = :best_lon,
                    best_lat = :best_lat,
                    best_geom = CASE 
                        WHEN :best_lon IS NOT NULL AND :best_lat IS NOT NULL 
                        THEN ST_SetSRID(ST_MakePoint(:best_lon, :best_lat), 4326)
                        ELSE NULL 
                    END,
                    status = :status,
                    notes = :notes,
                    updated_at = NOW()
                WHERE id = :id
            """)
            
            conn.execute(update_query, {
                'id': record_id,
                'dist_src_nom_m': dist_src_nom_m,
                'dist_src_g_m': dist_src_g_m,
                'dist_nom_g_m': dist_nom_g_m,
                'best_provider': best_provider,
                'best_lon': best_lon,
                'best_lat': best_lat,
                'status': status,
                'notes': notes_text
            })
            conn.commit()
    
    # Print statistics
    print("\n" + "="*60)
    print("[OK] Distance computation completed!")
    print("="*60)
    print(f"\nStatus distribution:")
    print(f"  - OK: {stats['ok']}")
    print(f"  - Needs review: {stats['needs_review']}")
    print(f"  - Mismatch: {stats['mismatch']}")
    print(f"  - Not found: {stats['not_found']}")
    
    print(f"\nBest provider distribution:")
    print(f"  - Source: {stats['best_src']}")
    print(f"  - Nominatim: {stats['best_nominatim']}")
    print(f"  - Google: {stats['best_google']}")
    print("="*60)
    
    # Show sample results
    print("\n[*] Sample results:")
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, name, settlement,
                   dist_src_nom_m, dist_src_g_m,
                   best_provider, status, notes
            FROM community_centers
            ORDER BY id
            LIMIT 10
        """))
        
        for row in result:
            print(f"\n  ID {row.id}: {row.name}")
            print(f"    Settlement: {row.settlement}")
            if row.dist_src_nom_m is not None:
                print(f"    Distance src->nom: {row.dist_src_nom_m:.0f}m")
            if row.dist_src_g_m is not None:
                print(f"    Distance src->google: {row.dist_src_g_m:.0f}m")
            print(f"    Best: {row.best_provider or 'none'}")
            print(f"    Status: {row.status or 'unknown'}")
            if row.notes:
                print(f"    Notes: {row.notes}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Compute distances and assign status to geocoded records'
    )
    parser.add_argument(
        '--config',
        default='config/config.yaml',
        help='Path to config file (default: config/config.yaml)'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Limit number of records to process (for testing)'
    )
    
    args = parser.parse_args()
    
    # Check if config exists
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        sys.exit(1)
    
    # Load config
    config = load_config(args.config)
    
    # Compute distances and status
    compute_distances_and_status(config, limit=args.limit)


if __name__ == '__main__':
    main()

