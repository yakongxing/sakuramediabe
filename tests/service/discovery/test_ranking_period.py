import pytest

from src.api.exception.errors import ApiError
from src.service.discovery.ranking_service import (
    RankingBoardDefinition,
    RankingCatalogService,
)


@pytest.mark.parametrize("period", [None, "", "  ", "daily", " DAILY "])
def test_current_board_resolves_client_default_to_existing_scope(period):
    board = RankingBoardDefinition(key="current", name="当前榜单")
    assert RankingCatalogService._resolve_period(board, period) == ""


@pytest.mark.parametrize("period", ["weekly", "monthly", "2026", "unknown"])
def test_current_board_rejects_nondefault_periods(period):
    board = RankingBoardDefinition(key="current", name="当前榜单")
    with pytest.raises(ApiError) as error:
        RankingCatalogService._resolve_period(board, period)
    assert error.value.status_code == 422
    assert error.value.code == "invalid_ranking_period"


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
def test_periodic_board_keeps_each_period_scope(period):
    board = RankingBoardDefinition(
        key="hot", name="热榜", supported_periods=("daily", "weekly", "monthly"),
    )
    assert RankingCatalogService._resolve_period(board, period) == period


@pytest.mark.parametrize("period", [None, "", "daily", "2024"])
def test_dynamic_year_board_still_requires_a_supported_year(period):
    board = RankingBoardDefinition(
        key="yearly", name="年度榜单", supported_periods_provider=lambda: ("2025", "2026"),
    )
    with pytest.raises(ApiError) as error:
        RankingCatalogService._resolve_period(board, period)
    assert error.value.status_code == 422
    assert error.value.code == "invalid_ranking_period"
