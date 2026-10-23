-- Functions (incl. overloads and a procedure), triggers and indexes.
CREATE FUNCTION app.score(a integer) RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT a * 2 $$;
CREATE FUNCTION app.score(a integer, b integer DEFAULT 1) RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT a * b $$;
CREATE FUNCTION app.score(a text) RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT length(a) $$;
COMMENT ON FUNCTION app.score(text) IS 'Score by text length';

CREATE FUNCTION app.articles_by(p_author bigint)
RETURNS SETOF app.articles LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$ SELECT * FROM app.articles WHERE author_id = p_author $$;

CREATE PROCEDURE app.archive_all() LANGUAGE plpgsql AS $$
BEGIN
  UPDATE app.articles SET status = 'archived';
END $$;

CREATE FUNCTION app.touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.created_at := now();
  RETURN NEW;
END $$;

CREATE TRIGGER authors_touch BEFORE INSERT ON app.authors FOR EACH ROW EXECUTE FUNCTION app.touch();
CREATE TRIGGER articles_audit AFTER UPDATE OF title ON app.articles
  FOR EACH ROW WHEN (OLD.title IS DISTINCT FROM NEW.title) EXECUTE FUNCTION app.touch();
CREATE CONSTRAINT TRIGGER authors_check AFTER INSERT ON app.authors
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.touch();

CREATE INDEX articles_title_idx ON app.articles (title);
CREATE INDEX articles_lower_title_idx ON app.articles (lower(title)) WHERE status <> 'draft';
CREATE INDEX "Settings payload idx" ON "App Data"."User Settings" USING gin (payload);
CREATE INDEX events_payload_idx ON app.events USING gin (payload);
COMMENT ON INDEX app.articles_title_idx IS 'Title lookups';
