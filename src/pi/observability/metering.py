"""Usage metering: record token usage per run, monthly summaries, user quotas.

Storage reuses the server's async SQLAlchemy engine (UsageRecord table lives
in server/db.py to keep a single metadata base).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pi.observability.prices import estimate_cost
from pi.server.db import UsageRecord


def _month_prefix(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


@dataclass
class QuotaCheck:
    allowed: bool
    used_tokens: int
    quota_tokens: int


class UsageTracker:
    def __init__(self, engine, default_quota: int = 1_000_000):
        self.engine = engine
        self.default_quota = default_quota

    async def record(
        self,
        *,
        user_id: int,
        username: str,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        turns: int,
    ) -> float:
        cost = estimate_cost(model, input_tokens, output_tokens)
        row = UsageRecord(
            user_id=user_id,
            username=username,
            session_id=session_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=cost,
            turns=turns,
        )
        async with AsyncSession(self.engine) as s:
            s.add(row)
            await s.commit()
        return cost

    async def monthly_summary(self, username: str) -> dict[str, Any]:
        prefix = _month_prefix() + "-%"
        async with AsyncSession(self.engine) as s:
            rows = (
                (
                    await s.execute(
                        select(
                            UsageRecord.model,
                            func.sum(UsageRecord.input_tokens),
                            func.sum(UsageRecord.output_tokens),
                            func.sum(UsageRecord.est_cost_usd),
                            func.count(UsageRecord.id),
                        )
                        .where(UsageRecord.username == username, UsageRecord.created_at.like(prefix))
                        .group_by(UsageRecord.model)
                    )
                ).all()
            )
        models = [
            {
                "model": m,
                "input_tokens": int(i or 0),
                "output_tokens": int(o or 0),
                "est_cost_usd": round(float(c or 0), 6),
                "runs": int(n),
            }
            for m, i, o, c, n in rows
        ]
        total_in = sum(m["input_tokens"] for m in models)
        total_out = sum(m["output_tokens"] for m in models)
        return {
            "month": _month_prefix(),
            "models": models,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_est_cost_usd": round(sum(m["est_cost_usd"] for m in models), 6),
        }

    async def monthly_by_user(self) -> list[dict[str, Any]]:
        """Admin console: this month's aggregate per user (one GROUP BY, not N+1)."""
        prefix = _month_prefix() + "-%"
        async with AsyncSession(self.engine) as s:
            rows = (
                await s.execute(
                    select(
                        UsageRecord.username,
                        func.count(UsageRecord.id),
                        func.sum(UsageRecord.input_tokens),
                        func.sum(UsageRecord.output_tokens),
                        func.sum(UsageRecord.est_cost_usd),
                    )
                    .where(UsageRecord.created_at.like(prefix))
                    .group_by(UsageRecord.username)
                )
            ).all()
        return [
            {
                "username": u,
                "runs": int(n),
                "input_tokens": int(i or 0),
                "output_tokens": int(o or 0),
                "est_cost_usd": round(float(c or 0), 6),
            }
            for u, n, i, o, c in rows
        ]

    async def today_summary(self) -> dict[str, Any]:
        """Admin console overview: today's runs/tokens/cost in one aggregate."""
        prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%"
        async with AsyncSession(self.engine) as s:
            runs, in_t, out_t, cost = (
                await s.execute(
                    select(
                        func.count(UsageRecord.id),
                        func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.output_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.est_cost_usd), 0),
                    ).where(UsageRecord.created_at.like(prefix))
                )
            ).one()
        return {
            "date": prefix.rstrip("%"),
            "runs": int(runs),
            "input_tokens": int(in_t),
            "output_tokens": int(out_t),
            "est_cost_usd": round(float(cost), 6),
        }

    async def quota_check(self, user_id: int, quota_tokens: int) -> QuotaCheck:
        prefix = _month_prefix() + "-%"
        async with AsyncSession(self.engine) as s:
            used = (
                await s.execute(
                    select(func.coalesce(func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0)).where(
                        UsageRecord.user_id == user_id,
                        UsageRecord.created_at.like(prefix),
                    )
                )
            ).scalar_one()
        quota = quota_tokens if quota_tokens > 0 else self.default_quota
        return QuotaCheck(allowed=int(used) < quota, used_tokens=int(used), quota_tokens=quota)
