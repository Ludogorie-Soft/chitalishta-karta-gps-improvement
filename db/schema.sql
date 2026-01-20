-- Chitalishta Geocoder Database Schema
-- PostgreSQL + PostGIS

-- Enable PostGIS extension
CREATE EXTENSION IF NOT EXISTS postgis;

-- Drop table if exists (for clean recreation)
DROP TABLE IF EXISTS community_centers CASCADE;

-- Create main table
CREATE TABLE community_centers (
    -- ========================================
    -- PRIMARY KEY (auto-increment)
    -- ========================================
    id SERIAL PRIMARY KEY,
    
    -- ========================================
    -- SOURCE FIELDS (from Excel)
    -- ========================================
    fid INTEGER,  -- Not unique, from Excel
    name TEXT,
    address_raw TEXT,
    settlement TEXT,
    municipality TEXT,
    url TEXT,
    lon_src DOUBLE PRECISION,
    lat_src DOUBLE PRECISION,
    geom_src GEOMETRY(Point, 4326),

    -- ========================================
    -- NORMALIZATION
    -- ========================================
    address_query TEXT,  -- normalized query string for geocoding

    -- ========================================
    -- NOMINATIM RESULT
    -- ========================================
    lon_nom DOUBLE PRECISION,  -- NULL if no result found
    lat_nom DOUBLE PRECISION,  -- NULL if no result found
    geom_nom GEOMETRY(Point, 4326),  -- NULL if no result found
    nom_display_name TEXT,
    nom_osm_type TEXT,
    nom_osm_id BIGINT,
    nom_importance DOUBLE PRECISION,
    nom_class TEXT,
    nom_type TEXT,
    nom_confidence SMALLINT,  -- 0-100, computed score
    nom_raw_json JSONB,  -- Always populated after query (even if empty result)
    nom_queried_at TIMESTAMPTZ,  -- Timestamp of Nominatim query attempt

    -- ========================================
    -- GOOGLE RESULT
    -- ========================================
    lon_g DOUBLE PRECISION,  -- NULL if no result found
    lat_g DOUBLE PRECISION,  -- NULL if no result found
    geom_g GEOMETRY(Point, 4326),  -- NULL if no result found
    g_formatted_address TEXT,
    g_place_id TEXT,
    g_location_type TEXT,
    g_types JSONB,
    g_confidence SMALLINT,  -- 0-100
    g_raw_json JSONB,  -- Always populated after query (even if empty result)
    g_queried_at TIMESTAMPTZ,  -- Timestamp of Google query attempt

    -- ========================================
    -- DISTANCES (meters)
    -- ========================================
    dist_src_nom_m DOUBLE PRECISION,
    dist_src_g_m DOUBLE PRECISION,
    dist_nom_g_m DOUBLE PRECISION,

    -- ========================================
    -- DECISION
    -- ========================================
    best_provider TEXT,  -- 'src' / 'nominatim' / 'google' / 'none'
    best_lon DOUBLE PRECISION,
    best_lat DOUBLE PRECISION,
    best_geom GEOMETRY(Point, 4326),
    status TEXT,  -- 'ok' / 'needs_review' / 'mismatch' / 'not_found'
    notes TEXT,

    -- ========================================
    -- TIMESTAMPS
    -- ========================================
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ========================================
-- INDEXES
-- ========================================

-- Primary key index (automatic)
-- CREATE INDEX idx_community_centers_fid ON community_centers(fid);  -- redundant, PK already indexed

-- Spatial indexes (GIST)
CREATE INDEX idx_community_centers_geom_src ON community_centers USING GIST(geom_src);
CREATE INDEX idx_community_centers_geom_nom ON community_centers USING GIST(geom_nom);
CREATE INDEX idx_community_centers_geom_g ON community_centers USING GIST(geom_g);
CREATE INDEX idx_community_centers_best_geom ON community_centers USING GIST(best_geom);

-- Status index for filtering
CREATE INDEX idx_community_centers_status ON community_centers(status);

-- Optional: locality indexes for filtering
CREATE INDEX idx_community_centers_municipality ON community_centers(municipality);
CREATE INDEX idx_community_centers_settlement ON community_centers(settlement);

-- Query tracking indexes (to find unprocessed records)
CREATE INDEX idx_community_centers_nom_queried_at ON community_centers(nom_queried_at);
CREATE INDEX idx_community_centers_g_queried_at ON community_centers(g_queried_at);

-- ========================================
-- HELPER FUNCTION: Update updated_at timestamp
-- ========================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to auto-update updated_at
CREATE TRIGGER trigger_update_community_centers_updated_at
    BEFORE UPDATE ON community_centers
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ========================================
-- VERIFICATION
-- ========================================
-- Verify PostGIS is installed
SELECT PostGIS_version();

-- Verify table was created
SELECT 
    schemaname, 
    tablename, 
    tableowner 
FROM pg_tables 
WHERE tablename = 'community_centers';

-- Show table structure
\d community_centers

