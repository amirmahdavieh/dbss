-- Later changes to existing objects.
ALTER POLICY "Published articles are public" ON app.articles RENAME TO "Anyone reads published";
ALTER POLICY "Anyone reads published" ON app.articles USING (status IN ('published', 'archived'));
DROP POLICY "temp" ON app.articles;

DROP TRIGGER authors_touch ON app.authors;
CREATE TRIGGER authors_touch BEFORE INSERT OR UPDATE ON app.authors FOR EACH ROW EXECUTE FUNCTION app.touch();
ALTER TABLE app.articles DISABLE TRIGGER articles_audit;

DROP INDEX app.articles_title_idx;
CREATE UNIQUE INDEX articles_title_idx ON app.articles (author_id, title) INCLUDE (status);

ALTER TYPE app.status RENAME VALUE 'review' TO 'in_review';
CREATE OR REPLACE FUNCTION app.score(a text) RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT length(a) * 10 $$;
ALTER FUNCTION app.score(integer, integer) RENAME TO weighted_score;

ALTER TABLE app.authors RENAME COLUMN name TO display_name;
ALTER TABLE app.authors ALTER COLUMN display_name SET STATISTICS 500;
ALTER TABLE app.scratch ALTER COLUMN v SET STORAGE EXTERNAL;
REVOKE UPDATE (tags) ON app.articles FROM app_reader;
