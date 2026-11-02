import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

import {
  createWindowsDpapiUnprotect,
  createWindowsProfileReader,
  runAuthenticatedListAction,
} from "./grok_bot_list_probe.mjs";
import {
  createLedgerCall,
  findRequest as findMarkerRequest,
  isReply as isMarkerReply,
  OPERATION_ID as MARKER_OPERATION_ID,
  PROMPT as MARKER_PROMPT,
  resolveGrokBot,
  tailEntries as markerTailEntries,
  threadEntries as markerThreadEntries,
} from "./grok_bot_marker_smoke.mjs";


const MAX_INPUT_BYTES = 64 * 1024;
const MAX_PROMPT_BYTES = 32 * 1024;
const TRANSCRIPT_LIMIT = 40;
const MAX_REPLY_COUNT = 8;
const MAX_REPLY_BYTES = 8 * 1024;
const MAX_TOTAL_REPLY_BYTES = 24 * 1024;
const NONCE_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IDENTIFIER_CONTROL = /[\x00-\x1f\x7f]/;
const SAFE_REMOTE_ERROR_CODE = /^[a-z][a-z0-9_]{0,63}$/i;
const SAFE_REMOTE_REQUEST_ID = /^(?:[a-f0-9]{16,128}|[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)$/i;
const MARKER_PROMPT_SHA256 = createHash("sha256").update(MARKER_PROMPT, "utf8").digest("hex");
const DIAGNOSTIC_PHASES = new Set(["send_receipt", "request_lookup"]);
const DIAGNOSTIC_RESPONSE_TYPES = new Set([
  "object", "array", "null", "string", "number", "boolean", "undefined",
]);
const DIAGNOSTIC_REASONS = new Set([
  "RECEIPT_NOT_OBJECT",
  "RECEIPT_MESSAGE_ID_MISSING",
  "RECEIPT_MESSAGE_ID_CONFLICT",
  "RECEIPT_CONTEXT_MISMATCH",
  "RECEIPT_REQUEST_ID_MISSING",
  "RECEIPT_REQUEST_ID_CONFLICT",
  "RECEIPT_ROOT_ID_MISSING",
  "RECEIPT_ROOT_ID_CONFLICT",
  "REQUEST_NONCE_NOT_FOUND",
  "REQUEST_NONCE_AMBIGUOUS",
  "REQUEST_CONTEXT_MISMATCH",
  "REQUEST_MESSAGE_ID_UNOBSERVED",
  "REQUEST_ROOT_ID_MISSING",
  "REQUEST_ID_UNOBSERVED",
]);
const DIAGNOSTIC_KEY_ALLOWLIST = new Set([
  "accountIdentity", "account_identity", "agentId", "agent_id", "clientNonce",
  "client_nonce", "content", "entries", "id", "kind", "message", "messageId",
  "message_id", "requestId", "request_id", "requestMessageId", "request_message_id",
  "rootId", "root_id", "role", "sender", "targetId", "target_id", "targetIdentity",
  "target_identity", "teamIdentity", "team_identity", "text", "type",
]);
const MAX_DIAGNOSTIC_KEY_COUNT = 1_000_000;


class TaskError extends Error {
  constructor(stage, code, status) {
    super(code);
    this.name = "TaskError";
    this.stage = stage;
    this.code = code;
    if (Number.isInteger(status) && status >= 100 && status <= 599) this.status = status;
  }
}

function taskError(stage, code, status) {
  return new TaskError(stage, code, status);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nonEmptyId(value) {
  return typeof value === "string" && value.length > 0 && value.trim().length > 0 &&
    value.length <= 1024 && !IDENTIFIER_CONTROL.test(value);
}

function validOperationId(value) {
  return nonEmptyId(value) && value !== MARKER_OPERATION_ID;
}

function validatedRecordCorrelation(record) {
  const requestMessageId = record?.request_message_id;
  const messageId = record?.message_id;
  if (requestMessageId != null && messageId != null && requestMessageId !== messageId) {
    throw taskError("ledger", "OPERATION_RECORD_INVALID");
  }
  const selectedMessageId = requestMessageId ?? messageId;
  const present = [selectedMessageId, record?.request_id, record?.root_id]
    .map((value) => value != null);
  if (!present.some(Boolean)) return null;
  if (!present.every(Boolean) || !nonEmptyId(selectedMessageId) ||
    !nonEmptyId(record.request_id) || !nonEmptyId(record.root_id)) {
    throw taskError("ledger", "OPERATION_RECORD_INVALID");
  }
  return {
    messageId: selectedMessageId,
    requestId: record.request_id,
    rootId: record.root_id,
  };
}

function strictUtf8(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw taskError("input", "INPUT_UTF8_INVALID");
  }
}

function byteLength(text) {
  return Buffer.byteLength(text, "utf8");
}

export function validateTaskEnvelope(mode, value) {
  if (mode !== "execute" && mode !== "reconcile") {
    throw taskError("arguments", "MODE_REQUIRED");
  }
  if (!isRecord(value)) throw taskError("input", "INPUT_SCHEMA_INVALID");
  const expectedKeys = mode === "execute"
    ? ["operation_id", "prompt"]
    : ["operation_id"];
  if (Object.keys(value).length !== expectedKeys.length ||
    !expectedKeys.every((key) => Object.hasOwn(value, key))) {
    throw taskError("input", "INPUT_SCHEMA_INVALID");
  }
  if (!validOperationId(value.operation_id)) {
    throw taskError("input", "OPERATION_ID_INVALID");
  }
  if (mode === "execute") {
    if (typeof value.prompt !== "string" || !value.prompt.trim() ||
      value.prompt.includes("\0") || byteLength(value.prompt) > MAX_PROMPT_BYTES) {
      throw taskError("input", "PROMPT_INVALID");
    }
    // Reject JSON-escaped lone surrogates instead of silently changing prompt bytes.
    if (Buffer.from(value.prompt, "utf8").toString("utf8") !== value.prompt) {
      throw taskError("input", "PROMPT_INVALID");
    }
    return Object.freeze({ mode, operation_id: value.operation_id, prompt: value.prompt });
  }
  return Object.freeze({ mode, operation_id: value.operation_id });
}

export function parseTaskEnvelope(mode, raw) {
  const bytes = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
  if (bytes.length > MAX_INPUT_BYTES) throw taskError("input", "INPUT_TOO_LARGE");
  let parsed;
  try {
    parsed = JSON.parse(strictUtf8(bytes));
  } catch (error) {
    if (error instanceof TaskError) throw error;
    throw taskError("input", "INPUT_JSON_INVALID");
  }
  return validateTaskEnvelope(mode, parsed);
}

async function readBoundedStdin(stdin) {
  if (Buffer.isBuffer(stdin) || typeof stdin === "string" || stdin instanceof Uint8Array) {
    const bytes = Buffer.from(stdin);
    if (bytes.length > MAX_INPUT_BYTES) throw taskError("input", "INPUT_TOO_LARGE");
    return bytes;
  }
  if (!stdin || typeof stdin[Symbol.asyncIterator] !== "function") {
    throw taskError("input", "STDIN_UNAVAILABLE");
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_INPUT_BYTES) throw taskError("input", "INPUT_TOO_LARGE");
    chunks.push(bytes);
  }
  return Buffer.concat(chunks, size);
}

