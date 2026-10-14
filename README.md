# OCR ID Service

Self-hosted, CPU-only OCR worker for extracting structured identity data from document images. The project is open source and currently optimized for Algerian national identity cards (CNI), including French/Latin and Arabic text. It uses PaddleOCR, OpenCV, Pydantic, and BullMQ-compatible Redis queues, with no cloud API, paid service, or GPU requirement.

## Current scope

Algerian CNI is the supported and tested document type. The classifier has experimental hooks for passports and driving licenses, but those formats are not yet guaranteed and should be treated as `unknown` or reviewed manually when confidence is low. The extractor is intentionally modular: additional document formats can be added with new language patterns, preprocessing profiles, and field extractors.

This project performs image OCR and heuristic document classification. It does not verify government records, read NFC/chips, prove biometric authenticity, perform face matching, or replace a KYC/identity provider. OCR results must be reviewed and validated against an authoritative record before making an identity decision.

OCR results include a document-level `confidence` review signal computed as `0.60 × mean text-detection confidence + 0.25 × document-type classifier confidence + 0.15 × extracted-field completeness`. `field_confidence` maps each extracted identity field to the highest PaddleOCR confidence among detections that support its value; absent fields and values without a matching text detection score `0`. These values are heuristic signals, not calibrated probabilities that a document or identity is correct, and must not be used alone to approve KYC.

## Architecture

A TypeScript or NestJS backend submits each request to an app-scoped BullMQ queue such as `ocr-billing-requests`. The Python worker processes the image and writes a response to that app's per-request Redis response list, `ocr:responses:<app-id>:<request-id>`. The response is retained until consumed or its 24-hour TTL expires. The app receives a tenant-scoped Redis ACL user; the app ID identifies the namespace while the Redis credential enforces access. Jobs accept `image_base64` for independent containers; `image_path` is available only when a trusted producer shares the worker's input volume.

The Python worker uses the official BullMQ Python library and the same Redis/Lua queue contract as the Node.js BullMQ package. It connects only to Redis and the local model/cache/input volumes; it has no database connection and does not persist OCR output outside the response queue. No HTTP server is required; this makes it suitable as a standalone worker behind existing app backends.

## Requirements

- Python 3.11–3.13
- [uv](https://docs.astral.sh/uv/)
- Redis reachable through `REDIS_URL`
- CPU-only runtime; PaddlePaddle is installed with its CPU package

## Local development

```bash
cp .env.example .env
uv sync
uv run pytest
uv run ruff check .
uv run mypy app scripts
```

Start the worker in another terminal:

```bash
REDIS_URL=redis://localhost:6379/0 uv run python -m app.main
```

The first scan downloads PaddleOCR model files into the worker user's local cache. `OCR_LANGUAGES=fr,ar` enables French/Latin and Arabic recognition. Set `OCR_LANGUAGES=fr` or `OCR_LANGUAGES=ar` when a narrower workload is more important than bilingual coverage. `OCR_MODEL_PROFILE=mobile` selects smaller CPU models for deployment; `server` selects PaddleOCR's default server detector where additional memory and accuracy are preferred.

## CLI scan

The CLI performs a local scan without Redis and prints document classification, extracted fields, raw OCR text, and timing:

```bash
uv run python scripts/scan_image.py data/input/card.jpg --debug
```

With `OCR_DEBUG=true` or `--debug`, preprocessing stages, OCR detections, and the extracted result are written below `data/output/debug/<image-name>/`. Real identity images and generated output are ignored by Git.

## Node backend client

```ts
import { OcrIdClient } from '@ocr-id-service/node';

const ocr = new OcrIdClient({ appId: 'billing', redisUrl: process.env.OCR_REDIS_URL! });
const job = await ocr.submitScan({ imageBase64, requestId: crypto.randomUUID() });
const result = await ocr.waitForResult(job.id!);
await ocr.close();
```

See [docs/integration.md](docs/integration.md) for app onboarding, ACL isolation, response envelope, and backend integration guidance. Never give an app the Redis default/admin credential or expose Redis credentials to a browser.

## TypeScript queue test client

Start Redis and the Python worker with `OCR_APP_IDS` and an app password configured, install the optional TypeScript test dependencies, then submit an image:

```bash
REDIS_URL=redis://localhost:6379/0 uv run python -m app.main
pnpm install
pnpm test:queue -- data/input/card.jpg
```

The client waits for the matching app-scoped response and prints the document verdict and extracted identity fields. The result includes `national_id`, `passport`, `driving_license`, or `unknown`; a biometric chip cannot be inferred from an image alone.

## Docker

The image runs a non-root worker with no public HTTP port. It requires an explicit `REDIS_URL`, checks Redis reachability through its Docker health check, and does not include local `.env` files, test documents, development dependencies, or OCR caches in the image. The default `mobile` model profile limits CPU memory use; model downloads require outbound HTTPS on first startup. Configure it as a private Dokploy service and provide its Redis URL through Dokploy's encrypted environment settings.

This compose file runs only the OCR worker and expects Redis to be provided separately, so it does not create a second Redis instance:

```bash
cp .env.example .env
# Edit REDIS_URL so it is reachable from the container.
docker compose up --build
```

When Redis runs on the host, use a host-reachable address such as `redis://host.docker.internal:6379/0` where supported, or place the worker and Redis on the same user-managed Docker network. The `data/` directory is mounted for shared input images and debug output, and a named volume preserves downloaded PaddleOCR models across restarts.

For the shared Moustachir deployment, create a Dokploy Application from this repository and set its build type to **Dockerfile** (`Dockerfile`, context `.`). Deploy it in the `Main services` production environment on Dokploy's private application network so it can use the already-hosted Main Redis service; do not create a Compose stack or another Redis service. Provide the admin Redis URL and app-specific credentials through Dokploy's encrypted application environment. Add each onboarded app ID to `OCR_APP_IDS` and add its matching `OCR_APP_<ID>_PASSWORD` variable before redeploying; the worker provisions the scoped Redis ACL user and consumes that app's request queue. Redis ACL persistence must be enabled on the existing Redis service for tenant credentials to survive Redis restarts.

## Extraction and validation

The extractor recognizes French labels, Arabic text lines, spaced digits, common OCR label substitutions, and fuzzy label matches. It extracts `first_name`, `last_name`, `arabic_name`, ISO `birth_date`, `birth_place`, `gender`, 18-digit `nin`, and `document_number` when present. `validate_identity()` is a small explainable placeholder: NIN and birth date use exact matching, while names use fuzzy similarity.

## Privacy and security

Do not commit real identity images, OCR output, Redis credentials, or database records. Keep Redis private, restrict access to mounted data, and treat OCR output as sensitive personal data. This repository intentionally contains no sample identity documents.

## Contributing

Issues and pull requests are welcome. New document support should include representative synthetic or legally redistributable fixtures, extractor tests, and documentation of known limitations. Never submit real personal identity documents.

## License

Released under the MIT License. See [LICENSE](LICENSE).
