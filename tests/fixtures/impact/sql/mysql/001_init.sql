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