function safeError(error, fallbackStage, fallbackCode) {
  const stage = typeof error?.stage === "string" && /^[a-z_]{1,32}$/.test(error.stage)
    ? error.stage
    : fallbackStage;
  const code = typeof error?.code === "string" && /^[A-Z0-9_]{1,64}$/.test(error.code)
    ? error.code
    : fallbackCode;
  return {
    stage,
    code,
    ...(Number.isInteger(error?.status) && error.status >= 100 && error.status <= 599
      ? { status: error.status }
      : {}),
    ...(typeof error?.errorCode === "string" && SAFE_REMOTE_ERROR_CODE.test(error.errorCode.trim())
      ? { remote_error_code: error.errorCode.trim().toLowerCase() }
      : {}),
    ...(typeof error?.requestId === "string" && SAFE_REMOTE_REQUEST_ID.test(error.requestId.trim())
      ? { remote_request_id: error.requestId.trim() }
      : {}),
  };
}

function validateAnchor(record) {
  if (
    !isRecord(record) ||
    record.operation_id !== MARKER_OPERATION_ID ||
    record.transport_state !== "accepted" ||
    record.prompt_sha256 !== MARKER_PROMPT_SHA256 ||
    !nonEmptyId(record.account_identity) ||
    !nonEmptyId(record.team_identity) ||
    !nonEmptyId(record.target_identity) ||
    typeof record.client_nonce !== "string" ||
    !NONCE_PATTERN.test(record.client_nonce)
  ) {
    throw taskError("ledger", "MARKER_ANCHOR_INVALID");
  }
  return record;
}

function validateTaskRecord(record, operationId) {
  if (
    !isRecord(record) ||
    record.operation_id !== operationId ||
    !validOperationId(record.operation_id) ||
    !nonEmptyId(record.account_identity) ||
    !nonEmptyId(record.team_identity) ||
    !nonEmptyId(record.target_identity) ||
    typeof record.prompt_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(record.prompt_sha256) ||
    typeof record.client_nonce !== "string" ||
    !NONCE_PATTERN.test(record.client_nonce) ||
    !["prepared", "sending", "accepted", "unknown", "rejected"].includes(record.transport_state)
  ) {
    throw taskError("ledger", "OPERATION_RECORD_INVALID");
  }
  validatedRecordCorrelation(record);
  if (Object.hasOwn(record, "diagnostics")) {
    if (!Array.isArray(record.diagnostics) || record.diagnostics.length > 64 ||
      !record.diagnostics.every(isValidDiagnostic)) {
      throw taskError("ledger", "OPERATION_RECORD_INVALID");
    }
  }
  return record;
}

function readAnchor(ledgerCall) {
  let record;
  try {
    record = ledgerCall({ action: "anchor", operation_id: MARKER_OPERATION_ID });
  } catch (error) {
    throw taskError("ledger", safeError(error, "ledger", "MARKER_ANCHOR_UNAVAILABLE").code);
  }
  if (!isRecord(record) || record.ok !== true) throw taskError("ledger", "MARKER_ANCHOR_INVALID");
  return validateAnchor(record);
}

