# Implementation Plan: Improving Nominatim Geocoding for Big Cities

## Problem Analysis

### Current Issue

For addresses in the 4 big Bulgarian cities (София, Пловдив, Варна, Бургас), Nominatim is returning **city center coordinates** instead of specific street addresses, causing:
- Large distance errors (5-8km from source coordinates)
- Status marked as "mismatch" (serious issue)
- Original coordinates are better than Nominatim results

### Root Cause

The current implementation uses **structured queries** for cities:
```
structured:БУРГАС,БУРГАС,Bulgaria
```

This query only contains:
- `city=БУРГАС`
- `county=БУРГАС` 
- `country=Bulgaria`

**The street address is completely omitted from the query!**

Nominatim returns the city administrative boundary center since no specific address is provided.

### Evidence from Testing

Testing with actual problem cases (IDs: 5546, 7104, 7138, 3776):

| ID | Address | Structured Query Result | Free-form Query Result | Distance Improvement |
|----|---------|------------------------|------------------------|---------------------|
| 5546 | Христо Арнаудов 15, кв. Крайморие | City center (27.472, 42.494) | Exact building (27.487, 42.446) | ~5.5km → ~0m |
| 7138 | Консервна 36 | City center (27.472, 42.494) | Street found (27.528, 42.436) | ~7.9km → accurate |
| 7104 | Захари Стоянов 19 | City center (27.472, 42.494) | Multiple streets found | ~6.7km → needs filtering |
| 3776 | к-с Меден рудник бл. 25 | City center (27.472, 42.494) | No results | No improvement |

### Current Statistics (Бургас data)

- **Total records in Бургас:** 22
- **Status breakdown:**
  - mismatch: 4 (18%)
  - needs_review: 14 (64%)
  - ok: 4 (18%)
- **Distance distribution (all structured queries):**
  - <100m: 1 record
  - 100-500m: 3 records
  - 500-1000m: 3 records
  - 1-5km: 5 records
  - **>5km: 10 records (45%!)** ← This is the problem
- **Average distance for structured queries:** 4,328 meters

## Proposed Solutions

### Solution 1: Priority Free-form Query for Big Cities (RECOMMENDED)

**Strategy:** For addresses in big cities, use free-form query with full street address FIRST, before falling back to structured query.

#### Advantages
✅ Leverages Nominatim's better street-level matching  
✅ Includes the actual street address in the query  
✅ Testing shows dramatic improvements (5.5km → accurate)  
✅ Minimal code changes  
✅ Maintains backward compatibility  
✅ Can still fall back to structured if needed  

#### Disadvantages
❌ May return results from wrong municipalities (need validation)  
❌ Requires settlement name matching to filter results  

#### Implementation Steps

1. **Add big cities configuration** (`config/config.example.yaml`)
   ```yaml
   geocoding:
     big_cities:
       # List of settlements considered "big cities" where free-form queries work better
       - "ГРАД СОФИЯ"
       - "ГРАД ПЛОВДИВ" 
       - "ГРАД ВАРНА"
       - "ГРАД БУРГАС"
     big_city_strategy:
       # For big cities: try free-form with full address FIRST
       # Falls back to structured query if no good results
       use_freeform_first: true
       min_confidence_for_freeform: 40  # Accept lower confidence for street-level results
   ```

2. **Modify geocoding logic** (`scripts/02_geocode_hybrid.py`)
   - Detect if settlement is in big cities list
   - For big cities:
     1. Try free-form query with full `address_query` FIRST
     2. Validate result matches expected settlement
     3. If good match found (building/highway + settlement match), use it
     4. Otherwise fall back to structured query
     5. Then try other fallbacks as before
   - For small settlements: keep current logic (structured first)

3. **Enhance result validation**
   - Check OSM type: prefer `building`, `highway`, `place` over `administrative`
   - Check if settlement name appears in Nominatim response
   - Use transliteration matching (Cyrillic ↔ Latin)
   - Reject results clearly from wrong city

4. **Update confidence scoring** (`_calculate_confidence()`)
   - Boost confidence for street-level results (building/highway types)
   - Penalize administrative boundary results in big cities
   - Consider OSM importance score
   - Factor in address component matching

#### Pseudo-code Logic

