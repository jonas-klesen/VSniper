"""rescale scores from 1-10 to 1-100

Revision ID: 20260702a001
Revises: 7d7fe0eb44e7
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import json


revision: str = "20260702a001"
down_revision: Union[str, Sequence[str], None] = "7d7fe0eb44e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. app_settings: multiply alert_threshold by 10 (e.g. 9 -> 90)
    conn.execute(sa.text("UPDATE app_settings SET alert_threshold = alert_threshold * 10"))

    # 2. searches: multiply alert_threshold by 10 where set
    conn.execute(
        sa.text("UPDATE searches SET alert_threshold = alert_threshold * 10 WHERE alert_threshold IS NOT NULL")
    )

    # 3. candidates: update score_trace JSON — multiply raw score_10 by 10.
    #    Normalized values (final_score, threshold) stay the same because:
    #    old: final_score = score/10 = 0.8, new: final_score = score/100 = 80/100 = 0.8
    #    old: threshold = alert_threshold/10 = 0.9, new: threshold = alert_threshold/100 = 90/100 = 0.9
    #    So only score_10 (the raw integer) needs to change.
    rows = conn.execute(sa.text("SELECT id, score_trace FROM candidates")).fetchall()
    for row in rows:
        candidate_id = row[0]
        score_trace = row[1]
        if not score_trace:
            continue

        if isinstance(score_trace, str):
            score_trace = json.loads(score_trace)

        if "score_10" in score_trace:
            score_trace["score_10"] = score_trace["score_10"] * 10

        conn.execute(
            sa.text("UPDATE candidates SET score_trace = :st WHERE id = :id"),
            {"st": json.dumps(score_trace), "id": candidate_id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Reverse: divide back by 10
    conn.execute(sa.text("UPDATE app_settings SET alert_threshold = alert_threshold / 10"))
    conn.execute(
        sa.text("UPDATE searches SET alert_threshold = alert_threshold / 10 WHERE alert_threshold IS NOT NULL")
    )

    rows = conn.execute(sa.text("SELECT id, score_trace FROM candidates")).fetchall()
    for row in rows:
        candidate_id = row[0]
        score_trace = row[1]
        if not score_trace:
            continue

        if isinstance(score_trace, str):
            score_trace = json.loads(score_trace)

        if "score_10" in score_trace:
            score_trace["score_10"] = score_trace["score_10"] // 10

        conn.execute(
            sa.text("UPDATE candidates SET score_trace = :st WHERE id = :id"),
            {"st": json.dumps(score_trace), "id": candidate_id},
        )