function readTaskRecord(ledgerCall, operationId) {
  let record;
  try {
    record = ledgerCall({ action: "task_read", operation_id: operationId });
  } catch (error) {
    const safe = safeError(error, "ledger", "OPERATION_READ_FAILED");
    throw taskError(safe.stage, safe.code, safe.status);
  }
  if (!isRecord(record) || record.ok !== true) throw taskError("ledger", "OPERATION_RECORD_INVALID");
  return validateTaskRecord(record, operationId);
}

function assertRecordMatchesAnchor(record, anchor) {
  if (
    record.account_identity !== anchor.account_identity ||
    record.team_identity !== anchor.team_identity ||
    record.target_identity !== anchor.target_identity
  ) {
    throw taskError("ledger", "MARKER_ANCHOR_MISMATCH");
  }
}

function deliveryFromState(state) {
  if (state === "accepted" || state === "rejected") return state;
  return "unknown";
}

function identifierField(body, names) {
  if (!isRecord(body)) return null;
  const present = names.filter((name) => Object.hasOwn(body, name));
  if (present.length === 0) return null;
  const values = present.map((name) => body[name]);
  if (!values.every((value) => nonEmptyId(value)) || new Set(values).size !== 1) return null;
  return values[0];
}

function matchesOptionalContext(body, context) {
  if (!isRecord(body)) return false;
  const fields = [
    [["accountIdentity", "account_identity"], context.accountIdentity],
    [["teamIdentity", "team_identity"], context.teamIdentity],
    [["agentId", "agent_id", "targetId", "target_id", "targetIdentity", "target_identity"], context.targetIdentity],
    [["clientNonce", "client_nonce"], context.clientNonce],
  ];
  for (const [names, expected] of fields) {
    const present = names.filter((name) => Object.hasOwn(body, name));
    if (present.length === 0) continue;
    const values = present.map((name) => body[name]);
    if (!values.every((value) => nonEmptyId(value)) ||
      new Set(values).size !== 1 || values[0] !== expected) {
      return false;
    }
  }
  return true;
}

function responseType(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (value === undefined) return "undefined";
  if (typeof value === "object") return "object";
  return typeof value;
}

function diagnosticFor(phase, reason, response, {
  httpStatus = null,
  tailEntries,
  nonceMatches,
} = {}) {
  const present = new Set();
  let unknownKeyCount = 0;
  const visited = new Set();
  const stack = [response];
  while (stack.length > 0) {
    const value = stack.pop();
    if (value === null || typeof value !== "object" || visited.has(value)) continue;
    visited.add(value);
    if (Array.isArray(value)) {
      for (const item of value) stack.push(item);
      continue;
    }
    for (const [key, nested] of Object.entries(value)) {
      if (DIAGNOSTIC_KEY_ALLOWLIST.has(key)) present.add(key);
      else unknownKeyCount += 1;
      if (nested !== null && typeof nested === "object") stack.push(nested);
    }
  }
  const diagnostic = {
    phase,
    reason,
    response_type: responseType(response),
    present_keys: [...present].sort(),
    unknown_key_count: Math.min(unknownKeyCount, MAX_DIAGNOSTIC_KEY_COUNT),
    http_status: Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599
      ? httpStatus
      : null,
    observed_at: new Date().toISOString(),
  };
  if (Number.isInteger(tailEntries)) diagnostic.tail_entries = tailEntries;
  if (Number.isInteger(nonceMatches)) diagnostic.nonce_matches = nonceMatches;
  return diagnostic;
}

function isValidDiagnostic(diagnostic) {
  if (!isRecord(diagnostic)) return false;
  const allowed = new Set([
    "phase", "reason", "response_type", "present_keys", "unknown_key_count",
    "http_status", "observed_at", "tail_entries", "nonce_matches",
  ]);
  if (Object.keys(diagnostic).some((key) => !allowed.has(key)) ||
    !DIAGNOSTIC_PHASES.has(diagnostic.phase) || !DIAGNOSTIC_REASONS.has(diagnostic.reason) ||
    !DIAGNOSTIC_RESPONSE_TYPES.has(diagnostic.response_type) ||
    !Array.isArray(diagnostic.present_keys) || diagnostic.present_keys.length > DIAGNOSTIC_KEY_ALLOWLIST.size ||
    !diagnostic.present_keys.every((key) => DIAGNOSTIC_KEY_ALLOWLIST.has(key)) ||
    new Set(diagnostic.present_keys).size !== diagnostic.present_keys.length ||
    JSON.stringify(diagnostic.present_keys) !== JSON.stringify([...diagnostic.present_keys].sort()) ||
    !Number.isInteger(diagnostic.unknown_key_count) || diagnostic.unknown_key_count < 0 ||
    diagnostic.unknown_key_count > MAX_DIAGNOSTIC_KEY_COUNT ||
    !(diagnostic.http_status === null || (Number.isInteger(diagnostic.http_status) &&
      diagnostic.http_status >= 100 && diagnostic.http_status <= 599)) ||
    typeof diagnostic.observed_at !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(diagnostic.observed_at) ||
    Number.isNaN(Date.parse(diagnostic.observed_at))) return false;
  for (const key of ["tail_entries", "nonce_matches"]) {
    if (Object.hasOwn(diagnostic, key) &&
      (!Number.isInteger(diagnostic[key]) || diagnostic[key] < 0 || diagnostic[key] > TRANSCRIPT_LIMIT)) {
      return false;
    }
  }
  if (diagnostic.phase !== "request_lookup" &&
    (Object.hasOwn(diagnostic, "tail_entries") || Object.hasOwn(diagnostic, "nonce_matches"))) return false;
  return !Object.hasOwn(diagnostic, "nonce_matches") ||
    !Object.hasOwn(diagnostic, "tail_entries") || diagnostic.nonce_matches <= diagnostic.tail_entries;
}