```python
def geocode(self, address_query, settlement=None, municipality=None):
    # Check if this is a big city
    is_big_city = settlement in self.config['geocoding']['big_cities']
    
    result_data = None
    
    if is_big_city:
        # BIG CITY PATH: Try free-form with full address FIRST
        result_data = self._nominatim_request_freeform(
            query=address_query,
            address_query=address_query
        )
        
        # Validate the result
        if result_data and self._validate_big_city_result(result_data, settlement):
            return result_data
        
        # If free-form didn't work well, try structured as fallback
        if settlement_clean and municipality_for_structured:
            result_data = self._nominatim_request_structured(...)
            if result_data:
                return result_data
    else:
        # SMALL SETTLEMENT PATH: Use current logic (structured first)
        if settlement_clean and municipality_for_structured:
            result_data = self._nominatim_request_structured(...)
            if result_data:
                return result_data
    
    # Common fallbacks for both paths
    for query_attempt in queries_to_try:
        result_data = self._nominatim_request_freeform(...)
        if result_data:
            break
    
    return result_data

def _validate_big_city_result(self, result_data, expected_settlement):
    """Validate that free-form result is from the correct city"""
    # Check OSM type - prefer specific locations
    osm_type = result_data.get('type')
    if osm_type == 'administrative':
        return False  # Too generic for big city
    
    # Extract settlement from result
    nom_settlement = extract_settlement_from_result(result_data)
    
    # Check if settlements match (with transliteration)
    if settlement_matches(expected_settlement, nom_settlement):
        return True
    
    return False
```

---

### Solution 2: Enhanced Structured Query with Street Parameter

**Strategy:** Use Nominatim's `street` parameter in structured queries to include the street address.

#### Advantages
✅ Uses Nominatim's intended structured query format  
✅ More explicit address component separation  
✅ May provide better disambiguation  

#### Disadvantages
❌ Requires parsing street name from `address_query`  
❌ Bulgarian address formats are complex (к-с, жк, бл., etc.)  
❌ May not work well for complex addresses  
❌ More code complexity for address parsing  

#### Implementation Outline

1. **Add address parsing function**
   ```python
   def parse_bulgarian_address(address_query):
       """Extract street, number, district from Bulgarian address"""
       # Parse patterns like:
       # "Христо Арнаудов 15, кв. Крайморие"
       # "к-с Меден рудник бл. 25"
       # "ж.к. Западен парк, бл. 60"
       # Returns: dict with street, number, district, etc.
   ```

2. **Modify structured query to include street**
   ```python
   params = {
       'street': parsed_street,  # e.g., "Христо Арнаудов 15"
       'city': city,
       'county': county,
       'country': country,
       ...
   }
   ```

3. **Handle parsing failures gracefully**
   - Fall back to free-form if parsing fails
   - Log unparseable address patterns

---

### Solution 3: Hybrid Approach with Result Ranking

**Strategy:** Query both structured and free-form, then rank results based on quality metrics.

#### Advantages
✅ Gets best of both worlds  
✅ Can learn from result patterns  
✅ More robust  

#### Disadvantages
❌ 2x API calls = slower + rate limit issues  
❌ More complex result comparison logic  
❌ Reduced cache effectiveness  

#### Implementation Outline

1. **Make both queries** (for big cities only)
   - Structured query: `city=БУРГАС, county=БУРГАС`
   - Free-form query: full address string

2. **Rank results** based on:
   - OSM type (building > highway > place > administrative)
   - Distance from source coordinates (if available)
   - Confidence score
   - Settlement name match
   - Importance score

3. **Select best result**
   - Pick highest scoring result
   - Store both results in database for analysis

---

### Solution 4: Use Google Maps API for Big Cities

**Strategy:** Skip Nominatim entirely for big cities, use Google directly.

#### Advantages
✅ Google has excellent street-level data for major cities  
✅ Simple logic change  
✅ High accuracy expected  

#### Disadvantages
❌ Costs money (Google API charges)  
❌ Defeats purpose of open-source solution  
❌ Inconsistent approach across dataset  
❌ May hit API quota quickly  

---

## Recommended Implementation Plan

### Phase 1: Solution 1 Implementation (Priority Free-form for Big Cities)

