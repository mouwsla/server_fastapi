import os
from contextlib import asynccontextmanager

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Response
from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType

import redis
from redis.exceptions import RedisError

# Настройки подключений передаются контейнеру через переменные окружения.
S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]
S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_KEY"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_OBJECT_KEY = os.environ["S3_OBJECT_KEY"]
REDIS_URL = os.environ["REDIS_URL"]

# Браузер хранит тайл 7 дней, Redis — 24 часа.
CACHE_MAX_AGE = 60 * 60 * 24 * 7
REDIS_TILE_TTL_SECONDS = int(
    os.getenv("REDIS_TILE_TTL_SECONDS", "86400")
)


# MIME-тип ответа зависит от формата тайлов в PMTiles.
MIME_BY_TILE_TYPE = {
    TileType.MVT: "application/vnd.mapbox-vector-tile",
    TileType.PNG: "image/png",
    TileType.JPEG: "image/jpeg",
    TileType.WEBP: "image/webp",
    TileType.AVIF: "image/avif",
}


def make_s3_source(s3_client):
    """
    Создаёт функцию чтения заданного диапазона байтов из объекта S3.
    PMTiles Reader вызывает её для заголовка, директорий и самого тайла.
    """

    def get_bytes(offset: int, length: int) -> bytes:
        if length <= 0:
            return b""

        last_byte = offset + length - 1

        # Range-запрос читает только нужный участок большого PMTiles-файла.
        result = s3_client.get_object(
            Bucket=S3_BUCKET,
            Key=S3_OBJECT_KEY,
            Range=f"bytes={offset}-{last_byte}",
        )

        body = result["Body"]

        try:
            data = body.read()
        finally:
            body.close()

        if len(data) != length:
            raise RuntimeError(
                f"Expected {length} bytes from S3, received {len(data)}"
            )

        return data

    return get_bytes


# Создаём общие подключения при запуске и закрываем их при остановке.
@asynccontextmanager
async def lifespan(app: FastAPI):
    s3_client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=5,
            tcp_keepalive=True,
            max_pool_connections=50,
            retries={
                "mode": "standard",
                "total_max_attempts": 3,
            },
        ),
    )

    # Redis хранит готовые байты тайлов без декодирования текста.
    redis_client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=False,
        socket_connect_timeout=1,
        socket_timeout=1,
    )

    try:
        # Проверяем доступ к PMTiles и запоминаем версию объекта.
        object_info = s3_client.head_object(
            Bucket=S3_BUCKET,
            Key=S3_OBJECT_KEY,
        )
        object_etag = object_info["ETag"].strip('"')

        # Reader использует S3 Range-запросы для чтения индекса и тайлов.
        reader = Reader(make_s3_source(s3_client))
        header = reader.header()

        # app.state хранит общие объекты между HTTP-запросами.
        app.state.s3 = s3_client
        app.state.pmtiles = reader
        app.state.pmtiles_header = header
        app.state.redis = redis_client
        app.state.object_etag = object_etag

        yield
    finally:
        redis_client.close()
        s3_client.close()


app = FastAPI(lifespan=lifespan)


# Простая проверка работы HTTP-сервера.
@app.get("/health")
def health():
    return {"status": "ok"}


# Liveness сообщает, что процесс Python жив.
@app.get("/health/live")
def health_live():
    return {"status": "alive"}


# Readiness проверяет обязательный S3 и необязательный Redis.
@app.get("/health/ready")
def health_ready():
    checks = {
        "s3": "ok",
        "redis": "ok",
    }

    try:
        app.state.s3.head_object(
            Bucket=S3_BUCKET,
            Key=S3_OBJECT_KEY,
        )
    except (BotoCoreError, ClientError) as error:
        checks["s3"] = "error"

        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "checks": checks,
            },
        ) from error

    # Без Redis сервис продолжает работать, но без кэширования.
    try:
        app.state.redis.ping()
    except RedisError:
        checks["redis"] = "degraded"

    status = (
        "ready"
        if checks["redis"] == "ok"
        else "degraded"
    )

    return {
        "status": status,
        "checks": checks,
    }