function receiptAnalysis(body, context, httpStatus) {
  if (!isRecord(body)) {
    return {
      messageId: null,
      correlation: null,
      diagnostic: diagnosticFor("send_receipt", "RECEIPT_NOT_OBJECT", body, { httpStatus }),
    };
  }
  const messageNames = ["messageId", "message_id", "requestMessageId", "request_message_id"];
  const requestNames = ["requestId", "request_id"];
  const rootNames = ["rootId", "root_id"];
  const messagePresent = messageNames.some((name) => Object.hasOwn(body, name));
  const messageId = identifierField(body, messageNames);
  let reason;
  if (!messagePresent) reason = "RECEIPT_MESSAGE_ID_MISSING";
  else if (!messageId) reason = "RECEIPT_MESSAGE_ID_CONFLICT";
  else if (!matchesOptionalContext(body, context)) reason = "RECEIPT_CONTEXT_MISMATCH";
  else {
    const requestPresent = requestNames.some((name) => Object.hasOwn(body, name));
    const requestId = identifierField(body, requestNames);
    if (!requestPresent) reason = "RECEIPT_REQUEST_ID_MISSING";
    else if (!requestId) reason = "RECEIPT_REQUEST_ID_CONFLICT";
    else {
      const rootPresent = rootNames.some((name) => Object.hasOwn(body, name));
      const rootId = identifierField(body, rootNames);
      if (!rootPresent) reason = "RECEIPT_ROOT_ID_MISSING";
      else if (!rootId) reason = "RECEIPT_ROOT_ID_CONFLICT";
      else return { messageId, correlation: { messageId, requestId, rootId }, diagnostic: null };
    }
  }
  return {
    messageId,
    correlation: null,
    diagnostic: diagnosticFor("send_receipt", reason, body, { httpStatus }),
  };
}

function isRejectedSend(error) {
  return Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
}

function scopedEntry(entry, context) {
  if (!isRecord(entry)) return false;
  return matchesOptionalContext(entry, {
    ...context,
    clientNonce: context.clientNonce ?? entry.clientNonce,
  });
}

function findTaskRequest(entries, context) {
  const nonceMatches = entries.filter((entry) => entry?.clientNonce === context.clientNonce);
  let reason;
  let request = null;
  if (nonceMatches.length === 0) reason = "REQUEST_NONCE_NOT_FOUND";
  else if (nonceMatches.length > 1) reason = "REQUEST_NONCE_AMBIGUOUS";
  else {
    const candidate = nonceMatches[0];
    if (!scopedEntry(candidate, context)) {
      return { request, reason: "REQUEST_CONTEXT_MISMATCH", nonceMatches: nonceMatches.length };
    }
    const found = findMarkerRequest([candidate], context.clientNonce);
    if (!found?.messageIdObserved) {
      reason = "REQUEST_MESSAGE_ID_UNOBSERVED";
    } else {
      const rootNames = ["rootId", "root_id"];
      const explicitRootPresent = rootNames.some((name) => Object.hasOwn(candidate, name));
      const explicitRootId = identifierField(candidate, rootNames);
      if (explicitRootPresent && !explicitRootId) {
        throw taskError("transcript", "REQUEST_SCHEMA_INVALID");
      }
      request = {
        messageId: found.messageId,
        requestId: found.requestIdObserved ? found.requestId : null,
        // The marker parser has already proved this is the sole nonce-matched
        // user request and derives the missing root from this same entry's id.
        rootId: explicitRootId ?? found.rootId,
      };
      if (!request.requestId) reason = "REQUEST_ID_UNOBSERVED";
    }
  }
  return { request, reason, nonceMatches: nonceMatches.length };
}

function isToolReplyEntry(entry) {
  if (!isRecord(entry) || !isRecord(entry.message)) return false;
  const message = entry.message;
  const type = typeof message.type === "string"
    ? message.type.replace(/[-_ ]/g, "").toLowerCase()
    : "";
  return ["toolcall", "toolresult", "toolstart", "toolend", "functioncall"].includes(type) ||
    ["toolCall", "tool_call", "toolResult", "tool_result", "functionCall", "function_call"]
      .some((key) => Object.hasOwn(message, key));
}

