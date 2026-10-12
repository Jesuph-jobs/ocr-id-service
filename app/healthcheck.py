"""Container health check for Redis reachability."""

import asyncio
import logging

from redis.asyncio import Redis

from app.config import get_settings


async def check_redis() -> None:
    """Fail the container health check when its configured Redis is unreachable."""

    client = Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    try:
        await client.ping()
    finally:
        await client.aclose()


def main() -> None:
    """Run the asynchronous Redis health probe and exit with a health status."""

    try:
        asyncio.run(check_redis())
    except Exception as exc:
        logging.getLogger(__name__).error("Redis health check failed: %s", type(exc).__name__)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
