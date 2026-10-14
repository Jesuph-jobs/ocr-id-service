# Application integration

The OCR service has no database connection. Each onboarded application receives a dedicated Redis ACL user and a tenant namespace. Application backends connect to Redis through private server networking; browsers must call their own authenticated backend and must never receive Redis credentials.

## Queue contract

For app ID `billing`, the service consumes BullMQ requests from `ocr-billing-requests` and writes each response to `ocr:responses:billing:<request-id>`. The request ID is the BullMQ job ID. Responses contain `app_id`, `request_id`, `state`, `result`, and `error`; a response list expires after `OCR_RESPONSE_TTL_SECONDS` (24 hours by default). The app backend blocks on the exact response key for its own request, so unrelated tenants and requests cannot be consumed accidentally.

OCR output is not written to a database. A response remains only in its app/request-specific Redis response list until the app consumes it or its TTL expires. The Node client removes BullMQ request jobs when processing completes or fails, so the base64 image payload does not remain in completed or failed queue records. While a request is queued or processing, its input is held in Redis as a BullMQ job.

## Onboarding an app

1. Choose a lowercase app ID containing letters, numbers, `_` or `-` (maximum 63 characters).
2. Add the ID to `OCR_APP_IDS` and add a strong secret variable named `OCR_APP_<ID>_PASSWORD` to the service environment, replacing hyphens with underscores and uppercasing the ID.
3. Add the app ID to `OCR_APP_IDS` and its password variable as `OCR_APP_<ID>_PASSWORD` in the Dokploy Application environment, then redeploy. The worker creates Redis user `ocr_<ID>` scoped to that app's BullMQ keys and response keys. Confirm the existing Redis service persists ACL changes; the worker's `ACL SAVE` requires a configured ACL file.
4. Give the app backend the matching Redis URL with username `ocr_<ID>` and the app secret. Connect over a private network only. Never give an app the Redis default/admin credential.
5. Construct `OcrIdClient` with `appId` and the tenant Redis URL, submit an image, then wait for the response using the returned job ID.

The app ID is a namespace, not an authentication secret. Redis ACL credentials enforce isolation. Do not reuse the admin password across applications.

## Current service boundary

- The worker accepts `image_base64` or `image_path`. `image_path` requires a trusted volume shared with the worker; independent applications should use base64 for a controlled pilot. PDF rasterization and direct S3/MinIO object-key fetching are not implemented.
- Base64 image bytes remain in the BullMQ job while queued or processing. The client removes completed/failed jobs, and the worker removes its temporary local input after processing; Redis persistence and backups still follow the existing Redis service configuration.
- The response includes extracted identity fields and raw OCR text. Treat the entire response as sensitive; keep it out of logs, analytics, and browser payloads unless the host application's access policy explicitly allows the needed fields.
- The worker is an extractor, not a KYC decision engine. It does not verify government records, authenticity, biometrics, or NFC/chip data. Passport and driving-license classification are experimental; PDFs are unsupported; OCR output needs human review.
- The current Node client is a pnpm workspace package and depends on `@ocr-id-service/contracts` with `workspace:*`. A separate application repository cannot install it directly as-is; publish a versioned package or vendor/workspace-pin the client and contracts to an exact commit before production integration.

## Local development

Run a local Redis instance separately from the OCR worker:

```sh
docker run --rm --name ocr-dev-redis -p 6379:6379 redis:7-alpine
```

From the OCR repository, configure a local tenant and start the worker in another terminal:

```sh
export OCR_APP_IDS=demo
export OCR_APP_DEMO_PASSWORD="$(openssl rand -hex 32)"
export REDIS_URL=redis://127.0.0.1:6379/0
uv run python -m app.main
```

Run the sample queue client with that tenant's restricted Redis user:

```sh
export OCR_REDIS_URL="redis://ocr_demo:${OCR_APP_DEMO_PASSWORD}@127.0.0.1:6379/0"
OCR_APP_ID=demo pnpm test:queue -- /path/to/synthetic-id-image.jpg
```

The `pnpm test:queue` script currently prints extracted fields as JSON, so use only synthetic fixtures in CI, shared terminals, or retained shell logs until the script is redacted. The worker downloads PaddleOCR model files on first startup and therefore needs outbound HTTPS. Dokploy deployment uses the repository Dockerfile and the already-hosted Redis service; do not use the example Compose file to create another Redis instance.

## Node backend

```ts
import { OcrIdClient } from "@ocr-id-service/node";

const ocr = new OcrIdClient({
  appId: "billing",
  redisUrl: process.env.OCR_REDIS_URL!, // username ocr_billing + app-specific password
});
const job = await ocr.submitScan({ imageBase64, requestId: crypto.randomUUID() });
const result = await ocr.waitForResult(job.id!);
```

The backend should authenticate its own caller before submission and associate the returned `jobId` with its own user/document record. Do not trust caller-provided tenant IDs or expose a generic endpoint that lets users submit another app's ID.

## React package

Install `@ocr-id-service/react` and implement `IdentityOcrTransport` through the host application's authenticated API or RPC methods. Redis is not used in the browser.

```ts
const transport: IdentityOcrTransport = {
  submit: (input) => trpc.identityOcr.submit.mutate({ file: input.file, userId: input.userId }),
  status: (jobId) => trpc.identityOcr.status.query({ jobId }),
};
```

## Integration choices

Use npm packages for released integrations and versioned upgrades. A Git submodule is reasonable during active co-development when an application needs to pin the OCR repository to an exact commit, but the application still needs a small backend adapter. Once the APIs stabilize, consuming the packages is simpler than maintaining a submodule.
