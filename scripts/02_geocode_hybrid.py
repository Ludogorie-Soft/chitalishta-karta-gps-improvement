#!/usr/bin/env python3
"""
Script 02: Hybrid Geocoder (Nominatim + Google Fallback)

Geocodes addresses using Nominatim first, then falls back to Google
when Nominatim returns no results or low confidence results.

Usage:
    python scripts/02_geocode_hybrid.py --limit 5              # Test with 5 rows
    python scripts/02_geocode_hybrid.py                        # Process all rows
    python scripts/02_geocode_hybrid.py --municipality_limit ВРАЦА  # Only ВРАЦА municipality
    python scripts/02_geocode_hybrid.py --municipality_limit ВРАЦА --limit 10  # ВРАЦА, max 10 rows
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
from sqlalchemy import create_engine, text
from tqdm import tqdm


class GeocoderCache:
    """SQLite cache for geocoding responses."""
    
    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(cache_path))
        self._init_db()
    
    def _init_db(self):
        """Initialize cache database."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                address_query TEXT PRIMARY KEY,
                response_json TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()
    
    def get(self, address_query):
        """Get cached response for address."""
        cursor = self.conn.execute(
            "SELECT response_json FROM cache WHERE address_query = ?",
            (address_query,)
        )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None
    
    def set(self, address_query, response_data):
        """Cache response for address."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO cache (address_query, response_json, timestamp)
            VALUES (?, ?, ?)
            """,
            (address_query, json.dumps(response_data), datetime.now().isoformat())
        )
        self.conn.commit()
    
    def close(self):
        """Close database connection."""
        self.conn.close()


class NominatimGeocoder:
    """Nominatim geocoding with rate limiting and caching."""
    
    def __init__(self, config, cache_path):
        self.config = config['nominatim']
        self.cache = GeocoderCache(cache_path)
        self.last_request_time = 0
    
    def _rate_limit(self):
        """Enforce rate limiting (1 req/sec for Nominatim)."""
        rate_limit = self.config.get('rate_limit_seconds', 1.0)
        elapsed = time.time() - self.last_request_time
        if elapsed < rate_limit:
            time.sleep(rate_limit - elapsed)
        self.last_request_time = time.time()
    
    def geocode(self, address_query, settlement=None, municipality=None):
        """
        Geocode an address using Nominatim with fallback strategies.
        
        Tries:
        1. Full address query
        2. Settlement + municipality (if street address fails)
        3. Just settlement (if still no results)
        
        Returns:
            dict with keys: success, lat, lon, raw_json, confidence
        """
        # Check cache first (using full address as key)
        cached = self.cache.get(address_query)
        if cached is not None:
            return cached
        
        # Try different query strategies
        queries_to_try = [address_query]
        
        # Add fallback queries
        if settlement and municipality:
            # Clean settlement/municipality names
            settlement_clean = settlement.replace('СЕЛО ', '').replace('ГРАД ', '').strip()
            municipality_clean = municipality.strip()
            
            if settlement_clean and municipality_clean:
                queries_to_try.append(f"{settlement_clean}, {municipality_clean}, България")
        
        if settlement:
            settlement_clean = settlement.replace('СЕЛО ', '').replace('ГРАД ', '').strip()
            if settlement_clean:
                queries_to_try.append(f"{settlement_clean}, България")
        
        result_data = None
        
        for query_attempt in queries_to_try:
            # Rate limit
            self._rate_limit()
            
            # Make request to Nominatim
            params = {
                'q': query_attempt,
                'format': 'jsonv2',
                'countrycodes': 'bg',
                'limit': 1,
                'addressdetails': 1
            }
            
            headers = {
                'User-Agent': self.config['user_agent']
            }
            
            try:
                response = requests.get(
                    self.config['base_url'],
                    params=params,
                    headers=headers,
                    timeout=10
                )
                response.raise_for_status()
                
                results = response.json()
                
                if results and len(results) > 0:
                    result = results[0]
                    
                    # Extract data
                    lat = float(result.get('lat', 0))
                    lon = float(result.get('lon', 0))
                    
                    # Calculate confidence
                    confidence = self._calculate_confidence(result, address_query)
                    
                    # Reduce confidence if we had to use a fallback query
                    if query_attempt != address_query:
                        confidence = max(confidence - 20, 30)  # Penalty for fallback
                    
                    result_data = {
                        'success': True,
                        'lat': lat,
                        'lon': lon,
                        'display_name': result.get('display_name'),
                        'osm_type': result.get('osm_type'),
                        'osm_id': result.get('osm_id'),
                        'importance': result.get('importance'),
                        'class': result.get('class'),
                        'type': result.get('type'),
                        'confidence': confidence,
                        'raw_json': result,
                        'query_used': query_attempt  # Track which query worked
                    }
                    break  # Success, stop trying
                    
            except Exception as e:
                # Try next query
                continue
        
        # If no queries worked
        if result_data is None:
            result_data = {
                'success': False,
                'lat': None,
                'lon': None,
                'confidence': 0,
                'raw_json': {'error': 'No results found for any query strategy'},
                'queries_tried': queries_to_try
            }
        
        # Cache the result (including failures)
        self.cache.set(address_query, result_data)
        return result_data
    
    def _calculate_confidence(self, result, address_query):
        """
        Calculate confidence score (0-100) for Nominatim result.
        
        Heuristic:
        - Base score: 40 if result exists
        - +20 if class/type indicates street or building (not just municipality)
        - +10 if importance >= 0.4
        - +20 if settlement name appears in display_name
        - +10 if osm_type is 'way' or 'node' (precise locations)
        """
        score = 40  # Base score for having a result
        
        # Check if it's a precise location (not just administrative)
        osm_class = result.get('class', '').lower()
        osm_type = result.get('type', '').lower()
        
        if osm_class in ['building', 'amenity', 'tourism', 'leisure']:
            score += 20
        elif osm_class == 'highway' or osm_type in ['house', 'residential', 'commercial']:
            score += 15
        elif osm_class == 'place' and osm_type in ['village', 'town', 'city']:
            score += 5  # Just a municipality/settlement, less precise
        
        # Check importance
        importance = result.get('importance', 0)
        if importance >= 0.4:
            score += 10
        
        # Check OSM type (way/node are more precise than relation)
        result_osm_type = result.get('osm_type', '').lower()
        if result_osm_type in ['way', 'node']:
            score += 10
        
        # Bonus for having address details
        address = result.get('address', {})
        if address.get('house_number') or address.get('street') or address.get('road'):
            score += 10
        
        return min(score, 100)  # Cap at 100
    
    def close(self):
        """Close cache connection."""
        self.cache.close()


