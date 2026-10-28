# Python PMTiles tile server

Сервис отдаёт растровые тайлы по HTTP-маршруту:

```text
GET /tiles/{z}/{x}/{y}
```

Стек:

- FastAPI + Uvicorn — HTTP API;
- MinIO — хранение PMTiles через S3 API;
- Redis — кэш готовых тайлов;
- Caddy — единственная HTTP-точка входа;
- Docker Compose — запуск сервисов.

## Требования

Проверь, что установлены Docker и Docker Compose:

```bash
docker --version
docker compose version
```

Python, Redis и MinIO устанавливать на компьютер отдельно не нужно.

## 1. Подготовь PMTiles

Файл карты не хранится в Git из-за размера. Получи его отдельно и положи сюда:

```text
map/russia_raster_z5_z12.pmtiles
```

Проверь:

```bash
ls -lh map/russia_raster_z5_z12.pmtiles
```

## 2. Создай локальный `.env`

```bash
cp .env.example .env
```

При необходимости замени значения в `.env`. Файл содержит локальные пароли и не должен попадать в Git.

## 3. Первый запуск MinIO и Redis

На новом компьютере сначала запусти только зависимости:

```bash
docker compose up -d --build --wait minio redis
```

Проверь их состояние:

```bash
docker compose ps
```

MinIO и Redis должны иметь статус `healthy`.

## 4. Инициализируй MinIO

Этот шаг выполняется один раз для нового Docker volume. Он создаёт бакет `map`, read-only-пользователя приложения, назначает политику и загружает PMTiles.

Собери служебный образ `mc`:

```bash
docker compose --profile tools build mc
```

Запусти инициализацию:

```bash
docker compose --profile tools run --rm \
  --entrypoint /bin/sh \
  mc -c '
set -eu

mc alias set local \
  "$MINIO_ENDPOINT" \
  "$MINIO_ROOT_USER" \
  "$MINIO_ROOT_PASSWORD"

mc mb --ignore-existing local/map

mc admin policy create \
  local \
  tiles-readonly \
  /policies/tiles-readonly.json

mc admin user add \
  local \
  "$S3_ACCESS_KEY" \
  "$S3_SECRET_KEY"

mc admin policy attach \
  local \
  tiles-readonly \
  --user "$S3_ACCESS_KEY"

mc cp \
  /map/russia_raster_z5_z12.pmtiles \
  local/map/russia_raster_z5_z12.pmtiles
'
```

MinIO Console доступна только на этом компьютере:

```text
http://localhost:9001
```

Для входа используй `MINIO_ROOT_USER` и `MINIO_ROOT_PASSWORD` из `.env`.

## 5. Запусти приложение и Caddy

```bash
docker compose up -d --build --wait app caddy
```

Проверь все основные сервисы:

```bash
docker compose ps
```

Ожидается статус `healthy` у `minio`, `redis`, `app` и `caddy`.

## 6. Проверь API

Readiness:

```bash
curl -i http://localhost:8080/health/ready
```

Ожидаемый JSON:

```json
{"status":"ready","checks":{"s3":"ok","redis":"ok"}}
```

Проверка тайла без сохранения PNG:

```bash
curl -sS -D - -o /dev/null \
  http://localhost:8080/tiles/7/77/40
```

Первый запрос после очистки кэша вернёт:

```text
X-Cache: MISS
```

Повторный запрос вернёт:

```text
X-Cache: HIT
```

Интерактивная документация FastAPI:

```text
http://localhost:8080/docs
```

Порт Uvicorn `8000`, Redis `6379` и S3 API MinIO `9000` не предназначены для внешнего доступа к приложению. Клиент обращается к Caddy на `localhost:8080`.

## Последующие запуски

После первой инициализации достаточно:

```bash
docker compose up -d --build --wait
```

Просмотр логов:

```bash
docker compose logs -f --tail=100
```

Остановка:

```bash
docker compose down
```

`docker compose down` сохраняет MinIO volume. Команда ниже удалит MinIO-данные, бакет и загруженную карту:

```bash
docker compose down -v
```

Используй её только если действительно нужен полный сброс.

## Тесты

Обычные интеграционные тесты:

```bash
docker compose --profile test build tests
docker compose --profile test run --rm tests
```

Проверка работы без Redis:

```bash
./tests/run_redis_failure_test.sh
```

Скрипт временно приостанавливает Redis и автоматически восстанавливает его после теста.

## Что не должно попадать в Git

`.gitignore` исключает:

- `.env` и другие локальные env-файлы, кроме `.env.example`;
- `map/*.pmtiles`;
- `.venv`, `__pycache__`, `*.pyc` и кэши тестов;
- `.DS_Store`, настройки IDE и логи;
- архивы Docker-образов (`*.tar`, `*.tar.gz`, `*.zst`).

Перед первым коммитом проверь:

```bash
git status --short
git check-ignore -v \
  .env \
  map/russia_raster_z5_z12.pmtiles \
  app/__pycache__/main.cpython-315.pyc \
  .DS_Store
```

В списке изменений должны присутствовать `.env.example`, исходный код и конфигурация, но не `.env`, PMTiles или Python-кэши.
