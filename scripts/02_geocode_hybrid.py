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


def extract_nominatim_address_parts(raw_result):
    """
    Extract settlement, municipality, and region from Nominatim API result address object.

    The result is the raw API place object (with 'address' when addressdetails=1).
    Nominatim address keys vary by location; we check common keys for each level.

    Returns:
        tuple: (nom_settlement, nom_municipality, nom_region) - any may be None
    """
    if not raw_result or not isinstance(raw_result, dict):
        return (None, None, None)
    address = raw_result.get('address') or {}
    if not isinstance(address, dict):
        return (None, None, None)

    # Settlement: village, town, city (OSM place levels); fallback locality
    settlement = (
        address.get('village') or
        address.get('town') or
        address.get('city') or
        address.get('locality')
    )
    if settlement and isinstance(settlement, str):
        settlement = settlement.strip() or None
    else:
        settlement = None

    # Municipality (община): municipality or county
    municipality = address.get('municipality') or address.get('county')
    if municipality and isinstance(municipality, str):
        municipality = municipality.strip() or None
    else:
        municipality = None

    # Region (област): state, state_district, region
    region = (
        address.get('state') or
        address.get('state_district') or
        address.get('region')
    )
    if region and isinstance(region, str):
        region = region.strip() or None
    else:
        region = None

    return (settlement, municipality, region)


