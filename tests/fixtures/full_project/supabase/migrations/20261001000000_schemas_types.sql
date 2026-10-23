-- Schemas, roles and user-defined types.
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA extensions;
CREATE SCHEMA app;
CREATE SCHEMA "App Data";

CREATE ROLE app_reader NOLOGIN;
GRANT app_reader TO authenticated;

CREATE TYPE app.status AS ENUM ('draft', 'published');
ALTER TYPE app.status ADD VALUE 'archived';
ALTER TYPE app.status ADD VALUE 'review' BEFORE 'published';

CREATE DOMAIN app.positive_int AS integer DEFAULT 1 NOT NULL CHECK (VALUE > 0);
CREATE DOMAIN app.slug AS text COLLATE "C" CONSTRAINT slug_format CHECK (VALUE ~ '^[a-z0-9-]+$');

CREATE TYPE app.address AS (street text, city text COLLATE "C", zip varchar(10));
CREATE TYPE app.float_range AS RANGE (subtype = float8, subtype_diff = float8mi);
COMMENT ON TYPE app.status IS 'Publication workflow state';
