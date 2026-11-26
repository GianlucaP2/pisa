"""Database failures as the errors Pisa reports (a full sentence, never a traceback)."""

import psycopg

from pisa.domain.errors import PisaError

# PoolTimeout (no free connection because the server is down or overloaded) is an OperationalError too.
UNREACHABLE = (psycopg.OperationalError,)


def database_unreachable() -> PisaError:
    return PisaError("internal", "The database is unreachable.", "Try again shortly")
