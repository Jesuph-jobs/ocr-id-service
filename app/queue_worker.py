"""BullMQ-compatible worker that consumes OCR jobs from Redis."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from bullmq import Job, Worker
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import Settings, get_settings
from app.ocr_engine import AlgerianIDOCR
from app.utils import decode_base64_image

logger = logging.getLogger(__name__)
APP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def parse_app_ids(value: str) -> list[str]:
    """Parse and validate the configured tenant IDs."""

    app_ids = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not app_ids:
        raise ValueError("OCR_APP_IDS must contain at least one app ID")
    if len(set(app_ids)) != len(app_ids):
        raise ValueError("OCR_APP_IDS contains duplicate app IDs")
    invalid = [app_id for app_id in app_ids if not APP_ID_PATTERN.fullmatch(app_id)]
    if invalid:
        raise ValueError(f"Invalid OCR app ID(s): {', '.join(invalid)}")
    return app_ids


def request_queue_name(app_id: str) -> str:
    """Return the BullMQ request queue assigned to one app."""

    return f"ocr-{app_id}-requests"


def response_queue_key(app_id: str, request_id: str) -> str:
    """Return an app-scoped, request-specific response list key."""

    if not request_id or ":" in request_id or len(request_id) > 128:
        raise ValueError("request ID must be non-empty, at most 128 characters, and contain no ':'")
    return f"ocr:responses:{app_id}:{request_id}"


async def provision_app_user(redis: Redis, app_id: str, prefix: str, password: str) -> None:
    """Create a tenant Redis user limited to its BullMQ and response keys."""

    queue_pattern = f"{prefix}:{request_queue_name(app_id)}*"
    response_pattern = f"ocr:responses:{app_id}:*"
    await redis.acl_setuser(
        f"ocr_{app_id}",
        enabled=True,
        passwords=f"+{password}",
        categories=["+@all", "-@admin", "-@dangerous"],
        commands=[
            "+info",
            "-acl",
            "-config",
            "-flushdb",
            "-flushall",
            "-shutdown",
            "-module",
        ],
        keys=[queue_pattern, response_pattern],
        channels=[f"{prefix}:{request_queue_name(app_id)}*"],
        reset=True,
    )


async def provision_app_users_periodically(
    redis: Redis, app_credentials: dict[str, str], prefix: str
) -> None:
    """Restore tenant ACL users if the managed Redis service restarts."""

    while True:
        await asyncio.sleep(30)
        for app_id, password in app_credentials.items():
            try:
                await provision_app_user(redis, app_id, prefix, password)
            except Exception:
                logger.exception("Could not refresh Redis ACL for app_id=%s", app_id)


def _safe_input_path(raw_path: str, input_dir: Path, max_image_mb: int) -> Path:
    """Resolve a path and reject traversal outside the shared input directory."""

    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else input_dir / candidate).resolve()
    root = input_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("image_path must point inside OCR_INPUT_DIR")
    if not resolved.is_file():
        raise FileNotFoundError(f"Image does not exist: {resolved}")
    if resolved.stat().st_size > max_image_mb * 1024 * 1024:
        raise ValueError("Image exceeds OCR_MAX_IMAGE_MB")
    return resolved


async def process_job(job: Job, engine: AlgerianIDOCR, settings: Settings) -> dict[str, Any]:
    """Process a BullMQ job with either a shared path or base64 image payload."""

    data = job.data if isinstance(job.data, dict) else {}
    temporary_path: Path | None = None
    try:
        if data.get("image_base64"):
            image_bytes = decode_base64_image(str(data["image_base64"]))
            if len(image_bytes) > settings.ocr_max_image_mb * 1024 * 1024:
                raise ValueError("image_base64 exceeds OCR_MAX_IMAGE_MB")
            with tempfile.NamedTemporaryFile(
                dir=settings.ocr_input_dir, suffix=".jpg", delete=False
            ) as temporary:
                temporary.write(image_bytes)
                temporary_path = Path(temporary.name)
            image_path = temporary_path
        elif data.get("image_path"):
            image_path = _safe_input_path(
                str(data["image_path"]), Path(settings.ocr_input_dir), settings.ocr_max_image_mb
            )
        else:
            raise ValueError("BullMQ job data must include image_path or image_base64")
        result = await asyncio.to_thread(engine.scan, str(image_path))
        return result.model_dump(mode="json")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def run_worker(settings: Settings | None = None) -> None:
    """Run the long-lived BullMQ worker until interrupted."""

    runtime = settings or get_settings()
    app_ids = parse_app_ids(runtime.ocr_app_ids)
    Path(runtime.ocr_input_dir).mkdir(parents=True, exist_ok=True)
    engine = AlgerianIDOCR(runtime)
    responses: Redis = Redis.from_url(runtime.redis_url, decode_responses=True)
    app_credentials: dict[str, str] = {}
    for app_id in app_ids:
        password = os.getenv(f"OCR_APP_{app_id.upper().replace('-', '_')}_PASSWORD", "")
        if len(password) < 24:
            raise ValueError(
                f"OCR_APP_{app_id.upper().replace('-', '_')}_PASSWORD must be at least 24 "
                "characters"
            )
        app_credentials[app_id] = password
        await provision_app_user(responses, app_id, runtime.bullmq_prefix, password)
    try:
        await responses.acl_save()
    except ResponseError as exc:
        error = str(exc).lower()
        if (
            "no acl file configured" not in error
            and "not configured to use an acl file" not in error
        ):
            raise
        logger.warning(
            "Redis has no ACL file configured; tenant ACLs will be reprovisioned "
            "after Redis restarts"
        )

    workers: list[Worker] = []

    def processor_for(tenant_id: str) -> Any:
        async def processor(job: Job, token: Any) -> dict[str, Any]:
            del token
            request_id = str(job.id or "")
            logger.info("Processing OCR request app_id=%s request_id=%s", tenant_id, request_id)
            try:
                result = await process_job(job, engine, runtime)
                envelope: dict[str, Any] = {
                    "app_id": tenant_id,
                    "request_id": request_id,
                    "state": "completed",
                    "result": result,
                    "error": None,
                }
            except Exception as exc:
                logger.exception(
                    "OCR request failed app_id=%s request_id=%s", tenant_id, request_id
                )
                envelope = {
                    "app_id": tenant_id,
                    "request_id": request_id,
                    "state": "failed",
                    "result": None,
                    "error": str(exc),
                }
            response_key = response_queue_key(tenant_id, request_id)
            await responses.rpush(response_key, json.dumps(envelope, separators=(",", ":")))
            await responses.expire(response_key, runtime.ocr_response_ttl_seconds)
            return {"request_id": request_id, "response_enqueued": True}

        return processor

    for app_id in app_ids:
        queue_name = request_queue_name(app_id)

        workers.append(
            Worker(
                queue_name,
                processor_for(app_id),
                {
                    "connection": runtime.redis_url,
                    "prefix": runtime.bullmq_prefix,
                    "concurrency": runtime.bullmq_concurrency,
                    "lockDuration": runtime.bullmq_lock_duration_ms,
                    "stalledInterval": runtime.bullmq_stalled_interval_ms,
                    "name": f"{runtime.bullmq_worker_name}-{app_id}",
                    "autorun": False,
                },
            )
        )
        logger.info("Listening for OCR requests app_id=%s queue=%s", app_id, queue_name)
    acl_refresh_task = asyncio.create_task(
        provision_app_users_periodically(responses, app_credentials, runtime.bullmq_prefix)
    )
    try:
        await asyncio.gather(*(worker.run() for worker in workers))
    finally:
        acl_refresh_task.cancel()
        await asyncio.gather(acl_refresh_task, return_exceptions=True)
        await asyncio.gather(*(worker.close() for worker in workers))
        await responses.aclose()
