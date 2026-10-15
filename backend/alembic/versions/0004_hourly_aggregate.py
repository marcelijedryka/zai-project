"""Hourly continuous aggregate over the measurements hypertable (Timescale bonus).

``measurements_hourly`` keeps count, sum, min and max per series and hour. Sum and count (not an
average) are stored so that coarser roll-ups (days) compute a correct average. Real-time
aggregation is switched on explicitly (newer Timescale versions default to materialized-only), so
readers always see the newest raw rows too. The refresh policy looks at the whole history, because
the generator and the API accept backfilled past timestamps; Timescale only re-computes the
regions that were invalidated, so that stays cheap. It runs every minute: rows older than the
aggregate's watermark (about one hour back, moved forward by each run) are only visible after the
next run, and a freshly seeded database (14 days of history sent right after the first, empty run)
would otherwise show an almost empty aggregate for half an hour.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous) cannot run inside a transaction
    # block, and Alembic wraps migrations in one: step out of it for these statements. That also
    # means a failure half-way is not rolled back, hence the IF NOT EXISTS: a re-run resumes.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS measurements_hourly
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT
                series_id,
                time_bucket('1 hour', ts) AS bucket,
                count(*) AS sample_count,
                sum(value) AS value_sum,
                min(value) AS value_min,
                max(value) AS value_max
            FROM measurements
            GROUP BY series_id, time_bucket('1 hour', ts)
            WITH NO DATA
            """
        )
        op.execute(
            """
            SELECT add_continuous_aggregate_policy(
                'measurements_hourly',
                start_offset => NULL,
                end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 minute',
                if_not_exists => true
            )
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "SELECT remove_continuous_aggregate_policy('measurements_hourly', if_exists => true)"
        )
        op.execute("DROP MATERIALIZED VIEW IF EXISTS measurements_hourly")
