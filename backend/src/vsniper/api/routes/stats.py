from typing import Literal

from fastapi import APIRouter

from vsniper.core.state import get_state
from vsniper.domain.contracts import AiCostStats, DashboardStats, ScoreDistribution

router = APIRouter(tags=["stats"])


@router.get("/stats/dashboard", response_model=DashboardStats)
def dashboard_stats() -> DashboardStats:
    return get_state().candidates.get_dashboard_stats()


@router.get("/stats/costs", response_model=AiCostStats)
def ai_cost_stats() -> AiCostStats:
    return get_state().candidates.get_ai_cost_stats()


@router.get("/stats/score-distribution", response_model=ScoreDistribution)
def score_distribution(window: Literal["1h", "6h", "12h", "1d", "7d", "30d", "all"] = "7d") -> ScoreDistribution:
    return get_state().candidates.get_score_distribution(window)
