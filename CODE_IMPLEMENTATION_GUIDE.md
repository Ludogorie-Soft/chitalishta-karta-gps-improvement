# Code Implementation Guide for Big Cities Fix

## Overview

This document provides detailed code examples for implementing the big cities geocoding improvement.

---

## 1. Configuration Changes

### File: `config/config.example.yaml`

Add this new section after the existing `nominatim` configuration:

```yaml
nominatim:
  base_url: "https://nominatim.openstreetmap.org/search"
  user_agent: "ChitalishtatMapProject/1.0"
  rate_limit_seconds: 1.0
  
  # NEW: Big cities optimization
  big_cities:
    # Settlements where free-form queries with full address work better than structured queries
    # These cities have detailed street-level data in OpenStreetMap
    - "ГРАД СОФИЯ"
    - "ГРАД ПЛОВДИВ"
    - "ГРАД ВАРНА"
    - "ГРАД БУРГАС"
  
  big_city_strategy:
    # Try free-form query FIRST (instead of structured query)
    use_freeform_first: true
    
    # Minimum confidence threshold for free-form results
    # Lower than standard because street-level results are valuable even with lower confidence
    min_confidence_for_freeform: 40
    
    # Result validation settings
    validate_settlement_match: true      # Check if result is from expected city
    prefer_building_results: true        # Prefer building/highway over administrative
    reject_administrative_only: true     # Don't accept city center as final result
    
    # How many top results to check when validating
    max_results_to_check: 3
    
    # Fallback behavior
    fallback_to_structured: true         # Try structured query if free-form fails

# Existing distance thresholds (no changes needed)
distance_thresholds:
  ok_distance_m: 1000
  suspicious_distance_m: 5000
  min_confidence: 60
```

---

## 2. Core Geocoding Logic Changes

### File: `scripts/02_geocode_hybrid.py`

#### A. Add Helper Method to Check if Settlement is Big City

Add this method to the `NominatimGeocoder` class (around line 170):

```python
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
```

#### B. Add Result Validation Method for Big Cities

Add this method to the `NominatimGeocoder` class:

```python
def _validate_big_city_result(self, result_data, expected_settlement):
    """
    Validate that a free-form geocoding result is actually from the expected big city.
    
    For big cities, we want to ensure:
    1. The result is from the correct city (not same street name in different city)
    2. The result is specific (building/street), not just city center
    3. Settlement name matches (with transliteration tolerance)
    
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
        preferred_types = ['building', 'highway', 'place', 'amenity', 'shop', 'office']
        preferred_classes = ['building', 'highway', 'place']
        
        is_specific = (osm_type in preferred_types or osm_class in preferred_classes)
        
        # If it's not specific, require higher confidence
        if not is_specific and result_data.get('confidence', 0) < 70:
            return False
    
    # Check 3: Validate settlement match
    if strategy_config.get('validate_settlement_match', True):
        # Extract settlement from Nominatim response
        raw_json = result_data.get('raw_json', {})
        address_parts = extract_nominatim_address_parts(raw_json)
        nom_settlement = address_parts.get('settlement')
        
        # Normalize expected settlement (remove "ГРАД" prefix)
        expected_clean = expected_settlement.replace('ГРАД ', '').replace('СЕЛО ', '').strip()
        
        # Use the existing settlement_matches logic from script 03
        # For now, simple check (can be enhanced with transliteration)
        if nom_settlement:
            nom_clean = nom_settlement.upper().strip()
            if expected_clean.upper() not in nom_clean and nom_clean not in expected_clean.upper():
                # Settlement doesn't match - this might be wrong city
                return False
    
    return True
```

#### C. Modify Main `geocode()` Method

Replace the existing `geocode()` method (starting around line 172):

