import { Job, Queue, type JobsOptions } from "bullmq";
import { Redis } from "ioredis";
import { randomUUID } from "node:crypto";
import type { OcrJobStatus, OcrResult, OcrScanInput, OcrServiceResponse } from "@ocr-id-service/contracts";

export interface RedisConnection {
  host: string;
  port: number;
  db?: number;
  username?: string;
  password?: string;
  tls?: Record<string, never>;
}

export interface OcrIdClientOptions {
  redisUrl: string;
  appId: string;
  prefix?: string;
}

export interface SubmitScanOptions extends OcrScanInput {
  jobId?: string;
  attempts?: number;
}

/** Parse redis:// or rediss:// URLs into the connection shape shared by BullMQ. */
export function redisConnection(redisUrl: string): RedisConnection {
  const url = new URL(redisUrl);
  if (url.protocol !== "redis:" && url.protocol !== "rediss:") {
    throw new Error("Redis URL must start with redis:// or rediss://");
  }
  const db = Number.parseInt(url.pathname.slice(1) || "0", 10);
  if (!Number.isInteger(db) || db < 0) throw new Error("Redis database must be a non-negative integer");
  return {
    host: url.hostname,
    port: Number.parseInt(url.port || "6379", 10),
    db,
    username: url.username || undefined,
    password: url.password || undefined,
    ...(url.protocol === "rediss:" ? { tls: {} } : {}),
  };
}

function toWorkerInput(input: OcrScanInput, requestId: string): Record<string, string> {
  if (!input.imagePath && !input.imageBase64) throw new Error("imagePath or imageBase64 is required");
  if (input.imagePath && input.imageBase64) throw new Error("Provide only one image source");
  return {
    ...(input.imagePath ? { image_path: input.imagePath } : {}),
    ...(input.imageBase64 ? { image_base64: input.imageBase64 } : {}),
    request_id: requestId,
    ...(input.userId ? { user_id: input.userId } : {}),
    ...(input.documentId ? { document_id: input.documentId } : {}),
    ...(input.documentSide ? { document_side: input.documentSide } : {}),
    ...(input.schemaVersion ? { schema_version: input.schemaVersion } : {}),
  };
}

/** BullMQ client for submitting scans and waiting for Python worker results. */
export class OcrIdClient {
  private readonly queue: Queue;
  private readonly responses: Redis;
  private readonly queueName: string;
  private readonly appId: string;

  public constructor(options: OcrIdClientOptions) {
    if (!/^[a-z0-9][a-z0-9_-]{0,62}$/.test(options.appId)) throw new Error("appId must use lowercase letters, numbers, '_' or '-' (max 63 characters)");
    this.appId = options.appId;
    this.queueName = `ocr-${options.appId}-requests`;
    const connection = redisConnection(options.redisUrl);
    this.queue = new Queue(this.queueName, { connection, prefix: options.prefix });
    this.responses = new Redis(options.redisUrl, { maxRetriesPerRequest: null });
  }

  public async submitScan(input: SubmitScanOptions): Promise<Job> {
    const { jobId, attempts = 2, ...scan } = input;
    const requestId = jobId ?? scan.requestId ?? randomUUID();
    if (!/^[a-zA-Z0-9_-]{1,128}$/.test(requestId)) throw new Error("requestId must be 1–128 letters, numbers, '_' or '-'");
    const jobOptions: JobsOptions = {
      jobId: requestId,
      attempts,
      backoff: { type: "exponential", delay: 1000 },
      removeOnComplete: true,
      removeOnFail: true,
    };
    return this.queue.add("scan-id", toWorkerInput(scan, requestId), jobOptions);
  }

  public async waitForResult(jobId: string, timeoutMs = 300000): Promise<OcrResult> {
    const response = await this.waitForResponse(jobId, timeoutMs);
    if (response.state === "failed" || !response.result) throw new Error(response.error ?? "OCR request failed");
    return response.result;
  }

  public async waitForResponse(requestId: string, timeoutMs = 300000): Promise<OcrServiceResponse> {
    const responseKey = `ocr:responses:${this.appId}:${requestId}`;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const remainingSeconds = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
      const response = await this.responses.blpop(responseKey, remainingSeconds);
      if (!response) break;
      const parsed = JSON.parse(response[1]) as OcrServiceResponse;
      if (parsed.app_id !== this.appId || parsed.request_id !== requestId) {
        throw new Error("OCR response identity does not match the requested app and request");
      }
      return parsed;
    }
    throw new Error(`Timed out waiting for OCR response ${requestId}`);
  }

  public async getStatus(jobId: string): Promise<OcrJobStatus> {
    const rawResponse = await this.responses.lindex(`ocr:responses:${this.appId}:${jobId}`, 0);
    if (rawResponse) {
      const response = JSON.parse(rawResponse) as OcrServiceResponse;
      if (response.app_id !== this.appId || response.request_id !== jobId) {
        throw new Error("OCR response identity does not match the requested app and request");
      }
      if (response.state === "failed") {
        return { jobId, state: "failed", error: response.error ?? "OCR request failed" };
      }
      return { jobId, state: "completed", result: response.result ?? undefined };
    }
    const job = await this.queue.getJob(jobId);
    if (!job) return { jobId, state: "failed", error: "Job not found" };
    const state = await job.getState();
    if (state === "failed") return { jobId, state, error: job.failedReason };
    return { jobId, state: state === "active" ? "processing" : "queued" };
  }

  public async close(): Promise<void> {
    await Promise.all([this.queue.close(), this.responses.quit()]);
  }
}
