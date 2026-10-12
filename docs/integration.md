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