```python
def geocode(self, address_query, settlement=None, municipality=None):
    """
    Geocode an address using Nominatim with fallback strategies.
    
    NEW: For big cities, tries free-form query with full address FIRST,
    as this provides better street-level results than structured queries.
    
    Uses trusted Excel data (settlement, municipality) to disambiguate:
    - Big cities: free-form first, then structured fallback
    - Small settlements: structured first (current behavior)
    
    Query strategies:
    1. Big cities: Free-form with full address (validated) → Structured → Other fallbacks
    2. Small settlements: Structured → Free-form fallbacks (current behavior)
    
    Returns:
        dict with keys: success, lat, lon, raw_json, confidence
    """
    # Normalize municipality for structured search
    municipality_for_structured = normalize_municipality_for_nominatim(municipality)
    municipality_clean = municipality.strip() if municipality else None
    
    # Cache key
    cache_key = address_query
    if municipality:
        cache_key = f"{address_query}|municipality:{municipality_for_structured or municipality_clean or ''}"
    cached = self.cache.get(cache_key)
    if cached is not None:
        return cached
    
    # Clean settlement name
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
```

#### D. Enhance Confidence Scoring

Modify the `_calculate_confidence()` method (around line 340) to boost confidence for building-type results:

```python
def _calculate_confidence(self, result, address_query):
    """
    Calculate a confidence score (0-100) for a Nominatim result.
    
    NEW: Boosts confidence for building/highway results in big cities.
    
    Factors:
    - OSM importance score
    - Result type (building > highway > place > administrative)
    - Address component matching
    - Display name length (shorter = more specific)
    
    Returns:
        int: Confidence score 0-100
    """
    confidence = 50  # Base confidence
    
    # Factor 1: OSM importance (0.0 to 1.0, typically)
    importance = result.get('importance', 0)
    if importance:
        confidence += int(importance * 20)  # Max +20 points
    
    # Factor 2: Result type (NEW: boost for specific types)
    osm_type = result.get('type', '').lower()
    osm_class = result.get('class', '').lower()
    
    if osm_type == 'building' or osm_class == 'building':
        confidence += 15  # Building = very specific
    elif osm_type in ['highway', 'residential', 'pedestrian'] or osm_class == 'highway':
        confidence += 10  # Street = specific
    elif osm_type in ['place', 'amenity', 'shop', 'office']:
        confidence += 5   # Named place = somewhat specific
    elif osm_type == 'administrative' or osm_class == 'boundary':
        confidence -= 10  # Administrative = too generic
    
    # Factor 3: Display name specificity
    display_name = result.get('display_name', '')
    # More commas = more specific address
    comma_count = display_name.count(',')
    if comma_count >= 4:
        confidence += 10
    elif comma_count >= 3:
        confidence += 5
    
    # Factor 4: Address completeness
    address = result.get('address', {})
    if address:
        # Check for detailed address components
        if 'house_number' in address or 'building' in address:
            confidence += 10
        if 'road' in address or 'street' in address:
            confidence += 5
    
    # Clamp to 0-100 range
    confidence = max(0, min(100, confidence))
    
    return confidence
```

---

## 3. Testing Code

### Create Test Script: `scripts/test_big_city_fix.py`

