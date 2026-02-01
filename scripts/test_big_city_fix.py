#!/usr/bin/env python3
"""
Test script to validate big city geocoding improvements.
Checks the 4 problematic Бургас addresses and overall statistics.
"""

import sys
import yaml
from pathlib import Path
from sqlalchemy import create_engine, text


def load_config():
    """Load configuration from YAML file."""
    config_path = Path('config/config.yaml')
    if not config_path.exists():
        print("[ERROR] Config file not found: config/config.yaml")
        print("[INFO] Make sure you've copied config.example.yaml to config.yaml")
        sys.exit(1)
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def test_problem_cases():
    """Test the 4 known problematic IDs from Бургас"""
    config = load_config()
    engine = create_engine(config['db']['url'])
    
    test_ids = [5546, 7104, 7138, 3776]
    
    print("=" * 80)
    print("TESTING PROBLEMATIC БУРГАС ADDRESSES")
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
                print(f"\n{'─' * 80}")
                print(f"ID {row[0]}: {row[1]}")
                print(f"{'─' * 80}")
                print(f"  Address: {row[2]}")
                print(f"  Source coords: ({row[3]:.6f}, {row[4]:.6f})")
                
                if row[5] and row[6]:
                    print(f"  Nominatim coords: ({row[5]:.6f}, {row[6]:.6f})")
                    print(f"  Distance: {row[7]:.1f}m")
                    print(f"  Status: {row[8]}")
                    print(f"  Confidence: {row[9]}")
                    print(f"  Query: {row[11]}")
                    print(f"  Result: {row[10]}")
                    
                    # Evaluate improvement
                    if row[7] and row[7] < 100:
                        print(f"  ✅ EXCELLENT - Distance <100m")
                    elif row[7] and row[7] < 500:
                        print(f"  ✅ GOOD - Distance <500m")
                    elif row[7] and row[7] < 1000:
                        print(f"  ⚠️  OK - Distance <1km")
                    elif row[7] and row[7] < 5000:
                        print(f"  ⚠️  NEEDS REVIEW - Distance 1-5km")
                    else:
                        print(f"  ❌ PROBLEM - Distance >{row[7]:.0f}m (mismatch)")
                else:
                    print(f"  ❌ NOT GEOCODED YET")
    
    print("\n" + "=" * 80)
    print("БУРГАС OVERALL STATISTICS")
    print("=" * 80)
    
    # Overall statistics
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT 
                COUNT(*) as total,
                AVG(dist_src_nom_m) as avg_dist,
                SUM(CASE WHEN dist_src_nom_m < 100 THEN 1 ELSE 0 END) as under_100m,
                SUM(CASE WHEN dist_src_nom_m < 500 THEN 1 ELSE 0 END) as under_500m,
                SUM(CASE WHEN dist_src_nom_m > 5000 THEN 1 ELSE 0 END) as over_5km,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as ok_status,
                SUM(CASE WHEN status = 'needs_review' THEN 1 ELSE 0 END) as review_status,
                SUM(CASE WHEN status = 'mismatch' THEN 1 ELSE 0 END) as mismatch_status,
                AVG(nom_confidence) as avg_confidence
            FROM community_centers
            WHERE settlement = 'ГРАД БУРГАС'
            AND dist_src_nom_m IS NOT NULL
        """))
        
        row = result.fetchone()
        
        if row and row[0] > 0:
            total = row[0]
            print(f"\nTotal records: {total}")
            print(f"Average distance: {row[1]:.1f}m")
            print(f"Average confidence: {row[8]:.1f}")
            
            print(f"\n📊 Distance Distribution:")
            print(f"  Within 100m:  {row[2]:2d} ({row[2]/total*100:5.1f}%)")
            print(f"  Within 500m:  {row[3]:2d} ({row[3]/total*100:5.1f}%)")
            print(f"  Over 5km:     {row[4]:2d} ({row[4]/total*100:5.1f}%)")
            
            print(f"\n📊 Status Distribution:")
            print(f"  OK:           {row[5]:2d} ({row[5]/total*100:5.1f}%)")
            print(f"  Needs Review: {row[6]:2d} ({row[6]/total*100:5.1f}%)")
            print(f"  Mismatch:     {row[7]:2d} ({row[7]/total*100:5.1f}%)")
            
            print(f"\n🎯 Target Metrics vs Current:")
            print(f"  Within 500m:    Target >80%,  Current {row[3]/total*100:5.1f}%  {'✅' if row[3]/total >= 0.8 else '❌'}")
            print(f"  Over 5km:       Target <10%,  Current {row[4]/total*100:5.1f}%  {'✅' if row[4]/total <= 0.1 else '❌'}")
            print(f"  Status 'ok':    Target >70%,  Current {row[5]/total*100:5.1f}%  {'✅' if row[5]/total >= 0.7 else '❌'}")
            print(f"  Status 'mismatch': Target <5%, Current {row[7]/total*100:5.1f}%  {'✅' if row[7]/total <= 0.05 else '❌'}")
        else:
            print("\n⚠️  No geocoded records found for ГРАД БУРГАС")
            print("Run: python scripts/02_geocode_hybrid.py")
    
    print("\n" + "=" * 80)


def check_config():
    """Check if big cities configuration is present."""
    print("\n" + "=" * 80)
    print("CONFIGURATION CHECK")
    print("=" * 80)
    
    config = load_config()
    
    if 'nominatim' in config and 'big_cities' in config['nominatim']:
        big_cities = config['nominatim']['big_cities']
        print(f"\n✅ Big cities configuration found:")
        for city in big_cities:
            print(f"   - {city}")
        
        if 'big_city_strategy' in config['nominatim']:
            strategy = config['nominatim']['big_city_strategy']
            print(f"\n✅ Big city strategy:")
            print(f"   - Use freeform first: {strategy.get('use_freeform_first', False)}")
            print(f"   - Validate settlement: {strategy.get('validate_settlement_match', False)}")
            print(f"   - Prefer buildings: {strategy.get('prefer_building_results', False)}")
        else:
            print(f"\n❌ Big city strategy not configured")
    else:
        print(f"\n❌ Big cities not configured in config.yaml")
        print(f"   Please update your config.yaml with big_cities settings from config.example.yaml")
    
    print("=" * 80)


if __name__ == '__main__':
    try:
        check_config()
        test_problem_cases()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
