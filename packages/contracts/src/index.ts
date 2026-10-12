/** Document categories currently recognized by the Python worker. */
export type IdentityDocumentType = "national_id" | "passport" | "driving_license" | "unknown";

/** An app request accepted by the tenant-scoped OCR queue. Use one image source per job. */
export interface OcrScanInput {
  imagePath?: string;
  imageBase64?: string;
  requestId?: string;
  userId?: string;
  documentId?: string;
  documentSide?: "front" | "back" | "single";
  schemaVersion?: string;
}

/** Structured identity fields returned by the OCR worker. */
export interface OcrIdentityFields {
  document_type: IdentityDocumentType;
  document_type_confidence: number;
  first_name: string | null;
  last_name: string | null;
  arabic_name: string | null;
  birth_date: string | null;
  birth_place: string | null;
  gender: string | null;
  nin: string | null;
  document_number: string | null;
}

/** Extracted OCR result. */
export interface OcrResult {
  success: boolean;
  confidence: number;
  fields: OcrIdentityFields;
  raw_text: string[];
  processing_time_ms: number;
  error: string | null;
  processed_by?: string;
  schema_version?: string;
}

/** Response placed on the app's private response key for this request. */
export interface OcrServiceResponse {
  app_id: string;
  request_id: string;
  state: "completed" | "failed";
  result: OcrResult | null;
  error: string | null;
}

/** Normalized state returned by an application backend. */
export type OcrJobState = "queued" | "processing" | "completed" | "failed" | "manual_review";

export interface OcrJobStatus {
  jobId: string;
  state: OcrJobState;
  result?: OcrResult;
  error?: string;
}
