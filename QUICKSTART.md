# Quick Start: Testing the Big Cities Fix

## What Was Done

✅ Implemented free-form-first geocoding for big cities (София, Пловдив, Варна, Бургас)  
✅ Enhanced confidence scoring for building/street results  
✅ Added result validation to reject city centers  
✅ Created test script to verify improvements  

## Quick Start (5 Steps)

### Step 1: Verify Configuration

```bash
python scripts/test_big_city_fix.py
```

**Expected output:**
- ✅ Configuration check passes
- Shows current Бургас statistics (before fix)

### Step 2: Clear Cache (Optional but Recommended)

```bash
# Remove old city center results
sqlite3 data/cache/nominatim_cache.sqlite "DELETE FROM cache WHERE address_query LIKE '%БУРГАС%'"
```

### Step 3: Re-geocode Бургас

```bash
python scripts/02_geocode_hybrid.py --municipality_limit БУРГАС
```

**Time:** ~30 seconds for 22 records  
**What happens:** Uses new free-form-first strategy for Бургас addresses

### Step 4: Recompute Distances

```bash
python scripts/03_compute_distances.py
```

**What happens:** Recalculates distances and updates status

### Step 5: Check Results

```bash
python scripts/test_big_city_fix.py
```

**Expected improvements:**
- ✅ IDs 5546, 7104, 7138, 3776 should have <500m distance
- ✅ "Mismatch" status reduced from 18% to <5%
- ✅ "OK" status increased from 18% to >70%
- ✅ Average distance reduced from 4,328m to <500m

## Verification

### Check Specific Problem Case (ID 5546):

```bash
psql -h localhost -p 5436 -U postgres -d chitalishta_maps -c "
SELECT 
    id, name, 
    address_raw,
    dist_src_nom_m as distance_meters,
    status,
    nom_query_used as query,
    nom_display_name as result
FROM community_centers 
WHERE id = 5546;
"
```

**Before fix:**
- Query: `structured:БУРГАС,БУРГАС,Bulgaria`
- Result: `Бургас, България` (city center)
- Distance: 5,487m ❌

**After fix (expected):**
- Query: Full address string (free-form)
- Result: `15, Христо Арнаудов, кв. Крайморие, Бургас...`
- Distance: <100m ✅

## What Changed

### For Big Cities (София, Пловдив, Варна, Бургас):
```
OLD: structured query → city center → 5-8km error
NEW: free-form query → specific building → <100m accuracy
```

### For Small Villages:
```
UNCHANGED: structured query → village center → works well
```

## Troubleshooting

**Q: Test script shows "Big cities not configured"**  
A: Copy settings from `config.example.yaml` to your `config.yaml`

**Q: Still getting city center coordinates**  
A: Clear cache (Step 2) and re-geocode (Step 3)

**Q: No improvement after re-geocoding**  
A: Make sure you ran Step 4 (recompute distances)

**Q: Error "Config file not found"**  
A: `cp config/config.example.yaml config/config.yaml` and edit with your credentials

## Success Metrics

Run this to see improvement:

```bash
psql -h localhost -p 5436 -U postgres -d chitalishta_maps -c "
SELECT 
    COUNT(*) as total,
    ROUND(AVG(dist_src_nom_m)) as avg_distance_m,
    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as ok_count,
    SUM(CASE WHEN status = 'mismatch' THEN 1 ELSE 0 END) as mismatch_count,
    ROUND(AVG(nom_confidence)) as avg_confidence
FROM community_centers 
WHERE settlement = 'ГРАД БУРГАС'
AND dist_src_nom_m IS NOT NULL;
"
```

**Target:**
- avg_distance_m: <500
- ok_count: >15 (70% of 22)
- mismatch_count: <2 (5% of 22)
- avg_confidence: >60

## Next Steps

After validating Бургас improvements:

1. **Geocode София** (when ready):
   ```bash
   python scripts/02_geocode_hybrid.py --settlement_filter "ГРАД СОФИЯ"
   ```

2. **Geocode Пловдив** (when ready):
   ```bash
   python scripts/02_geocode_hybrid.py --settlement_filter "ГРАД ПЛОВДИВ"
   ```

3. **Geocode Варна** (when ready):
   ```bash
   python scripts/02_geocode_hybrid.py --settlement_filter "ГРАД ВАРНА"
   ```

4. **After each city**, run:
   ```bash
   python scripts/03_compute_distances.py
   python scripts/test_big_city_fix.py
   ```

## Documentation

- `IMPLEMENTATION_COMPLETE.md` - Full implementation details and next steps
- `NOMINATIM_BIG_CITIES_IMPROVEMENT_PLAN.md` - Complete analysis and solution options
- `CODE_IMPLEMENTATION_GUIDE.md` - Detailed code examples
- `BIG_CITIES_PROBLEM_SUMMARY.md` - Executive summary with examples

## Questions?

Check the documentation files above or examine:
- `scripts/02_geocode_hybrid.py` - Lines 171-454 for new logic
- `config/config.example.yaml` - Lines 14-49 for configuration
- `scripts/test_big_city_fix.py` - Test and validation script