function correlatedReplyEvidence(entries, request, context) {
  if (!request?.requestId) {
    return { replies: [], replyEntries: 0, toolEntries: 0, nonTextEntries: 0 };
  }
  const replies = [];
  let replyEntries = 0;
  let toolEntries = 0;
  let nonTextEntries = 0;
  for (const entry of entries) {
    if (!nonEmptyId(entry.id) || !scopedEntry(entry, context) ||
      !isMarkerReply(entry, request, context.clientNonce)) continue;
    replyEntries += 1;
    if (!isRecord(entry.message) || entry.message.type !== "text" ||
      typeof entry.message.content !== "string") {
      nonTextEntries += 1;
      if (isToolReplyEntry(entry)) toolEntries += 1;
      continue;
    }
    replies.push(entry.message.content);
  }
  return { replies, replyEntries, toolEntries, nonTextEntries };
}

function truncateUtf8(text, maximumBytes) {
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length <= maximumBytes) return { text, truncated: false };
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let end = maximumBytes;
  while (end >= 0) {
    try {
      return { text: decoder.decode(bytes.subarray(0, end)), truncated: true };
    } catch {
      end -= 1;
    }
  }
  return { text: "", truncated: true };
}

function boundedReplyResult(
  replies,
  { replyEntries = replies.length, toolEntries = 0, nonTextEntries = 0 } = {},
) {
  const selected = replies.slice(-MAX_REPLY_COUNT);
  const bounded = [];
  let remaining = MAX_TOTAL_REPLY_BYTES;
  let truncated = replies.length > selected.length;
  for (const reply of selected) {
    if (remaining <= 0) {
      truncated = true;
      break;
    }
    const maximum = Math.min(MAX_REPLY_BYTES, remaining);
    const result = truncateUtf8(reply, maximum);
    bounded.push(result.text);
    remaining -= byteLength(result.text);
    truncated ||= result.truncated;
  }
  return {
    reply_observed: replies.length > 0,
    reply_entries_observed: replyEntries,
    tool_entries_observed: toolEntries,
    non_text_entries_observed: nonTextEntries,
    tool_only: replyEntries > 0 && replies.length === 0 && toolEntries > 0 &&
      nonTextEntries === toolEntries,
    response_available: bounded.some((reply) => reply.length > 0),
    replies: bounded,
    response_truncated: truncated,
  };
}

function persistTaskState(ledgerCall, operationId, state, correlation = null, record = null, diagnostic = null) {
  try {
    const payload = { action: "task_state", operation_id: operationId, state };
    if (diagnostic) payload.diagnostic = diagnostic;
    if (correlation) {
      Object.assign(payload, {
        account_identity: record.account_identity,
        team_identity: record.team_identity,
        target_identity: record.target_identity,
        client_nonce: record.client_nonce,
        prompt_sha256: record.prompt_sha256,
        request_message_id: correlation.messageId,
        request_id: correlation.requestId,
        root_id: correlation.rootId,
      });
    }
    const result = ledgerCall(payload);
    if (!isRecord(result) || result.ok !== true || result.transport_state !== state) {
      throw taskError("ledger", "LEDGER_STATE_WRITE_FAILED");
    }
  } catch {
    throw taskError("ledger", "LEDGER_STATE_WRITE_FAILED");
  }
}

async function gatewayRequestDetailed(session, method, body, stage) {
  if (typeof session.gatewayRequestDetailed === "function") {
    const result = await session.gatewayRequestDetailed(method, body, stage);
    if (isRecord(result) && Object.hasOwn(result, "body")) {
      return {
        body: result.body,
        httpStatus: Number.isInteger(result.httpStatus) ? result.httpStatus : null,
      };
    }
    return { body: result, httpStatus: null };
  }
  return { body: await session.gatewayRequest(method, body, stage), httpStatus: null };
}

function recordCorrelation(record) {
  return validatedRecordCorrelation(record);
}

function outputCorrelation(correlation) {
  if (!correlation || !nonEmptyId(correlation.messageId) ||
    !nonEmptyId(correlation.requestId) || !nonEmptyId(correlation.rootId)) {
    return undefined;
  }
  return {
    request_message_id: correlation.messageId,
    request_id: correlation.requestId,
    root_id: correlation.rootId,
  };
}

function protocolErrorCategory(error) {
  if (!error) return undefined;
  if (error.code === "HTTP_STATUS" || error.code === "REDIRECT_REJECTED") return "http_status";
  if (error.code === "RESPONSE_READ_FAILED" || error.code === "REQUEST_FAILED") return "transport";
  if (error.code === "INVALID_JSON" ||
    (typeof error.code === "string" && error.code.includes("SCHEMA"))) return "schema";
  if (error.stage === "ledger") return "ledger";
  if (error.stage === "transcript" || error.stage === "thread") return "inspection";
  return "transport";
}

