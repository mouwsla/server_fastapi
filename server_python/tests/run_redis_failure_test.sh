#!/bin/sh

set -eu

restore_redis() {
    echo "Restoring Redis..."

    docker compose unpause redis >/dev/null 2>&1 || true

    attempt=0

    while [ "$attempt" -lt 15 ]; do
        if docker compose exec -T redis redis-cli ping 2>/dev/null \
            | grep -q PONG
        then
            echo "Redis restored"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    echo "Redis did not recover" >&2
    return 1
}

trap restore_redis EXIT

docker compose --profile test build tests

echo "Pausing Redis..."
docker compose pause redis

docker compose --profile test run \
    --rm \
    --no-deps \
    -e EXPECT_REDIS_DOWN=1 \
    tests \
    pytest -v -p no:cacheprovider tests/test_redis_failure.py