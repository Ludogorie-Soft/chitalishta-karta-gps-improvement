# Big Cities Geocoding Fix - Quick Reference Card

## Problem Statement

**Current Issue:** Nominatim returns city center coordinates instead of specific street addresses for big cities.

**Root Cause:** Structured queries (`city=БУРГАС, county=БУРГАС`) omit the street address.

**Impact:** 
- 45% of Бургас addresses have >5km errors
- 18% marked as "mismatch" 
- Average distance: 4,328m (should be <300m)

---

## Solution Summary

**Change query order for big cities to try free-form queries with full street address FIRST**

```
BEFORE: structured → free-form → fallbacks
AFTER:  free-form → structured → fallbacks  (big cities only)
```

---

## Evidence

| ID | Address | Current Result | Proposed Result | Improvement |
|----|---------|---------------|----------------|-------------|
| 5546 | Христо Арнаудов 15 | City center (5.5km error) | Exact building | ✅ 5.5km → 0m |
| 7138 | Консервна 36 | City center (7.9km error) | Street found | ✅ 7.9km → 50m |

---

## Implementation Files

### Must Change:
1. **config/config.example.yaml** - Add big cities list and strategy
2. **scripts/02_geocode_hybrid.py** - Modify `geocode()` method (~150 lines)

### Optional:
3. **utils/address_matching.py** - Extract settlement matching utilities
4. **scripts/test_big_city_fix.py** - Testing script

---

## Key Code Changes

### 1. Configuration (config.yaml)

```yaml
nominatim:
  big_cities:
    - "ГРАД СОФИЯ"
    - "ГРАД ПЛОВДИВ"
    - "ГРАД ВАРНА"
    - "ГРАД БУРГАС"
  
  big_city_strategy:
    use_freeform_first: true
    validate_settlement_match: true
    prefer_building_results: true
    reject_administrative_only: true
```

### 2. Geocoding Logic (02_geocode_hybrid.py)

```python
def geocode(self, address_query, settlement=None, municipality=None):
    # ... (setup code)
    
    is_big_city = self._is_big_city(settlement)
    
    if is_big_city:
        # NEW: Try free-form FIRST for big cities
        result = self._nominatim_request_freeform(address_query, address_query)
        if result and self._validate_big_city_result(result, settlement):
            return result  # Success!
        
        # Fall back to structured
        result = self._nominatim_request_structured(...)
        if result:
            return result
    else:
        # UNCHANGED: Small settlements use structured first
        result = self._nominatim_request_structured(...)
        if result:
            return result
    
    # Common fallbacks...
```

### 3. Result Validation

```python
def _validate_big_city_result(self, result, expected_settlement):
    """Ensure result is from correct city and is specific (not city center)"""
    
    # Reject administrative boundaries (city centers)
    if result['type'] == 'administrative':
        return False
    
    # Prefer buildings and streets
    if result['type'] in ['building', 'highway']:
        return True
    
    # Check settlement name matches
    return settlement_matches(expected_settlement, result_settlement)
```

---

## Testing Procedure

```bash
# 1. Check current status
python scripts/test_big_city_fix.py

# 2. Re-geocode Бургас (after implementing changes)
python scripts/02_geocode_hybrid.py --settlement_filter "ГРАД БУРГАС"

# 3. Recompute distances
python scripts/03_compute_distances.py --settlement_filter "ГРАД БУРГАС"

# 4. Check improvements
python scripts/test_big_city_fix.py
```

---

## Success Metrics

| Metric | Current | Target | Description |
|--------|---------|--------|-------------|
| Mismatch rate (>5km) | 18% | <5% | Serious errors |
| OK status | 18% | >70% | Acceptable quality |
| Average distance | 4,328m | <500m | Overall accuracy |
| >5km errors | 45% | <10% | Bad results |

---

## Risk Assessment

**Risk Level: LOW**

✅ Backward compatible (small settlements unchanged)  
✅ Can be disabled via config  
✅ No database schema changes  
✅ Easy to test incrementally  
✅ Easy to rollback if needed  

---

## Why This Works

1. **Free-form queries include street names** → Nominatim can match actual addresses
2. **OpenStreetMap has excellent big city data** → Street-level detail available
3. **Structured queries are too generic** → Only city+county, no street
4. **Result validation prevents errors** → Check for correct city, reject city centers

---

## Timeline

**Phase 1: Implementation** (13-16 hours)
- Add configuration
- Modify geocoding logic  
- Add validation
- Test with Бургас

**Phase 2: Validation** (7-9 hours, after other cities geocoded)
- Test with София, Пловдив, Варна
- Fine-tune settings
- Handle edge cases

---

## Rollback

### Via Configuration:
```yaml
nominatim:
  big_city_strategy:
    use_freeform_first: false  # Disable new behavior
```

### Via Git:
```bash
git checkout HEAD -- scripts/02_geocode_hybrid.py config/config.example.yaml
```

### Via Database Backup:
```bash
psql -h localhost -p 5436 -U postgres chitalishta_maps < backup_before_fix.sql
```

---

## Questions for Decision

1. **Re-geocode Бургас immediately?** (Yes for validation)
2. **Clear cache for big cities?** (Yes, has bad data)
3. **Log both query types for comparison?** (Optional, useful for analysis)
4. **Confidence threshold?** (Recommend 40 for street-level results)

---

## Documentation Files

1. **NOMINATIM_BIG_CITIES_IMPROVEMENT_PLAN.md** - Detailed analysis and 4 solution options
2. **BIG_CITIES_PROBLEM_SUMMARY.md** - Executive summary with examples
3. **CODE_IMPLEMENTATION_GUIDE.md** - Step-by-step code examples
4. **QUICK_REFERENCE_CARD.md** - This file (one-page overview)

---

## Next Steps

1. ✅ **Review plan** - Done
2. ⏳ **Get approval** - Awaiting your decision
3. ⏳ **Implement changes** - Modify config and geocoding script
4. ⏳ **Test with Бургас** - Re-geocode 22 records
5. ⏳ **Validate results** - Check distance improvements
6. ⏳ **Apply to other cities** - When София, Пловдив, Варна are ready

---

## Contact Points in Code

- **Main geocoding logic:** `scripts/02_geocode_hybrid.py`, line 172, `geocode()` method
- **Structured query:** `scripts/02_geocode_hybrid.py`, line 247, `_nominatim_request_structured()`
- **Free-form query:** `scripts/02_geocode_hybrid.py`, line 293, `_nominatim_request_freeform()`
- **Confidence scoring:** `scripts/02_geocode_hybrid.py`, line 340, `_calculate_confidence()`
- **Config loading:** `scripts/02_geocode_hybrid.py`, line 630, `main()` function
