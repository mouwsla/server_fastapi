import os

import httpx
import pytest


BASE_URL = os.getenv("BASE_URL", "http://app:8000")
TILE_PATH = "/tiles/7/77/40"
REQUEST_TIMEOUT = 10.0


pytestmark = pytest.mark.skipif(
    os.getenv("EXPECT_REDIS_DOWN") != "1",
    reason="Redis failure test is disabled",
)


def test_app_works_without_redis():
    readiness = httpx.get(
        f"{BASE_URL}/health/ready",
        timeout=REQUEST_TIMEOUT,
    )

    assert readiness.status_code == 200

    readiness_data = readiness.json()

    assert readiness_data["status"] == "degraded"
    assert readiness_data["checks"]["s3"] == "ok"
    assert readiness_data["checks"]["redis"] == "degraded"

    tile_response = httpx.get(
        f"{BASE_URL}{TILE_PATH}",
        timeout=REQUEST_TIMEOUT,
    )

    assert tile_response.status_code == 200
    assert tile_response.headers["content-type"] == "image/png"
    assert tile_response.headers["x-cache"] == "BYPASS"
    assert len(tile_response.content) > 0