function outcome({
  operationId,
  deliveryStatus,
  sendAttempted,
  inspectionStatus,
  replies = [],
  replyEntries = 0,
  toolEntries = 0,
  nonTextEntries = 0,
  correlation,
  remoteTerminal = "unsupported",
  remoteTerminalSupported = false,
  error,
  sendError,
  inspectionError,
  diagnostics = [],
}) {
  const bounded = boundedReplyResult(replies, { replyEntries, toolEntries, nonTextEntries });
  // `ok` is protocol success only.  A successful protocol read does not prove
  // that the remote agent finished its work; that requires a supported remote
  // terminal marker, which this transport does not provide today.
  const protocolOk = deliveryStatus === "accepted" &&
    inspectionStatus !== "unknown" && !error && !sendError && !inspectionError;
  let completionStatus;
  if (deliveryStatus === "rejected") completionStatus = "rejected";
  else if (inspectionStatus === "unknown") completionStatus = "unknown";
  else if (inspectionStatus === "pending") completionStatus = "pending";
  else if (replyEntries === 0) completionStatus = "empty";
  else if (replies.length === 0 && toolEntries > 0 && toolEntries === bounded.non_text_entries_observed) {
    completionStatus = "tool_only";
  } else if (replies.length === 0 && bounded.non_text_entries_observed > 0) completionStatus = "non_text";
  else if (!bounded.response_available) completionStatus = "empty";
  else if (bounded.response_truncated) completionStatus = "truncated";
  else if (remoteTerminalSupported && remoteTerminal === "completed") completionStatus = "completed";
  else completionStatus = "nonterminal";
  const primaryError = error || sendError || inspectionError;
  return {
    ok: protocolOk,
    operation_id: operationId,
    delivery_status: deliveryStatus,
    send_attempted: sendAttempted,
    inspection_status: inspectionStatus,
    operation_completed: protocolOk && completionStatus === "completed",
    completion_status: completionStatus,
    ...bounded,
    remote_terminal: remoteTerminal,
    remote_terminal_supported: remoteTerminalSupported,
    ...(protocolErrorCategory(primaryError)
      ? { error_category: protocolErrorCategory(primaryError) }
      : {}),
    ...(outputCorrelation(correlation) ? { correlation: outputCorrelation(correlation) } : {}),
    ...(diagnostics.length ? { diagnostics } : {}),
    ...(error ? error : {}),
    ...(sendError ? { send_error: sendError } : {}),
    ...(inspectionError ? { inspection_error: inspectionError } : {}),
  };
}

async function inspectOperation({
  session,
  record,
  ledgerCall,
  sendAttempted,
  initialDelivery,
  priorError,
  diagnostics = [],
}) {
  let deliveryStatus = initialDelivery;
  let inspectionStatus = "pending";
  let request;
  let entries;
  let replyEntries = 0;
  let toolEntries = 0;
  let nonTextEntries = 0;
  let error = priorError;
  let inspectionError;
  const operationDiagnostics = [...(record.diagnostics || []), ...diagnostics];
  const correlationContext = {
    accountIdentity: record.account_identity,
    teamIdentity: record.team_identity,
    targetIdentity: record.target_identity,
    clientNonce: record.client_nonce,
  };
  let savedCorrelation;
  try {
    savedCorrelation = recordCorrelation(record);
  } catch (recordFailure) {
    inspectionError = safeError(recordFailure, "ledger", "OPERATION_RECORD_INVALID");
    return outcome({
      operationId: record.operation_id,
      deliveryStatus,
      sendAttempted,
      inspectionStatus: "unknown",
      diagnostics: operationDiagnostics,
      error: { ...error, ...inspectionError },
      sendError: priorError,
      inspectionError,
    });
  }

  if (savedCorrelation) {
    request = savedCorrelation;
  } else {
    try {
      const tailResult = await gatewayRequestDetailed(
        session,
        "getAgentTranscriptTail",
        { id: record.target_identity, limit: TRANSCRIPT_LIMIT },
        "transcript",
      );
      entries = markerTailEntries(tailResult.body);
      const lookup = findTaskRequest(entries, correlationContext);
      request = lookup.request;
      if (lookup.reason) {
        const diagnostic = diagnosticFor("request_lookup", lookup.reason, tailResult.body, {
          httpStatus: tailResult.httpStatus,
          tailEntries: entries.length,
          nonceMatches: lookup.nonceMatches,
        });
        operationDiagnostics.push(diagnostic);
        try {
          persistTaskState(ledgerCall, record.operation_id, deliveryStatus, null, null, diagnostic);
        } catch (stateError) {
          error = safeError(stateError, "ledger", "LEDGER_STATE_WRITE_FAILED");
        }
      }
    } catch (inspectionFailure) {
      inspectionError = safeError(inspectionFailure, "transcript", "TRANSCRIPT_READ_FAILED");
      return outcome({
        operationId: record.operation_id,
        deliveryStatus,
        sendAttempted,
        inspectionStatus: "unknown",
        diagnostics: operationDiagnostics,
        error: { ...error, ...inspectionError },
        sendError: priorError,
        inspectionError,
      });
    }
  }

  if (!request || !request.requestId) {
    if (request && deliveryStatus !== "accepted") {
      deliveryStatus = "accepted";
      try {
        persistTaskState(ledgerCall, record.operation_id, "accepted");
      } catch (stateError) {
        error = safeError(stateError, "ledger", "LEDGER_STATE_WRITE_FAILED");
      }
    }
    return outcome({
      operationId: record.operation_id,
      deliveryStatus,
      sendAttempted,
      inspectionStatus,
      diagnostics: operationDiagnostics,
      error,
      sendError: priorError,
    });
  }

  deliveryStatus = "accepted";
  const discoveredCorrelation = savedCorrelation || !request.requestId ? null : request;
  if (discoveredCorrelation) {
    try {
      persistTaskState(ledgerCall, record.operation_id, "accepted", discoveredCorrelation, record);
    } catch (stateError) {
      error = safeError(stateError, "ledger", "LEDGER_STATE_WRITE_FAILED");
    }
  } else if (initialDelivery !== "accepted") {
    try {
      persistTaskState(ledgerCall, record.operation_id, "accepted");
    } catch (stateError) {
      error = safeError(stateError, "ledger", "LEDGER_STATE_WRITE_FAILED");
    }
  }

  let evidence = entries
    ? correlatedReplyEvidence(entries, request, correlationContext)
    : { replies: [], replyEntries: 0, toolEntries: 0, nonTextEntries: 0 };
  let replies = evidence.replies;
  replyEntries = evidence.replyEntries;
  toolEntries = evidence.toolEntries;
  nonTextEntries = evidence.nonTextEntries;
  if (replies.length === 0) {
    try {
      const thread = await session.gatewayRequest(
        "getAgentThread",
        { id: record.target_identity, rootId: request.rootId },
        "thread",
      );
      const combinedEntries = new Map();
      for (const entry of [...(entries || []), ...markerThreadEntries(thread, request.rootId)]) {
        if (nonEmptyId(entry?.id)) combinedEntries.set(entry.id, entry);
      }
      evidence = correlatedReplyEvidence(
        [...combinedEntries.values()],
        request,
        correlationContext,
      );
      replies = evidence.replies;
      replyEntries = evidence.replyEntries;
      toolEntries = evidence.toolEntries;
      nonTextEntries = evidence.nonTextEntries;
      inspectionStatus = replyEntries > 0 ? "observed" : "pending";
    } catch (threadFailure) {
      inspectionStatus = "unknown";
      inspectionError = safeError(threadFailure, "thread", "THREAD_READ_FAILED");
      error = { ...error, ...inspectionError };
    }
  } else {
    inspectionStatus = "observed";
  }

  return outcome({
    operationId: record.operation_id,
    deliveryStatus,
    sendAttempted,
    inspectionStatus,
    diagnostics: operationDiagnostics,
    replies,
    replyEntries,
    toolEntries,
    nonTextEntries,
    correlation: request,
    error,
    sendError: priorError,
    inspectionError,
  });
}