class GoogleGeocoder:
    """Google Geocoding API with caching."""
    
    def __init__(self, config, cache_path):
        self.config = config['google']
        self.cache = GeocoderCache(cache_path)
    
    def geocode(self, address_query):
        """
        Geocode an address using Google Geocoding API.
        
        Returns:
            dict with keys: success, lat, lon, raw_json, confidence
        """
        # Check cache first
        cached = self.cache.get(address_query)
        if cached is not None:
            return cached
        
        # Make request to Google
        params = {
            'address': address_query,
            'key': self.config['api_key'],
            'region': 'bg'
        }
        
        try:
            response = requests.get(
                'https://maps.googleapis.com/maps/api/geocode/json',
                params=params,
                timeout=10
            )
            response.raise_for_status()
            
            result_json = response.json()
            
            if result_json.get('status') == 'OK' and result_json.get('results'):
                result = result_json['results'][0]
                
                # Extract data
                location = result['geometry']['location']
                lat = location['lat']
                lon = location['lng']
                
                # Calculate confidence
                confidence = self._calculate_confidence(result)
                
                data = {
                    'success': True,
                    'lat': lat,
                    'lon': lon,
                    'formatted_address': result.get('formatted_address'),
                    'place_id': result.get('place_id'),
                    'location_type': result['geometry'].get('location_type'),
                    'types': result.get('types', []),
                    'confidence': confidence,
                    'raw_json': result
                }
            else:
                # No results or error
                data = {
                    'success': False,
                    'lat': None,
                    'lon': None,
                    'confidence': 0,
                    'raw_json': result_json
                }
            
            # Cache the result (including failures)
            self.cache.set(address_query, data)
            return data
            
        except Exception as e:
            # Cache the error too
            data = {
                'success': False,
                'lat': None,
                'lon': None,
                'confidence': 0,
                'raw_json': {'error': str(e)}
            }
            self.cache.set(address_query, data)
            return data
    
    def _calculate_confidence(self, result):
        """
        Calculate confidence score (0-100) for Google result.
        
        Based on location_type:
        - ROOFTOP: 95 (most precise)
        - RANGE_INTERPOLATED: 80
        - GEOMETRIC_CENTER: 60
        - APPROXIMATE: 40
        """
        location_type = result['geometry'].get('location_type', 'APPROXIMATE')
        
        confidence_map = {
            'ROOFTOP': 95,
            'RANGE_INTERPOLATED': 80,
            'GEOMETRIC_CENTER': 60,
            'APPROXIMATE': 40
        }
        
        return confidence_map.get(location_type, 40)
    
    def close(self):
        """Close cache connection."""
        self.cache.close()


