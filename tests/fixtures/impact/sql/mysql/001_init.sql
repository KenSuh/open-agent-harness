# MySQL-style migration; CREATE TABLE ghost_hash (x int);
CREATE TABLE `customers` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `name` VARCHAR(100) DEFAULT 'it\'s; CREATE TABLE ghost_escape (x int)',
  INDEX idx_name (`name`)
) ENGINE=InnoDB;
CREATE TABLE invoices (
  id INT PRIMARY KEY,
  customer_id INT,
  CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES `customers`(`id`)
);
CREATE INDEX idx_invoices_customer ON invoices (customer_id);
CREATE ALGORITHM=MERGE VIEW invoice_view AS SELECT i.id FROM invoices i;
DELIMITER $$
CREATE DEFINER=`admin`@`%` PROCEDURE close_invoice(IN invoice_id INT)
BEGIN
  IF invoice_id > 0 THEN
    UPDATE invoices SET id = id WHERE id = invoice_id;
  END IF;
  DELETE FROM customers WHERE id = 0;
END$$
CREATE TRIGGER invoices_ai AFTER INSERT ON invoices FOR EACH ROW
BEGIN
  INSERT INTO customers (id) VALUES (NEW.customer_id);
END$$
DELIMITER ;
RENAME TABLE invoices TO bills;
GRANT SELECT, UPDATE ON shop.customers TO 'report'@'%';
GRANT ALL PRIVILEGES ON shop.* TO 'admin'@'localhost';
-- OAH-REGRESSION {"round":"R4","id":"mysql-executable-view-plain","file":"mysql.sql","parts":["\n/*! CREATE VIEW v AS SELECT * FROM users */;"],"entities":[],"links":[],"executable_lines":[2]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-executable-trigger-plain","file":"mysql.sql","parts":["\n/*! CREATE TRIGGER t AFTER INSERT ON users FOR EACH ROW BEGIN UPDATE users SET id = 1; END */;"],"entities":[],"links":[],"executable_lines":[2]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-executable-view-50001","file":"mysql.sql","parts":["\n/*!50001 CREATE VIEW v AS SELECT * FROM users */;"],"entities":[],"links":[],"executable_lines":[2]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-executable-trigger-50001","file":"mysql.sql","parts":["\n/*!50001 CREATE TRIGGER t AFTER INSERT ON users FOR EACH ROW BEGIN UPDATE users SET id = 1; END */;"],"entities":[],"links":[],"executable_lines":[2]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-executable-fragments","file":"migration.sql","parts":["\n/*!50001 CREATE */\n/*!50001 VIEW v AS SELECT * FROM users */;\nCREATE TABLE following(id int);"],"entities":["db_table:following"],"links":[],"executable_lines":[2,3]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-ordinary-comment-is-opaque","file":"mysql.sql","parts":["/* CREATE TABLE ghost(id int); */\nCREATE TABLE following(id int);"],"entities":["db_table:following"],"links":[]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-backslash-'","file":"mysql.sql","parts":["CREATE TABLE users(id int);\nCREATE PROCEDURE p() BEGIN SELECT 'escaped \\' quote; CREATE TABLE ghost(id int);';\nSELECT id FROM users; END;\nCREATE TABLE following(id int);"],"entities":["db_table:users","db_routine:p","db_table:following"],"links":[["db_routine:p","db_table:users","uses"]]}
-- OAH-REGRESSION {"round":"R4","id":"mysql-backslash-\"","file":"mysql.sql","parts":["CREATE TABLE users(id int);\nCREATE PROCEDURE p() BEGIN SELECT \"escaped \\\" quote; CREATE TABLE ghost(id int);\";\nSELECT id FROM users; END;\nCREATE TABLE following(id int);"],"entities":["db_table:users","db_routine:p","db_table:following"],"links":[["db_routine:p","db_table:users","uses"]]}
