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
