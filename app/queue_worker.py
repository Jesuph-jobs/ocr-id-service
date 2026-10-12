"""BullMQ-compatible worker that consumes OCR jobs from Redis."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from bullmq import Job, Worker

from app.config import Settings, get_settings
from app.ocr_engine import AlgerianIDOCR
from app.utils import decode_base64_image

logger = logging.getLogger(__name__)


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
    Path(runtime.ocr_input_dir).mkdir(parents=True, exist_ok=True)
    engine = AlgerianIDOCR(runtime)

    async def processor(job: Job, token: str) -> dict[str, Any]:
        del token
        logger.info("Processing BullMQ job id=%s name=%s", job.id, job.name)
        return await process_job(job, engine, runtime)

    worker = Worker(
        runtime.bullmq_queue,
        processor,
        {
            "connection": runtime.redis_url,
            "prefix": runtime.bullmq_prefix,
            "concurrency": runtime.bullmq_concurrency,
            "lockDuration": runtime.bullmq_lock_duration_ms,
            "stalledInterval": runtime.bullmq_stalled_interval_ms,
            "name": runtime.bullmq_worker_name,
        },
    )
    logger.info(
        "Listening on BullMQ queue=%s with configured Redis connection", runtime.bullmq_queue
    )
    try:
        await worker.run()
    finally:
        await worker.close()