async function executeWithSession({ session, input, anchor, ledgerCall }) {
  const target = resolveGrokBot(session.agents);
  if (target.id !== anchor.target_identity) throw taskError("resolve", "TARGET_IDENTITY_MISMATCH");

  let admission;
  try {
    admission = ledgerCall({
      action: "task_begin",
      operation_id: input.operation_id,
      account_identity: session.accountIdentity,
      team_identity: session.teamIdentity,
      target_identity: target.id,
      prompt: input.prompt,
    });
  } catch (error) {
    const safe = safeError(error, "ledger", "OPERATION_ADMISSION_FAILED");
    throw taskError(safe.stage, safe.code, safe.status);
  }
  if (!isRecord(admission) || admission.ok !== true || typeof admission.created !== "boolean") {
    throw taskError("ledger", "OPERATION_ADMISSION_INVALID");
  }
  let record = validateTaskRecord(admission, input.operation_id);
  assertRecordMatchesAnchor(record, anchor);
  const expectedPromptHash = createHash("sha256").update(input.prompt, "utf8").digest("hex");
  if (record.prompt_sha256 !== expectedPromptHash) throw taskError("ledger", "OPERATION_CONFLICT");

  let sendAttempted = false;
  let deliveryStatus = deliveryFromState(record.transport_state);
  let sendError;
  const diagnostics = [];
  if (admission.created) {
    sendAttempted = true;
    let receipt;
    try {
      const receiptResult = await gatewayRequestDetailed(
        session,
        "sendPrompt",
        {
          agentId: target.id,
          prompt: input.prompt,
          clientNonce: record.client_nonce,
        },
        "send_prompt",
      );
      receipt = receiptResult.body;
      const sendContext = {
        accountIdentity: session.accountIdentity,
        teamIdentity: session.teamIdentity,
        targetIdentity: target.id,
        clientNonce: record.client_nonce,
      };
      const analysis = receiptAnalysis(receipt, sendContext, receiptResult.httpStatus);
      const receiptAccepted = analysis.messageId !== null;
      const correlation = analysis.correlation;
      deliveryStatus = receiptAccepted ? "accepted" : "unknown";
      if (analysis.diagnostic) diagnostics.push(analysis.diagnostic);
      try {
        persistTaskState(
          ledgerCall,
          input.operation_id,
          deliveryStatus,
          correlation,
          record,
          analysis.diagnostic,
        );
      } catch (stateError) {
        sendError = safeError(stateError, "ledger", "LEDGER_STATE_WRITE_FAILED");
      }
      if (correlation) {
        record = { ...record,
          request_message_id: correlation.messageId,
          request_id: correlation.requestId,
          root_id: correlation.rootId,
        };
      }
    } catch (error) {
      deliveryStatus = isRejectedSend(error) ? "rejected" : "unknown";
      sendError = safeError(error, "send_prompt", "SEND_FAILED");
      try {
        persistTaskState(ledgerCall, input.operation_id, deliveryStatus);
      } catch {
        sendError = { stage: "ledger", code: "LEDGER_STATE_WRITE_FAILED" };
      }
    }
  }
  return inspectOperation({
    session,
    record,
    ledgerCall,
    sendAttempted,
    initialDelivery: deliveryStatus,
    priorError: sendError,
    diagnostics,
  });
}

