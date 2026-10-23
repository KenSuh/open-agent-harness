-- SQLite-style migration
CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT, author_id INTEGER REFERENCES authors(id));
CREATE TABLE IF NOT EXISTS authors (id INTEGER PRIMARY KEY, name TEXT);
CREATE VIRTUAL TABLE notes_fts USING fts5(body);
CREATE TEMP VIEW IF NOT EXISTS recent_notes AS SELECT * FROM notes WHERE id > 10;
CREATE INDEX IF NOT EXISTS notes_author ON notes(author_id);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
  SELECT CASE WHEN new.body = '' THEN RAISE(ABORT, 'empty; END') END;
END;
DROP TABLE IF EXISTS old_notes;