def load_config(config_path="config/config.yaml"):
    """Load configuration from YAML file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def geocode_records(config, limit=None, municipality_limit=None):
    """
    Geocode records using hybrid approach (Nominatim first, then Google).
    
    Args:
        config: Configuration dictionary
        limit: Maximum number of records to process (None for all)
        municipality_limit: Filter by municipality name (partial match)
    """
    # Initialize geocoders
    nominatim = NominatimGeocoder(
        config,
        Path('data/cache/nominatim_cache.sqlite')
    )
    google = GoogleGeocoder(
        config,
        Path('data/cache/google_cache.sqlite')
    )
    
    # Connect to database
    db_url = config['db']['url']
    engine = create_engine(db_url)
    
    # Get records that need geocoding
    print("[*] Finding records to geocode...")
    
    # Build query based on filters
    where_clauses = ["nom_queried_at IS NULL"]
    params = {}
    
    if municipality_limit:
        where_clauses.append("municipality ILIKE :municipality")
        params['municipality'] = f'%{municipality_limit}%'
        print(f"[*] Filtering by municipality: {municipality_limit}")
    
    where_sql = " AND ".join(where_clauses)
    
    with engine.connect() as conn:
        if limit:
            query = text(f"""
                SELECT id, address_query, settlement, municipality
                FROM community_centers
                WHERE {where_sql}
                ORDER BY id
                LIMIT :limit
            """)
            params['limit'] = limit
            result = conn.execute(query, params)
        else:
            query = text(f"""
                SELECT id, address_query, settlement, municipality
                FROM community_centers
                WHERE {where_sql}
                ORDER BY id
            """)
            result = conn.execute(query, params)
        
        records = result.fetchall()
    
    if not records:
        print("[OK] No records to geocode!")
        return
    
    print(f"[*] Found {len(records)} records to geocode")
    
    # Thresholds
    min_confidence = config['thresholds'].get('min_confidence', 60)
    
    # Process each record
    stats = {
        'total': len(records),
        'nominatim_success': 0,
        'nominatim_failed': 0,
        'google_called': 0,
        'google_success': 0,
        'google_failed': 0
    }
    
    for record in tqdm(records, desc="Geocoding"):
        record_id = record.id
        address_query = record.address_query
        settlement = record.settlement
        municipality = record.municipality
        
        if not address_query:
            print(f"\n[WARNING] Record {record_id} has no address_query, skipping")
            continue
        
        # Step 1: Try Nominatim (with fallback strategies)
        nom_result = nominatim.geocode(address_query, settlement, municipality)
        
        # Always store Nominatim result
        with engine.connect() as conn:
            update_query = text("""
                UPDATE community_centers
                SET 
                    lon_nom = :lon_nom,
                    lat_nom = :lat_nom,
                    geom_nom = CASE 
                        WHEN :lon_nom IS NOT NULL AND :lat_nom IS NOT NULL 
                        THEN ST_SetSRID(ST_MakePoint(:lon_nom, :lat_nom), 4326)
                        ELSE NULL 
                    END,
                    nom_display_name = :display_name,
                    nom_osm_type = :osm_type,
                    nom_osm_id = :osm_id,
                    nom_importance = :importance,
                    nom_class = :class,
                    nom_type = :type,
                    nom_confidence = :confidence,
                    nom_raw_json = :raw_json,
                    nom_queried_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
            """)
            
            conn.execute(update_query, {
                'id': record_id,
                'lon_nom': nom_result.get('lon'),
                'lat_nom': nom_result.get('lat'),
                'display_name': nom_result.get('display_name'),
                'osm_type': nom_result.get('osm_type'),
                'osm_id': nom_result.get('osm_id'),
                'importance': nom_result.get('importance'),
                'class': nom_result.get('class'),
                'type': nom_result.get('type'),
                'confidence': nom_result.get('confidence', 0),
                'raw_json': json.dumps(nom_result.get('raw_json', {}))
            })
            conn.commit()
        
        if nom_result['success']:
            stats['nominatim_success'] += 1
        else:
            stats['nominatim_failed'] += 1
        
        # Step 2: Decide if we need Google fallback
        need_google = False
        
        if not nom_result['success']:
            need_google = True
        elif nom_result.get('confidence', 0) < min_confidence:
            need_google = True
        
        # Step 3: Try Google if needed
        if need_google:
            stats['google_called'] += 1
            google_result = google.geocode(address_query)
            
            # Store Google result
            with engine.connect() as conn:
                update_query = text("""
                    UPDATE community_centers
                    SET 
                        lon_g = :lon_g,
                        lat_g = :lat_g,
                        geom_g = CASE 
                            WHEN :lon_g IS NOT NULL AND :lat_g IS NOT NULL 
                            THEN ST_SetSRID(ST_MakePoint(:lon_g, :lat_g), 4326)
                            ELSE NULL 
                        END,
                        g_formatted_address = :formatted_address,
                        g_place_id = :place_id,
                        g_location_type = :location_type,
                        g_types = :types,
                        g_confidence = :confidence,
                        g_raw_json = :raw_json,
                        g_queried_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :id
                """)
                
                conn.execute(update_query, {
                    'id': record_id,
                    'lon_g': google_result.get('lon'),
                    'lat_g': google_result.get('lat'),
                    'formatted_address': google_result.get('formatted_address'),
                    'place_id': google_result.get('place_id'),
                    'location_type': google_result.get('location_type'),
                    'types': json.dumps(google_result.get('types', [])),
                    'confidence': google_result.get('confidence', 0),
                    'raw_json': json.dumps(google_result.get('raw_json', {}))
                })
                conn.commit()
            
            if google_result['success']:
                stats['google_success'] += 1
            else:
                stats['google_failed'] += 1
    
    # Close geocoders
    nominatim.close()
    google.close()
    
    # Print statistics
    print("\n" + "="*60)
    print("[OK] Geocoding completed!")
    print("="*60)
    print(f"Total records processed: {stats['total']}")
    print(f"\nNominatim:")
    print(f"  - Success: {stats['nominatim_success']}")
    print(f"  - Failed: {stats['nominatim_failed']}")
    print(f"\nGoogle:")
    print(f"  - Called: {stats['google_called']}")
    print(f"  - Success: {stats['google_success']}")
    print(f"  - Failed: {stats['google_failed']}")
    print("="*60)
    
    # Show sample results
    print("\n[*] Sample results:")
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, name, settlement,
                   lon_nom, lat_nom, nom_confidence,
                   lon_g, lat_g, g_confidence
            FROM community_centers
            WHERE nom_queried_at IS NOT NULL
            ORDER BY id
            LIMIT 5
        """))
        
        for row in result:
            print(f"\n  ID {row.id}: {row.name}")
            print(f"    Settlement: {row.settlement}")
            if row.lon_nom:
                print(f"    Nominatim: ({row.lat_nom}, {row.lon_nom}) confidence={row.nom_confidence}")
            else:
                print(f"    Nominatim: No result")
            if row.lon_g:
                print(f"    Google: ({row.lat_g}, {row.lon_g}) confidence={row.g_confidence}")
            else:
                print(f"    Google: Not called or no result")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Hybrid geocoder (Nominatim + Google fallback)'
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
    parser.add_argument(
        '--municipality_limit',
        type=str,
        default=None,
        help='Filter by municipality name (partial match, e.g., "ВРАЦА")'
    )
    
    args = parser.parse_args()
    
    # Check if config exists
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        sys.exit(1)
    
    # Load config
    config = load_config(args.config)
    
    # Run geocoding
    geocode_records(config, limit=args.limit, municipality_limit=args.municipality_limit)


if __name__ == '__main__':
    main()

