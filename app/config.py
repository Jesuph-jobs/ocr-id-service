"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the OCR worker."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    redis_url: str = Field(validation_alias="REDIS_URL", min_length=1)
    bullmq_queue: str = Field(default="algerian-id-ocr", validation_alias="BULLMQ_QUEUE")
    bullmq_prefix: str = Field(default="bull", validation_alias="BULLMQ_PREFIX")
    bullmq_worker_name: str = Field(
        default="algerian-id-ocr-python", validation_alias="BULLMQ_WORKER_NAME"
    )
    bullmq_concurrency: int = Field(default=1, validation_alias="BULLMQ_CONCURRENCY", gt=0)
    bullmq_lock_duration_ms: int = Field(
        default=300000, validation_alias="BULLMQ_LOCK_DURATION_MS", gt=0
    )
    bullmq_stalled_interval_ms: int = Field(
        default=60000, validation_alias="BULLMQ_STALLED_INTERVAL_MS", gt=0
    )
    ocr_languages: str = Field(default="fr,ar", validation_alias="OCR_LANGUAGES")
    ocr_model_profile: Literal["mobile", "server"] = Field(
        default="mobile", validation_alias="OCR_MODEL_PROFILE"
    )
    ocr_use_gpu: bool = Field(default=False, validation_alias="OCR_USE_GPU")
    ocr_debug: bool = Field(default=False, validation_alias="OCR_DEBUG")
    ocr_debug_dir: str = Field(default="data/output/debug", validation_alias="OCR_DEBUG_DIR")
    ocr_input_dir: str = Field(default="data/input", validation_alias="OCR_INPUT_DIR")
    ocr_max_image_mb: int = Field(default=10, validation_alias="OCR_MAX_IMAGE_MB", gt=0)
    ocr_log_level: str = Field(default="INFO", validation_alias="OCR_LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""

    return Settings()
