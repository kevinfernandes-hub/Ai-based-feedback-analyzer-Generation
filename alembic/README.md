This directory contains Alembic migration scripts. To use Alembic:

1. Install alembic: `pip install alembic`
2. Initialize with existing env if needed, or run migrations:

   export DATABASE_URL="postgresql://..."
   alembic upgrade head

The `versions/0001_initial.sql` file contains a suggested initial migration matching the current app schema.
