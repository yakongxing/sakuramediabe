from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers.deps import db_deps, get_current_user
from src.api.routers.discovery.ranking_sources import router
from src.model import Movie, RankingItem
from src.service.discovery.ranking_service import (
    RANKING_SOURCES,
    RankingBoardDefinition,
    RankingSourceDefinition,
    RankingSyncService,
)


def test_frontend_reads_synced_current_boards_with_daily_default(test_db, monkeypatch):
    numbers = [f"RANK-{i:03d}" for i in range(1, 4)]
    for i, number in enumerate(numbers, start=1):
        Movie.create(movie_number=number, javdb_id=f"id{i}", title=f"Movie {i}", heat=i)
    monkeypatch.setitem(
        RANKING_SOURCES, "supjav",
        RankingSourceDefinition(
            key="supjav", name="Supjav",
            boards=tuple(
                RankingBoardDefinition(key=key, name=name, fetch_numbers=lambda _: numbers)
                for key, name in (("day", "日榜"), ("week", "周榜"), ("month", "月榜"))
            ),
        ),
    )
    result = RankingSyncService().sync_all_rankings(source_keys=("supjav",))
    assert result["success_targets"] == 3
    assert result["stored_items"] == 9

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[db_deps] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: object()
    with TestClient(app) as client:
        boards_response = client.get("/ranking-sources/supjav/boards")
        assert boards_response.status_code == 200
        boards = boards_response.json()
        assert [board["board_key"] for board in boards] == ["day", "week", "month"]
        assert all(board["supported_periods"] == [] for board in boards)
        for board in boards:
            path = f"/ranking-sources/supjav/boards/{board['board_key']}/items"
            # 无周期榜单的客户端默认值，应读取已经同步到空周期的条目。
            query = {"period": "daily", "page": 1, "page_size": 24}
            response = client.get(path, params=query)
            assert response.status_code == 200, response.json()
            body = response.json()
            assert body["total"] == 3
            assert body["synced_at"] is not None
            assert [(item["movie_number"], item["rank"]) for item in body["items"]] == [
                (number, rank) for rank, number in enumerate(numbers, start=1)
            ]
            assert client.get(path, params={**query, "period": ""}).json() == body
            assert client.get(path, params={"page_size": 24}).json() == body
            sorted_response = client.get(path, params={**query, "sort": "heat:desc", "page_size": 2})
            assert sorted_response.status_code == 200
            assert [item["rank"] for item in sorted_response.json()["items"]] == [3, 2]
            next_page = client.get(path, params={**query, "sort": "heat:desc", "page_size": 2, "page": 2})
            assert next_page.status_code == 200
            assert [item["rank"] for item in next_page.json()["items"]] == [1]
    assert {row.period for row in RankingItem.select()} == {""}