def normalize_municipality_for_nominatim(municipality):
    """
    Normalize Excel "Община" value to a short name suitable for Nominatim structured search (county).

    Excel often has long strings like "община БУРГАС СЕЛО ИЗВОР Михаи" (address fragment).
    Nominatim only matches real admin names (e.g. "Бургас"), so we extract the first
    meaningful word after "община " to use as county= in structured search.
    """
    if not municipality or not isinstance(municipality, str):
        return None
    s = municipality.strip()
    if not s:
        return None
    # Strip leading "община " (case-insensitive)
    for prefix in ('община ', 'ОБЩИНА ', 'Община '):
        if s.upper().startswith(prefix.upper()):
            s = s[len(prefix):].strip()
            break
    if not s:
        return None
    words = s.split()
    # Skip leading settlement-type tokens (СЕЛО, ГРАД, etc.) so we get the actual municipality name
    skip = ('СЕЛО', 'ГРАД', 'С.', 'ГР.')
    while words and words[0].upper().strip() in skip:
        words = words[1:]
    first_word = words[0] if words else None
    if not first_word or len(first_word) > 80:
        return None
    return first_word.strip()


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
        self.full_config = config  # Store full config for thresholds access
        self.cache = GeocoderCache(cache_path)
        self.last_request_time = 0
    
    def _rate_limit(self):
        """Enforce rate limiting (1 req/sec for Nominatim)."""
        rate_limit = self.config.get('rate_limit_seconds', 1.0)
        elapsed = time.time() - self.last_request_time
        if elapsed < rate_limit:
            time.sleep(rate_limit - elapsed)
        self.last_request_time = time.time()
    
    def _is_big_city(self, settlement):
        """
        Check if settlement is configured as a big city.
        
        Args:
            settlement: Settlement name (e.g., "ГРАД БУРГАС")
        
        Returns:
            bool: True if settlement is in big_cities list
        """
        if not settlement:
            return False
        
        big_cities = self.config.get('big_cities', [])
        return settlement.strip() in big_cities
    
    def _validate_big_city_result(self, result_data, expected_settlement):
        """
        Validate that a free-form geocoding result is actually from the expected big city.
        
        For big cities, we want to ensure:
        1. The result is from the correct city (not same street name in different city)
        2. The result is specific (building/street), not just city center
        3. Settlement name matches (with basic normalization)
        
        Args:
            result_data: Dict from _nominatim_request_freeform
            expected_settlement: Expected settlement (e.g., "ГРАД БУРГАС")
        
        Returns:
            bool: True if result is valid for this big city
        """
        if not result_data or not result_data.get('success'):
            return False
        
        strategy_config = self.config.get('big_city_strategy', {})
        
        # Check 1: Reject administrative-only results (these are usually city centers)
        if strategy_config.get('reject_administrative_only', True):
            osm_type = result_data.get('type', '')
            osm_class = result_data.get('class', '')
            
            # Administrative boundary = city center, not specific address
            if osm_type == 'administrative' or osm_class == 'boundary':
                return False
        
        # Check 2: Prefer building/highway results (actual addresses)
        if strategy_config.get('prefer_building_results', True):
            osm_type = result_data.get('type', '')
            osm_class = result_data.get('class', '')
            
            # These types indicate specific locations
            preferred_classes = ['building', 'highway', 'amenity', 'shop', 'office', 'tourism']
            preferred_types = ['building', 'residential', 'house', 'commercial', 'yes']
            
            is_specific = (osm_class in preferred_classes or osm_type in preferred_types)
            
            # If it's not specific, require higher confidence
            if not is_specific and result_data.get('confidence', 0) < 70:
                return False
        
        # Check 3: Validate settlement match (basic check)
        if strategy_config.get('validate_settlement_match', True):
            # Extract settlement from Nominatim response
            raw_json = result_data.get('raw_json', {})
            address_parts = extract_nominatim_address_parts(raw_json)
            nom_settlement = address_parts[0]  # Returns tuple (settlement, municipality, region)
            
            # Normalize expected settlement (remove "ГРАД" prefix)
            expected_clean = expected_settlement.replace('ГРАД ', '').replace('СЕЛО ', '').strip().upper()
            
            # Basic settlement matching
            if nom_settlement:
                nom_clean = nom_settlement.upper().strip()
                # Check if settlements match (either contains the other)
                if expected_clean not in nom_clean and nom_clean not in expected_clean:
                    # Settlement doesn't match - this might be wrong city
                    return False
        
        return True
    
    def geocode(self, address_query, settlement=None, municipality=None):
        """
        Geocode an address using Nominatim with fallback strategies.
        
        NEW: For big cities (София, Пловдив, Варна, Бургас), tries free-form query
        with full address FIRST, as this provides better street-level results than
        structured queries which only include city+county.

        Uses trusted Excel data (settlement, municipality) to disambiguate:
        - Big cities: free-form first (with validation) → structured fallback → other fallbacks
        - Small settlements: structured first → free-form fallbacks (current behavior)

        Query strategies:
        1. Big cities: Free-form with full address (validated) → Structured → Other fallbacks
        2. Small settlements: Structured → Free-form fallbacks (current behavior)

        Returns:
            dict with keys: success, lat, lon, raw_json, confidence, query_used
        """
        # Normalize municipality to a short name for structured search (Excel often has
        # long strings like "община БУРГАС СЕЛО ИЗВОР Михаи"; Nominatim needs "Бургас")
        municipality_for_structured = normalize_municipality_for_nominatim(municipality)
        municipality_clean = municipality.strip() if municipality else None

        # Cache key: include municipality when present so same address in different
        # municipalities do not share a cached result (use normalized name when available)
        cache_key = address_query
        if municipality:
            cache_key = f"{address_query}|municipality:{municipality_for_structured or municipality_clean or ''}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        settlement_clean = None
        if settlement:
            settlement_clean = settlement.replace('СЕЛО ', '').replace('ГРАД ', '').strip()

        # Prepare fallback queries
        queries_to_try = [address_query]
        if settlement_clean and municipality_clean:
            queries_to_try.append(f"{settlement_clean}, {municipality_clean}, България")
        if settlement_clean:
            queries_to_try.append(f"{settlement_clean}, България")

        result_data = None
        
        # Determine if this is a big city
        is_big_city = self._is_big_city(settlement)
        strategy_config = self.config.get('big_city_strategy', {})

        # ============================================================
        # BIG CITY PATH: Free-form with full address FIRST
        # ============================================================
        if is_big_city and strategy_config.get('use_freeform_first', True):
            # Try free-form query with the full address first
            result_data = self._nominatim_request_freeform(
                query=address_query,
                address_query=address_query
            )
            
            # Validate the result
            if result_data and self._validate_big_city_result(result_data, settlement):
                # Good result from free-form query - use it!
                self.cache.set(cache_key, result_data)
                return result_data
            
            # Free-form didn't work well, try structured as fallback
            if strategy_config.get('fallback_to_structured', True):
                if settlement_clean and municipality_for_structured:
                    result_data = self._nominatim_request_structured(
                        city=settlement_clean,
                        county=municipality_for_structured,
                        country='Bulgaria',
                        address_query=address_query
                    )
                    if result_data is not None:
                        self.cache.set(cache_key, result_data)
                        return result_data

        # ============================================================
        # SMALL SETTLEMENT PATH: Structured query FIRST (current behavior)
        # ============================================================
        else:
            # Current logic: try structured first for small settlements
            if settlement_clean and municipality_for_structured:
                result_data = self._nominatim_request_structured(
                    city=settlement_clean,
                    county=municipality_for_structured,
                    country='Bulgaria',
                    address_query=address_query
                )
                if result_data is not None:
                    self.cache.set(cache_key, result_data)
                    return result_data

        # ============================================================
        # COMMON FALLBACKS: Try other query variations
        # ============================================================
        if result_data is None:
            for query_attempt in queries_to_try:
                result_data = self._nominatim_request_freeform(
                    query_attempt, address_query
                )
                if result_data is not None:
                    break

        # No results found
        if result_data is None:
            result_data = {
                'success': False,
                'lat': None,
                'lon': None,
                'confidence': 0,
                'raw_json': {'error': 'No results found for any query strategy'},
                'queries_tried': queries_to_try
            }

        self.cache.set(cache_key, result_data)
        return result_data

    def _nominatim_request_structured(self, city, county, country, address_query):
        """
        Nominatim structured search (city, county, country). Cannot be combined with q=.
        Use when we have trusted settlement + municipality so result is from correct municipality.
        """
        self._rate_limit()
        params = {
            'city': city,
            'county': county,
            'country': country,
            'format': 'jsonv2',
            'countrycodes': 'bg',
            'limit': 1,
            'addressdetails': 1
        }
        headers = {'User-Agent': self.config['user_agent']}
        try:
            response = requests.get(
                self.config['base_url'],
                params=params,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            results = response.json()
            if not results or len(results) == 0:
                return None
            result = results[0]
            lat = float(result.get('lat', 0))
            lon = float(result.get('lon', 0))
            confidence = self._calculate_confidence(result, address_query)
            return {
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
                'query_used': f"structured:{city},{county},{country}"
            }
        except Exception:
            return None

    def _nominatim_request_freeform(self, query_attempt, address_query):
        """Nominatim free-form search (q=)."""
        self._rate_limit()
        params = {
            'q': query_attempt,
            'format': 'jsonv2',
            'countrycodes': 'bg',
            'limit': 1,
            'addressdetails': 1
        }
        headers = {'User-Agent': self.config['user_agent']}
        try:
            response = requests.get(
                self.config['base_url'],
                params=params,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            results = response.json()
            if not results or len(results) == 0:
                return None
            result = results[0]
            lat = float(result.get('lat', 0))
            lon = float(result.get('lon', 0))
            confidence = self._calculate_confidence(result, address_query)
            if query_attempt != address_query:
                confidence = max(confidence - 20, 30)
            return {
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
                'query_used': query_attempt
            }
        except Exception:
            return None
    
    def _calculate_confidence(self, result, address_query):
        """
        Calculate confidence score (0-100) for Nominatim result.
        
        NEW: Boosts confidence for building/highway results (specific locations).
        Penalizes administrative boundaries (city centers) for big cities.
        
        Factors:
        - Base score: 40 if result exists
        - OSM type (building/highway = specific, administrative = generic)
        - Address component completeness (house number, street)
        - OSM importance score
        - Result precision (way/node vs relation)
        """
        score = 40  # Base score for having a result
        
        # Factor 1: Result type specificity (NEW: enhanced scoring)
        osm_class = result.get('class', '').lower()
        osm_type = result.get('type', '').lower()
        
        # Building results are most specific
        if osm_class == 'building' or osm_type == 'building':
            score += 20
        elif osm_type in ['house', 'yes', 'residential', 'commercial', 'apartments']:
            score += 15
        # Highway/street results are very good for addresses
        elif osm_class == 'highway':
            score += 15
        elif osm_type in ['residential', 'pedestrian', 'service', 'unclassified']:
            score += 10
        # Amenities and places are good
        elif osm_class in ['amenity', 'tourism', 'leisure', 'shop', 'office']:
            score += 12
        # Place types - depends on specificity
        elif osm_class == 'place':
            if osm_type in ['village', 'town', 'city']:
                score += 5  # Just a municipality/settlement, less precise
            elif osm_type in ['neighbourhood', 'suburb', 'quarter']:
                score += 8  # More specific than city
        # Administrative boundaries are too generic (city centers)
        elif osm_class == 'boundary' or osm_type == 'administrative':
            score -= 15  # Penalize - usually city center
        
        # Factor 2: Address component completeness
        address = result.get('address', {})
        if address:
            # House number is very specific
            if address.get('house_number') or address.get('building'):
                score += 15
            # Street/road name is good
            if address.get('street') or address.get('road'):
                score += 8
            # Neighbourhood/suburb adds specificity
            if address.get('neighbourhood') or address.get('suburb') or address.get('quarter'):
                score += 5
        
        # Factor 3: OSM importance (0.0 to 1.0, typically)
        importance = result.get('importance', 0)
        if importance:
            score += int(importance * 15)  # Max +15 points
        
        # Factor 4: OSM geometry type (way/node are more precise than relation)
        result_osm_type = result.get('osm_type', '').lower()
        if result_osm_type == 'node':
            score += 8  # Point location - very precise
        elif result_osm_type == 'way':
            score += 5  # Line or polygon - precise
        elif result_osm_type == 'relation':
            score -= 5  # Usually administrative areas - less precise
        
        # Factor 5: Display name specificity (more commas = more specific)
        display_name = result.get('display_name', '')
        comma_count = display_name.count(',')
        if comma_count >= 5:
            score += 8
        elif comma_count >= 4:
            score += 5
        elif comma_count >= 3:
            score += 3
        
        # Clamp to 0-100 range
        confidence = max(0, min(100, score))
        
        return confidence
    
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
            'region': 'bg',
            'language': 'bg'  # Request results in Bulgarian
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
        
        # Step 1: Try Nominatim (structured by settlement+municipality when trusted, then fallbacks)
        nom_result = nominatim.geocode(address_query, settlement, municipality)
        
        # Extract settlement, municipality, region from Nominatim address for storage
        raw_json = nom_result.get('raw_json') or {}
        nom_settlement, nom_municipality, nom_region = extract_nominatim_address_parts(raw_json)

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
                    nom_settlement = :nom_settlement,
                    nom_municipality = :nom_municipality,
                    nom_region = :nom_region,
                    nom_query_used = :nom_query_used,
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
                'raw_json': json.dumps(raw_json),
                'nom_settlement': nom_settlement,
                'nom_municipality': nom_municipality,
                'nom_region': nom_region,
                'nom_query_used': nom_result.get('query_used'),
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

