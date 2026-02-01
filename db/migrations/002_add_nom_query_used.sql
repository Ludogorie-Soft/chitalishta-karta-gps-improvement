-- Migration: Add nom_query_used to community_centers
-- Stores the query actually sent to Nominatim: "structured:city,county,country" or free-form string.
-- Use: value starts with "structured:" => structured search; otherwise => free-form fallback.

ALTER TABLE community_centers ADD COLUMN IF NOT EXISTS nom_query_used TEXT;

COMMENT ON COLUMN community_centers.nom_query_used IS 'Query actually sent to Nominatim: structured:city,county,country or free-form string';