```python
#!/usr/bin/env python3
"""
Test script to validate big city geocoding improvements.
Checks the 4 problematic Бургас addresses before and after fix.
"""

import sys
import yaml
from sqlalchemy import create_engine, text

def load_config():
    with open('config/config.yaml', 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def test_problem_cases():
    """Test the 4 known problematic IDs from Бургас"""
    config = load_config()
    engine = create_engine(config['database']['connection_string'])
    
    test_ids = [5546, 7104, 7138, 3776]
    
    print("Testing problematic Бургас addresses:")
    print("=" * 80)
    
    with engine.connect() as conn:
        for test_id in test_ids:
            result = conn.execute(text("""
                SELECT id, name, address_raw, 
                       lon_src, lat_src, lon_nom, lat_nom,
                       dist_src_nom_m, status, nom_confidence,
                       nom_display_name, nom_query_used
                FROM community_centers
                WHERE id = :id
            """), {"id": test_id})
            
            row = result.fetchone()
            if row:
                print(f"\nID {row[0]}: {row[1]}")
                print(f"  Address: {row[2]}")
                print(f"  Distance: {row[7]:.1f}m")
                print(f"  Status: {row[8]}")
                print(f"  Confidence: {row[9]}")
                print(f"  Query: {row[11]}")
                print(f"  Result: {row[10]}")
                
                # Evaluate improvement
                if row[7] and row[7] < 500:
                    print(f"  ✅ GOOD - Distance <500m")
                elif row[7] and row[7] < 1000:
                    print(f"  ⚠️  OK - Distance <1km") 
                else:
                    print(f"  ❌ PROBLEM - Distance >{row[7]:.0f}m")
    
    print("\n" + "=" * 80)
    
    # Overall statistics
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT 
                COUNT(*) as total,
                AVG(dist_src_nom_m) as avg_dist,
                SUM(CASE WHEN dist_src_nom_m < 500 THEN 1 ELSE 0 END) as under_500m,
                SUM(CASE WHEN dist_src_nom_m > 5000 THEN 1 ELSE 0 END) as over_5km,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as ok_status,
                SUM(CASE WHEN status = 'mismatch' THEN 1 ELSE 0 END) as mismatch_status
            FROM community_centers
            WHERE settlement = 'ГРАД БУРГАС'
            AND dist_src_nom_m IS NOT NULL
        """))
        
        row = result.fetchone()
        print(f"\nБургас Overall Statistics:")
        print(f"  Total records: {row[0]}")
        print(f"  Average distance: {row[1]:.1f}m")
        print(f"  Within 500m: {row[2]} ({row[2]/row[0]*100:.1f}%)")
        print(f"  Over 5km: {row[3]} ({row[3]/row[0]*100:.1f}%)")
        print(f"  Status 'ok': {row[4]} ({row[4]/row[0]*100:.1f}%)")
        print(f"  Status 'mismatch': {row[5]} ({row[5]/row[0]*100:.1f}%)")
        
        # Target metrics
        print(f"\n  Target Metrics:")
        print(f"    Within 500m: >80% (current: {row[2]/row[0]*100:.1f}%)")
        print(f"    Over 5km: <10% (current: {row[3]/row[0]*100:.1f}%)")
        print(f"    Status 'ok': >70% (current: {row[4]/row[0]*100:.1f}%)")
        print(f"    Status 'mismatch': <5% (current: {row[5]/row[0]*100:.1f}%)")

if __name__ == '__main__':
    test_problem_cases()
```

### Usage:

```bash
# Before fix
python scripts/test_big_city_fix.py

# Re-geocode Бургас after implementing fix
python scripts/02_geocode_hybrid.py --settlement_filter "ГРАД БУРГАС"

# Recompute distances
python scripts/03_compute_distances.py

# After fix
python scripts/test_big_city_fix.py
```

---

## 4. Settlement Matching Utility (Enhancement)

### Option: Extract to shared utility file

Create `utils/address_matching.py`:

