-- DBSS builtin:supabase bootstrap.
--
-- The bare supabase/postgres image contains roles, extensions and a minimal
-- `auth` schema, but the Auth and Storage services create most of their tables
-- at runtime. Project migrations frequently reference those objects (foreign keys
-- to auth.users, policies on storage.objects, auth.jwt() in RLS expressions), so
-- this script recreates the commonly referenced parts. Everything created here is
-- part of the baseline snapshot and is never emitted as a DBSS-managed file unless
-- a migration changes it.
--
-- For higher fidelity, add your own bootstrap file (e.g. a schema-only dump of
-- your local Supabase `auth` and `storage` schemas) to "bootstrap" in dbss.config.json.

-- auth -------------------------------------------------------------------------------

ALTER TABLE auth.users
    ADD COLUMN IF NOT EXISTS phone text DEFAULT NULL::character varying,
    ADD COLUMN IF NOT EXISTS phone_confirmed_at timestamptz,
    ADD COLUMN IF NOT EXISTS phone_change text DEFAULT ''::character varying,
    ADD COLUMN IF NOT EXISTS phone_change_token character varying(255) DEFAULT ''::character varying,
    ADD COLUMN IF NOT EXISTS phone_change_sent_at timestamptz,
    ADD COLUMN IF NOT EXISTS email_confirmed_at timestamptz,
    ADD COLUMN IF NOT EXISTS email_change_token_current character varying(255) DEFAULT ''::character varying,
    ADD COLUMN IF NOT EXISTS email_change_confirm_status smallint DEFAULT 0,
    ADD COLUMN IF NOT EXISTS banned_until timestamptz,
    ADD COLUMN IF NOT EXISTS reauthentication_token character varying(255) DEFAULT ''::character varying,
    ADD COLUMN IF NOT EXISTS reauthentication_sent_at timestamptz,
    ADD COLUMN IF NOT EXISTS is_sso_user boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS is_anonymous boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS auth.identities (
    provider_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    identity_data jsonb NOT NULL,
    provider text NOT NULL,
    last_sign_in_at timestamptz,
    created_at timestamptz,
    updated_at timestamptz,
    email text GENERATED ALWAYS AS (lower(identity_data ->> 'email')) STORED,
    id uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    UNIQUE (provider_id, provider)
);
ALTER TABLE auth.identities OWNER TO supabase_auth_admin;

CREATE TABLE IF NOT EXISTS auth.sessions (
    id uuid NOT NULL PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    created_at timestamptz,
    updated_at timestamptz,
    not_after timestamptz,
    refreshed_at timestamp without time zone,
    user_agent text,
    ip inet
);
ALTER TABLE auth.sessions OWNER TO supabase_auth_admin;

CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb
    LANGUAGE sql STABLE
    AS $$
  select
    coalesce(
        nullif(current_setting('request.jwt.claim', true), ''),
        nullif(current_setting('request.jwt.claims', true), '')
    )::jsonb
$$;
ALTER FUNCTION auth.jwt() OWNER TO supabase_auth_admin;
GRANT EXECUTE ON FUNCTION auth.jwt() TO PUBLIC;

-- storage ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS storage.buckets (
    id text NOT NULL PRIMARY KEY,
    name text NOT NULL,
    owner uuid,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    public boolean DEFAULT false,
    avif_autodetection boolean DEFAULT false,
    file_size_limit bigint,
    allowed_mime_types text[],
    owner_id text
);
CREATE UNIQUE INDEX IF NOT EXISTS bname ON storage.buckets USING btree (name);

CREATE TABLE IF NOT EXISTS storage.objects (
    id uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    bucket_id text REFERENCES storage.buckets (id),
    name text,
    owner uuid,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    last_accessed_at timestamptz DEFAULT now(),
    metadata jsonb,
    path_tokens text[] GENERATED ALWAYS AS (string_to_array(name, '/'::text)) STORED,
    version text,
    owner_id text,
    user_metadata jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS bucketid_objname ON storage.objects USING btree (bucket_id, name);
CREATE INDEX IF NOT EXISTS name_prefix_search ON storage.objects USING btree (name text_pattern_ops);
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage.buckets ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION storage.foldername(name text) RETURNS text[]
    LANGUAGE plpgsql IMMUTABLE
    AS $$
DECLARE
    _parts text[];
BEGIN
    SELECT string_to_array(name, '/') INTO _parts;
    RETURN _parts[1 : array_length(_parts, 1) - 1];
END
$$;

CREATE OR REPLACE FUNCTION storage.filename(name text) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE
    _parts text[];
BEGIN
    SELECT string_to_array(name, '/') INTO _parts;
    RETURN _parts[array_length(_parts, 1)];
END
$$;

CREATE OR REPLACE FUNCTION storage.extension(name text) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE
    _parts text[];
    _filename text;
BEGIN
    SELECT string_to_array(name, '/') INTO _parts;
    SELECT _parts[array_length(_parts, 1)] INTO _filename;
    RETURN reverse(split_part(reverse(_filename), '.', 1));
END
$$;

ALTER TABLE storage.buckets OWNER TO supabase_storage_admin;
ALTER TABLE storage.objects OWNER TO supabase_storage_admin;
ALTER FUNCTION storage.foldername(text) OWNER TO supabase_storage_admin;
ALTER FUNCTION storage.filename(text) OWNER TO supabase_storage_admin;
ALTER FUNCTION storage.extension(text) OWNER TO supabase_storage_admin;

GRANT USAGE ON SCHEMA storage TO postgres, anon, authenticated, service_role;
GRANT ALL ON TABLE storage.buckets TO anon, authenticated, service_role, postgres;
GRANT ALL ON TABLE storage.objects TO anon, authenticated, service_role, postgres;
