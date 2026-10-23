-- Postgres-style schema. CREATE TABLE ghost_in_comment (id int);
/* block comment: CREATE TABLE ghost_block (id int); /* nested */ CREATE TABLE ghost_nested (id int); */
CREATE TABLE IF NOT EXISTS public."Users" (
    id bigserial PRIMARY KEY,
    email text NOT NULL DEFAULT 'CREATE TABLE ghost_string (x int); -- not a comment'
);
CREATE TABLE orders (
    id bigserial PRIMARY KEY,
    user_id bigint REFERENCES public.users (id)
);
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS orders_user_idx ON orders (user_id);
CREATE MATERIALIZED VIEW order_totals AS
    SELECT u.id, count(o.id) FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.id;
CREATE OR REPLACE FUNCTION touch_order() RETURNS trigger AS $body$
BEGIN
    UPDATE orders SET id = id WHERE id = NEW.id;
    RETURN NEW;
END;
$body$ LANGUAGE plpgsql;
CREATE TRIGGER orders_touch BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION touch_order();
GRANT SELECT, INSERT ON TABLE orders, users TO app_rw;
GRANT EXECUTE ON FUNCTION touch_order() TO app_rw;
-- OAH-REGRESSION {"round":"R4","id":"postgres-standard-backslash-1","file":"db/001_init.sql","parts":["CREATE FUNCTION f() RETURNS text LANGUAGE sql AS 'SELECT concat(''\\'', ''CREATE TABLE ghost(id int);'', ''\\'')';\nCREATE TABLE following(id int);"],"entities":["db_routine:f","db_table:following"],"links":[]}
-- OAH-REGRESSION {"round":"R4","id":"postgres-standard-backslash-2","file":"db/001_init.sql","parts":["CREATE FUNCTION f() RETURNS text LANGUAGE sql AS 'SELECT concat(''\\\\'', ''CREATE TABLE ghost(id int);'', ''\\\\'')';\nCREATE TABLE following(id int);"],"entities":["db_routine:f","db_table:following"],"links":[]}
-- OAH-REGRESSION {"round":"R4","id":"postgres-standard-quote-boundary","file":"db/001_init.sql","parts":["SELECT '\\';\nCREATE TABLE following(id int);"],"entities":["db_table:following"],"links":[],"events":{"db_table:following":[["db/001_init.sql",2,"create"]]}}
-- OAH-REGRESSION {"round":"R4","id":"postgres-E-body-standard-inner","file":"db/001_init.sql","parts":["CREATE TABLE users(id int);\nCREATE FUNCTION f() RETURNS text LANGUAGE sql AS E'SELECT concat(\\'\\\\\\', \\'CREATE TABLE ghost(id int);\\', \\'\\\\\\'); SELECT id FROM users';"],"entities":["db_table:users","db_routine:f"],"links":[["db_routine:f","db_table:users","uses"]]}
-- OAH-REGRESSION {"round":"R4","id":"postgres-E-body-escaped-inner","file":"db/001_init.sql","parts":["CREATE TABLE users(id int);\nCREATE FUNCTION f() RETURNS text LANGUAGE sql AS E'SELECT E\\'escaped \\\\\\' quote; CREATE TABLE ghost(id int);\\'; SELECT id FROM users';"],"entities":["db_table:users","db_routine:f"],"links":[["db_routine:f","db_table:users","uses"]]}
