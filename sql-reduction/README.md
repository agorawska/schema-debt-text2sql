# sql-reduction

Reduces a SQL schema dump (DDL) to a compact representation that keeps the character of the data and stays executable. Developed for MySQL dumps (`mysqldump --no-data`); also handles simple PostgreSQL DDL.

## Usage

```bash
python3 main.py [input.sql] [output.sql] [removed_lines.txt]
```

Defaults: `dump.sql` → `simplified_dump.sql`, removed lines → `removed_lines.txt`. Requires Python 3 (standard library only).

## What is kept

- tables, columns and their types with lengths (`varchar(255)`, `decimal(10,2)`, `json`, ...),
- `tinyint(1)` (boolean semantics in MySQL),
- `NOT NULL` and explicit `NULL` (e.g. `timestamp NULL`, which differs from `timestamp` in MySQL 5.7),
- `PRIMARY KEY`, `FOREIGN KEY ... REFERENCES`,
- indexes required by foreign keys (InnoDB needs an index on referenced columns),
- backtick-quoted identifiers.

## What is removed

- dump comments and conditional statements (`-- ...`, `/*!40101 SET ... */;`),
- table options (`ENGINE`, `AUTO_INCREMENT=`, `CHARSET`, `COLLATE`),
- all other indexes: `KEY`, `UNIQUE KEY`, `FULLTEXT KEY`, `CREATE [UNIQUE] INDEX`, column-level `UNIQUE`,
- FK constraint names and referential actions (`ON DELETE` / `ON UPDATE`),
- column attributes: `unsigned`, `zerofill`, `AUTO_INCREMENT`, `GENERATED ... AS IDENTITY`, `DEFAULT`, `ON UPDATE CURRENT_TIMESTAMP`, `CHARACTER SET`, `COLLATE`, `COMMENT`,
- generated column expressions (the column and its type are kept),
- integer display widths (`int(11)` → `int`),
- the database/schema prefix in table names.

If the input disables foreign key checks, the output starts with `SET FOREIGN_KEY_CHECKS=0;`, because tables in a dump are ordered alphabetically, not by dependency.

## Outputs and checks

- **Reduced DDL**: written to the output file.
- **Removed lines log**: every input line that was removed entirely (lines that were only shortened are not listed), in the format:
  ```
  table: <table name, or - outside any table>
  line in input file: <line number>
  <original content>
  ```
- **Integrity check**: the script compares the set of tables and the number of columns per table before and after reduction. On any difference it prints the details and exits with code 1.