```python
#!/usr/bin/env python3
"""
Address matching utilities for Bulgarian addresses.
Handles transliteration, normalization, and settlement matching.
"""

def cyrillic_to_latin(text):
    """
    Transliterate Cyrillic to Latin using Bulgarian transliteration rules.
    
    Examples:
        БУРГАС -> BURGAS
        София -> Sofia
        Варна -> Varna
    """
    if not text:
        return ''
    
    transliteration_map = {
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E',
        'Ж': 'ZH', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L',
        'М': 'M', 'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S',
        'Т': 'T', 'У': 'U', 'Ф': 'F', 'Х': 'H', 'Ц': 'TS', 'Ч': 'CH',
        'Ш': 'SH', 'Щ': 'SHT', 'Ъ': 'A', 'Ь': 'Y', 'Ю': 'YU', 'Я': 'YA',
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l',
        'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's',
        'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch',
        'ш': 'sh', 'щ': 'sht', 'ъ': 'a', 'ь': 'y', 'ю': 'yu', 'я': 'ya'
    }
    
    result = []
    for char in text:
        result.append(transliteration_map.get(char, char))
    
    return ''.join(result)


def normalize_settlement_name(name):
    """
    Normalize settlement name for matching.
    Removes prefixes like СЕЛО, ГРАД, С., ГР.
    """
    if not name:
        return ''
    
    name = name.upper().strip()
    
    # Remove common prefixes
    prefixes = ['СЕЛО ', 'ГРАД ', 'С. ', 'ГР. ', 'С.', 'ГР.']
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
            break
    
    return name


def settlement_matches(expected_settlement, actual_settlement):
    """
    Check if two settlement names match, accounting for:
    - Case differences
    - Cyrillic/Latin transliteration
    - Settlement type prefixes (СЕЛО, ГРАД)
    - Whitespace normalization
    
    Args:
        expected_settlement: Settlement from source data (e.g., "ГРАД БУРГАС")
        actual_settlement: Settlement from geocoder (e.g., "Burgas" or "Бургас")
    
    Returns:
        bool: True if settlements match
    """
    if not expected_settlement or not actual_settlement:
        return False
    
    # Normalize both
    expected_clean = normalize_settlement_name(expected_settlement)
    actual_clean = normalize_settlement_name(actual_settlement)
    
    # Direct match (case-insensitive)
    if expected_clean.upper() == actual_clean.upper():
        return True
    
    # Try transliteration
    expected_latin = cyrillic_to_latin(expected_clean).upper()
    actual_latin = cyrillic_to_latin(actual_clean).upper()
    
    if expected_latin == actual_latin:
        return True
    
    # Partial match (one contains the other, for compound names)
    if len(expected_clean) > 3 and len(actual_clean) > 3:
        if expected_clean.upper() in actual_clean.upper():
            return True
        if actual_clean.upper() in expected_clean.upper():
            return True
    
    return False
```

Then import in `scripts/02_geocode_hybrid.py`:

```python
from utils.address_matching import settlement_matches, normalize_settlement_name
```

---

## 5. Command Line Testing

### Step-by-step testing process:

```bash
# 1. Backup current database (optional but recommended)
pg_dump -h localhost -p 5436 -U postgres chitalishta_maps > backup_before_fix.sql

# 2. Check current status
python scripts/test_big_city_fix.py

# 3. Clear Nominatim cache for Бургас (optional - forces re-geocoding)
sqlite3 data/cache/nominatim_cache.sqlite "DELETE FROM cache WHERE query LIKE '%БУРГАС%'"

# 4. Re-geocode Бургас with new logic
python scripts/02_geocode_hybrid.py --settlement_filter "ГРАД БУРГАС"

# 5. Recompute distances and status
python scripts/03_compute_distances.py --settlement_filter "ГРАД БУРГАС"

# 6. Check results
python scripts/test_big_city_fix.py

# 7. Compare specific problem cases
psql -h localhost -p 5436 -U postgres -d chitalishta_maps -c \
  "SELECT id, name, dist_src_nom_m, status, nom_query_used 
   FROM community_centers 
   WHERE id IN (5546, 7104, 7138, 3776);"
```

---

## 6. Rollback Plan

If the changes cause issues:

### A. Disable via Configuration

```yaml
# In config/config.yaml
nominatim:
  big_city_strategy:
    use_freeform_first: false  # Disable new logic
```

### B. Restore from Backup

```bash
# Restore database backup
psql -h localhost -p 5436 -U postgres chitalishta_maps < backup_before_fix.sql
```

### C. Revert Code Changes

```bash
# If using git
git checkout HEAD -- scripts/02_geocode_hybrid.py
git checkout HEAD -- config/config.example.yaml
```

---

## Summary of Changes

### Files Modified:
1. `config/config.example.yaml` - Add big_cities configuration
2. `scripts/02_geocode_hybrid.py` - Modify query logic (~150 lines changed)

### Files Created (Optional):
1. `utils/address_matching.py` - Shared utilities
2. `scripts/test_big_city_fix.py` - Testing script

### Backward Compatibility:
- ✅ Small settlements use existing logic (no change)
- ✅ Can be disabled via configuration
- ✅ All fallbacks remain in place
- ✅ No database schema changes

### Testing Strategy:
1. Test with Бургас (22 records, already geocoded)
2. Validate 4 specific problem cases
3. Check overall statistics improvement
4. Apply to other big cities when ready
