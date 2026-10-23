-- RLS policies, grants, default privileges.
ALTER TABLE app.articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.articles FORCE ROW LEVEL SECURITY;
CREATE POLICY "Published articles are public" ON app.articles FOR SELECT USING (status = 'published');
CREATE POLICY "Authors manage own" ON app.articles AS PERMISSIVE FOR ALL TO authenticated
  USING (author_id IN (SELECT id FROM app.authors WHERE user_id = auth.uid()));
CREATE POLICY "No archived edits" ON app.articles AS RESTRICTIVE FOR UPDATE TO authenticated, app_reader
  USING (status <> 'archived') WITH CHECK (status <> 'archived');
CREATE POLICY "temp" ON app.articles FOR DELETE USING (false);

ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO app_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

GRANT USAGE ON SCHEMA app TO anon, authenticated, app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO authenticated;
GRANT SELECT, INSERT, UPDATE ON app.articles TO authenticated;
GRANT UPDATE (title, tags) ON app.articles TO app_reader;
GRANT USAGE, SELECT ON SEQUENCE app.articles_id_seq TO authenticated;
REVOKE EXECUTE ON FUNCTION app.score(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.score(integer) TO authenticated WITH GRANT OPTION;
GRANT USAGE ON TYPE app.status TO anon;
GRANT ALL ON "App Data"."User Settings" TO service_role;
