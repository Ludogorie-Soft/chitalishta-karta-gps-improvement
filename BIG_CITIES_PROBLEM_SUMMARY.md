# Big Cities Geocoding Problem - Quick Summary

## The Problem in 3 Points

1. **Structured queries are too generic for big cities**
   - Current query: `structured:БУРГАС,БУРГАС,Bulgaria`
   - Missing: The actual street address!
   - Result: Nominatim returns city center coordinates

2. **Impact is significant**
   - 45% of Бургас addresses have >5km error
   - 18% marked as "mismatch" (should use original coords)
   - Average distance: 4,328m (should be <300m)

3. **Free-form queries work much better**
   - Include full street address
   - Nominatim finds specific buildings/streets
   - Distance errors drop from 5-8km to <100m

---

## Real Examples from Your Data

### ID 5546 - Христо Арнаудов 15, кв. Крайморие

| Query Type | Query Sent | Result | Distance Error |
|------------|-----------|---------|----------------|
| **Structured** (current) | `city=БУРГАС, county=БУРГАС` | City center (27.472, 42.494) | **5,487m** ❌ |
| **Free-form** (proposed) | `Христо Арнаудов 15, кв. Крайморие, Бургас` | Exact building (27.487, 42.446) | **~0m** ✅ |

### ID 7138 - Консервна 36

| Query Type | Query Sent | Result | Distance Error |
|------------|-----------|---------|----------------|
| **Structured** (current) | `city=БУРГАС, county=БУРГАС` | City center (27.472, 42.494) | **7,886m** ❌ |
| **Free-form** (proposed) | `Консервна 36, Бургас, България` | Street (27.528, 42.436) | **~50m** ✅ |

---

## The Solution

### Change Query Order for Big Cities

```
CURRENT LOGIC (all settlements):
1. Structured query (city + county) 
2. Free-form query with address ← Too late!
3. Free-form query with settlement only

PROPOSED LOGIC (big cities only):
1. Free-form query with full address ← Try this FIRST!
2. Validate result is from correct city
3. Fall back to structured if needed
4. Other fallbacks
```

### Configuration Approach

```yaml
# config/config.yaml
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
```

---

## Expected Improvements

### Бургас Statistics

| Metric | Current | Expected After Fix |
|--------|---------|-------------------|
| Mismatch rate (>5km) | 18% (4 records) | <5% (1 record) |
| OK status rate | 18% | >70% |
| Average distance | 4,328m | <300m |
| >5km errors | 10 records (45%) | 1-2 records (5-10%) |

### Why This Works

1. **Free-form queries include street names**
   - Nominatim can match actual street addresses
   - Returns building/highway types instead of administrative boundaries

2. **OpenStreetMap has excellent data for big cities**
   - Major Bulgarian cities are well-mapped
   - Street-level detail is available
   - Small villages may not have as much detail (keep structured for them)

3. **Settlement validation prevents wrong city matches**
   - Check if result is actually from Бургас
   - Use transliteration to match Cyrillic/Latin
   - Reject results from other cities with same street name

---

## Implementation Effort

### Phase 1: Core Implementation
- **Effort:** 13-16 hours
- **Changes:** 
  - Add configuration for big cities
  - Modify geocoding query order
  - Add result validation
  - Update confidence scoring
  - Test with Бургас data

### Phase 2: Validation & Tuning  
- **Effort:** 7-9 hours
- **When:** After София, Пловдив, Варна are geocoded
- **Focus:** Fine-tune settings, handle edge cases

---

## Next Steps

1. **Review this plan** ✓ You are here
2. **Approve approach** ← Awaiting your decision
3. **Implement Phase 1** (modify scripts)
4. **Test with Бургас** (re-geocode 22 records)
5. **Validate improvements** (check distances)
6. **Apply to other cities** (when ready)
7. **Fine-tune** (adjust settings based on results)

---

## Questions to Consider

1. **Should we re-geocode Бургас immediately after implementation?**
   - Pro: Quick validation of fix
   - Con: Uses API quota

2. **Should we clear the cache for big cities?**
   - Current cache has bad results
   - Need to re-query with new strategy

3. **Should we log both structured and free-form results for comparison?**
   - Useful for validation
   - Requires 2x API calls (slower)

4. **What confidence threshold for big city results?**
   - Currently: 60 (standard)
   - Proposed: 40 for street-level results
   - Street-level results are more valuable even with lower confidence

---

## Files That Will Change

### Modified Files
- `config/config.example.yaml` - Add big cities config
- `scripts/02_geocode_hybrid.py` - Change query logic (~100-150 lines)
- `scripts/03_compute_distances.py` - Extract settlement matching utility

### New Files (Optional)
- `utils/address_matching.py` - Shared matching functions
- `scripts/analysis/compare_results.py` - Before/after comparison
- `tests/test_big_city_geocoding.py` - Unit tests

---

## Success Criteria

✅ Distance errors >5km reduced from 45% to <10%  
✅ "Mismatch" status reduced from 18% to <5%  
✅ "OK" status increased from 18% to >70%  
✅ Average distance reduced from 4,328m to <500m  
✅ No regression for small settlements (keep current logic)  
✅ Solution works for all 4 big cities  

---

## Risk: Very Low

- Backward compatible (only affects big cities)
- Can be disabled via configuration
- Keeps all fallback strategies
- No database schema changes required
- Easy to test and validate incrementally
- Can revert if issues arise