**Effort:** Medium  
**Impact:** High  
**Risk:** Low  

**Steps:**

1. **Configuration** (1 hour)
   - Add `big_cities` list to config
   - Add `big_city_strategy` settings
   - Document configuration options

2. **Core Geocoding Logic** (3-4 hours)
   - Modify `NominatimGeocoder.geocode()` method
   - Add big city detection
   - Reorder query strategies for big cities
   - Implement `_validate_big_city_result()` method
   - Add result type checking (building vs administrative)

3. **Settlement Matching Enhancement** (2-3 hours)
   - Move `settlement_matches()` from script 03 to shared utility
   - Enhance transliteration for city names
   - Add more robust matching logic
   - Handle district/neighborhood names

4. **Confidence Scoring Update** (2 hours)
   - Update `_calculate_confidence()` method
   - Boost score for building/highway types
   - Penalize administrative types in big cities
   - Consider importance scores

5. **Testing** (3-4 hours)
   - Re-geocode Бургас records with new logic
   - Compare before/after distances
   - Verify status improvements
   - Test edge cases (addresses without street numbers, etc.)

6. **Database Migration** (1 hour)
   - Optional: add column to track which query strategy succeeded
   - Add column for validation flags

7. **Documentation** (1 hour)
   - Update README with big cities strategy
   - Document new configuration options
   - Add troubleshooting guide

**Total Effort:** 13-16 hours

### Phase 2: Validation & Refinement (After Sofia/Varna/Plovdiv geocoding)

1. **Analyze Results Across All Big Cities** (2 hours)
   - Compare accuracy metrics per city
   - Identify remaining problem patterns
   - Check if strategy works consistently

2. **Fine-tune Settings** (2-3 hours)
   - Adjust confidence thresholds
   - Refine settlement matching rules
   - Update big cities list if needed

3. **Handle Edge Cases** (3-4 hours)
   - Implement Solution 2 (street parameter) for complex cases
   - Add special handling for жк, к-с addresses
   - Handle district-only addresses

**Total Effort:** 7-9 hours

### Phase 3: Advanced Improvements (Optional)

1. **Implement Result Caching Strategy** (2 hours)
   - Separate cache for big cities
   - Store multiple results for ranking

2. **Add Result Quality Metrics** (2 hours)
   - Log OSM type distribution
   - Track query strategy success rates
   - Create geocoding quality report

3. **Machine Learning for Result Selection** (future)
   - Train model on validated results
   - Predict best result from multiple candidates

---

## Testing Strategy

### Test Cases

1. **Existing Problem Cases (Regression Test)**
   - IDs: 5546, 7104, 7138, 3776
   - Expected: Distance < 500m, status = "ok"

2. **Different Address Types**
   - Street + number: "Христо Арнаудов 15"
   - Complex + district: "к-с Меден рудник бл. 25"
   - Boulevard: "бул. Сливница 46"
   - Residential complex: "ж.к. Западен парк, бл. 60"

3. **Ambiguous Street Names**
   - "Захари Стоянов" (exists in multiple cities)
   - Common street names

4. **Edge Cases**
   - Addresses without street numbers
   - Postal code only
   - District/neighborhood only

### Success Metrics

**Primary Metrics:**
- **Distance accuracy:** >80% of big city addresses within 500m
- **Mismatch reduction:** <5% with distance >5km (currently 18% for Бургас)
- **Status improvement:** >70% marked as "ok" (currently 18%)

**Secondary Metrics:**
- Average distance: <300m (currently 4,328m)
- Confidence score: >60 average
- Query success rate: >90%

### Testing Commands

```bash
# Re-geocode only Бургас records with new logic
python scripts/02_geocode_hybrid.py --municipality_limit БУРГАС

# Recompute distances and status
python scripts/03_compute_distances.py

# Generate comparison report
python scripts/analysis/compare_geocoding_versions.py
```

---

## Database Schema Changes (Optional)

### Add Query Strategy Tracking

```sql
ALTER TABLE community_centers 
ADD COLUMN nom_query_strategy VARCHAR(50);
-- Values: 'freeform_first', 'structured_first', 'structured_only', etc.

ALTER TABLE community_centers
ADD COLUMN nom_result_type VARCHAR(50);
-- Values: 'building', 'highway', 'administrative', 'place', etc.

ALTER TABLE community_centers
ADD COLUMN nom_validation_flags TEXT;
-- JSON: {"settlement_match": true, "building_type": true, ...}
```

