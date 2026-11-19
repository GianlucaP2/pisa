# Migrations

`NNNN_snake_case.sql`, numbered from 1 without gaps, applied in order by `pisa/db/migrate.py` (`pisa migrate`), each
file in its own transaction (so `CREATE INDEX CONCURRENTLY` can't run inside one), recorded in
`schema_migrations` with its name and checksum. Forward only: roll back the code, not the schema
(docs-site/self-hosting.md, "Updating").

- `0001_baseline.sql` is the whole schema of a fresh database: tables, indexes, constraints, functions,
  triggers, views and seed rows, grouped by feature. The database, its roles and their default privileges come
  from `deploy/postgres/init` (superuser), so the baseline holds no `CREATE ROLE` or `GRANT`.
- A change is a new file, `0002_*` and on. An applied file is never edited (the runner refuses a changed
  checksum), and a database made by another migration history is refused: start it empty.
- `/readyz` reports the schema ready when the highest applied version equals the highest file's.
- Additive changes first; a constraint on a big table is `NOT VALID`, then `VALIDATE`.
- **A future index on a kept table is built `CONCURRENTLY`**: nothing about the user is deleted by age, so those
  tables only grow, and a plain `CREATE INDEX` locks writes for its whole build. That needs a migration run
  outside a transaction: add that to `migrate.py` with the first such index.
- `tests/test_migrate.py` checks that the migrations applied to an empty database give the schema listed in
  `tests/schema.txt`; a new migration regenerates that file (the command is in the test's docstring).
