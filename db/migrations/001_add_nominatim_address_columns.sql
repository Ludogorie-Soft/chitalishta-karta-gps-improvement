-- Migration: Add nom_settlement, nom_municipality, nom_region to community_centers
-- Run this on existing databases that were created before these columns existed.
-- New installs use schema.sql which already includes these columns.

ALTER TABLE community_centers ADD COLUMN IF NOT EXISTS nom_settlement TEXT;
ALTER TABLE community_centers ADD COLUMN IF NOT EXISTS nom_municipality TEXT;
ALTER TABLE community_centers ADD COLUMN IF NOT EXISTS nom_region TEXT;

COMMENT ON COLUMN community_centers.nom_settlement IS 'Extracted from Nominatim address (e.g. Нивянин, Кермен)';
COMMENT ON COLUMN community_centers.nom_municipality IS 'Extracted from Nominatim address (e.g. Радомир, Сливен)';
COMMENT ON COLUMN community_centers.nom_region IS 'Extracted from Nominatim address (e.g. Враца, Перник)';