# Возвращает состояние PMTiles-объекта в MinIO.
@app.get("/storage/health")
def storage_health():
    try:
        result = app.state.s3.head_object(
            Bucket=S3_BUCKET,
            Key=S3_OBJECT_KEY,
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "unknown")
        raise HTTPException(
            status_code=502,
            detail=f"Cannot access S3 object: {code}",
        ) from error

    header = app.state.pmtiles_header

    return {
        "status": "ok",
        "bucket": S3_BUCKET,
        "object": S3_OBJECT_KEY,
        "size_bytes": result["ContentLength"],
        "tile_type": header["tile_type"].name.lower(),
        "min_zoom": header["min_zoom"],
        "max_zoom": header["max_zoom"],
    }


# Отдельная проверка соединения с Redis.
@app.get("/cache/health")
def cache_health():
    try:
        app.state.redis.ping()
    except RedisError as error:
        raise HTTPException(
            status_code=503,
            detail="Redis is unavailable",
        ) from error

    return {"status": "ok"}


# Возвращает один тайл по координатам XYZ.
@app.get("/tiles/{z}/{x}/{y}")
def get_tile(z: int, x: int, y: int):
    header = app.state.pmtiles_header

    # Архив содержит только уровни от min_zoom до max_zoom.
    if not (header["min_zoom"] <= z <= header["max_zoom"]):
        raise HTTPException(
            status_code=404,
            detail="Zoom out of range",
        )

    max_coordinate = (1 << z) - 1

    # На уровне z координаты x и y лежат в диапазоне 0..2^z-1.
    if not (
        0 <= x <= max_coordinate
        and 0 <= y <= max_coordinate
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid tile coordinates",
        )

    cache_key = make_tile_cache_key(app, z, x, y)
    cache_status = "MISS"

    # Сначала пытаемся вернуть готовый тайл из Redis.
    try:
        cached_tile = app.state.redis.get(cache_key)
    except RedisError:
        cached_tile = None
        cache_status = "BYPASS"

    if cached_tile is not None:
        return make_tile_response(
            tile_data=cached_tile,
            header=header,
            cache_status="HIT",
        )

    # При промахе PMTiles Reader находит и читает нужный диапазон из S3.
    try:
        tile_data = app.state.pmtiles.get(z, x, y)
    except (BotoCoreError, ClientError) as error:
        raise HTTPException(
            status_code=502,
            detail="Cannot read tile from S3",
        ) from error

    if tile_data is None:
        raise HTTPException(
            status_code=404,
            detail="Tile not found",
        )

    # Если Redis доступен, сохраняем готовый тайл с ограниченным TTL.
    if cache_status != "BYPASS":
        try:
            app.state.redis.set(
                cache_key,
                tile_data,
                ex=REDIS_TILE_TTL_SECONDS,
            )
        except RedisError:
            cache_status = "BYPASS"

    return make_tile_response(
        tile_data=tile_data,
        header=header,
        cache_status=cache_status,
    )


# ETag в ключе отделяет кэш разных версий PMTiles-файла.
def make_tile_cache_key(
    app: FastAPI,
    z: int,
    x: int,
    y: int,
) -> str:
    return (
        f"tile:{S3_BUCKET}:{S3_OBJECT_KEY}:"
        f"{app.state.object_etag}:{z}:{x}:{y}"
    )


# Формирует HTTP-ответ с типом тайла и заголовками кэширования.
def make_tile_response(
    tile_data: bytes,
    header: dict,
    cache_status: str,
) -> Response:
    tile_type = header["tile_type"]

    media_type = MIME_BY_TILE_TYPE.get(
        tile_type,
        "application/octet-stream",
    )

    response_headers = {
        "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
        "X-Cache": cache_status,
    }

    # Сохраняем информацию о сжатии тайла из заголовка PMTiles.
    if header["tile_compression"] == Compression.GZIP:
        response_headers["Content-Encoding"] = "gzip"

    return Response(
        content=tile_data,
        media_type=media_type,
        headers=response_headers,
    )