export async function runGrokBotTask({
  mode,
  input,
  dependencies,
  ledgerCall = createLedgerCall(),
  authenticatedAction = runAuthenticatedListAction,
} = {}) {
  const envelope = validateTaskEnvelope(mode, input);
  if (typeof ledgerCall !== "function") throw taskError("ledger", "LEDGER_ADAPTER_REQUIRED");
  if (typeof authenticatedAction !== "function") throw taskError("transport", "ACTION_ADAPTER_REQUIRED");

  if (envelope.mode === "reconcile") {
    // Read first so an absent operation never authenticates or creates a ledger row.
    const record = readTaskRecord(ledgerCall, envelope.operation_id);
    const anchor = readAnchor(ledgerCall);
    assertRecordMatchesAnchor(record, anchor);
    return authenticatedAction({
      ...dependencies,
      expectedAccountIdentity: record.account_identity,
      expectedTeamIdentity: record.team_identity,
      allowSendPrompt: false,
      action: async (session) => {
        const target = resolveGrokBot(session.agents);
        if (target.id !== record.target_identity || target.id !== anchor.target_identity) {
          throw taskError("resolve", "TARGET_IDENTITY_MISMATCH");
        }
        if (session.accountIdentity !== record.account_identity ||
          session.teamIdentity !== record.team_identity) {
          throw taskError("account", "ACCOUNT_CONTEXT_MISMATCH");
        }
        return inspectOperation({
          session,
          record,
          ledgerCall,
          sendAttempted: false,
          initialDelivery: deliveryFromState(record.transport_state),
        });
      },
    });
  }

  const anchor = readAnchor(ledgerCall);
  try {
    const existing = readTaskRecord(ledgerCall, envelope.operation_id);
    assertRecordMatchesAnchor(existing, anchor);
    const expectedPromptHash = createHash("sha256").update(envelope.prompt, "utf8").digest("hex");
    if (existing.prompt_sha256 !== expectedPromptHash) {
      throw taskError("ledger", "OPERATION_CONFLICT");
    }
  } catch (error) {
    const safe = safeError(error, "ledger", "OPERATION_READ_FAILED");
    if (safe.code !== "OPERATION_NOT_FOUND") throw error;
  }
  return authenticatedAction({
    ...dependencies,
    expectedAccountIdentity: anchor.account_identity,
    expectedTeamIdentity: anchor.team_identity,
    allowSendPrompt: true,
    action: (session) => {
      if (session.accountIdentity !== anchor.account_identity ||
        session.teamIdentity !== anchor.team_identity) {
        throw taskError("account", "ACCOUNT_CONTEXT_MISMATCH");
      }
      return executeWithSession({ session, input: envelope, anchor, ledgerCall });
    },
  });
}

function modeFromArgv(argv) {
  if (!Array.isArray(argv) || argv.length !== 1) throw taskError("arguments", "MODE_REQUIRED");
  if (argv[0] === "--execute") return "execute";
  if (argv[0] === "--reconcile") return "reconcile";
  throw taskError("arguments", "MODE_REQUIRED");
}

function stdoutResult(stdout, value) {
  stdout.write(`${JSON.stringify(value)}\n`);
}

export async function main({
  argv = process.argv.slice(2),
  stdin = process.stdin,
  stdout = process.stdout,
  dependencies,
  ledgerCall,
  authenticatedAction,
} = {}) {
  let mode;
  let envelope;
  try {
    mode = modeFromArgv(argv);
    envelope = parseTaskEnvelope(mode, await readBoundedStdin(stdin));
  } catch (error) {
    stdoutResult(stdout, { ok: false, ...safeError(error, "input", "INPUT_INVALID") });
    return 2;
  }

  try {
    const deps = dependencies ?? (process.platform === "win32"
      ? {
          profileReader: createWindowsProfileReader(),
          dpapiUnprotect: createWindowsDpapiUnprotect(),
          fetchImpl: globalThis.fetch,
        }
      : null);
    if (!deps) throw taskError("profile", "WINDOWS_REQUIRED");
    const result = await runGrokBotTask({
      mode,
      input: mode === "execute"
        ? { operation_id: envelope.operation_id, prompt: envelope.prompt }
        : { operation_id: envelope.operation_id },
      dependencies: deps,
      ...(ledgerCall === undefined ? {} : { ledgerCall }),
      ...(authenticatedAction === undefined ? {} : { authenticatedAction }),
    });
    stdoutResult(stdout, result);
    // A structured transport or inspection error is a failed helper call even
    // when the durable operation may already have crossed sendPrompt.  The
    // caller must retain the operation id and reconcile instead of retrying.
    return result?.ok === true ? 0 : 1;
  } catch (error) {
    stdoutResult(stdout, { ok: false, ...safeError(error, "task", "INTERNAL_ERROR") });
    return 1;
  }
}

if (
  process.argv[1] &&
  pathToFileURL(process.argv[1]).href === import.meta.url
) {
  process.exitCode = await main();
}