---

## Configuration File Changes

### `config/config.example.yaml`

```yaml
nominatim:
  base_url: "https://nominatim.openstreetmap.org/search"
  user_agent: "ChitalishtatMapProject/1.0"
  rate_limit_seconds: 1.0
  
  # Big cities optimization
  big_cities:
    # Settlements where free-form queries work better than structured
    - "ГРАД СОФИЯ"
    - "ГРАД ПЛОВДИВ"
    - "ГРАД ВАРНА"
    - "ГРАД БУРГАС"
  
  big_city_strategy:
    # Try free-form query with full address FIRST for big cities
    use_freeform_first: true
    
    # Accept lower confidence for street-level results
    min_confidence_for_freeform: 40
    
    # Validation settings
    validate_settlement_match: true
    prefer_building_results: true
    reject_administrative_results: true
    
    # Fallback settings
    fallback_to_structured: true
    max_results_to_check: 3

distance_thresholds:
  ok_distance_m: 1000
  suspicious_distance_m: 5000
  min_confidence: 60
```

---

## Code Changes Summary

### Files to Modify

1. **`config/config.example.yaml`**
   - Add big cities configuration section

2. **`scripts/02_geocode_hybrid.py`**
   - Modify `NominatimGeocoder.geocode()` method
   - Add `_validate_big_city_result()` method
   - Add `_is_big_city()` method
   - Enhance `_calculate_confidence()` method
   - Update query strategy logic

3. **`scripts/03_compute_distances.py`**
   - Extract `settlement_matches()` to shared utility
   - Consider using query strategy in status calculation

### New Files to Create

1. **`utils/address_matching.py`** (optional)
   - Shared settlement matching functions
   - Transliteration utilities
   - Address parsing helpers

2. **`scripts/analysis/compare_geocoding_versions.py`** (optional)
   - Compare before/after geocoding results
   - Generate accuracy reports

3. **`tests/test_big_city_geocoding.py`** (optional)
   - Unit tests for new logic
   - Integration tests with known addresses

---

## Risk Assessment

### Low Risk
✅ Solution 1 is backward compatible  
✅ Existing cache remains valid  
✅ No database schema changes required  
✅ Can be easily reverted  

### Medium Risk
⚠️ May need configuration tuning per city  
⚠️ Settlement matching may have edge cases  
⚠️ Rate limiting considerations with more queries  

### Mitigation
- Implement with feature flag (can disable if issues)
- Test thoroughly with Бургас before scaling
- Keep detailed logs of query strategies
- Monitor API rate limits

---

## Alternative Considerations

### Why Not Fix at Distance Calculation Stage?

The distance calculation (script 03) could theoretically detect this and prefer source coordinates, BUT:
- ❌ Doesn't fix root cause
- ❌ Loses value of geocoding service
- ❌ Can't distinguish between "city center fallback" and "correct result that happens to be far"
- ❌ Perpetuates bad data in database

**Better to fix at geocoding stage.**

### Why Not Use OSM Overpass API?

Overpass API allows direct querying of OSM data:
- ✅ No rate limits (self-hosted)
- ✅ More control over results
- ❌ Requires setting up own infrastructure
- ❌ More complex query language
- ❌ No geocoding/address parsing built-in

**Good for future optimization, but overkill for now.**

---

## Conclusion

**Recommended approach: Solution 1 (Priority Free-form for Big Cities)**

This provides the best balance of:
- ✅ High impact (fixes 45% of big city records)
- ✅ Low risk (backward compatible, easily tested)
- ✅ Reasonable effort (13-16 hours initial implementation)
- ✅ Maintainable (clean configuration-driven approach)

The root cause is clear: structured queries omit street addresses. Free-form queries include them and work much better for big cities where OpenStreetMap has detailed street data.

Implementation should be done incrementally:
1. Test with Бургас (already geocoded)
2. Validate improvements
3. Apply to София, Пловдив, Варна when ready
4. Fine-tune based on results
5. Consider hybrid approaches for remaining edge cases
