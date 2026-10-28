import os

import httpx
import redis


BASE_URL = os.getenv("BASE_URL", "http://app:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

TILE_PATH = "/tiles/7/77/40"
REQUEST_TIMEOUT = 10.0


def test_readiness():
    response = httpx.get(
        f"{BASE_URL}/health/ready",
        timeout=REQUEST_TIMEOUT,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["checks"]["s3"] == "ok"
    assert data["checks"]["redis"] == "ok"


def test_invalid_tile_coordinate():
    response = httpx.get(
        f"{BASE_URL}/tiles/5/32/0",
        timeout=REQUEST_TIMEOUT,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid tile coordinates"


def test_zoom_out_of_range():
    response = httpx.get(
        f"{BASE_URL}/tiles/4/0/0",
        timeout=REQUEST_TIMEOUT,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Zoom out of range"


def test_tile_cache_miss_then_hit():
    redis_client = redis.Redis.from_url(REDIS_URL)

    matching_keys = list(
        redis_client.scan_iter(
            match="tile:*:7:77:40",
        )
    )

    if matching_keys:
        redis_client.delete(*matching_keys)

    first_response = httpx.get(
        f"{BASE_URL}{TILE_PATH}",
        timeout=REQUEST_TIMEOUT,
    )

    assert first_response.status_code == 200
    assert first_response.headers["content-type"] == "image/png"
    assert first_response.headers["x-cache"] == "MISS"
    assert len(first_response.content) > 0

    second_response = httpx.get(
        f"{BASE_URL}{TILE_PATH}",
        timeout=REQUEST_TIMEOUT,
    )

    assert second_response.status_code == 200
    assert second_response.headers["x-cache"] == "HIT"
    assert second_response.content == first_response.content