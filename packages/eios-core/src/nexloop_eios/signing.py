"""The lifetime a client gives a signed command or read proof (its claims' expires_at upper bound).

The SQL entries accept expires_at in (clock_timestamp(), clock_timestamp() + 25 s], on the database host's clock. A client signs on its
own host: a client clock ahead of the database by any amount once made every proof exceed the 25 s bound (stage 2026-10-20 21:31:53,
two hosts with different NTP sources). Signing with 20 s leaves 5 s of clock skew between the hosts; the SQL bound is unchanged.
"""
from datetime import timedelta

SIGNED_COMMAND_TTL = timedelta(seconds=20)
SQL_SIGNED_COMMAND_BOUND = timedelta(seconds=25)  # the SQL entries' upper bound (documentation and tests only)
