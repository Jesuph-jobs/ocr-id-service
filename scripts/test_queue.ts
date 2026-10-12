import { OcrIdClient } from "@ocr-id-service/node";
import { readFile, stat } from "node:fs/promises";
import { randomUUID } from "node:crypto";

const args = process.argv.slice(2);
if (args[0] === "--") args.shift();
const imagePath = args[0];
const redisUrl = process.env.OCR_REDIS_URL;
const appId = process.env.OCR_APP_ID ?? "moustachir";
const timeoutMs = Number.parseInt(process.env.OCR_TIMEOUT_MS ?? "300000", 10);

if (!imagePath || !redisUrl || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
  console.error("Usage: OCR_REDIS_URL=redis://ocr_<app>:<password>@host:6379/0 pnpm test:queue -- image.jpg");
  process.exit(1);
}

const imageStats = await stat(imagePath);
if (!imageStats.isFile()) throw new Error(`Not a file: ${imagePath}`);
const imageBase64 = (await readFile(imagePath)).toString("base64");
const client = new OcrIdClient({ appId, redisUrl });

try {
  const job = await client.submitScan({ imageBase64, requestId: randomUUID() });
  console.log(`Queued OCR request ${job.id} for app ${appId}; waiting for its private response...`);
  const result = await client.waitForResult(job.id!, timeoutMs);
  console.log(JSON.stringify({
    request_id: job.id,
    success: result.success,
    document_type: result.fields.document_type,
    confidence: result.confidence,
    fields: result.fields,
    processing_time_ms: result.processing_time_ms,
    error: result.error,
  }, null, 2));
  if (!result.success) process.exitCode = 2;
} finally {
  await client.close();
}
