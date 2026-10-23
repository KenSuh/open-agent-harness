-- Second migration: alter, rename, drop and revoke.
ALTER TABLE orders ADD COLUMN note text;
ALTER TABLE orders RENAME TO purchases;
DROP VIEW IF EXISTS legacy_view;
REVOKE INSERT ON users FROM app_rw;
