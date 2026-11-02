import { createDecipheriv } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const ENSURE_SANDBOX_URL =
  "https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox";
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_ERROR_DIAGNOSTIC_BYTES = 64 * 1024;
const REQUEST_TIMEOUT_MS = 30_000;
const DPAPI_TIMEOUT_MS = 5_000;
const DPAPI_MAX_BUFFER = 64 * 1024;
const APP_VERSION_PATTERN = /^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$/;
const SAFE_REMOTE_ERROR_CODES = new Set([
  "aborted",
  "account_not_found",
  "already_exists",
  "authentication_failed",
  "bad_gateway",
  "bad_request",
  "canceled",
  "cancelled",
  "conflict",
  "data_loss",
  "deadline_exceeded",
  "failed_precondition",
  "forbidden",
  "gateway_timeout",
  "internal",
  "internal_error",
  "invalid_argument",
  "invalid_request",
  "not_found",
  "out_of_range",
  "permission_denied",
  "rate_limited",
  "resource_exhausted",
  "route_unavailable",
  "sandbox_unavailable",
  "server_error",
  "service_unavailable",
  "timeout",
  "too_many_requests",
  "unauthenticated",
  "unauthorized",
  "unknown",
  "unavailable",
  "unimplemented",
]);
const SAFE_ERROR_CODE_HEADERS = Object.freeze([
  "x-error-code",
  "x-cursor-error-code",
]);
const SAFE_REQUEST_ID_HEADERS = Object.freeze([
  "x-request-id",
  "x-correlation-id",
  "x-cursor-request-id",
  "x-cursor-correlation-id",
]);
const SAFE_ERROR_CODE_KEYS = Object.freeze([
  "code",
  "error",
  "errorCode",
  "error_code",
]);
const SAFE_REQUEST_ID_KEYS = Object.freeze([
  "requestId",
  "request_id",
  "requestID",
  "correlationId",
  "correlation_id",
  "correlationID",
]);
const SAFE_CORRELATION_ID_PATTERN = /^(?:[a-f0-9]{16,128}|[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)$/i;
const PROFILE_FILES = new Set([
  "sand-secrets.json",
  "Local State",
  "desktop-status.json",
]);
const RESERVED_GATEWAY_HEADERS = new Set([
  "authorization",
  "connection",
  "connect-protocol-version",
  "content-length",
  "content-type",
  "cookie",
  "host",
  "x-cursor-client-type",
  "x-cursor-client-version",
  "x-cursor-team-id",
  "x-sand-box-namespace",
]);
const AUTHENTICATED_GATEWAY_METHODS = new Set([
  "sendPrompt",
  "getAgentTranscriptTail",
  "getAgentThread",
]);
const READ_ONLY_GATEWAY_METHODS = new Set([
  "getAgentTranscriptTail",
  "getAgentThread",
]);
const NO_SELECTED_TEAM = "__no_selected_team__";

class ProbeError extends Error {
  constructor(stage, code, status, diagnostics = {}) {
    super(code);
    this.name = "ProbeError";
    this.stage = stage;
    this.code = code;
    if (Number.isInteger(status) && status >= 100 && status <= 599) {
      this.status = status;
    }
    const errorCode = safeRemoteErrorCode(diagnostics?.errorCode);
    const requestId = safeCorrelationId(diagnostics?.requestId);
    if (errorCode !== undefined) this.errorCode = errorCode;
    if (requestId !== undefined) this.requestId = requestId;
  }
}

function probeError(stage, code, status, diagnostics) {
  return new ProbeError(stage, code, status, diagnostics);
}

function safeRemoteErrorCode(value) {
  if (typeof value !== "string") return undefined;
  const normalized = value.trim().toLowerCase();
  if (
    !/^[a-z][a-z0-9_]{0,63}$/.test(normalized) ||
    !SAFE_REMOTE_ERROR_CODES.has(normalized)
  ) {
    return undefined;
  }
  return normalized;
}

function safeCorrelationId(value) {
  if (typeof value !== "string") return undefined;
  const normalized = value.trim();
  if (
    !normalized ||
    normalized.length > 128 ||
    !SAFE_CORRELATION_ID_PATTERN.test(normalized) ||
    /(?:authorization|bearer|cookie|credential|key|password|secret|token)/i.test(normalized)
  ) {
    return undefined;
  }
  return normalized;
}

function responseHeader(response, name) {
  try {
    const value = response?.headers?.get?.(name);
    return typeof value === "string" ? value : undefined;
  } catch {
    return undefined;
  }
}

function nestedErrorRecords(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) return [];
  const records = [body];
  for (const key of ["error", "details", "metadata"]) {
    const nested = body[key];
    if (nested && typeof nested === "object" && !Array.isArray(nested)) {
      records.push(nested);
    }
  }
  return records;
}

function parseSafeResponseDiagnostics(response, bodyText) {
  let body;
  if (
    typeof bodyText === "string" &&
    Buffer.byteLength(bodyText, "utf8") <= MAX_ERROR_DIAGNOSTIC_BYTES
  ) {
    try {
      body = JSON.parse(bodyText);
    } catch {
      body = undefined;
    }
  }

  let errorCode;
  for (const header of SAFE_ERROR_CODE_HEADERS) {
    errorCode = safeRemoteErrorCode(responseHeader(response, header));
    if (errorCode !== undefined) break;
  }
  let requestId;
  for (const header of SAFE_REQUEST_ID_HEADERS) {
    requestId = safeCorrelationId(responseHeader(response, header));
    if (requestId !== undefined) break;
  }

  for (const record of nestedErrorRecords(body)) {
    if (errorCode === undefined) {
      for (const key of SAFE_ERROR_CODE_KEYS) {
        errorCode = safeRemoteErrorCode(record[key]);
        if (errorCode !== undefined) break;
      }
    }
    if (requestId === undefined) {
      for (const key of SAFE_REQUEST_ID_KEYS) {
        requestId = safeCorrelationId(record[key]);
        if (requestId !== undefined) break;
      }
    }
    if (errorCode !== undefined && requestId !== undefined) break;
  }

  return {
    ...(errorCode === undefined ? {} : { errorCode }),
    ...(requestId === undefined ? {} : { requestId }),
  };
}

async function readSafeResponseDiagnostics(response, stage) {
  let bodyText;
  try {
    bodyText = await readResponseText(response, stage);
  } catch {
    // HTTP failure status remains the primary diagnostic when the body is
    // unreadable or exceeds the bounded response budget.
  }
  return parseSafeResponseDiagnostics(response, bodyText);
}

export function createWindowsProfileReader({
  appData = process.env.APPDATA,
  platform = process.platform,
} = {}) {
  if (platform !== "win32") throw probeError("profile", "WINDOWS_REQUIRED");
  if (typeof appData !== "string" || !appData) {
    throw probeError("profile", "PROFILE_UNAVAILABLE");
  }
  const profileRoot = join(appData, "Grok Bot");
  return Object.freeze({
    read(name) {
      if (!PROFILE_FILES.has(name)) throw probeError("profile", "PROFILE_FILE_NOT_ALLOWED");
      return readFileSync(join(profileRoot, name), "utf8");
    },
    exists(name) {
      if (!PROFILE_FILES.has(name)) throw probeError("profile", "PROFILE_FILE_NOT_ALLOWED");
      return existsSync(join(profileRoot, name));
    },
  });
}

export function createWindowsDpapiUnprotect(
  execFileSyncImpl = execFileSync,
  { systemRoot = process.env.SystemRoot || "C:\\Windows" } = {},
) {
  const executable = join(
    systemRoot,
    "System32/WindowsPowerShell/v1.0/powershell.exe",
  );
  const script =
    "Add-Type -AssemblyName System.Security; " +
    "$wrapped = [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()); " +
    "[Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Unprotect(" +
    "$wrapped, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))";

  return (wrappedKey) => {
    if (!Buffer.isBuffer(wrappedKey) || wrappedKey.length === 0) {
      throw probeError("dpapi", "DPAPI_INPUT_INVALID");
    }
    let output;
    try {
      output = execFileSyncImpl(
        executable,
        ["-NoProfile", "-NonInteractive", "-Command", script],
        {
          input: Buffer.from(wrappedKey.toString("base64"), "ascii"),
          windowsHide: true,
          timeout: DPAPI_TIMEOUT_MS,
          maxBuffer: DPAPI_MAX_BUFFER,
          stdio: ["pipe", "pipe", "pipe"],
          env: {
            SystemRoot: systemRoot,
            WINDIR: systemRoot,
            PATH: process.env.PATH || "",
          },
        },
      );
    } catch {
      throw probeError("dpapi", "DPAPI_UNPROTECT_FAILED");
    }
    const encoded = Buffer.isBuffer(output)
      ? output.toString("ascii").trim()
      : String(output).trim();
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
      throw probeError("dpapi", "DPAPI_OUTPUT_INVALID");
    }
    const key = Buffer.from(encoded, "base64");
    if (key.length !== 32) throw probeError("dpapi", "DPAPI_KEY_INVALID");
    return key;
  };
}

function readJson(profileReader, name, stage, code) {
  let text;
  try {
    text = profileReader.read(name);
  } catch {
    throw probeError(stage, code);
  }
  if (Buffer.isBuffer(text)) text = text.toString("utf8");
  if (typeof text !== "string") throw probeError(stage, code);
  try {
    return JSON.parse(text);
  } catch {
    throw probeError(stage, code);
  }
}

function readAppVersion(profileReader) {
  const status = readJson(
    profileReader,
    "desktop-status.json",
    "app_version",
    "APP_VERSION_UNAVAILABLE",
  );
  if (status?.signedIn !== true) {
    throw probeError("app_version", "APP_NOT_SIGNED_IN");
  }
  const version = status?.appVersion;
  if (typeof version !== "string" || !APP_VERSION_PATTERN.test(version)) {
    throw probeError("app_version", "APP_VERSION_INVALID");
  }
  return version;
}

function readAccountPin(profileReader) {
  const stored = readJson(
    profileReader,
    "sand-secrets.json",
    "account",
    "ACCOUNT_FILE_UNAVAILABLE",
  );
  if (stored?.version != null && stored.version !== 1) {
    throw probeError("account", "ACCOUNT_FILE_VERSION_UNSUPPORTED");
  }

  let cursorAccounts;
  try {
    cursorAccounts = JSON.parse(stored?.["cursor-accounts"] ?? "null");
  } catch {
    throw probeError("account", "ACCOUNT_FILE_INVALID");
  }
  const accounts = cursorAccounts?.accounts;
  const activeIdentity = cursorAccounts?.active;
  if (!accounts || typeof accounts !== "object" || Array.isArray(accounts)) {
    throw probeError("account", "ACCOUNT_FILE_INVALID");
  }
  if (typeof activeIdentity !== "string" || !activeIdentity) {
    throw probeError(
      "account",
      Object.keys(accounts).length > 1
        ? "ACTIVE_ACCOUNT_AMBIGUOUS"
        : "ACTIVE_ACCOUNT_MISSING",
    );
  }
  if (!Object.hasOwn(accounts, activeIdentity)) {
    throw probeError("account", "ACTIVE_ACCOUNT_NOT_FOUND");
  }

  const active = accounts[activeIdentity];
  const accessTokenCiphertext = active?.["cursor-access-token"];
  const teamCiphertext = active?.["cursor-selected-team-id"] ?? null;
  if (typeof accessTokenCiphertext !== "string" || !accessTokenCiphertext) {
    throw probeError("account", "ACTIVE_ACCESS_TOKEN_MISSING");
  }
  if (teamCiphertext !== null && (typeof teamCiphertext !== "string" || !teamCiphertext)) {
    throw probeError("account", "SELECTED_TEAM_INVALID");
  }
  return Object.freeze({
    activeIdentity,
    accessTokenCiphertext,
    teamCiphertext,
  });
}

function assertAccountPin(profileReader, pinned) {
  readAppVersion(profileReader);
  let current;
  try {
    current = readAccountPin(profileReader);
  } catch {
    throw probeError("account", "ACCOUNT_SELECTION_CHANGED");
  }
  if (current.activeIdentity !== pinned.activeIdentity) {
    throw probeError("account", "ACTIVE_ACCOUNT_CHANGED");
  }
  if (current.accessTokenCiphertext !== pinned.accessTokenCiphertext) {
    throw probeError("account", "ACTIVE_ACCOUNT_CHANGED");
  }
  if (current.teamCiphertext !== pinned.teamCiphertext) {
    throw probeError("account", "SELECTED_TEAM_CHANGED");
  }
}

function decodeBase64(value, stage, code) {
  if (
    typeof value !== "string" ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    throw probeError(stage, code);
  }
  const decoded = Buffer.from(value, "base64");
  if (!decoded.length) throw probeError(stage, code);
  return decoded;
}

function readDpapiWrappedKey(profileReader) {
  const localState = readJson(
    profileReader,
    "Local State",
    "dpapi",
    "LOCAL_STATE_UNAVAILABLE",
  );
  const encoded = localState?.os_crypt?.encrypted_key;
  const wrapped = decodeBase64(encoded, "dpapi", "DPAPI_KEY_MISSING");
  if (wrapped.subarray(0, 5).toString("latin1") !== "DPAPI") {
    throw probeError("dpapi", "DPAPI_PREFIX_INVALID");
  }
  return wrapped.subarray(5);
}

function decryptV10Secret(encoded, key, code) {
  if (!Buffer.isBuffer(key) || key.length !== 32) {
    throw probeError("account", "SAFE_STORAGE_KEY_INVALID");
  }
  const encrypted = decodeBase64(encoded, "account", code);
  if (
    encrypted.subarray(0, 3).toString("latin1") !== "v10" ||
    encrypted.length < 3 + 12 + 16
  ) {
    throw probeError("account", code);
  }
  try {
    const decipher = createDecipheriv(
      "aes-256-gcm",
      key,
      encrypted.subarray(3, 15),
    );
    decipher.setAuthTag(encrypted.subarray(-16));
    return Buffer.concat([
      decipher.update(encrypted.subarray(15, -16)),
      decipher.final(),
    ]).toString("utf8");
  } catch {
    throw probeError("account", code);
  }
}

function readGatewayBase(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw probeError("ensure_sandbox", "GATEWAY_URL_INVALID");
  }
  const host = parsed.hostname.toLowerCase();
  const cursorApiHost =
    host === "cursor.sh" ||
    host === "cursor.com" ||
    host.endsWith(".cursor.sh") ||
    host.endsWith(".cursor.com");
  const cursorGatewayHost = cursorApiHost || host.endsWith(".cursorvm.com");
  if (
    parsed.protocol !== "https:" ||
    !cursorGatewayHost ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw probeError("ensure_sandbox", "GATEWAY_URL_REJECTED");
  }
  return parsed.toString().replace(/\/$/, "");
}

function normalizeGatewayHeaders(body) {
  const raw = body?.gatewayHeaders ?? body?.gateway_headers;
  if (raw != null && (typeof raw !== "object" || Array.isArray(raw))) {
    throw probeError("ensure_sandbox", "GATEWAY_HEADERS_INVALID");
  }
  const headers = {};
  if (raw) {
    for (const [key, value] of Object.entries(raw)) {
      const name = String(key).toLowerCase();
      const text = String(value ?? "");
      if (
        !/^[!#$%&'*+\-.^_`|~0-9a-z]+$/.test(name) ||
        RESERVED_GATEWAY_HEADERS.has(name) ||
        /[\r\n\0]/.test(text) ||
        text.length > 8192
      ) {
        throw probeError("ensure_sandbox", "GATEWAY_HEADERS_INVALID");
      }
      if (text) headers[name] = text;
    }
  }
  if (Object.keys(headers).length) return headers;
  const networkToken =
    body?.anyrunNetworkToken ?? body?.anyrun_network_token ?? body?.networkToken;
  if (networkToken == null || networkToken === "") return {};
  const text = String(networkToken);
  if (/[\r\n\0]/.test(text) || text.length > 8192) {
    throw probeError("ensure_sandbox", "GATEWAY_HEADERS_INVALID");
  }
  return { "x-anyrun-network-token": text };
}

async function readResponseText(response, stage) {
  const contentLength = response.headers?.get?.("content-length");
  if (contentLength && /^\d+$/.test(contentLength) && Number(contentLength) > MAX_RESPONSE_BYTES) {
    throw probeError(stage, "RESPONSE_TOO_LARGE", response.status);
  }
  const reader = response.body?.getReader?.();
  if (!reader) {
    let bytes;
    try {
      bytes = Buffer.from(await response.arrayBuffer());
    } catch {
      throw probeError(stage, "RESPONSE_READ_FAILED", response.status);
    }
    if (bytes.length > MAX_RESPONSE_BYTES) {
      throw probeError(stage, "RESPONSE_TOO_LARGE", response.status);
    }
    return bytes.toString("utf8");
  }

  const chunks = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = Buffer.from(value);
      total += chunk.length;
      if (total > MAX_RESPONSE_BYTES) {
        try {
          await reader.cancel();
        } catch {
          // The safe error below is the only reported result.
        }
        throw probeError(stage, "RESPONSE_TOO_LARGE", response.status);
      }
      chunks.push(chunk);
    }
  } catch (error) {
    if (error instanceof ProbeError) throw error;
    throw probeError(stage, "RESPONSE_READ_FAILED", response.status);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function postJson(fetchImpl, url, headers, stage, body = {}) {
  let response;
  try {
    response = await fetchImpl(url, {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      headers,
      body: JSON.stringify(body),
    });
  } catch {
    throw probeError(stage, "REQUEST_FAILED");
  }
  const status = response?.status;
  if (!Number.isInteger(status)) throw probeError(stage, "INVALID_HTTP_RESPONSE");
  const diagnostics = !response.ok
    ? await readSafeResponseDiagnostics(response, stage)
    : undefined;
  if (
    response.redirected === true ||
    (response.url && response.url !== url) ||
    (status >= 300 && status < 400)
  ) {
    throw probeError(stage, "REDIRECT_REJECTED", status, diagnostics);
  }
  if (!response.ok) throw probeError(stage, "HTTP_STATUS", status, diagnostics);

  const text = await readResponseText(response, stage);
  try {
    return { status, body: JSON.parse(text) };
  } catch (error) {
    if (error instanceof ProbeError) throw error;
    throw probeError(stage, "INVALID_JSON", status);
  }
}

function unwrapAgents(body) {
  if (Array.isArray(body)) return body;
  if (Array.isArray(body?.agents)) return body.agents;
  if (body?.agent) return [body.agent];
  return [];
}

function countAgentKinds(body) {
  let botCount = 0;
  let groupCount = 0;
  for (const agent of unwrapAgents(body)) {
    if (!agent || typeof agent !== "object") continue;
    const id = agent.id || agent.agentId;
    if (typeof id !== "string" || !id) continue;
    const members = agent.memberIds || agent.memberAgentIds || [];
    const isGroup =
      agent.isGroup === true ||
      (agent.isGroup == null && Array.isArray(members) && members.length > 0);
    if (isGroup) groupCount += 1;
    else botCount += 1;
  }
  return { botCount, groupCount };
}

function ensureRoute(body) {
  const gatewayUrl = body?.gatewayUrl ?? body?.gateway_url;
  const gatewayToken = body?.gatewayToken ?? body?.gateway_token;
  if (typeof gatewayToken !== "string" || !gatewayToken) {
    throw probeError("ensure_sandbox", "ROUTE_TOKEN_MISSING");
  }
  return {
    gatewayUrl: readGatewayBase(gatewayUrl),
    gatewayToken,
    gatewayHeaders: normalizeGatewayHeaders(body),
  };
}

export async function runListProbe({
  profileReader,
  dpapiUnprotect,
  fetchImpl = globalThis.fetch,
  onAuthenticatedList,
  expectedAccountIdentity,
  expectedTeamIdentity,
  allowSendPrompt = true,
} = {}) {
  if (!profileReader || typeof profileReader.read !== "function") {
    throw probeError("profile", "PROFILE_READER_REQUIRED");
  }
  if (typeof dpapiUnprotect !== "function") {
    throw probeError("dpapi", "DPAPI_ADAPTER_REQUIRED");
  }
  if (typeof fetchImpl !== "function") {
    throw probeError("transport", "FETCH_UNAVAILABLE");
  }
  if (onAuthenticatedList !== undefined && typeof onAuthenticatedList !== "function") {
    throw probeError("transport", "ACTION_ADAPTER_INVALID");
  }
  if (expectedAccountIdentity !== undefined &&
    (typeof expectedAccountIdentity !== "string" || !expectedAccountIdentity)) {
    throw probeError("account", "EXPECTED_ACCOUNT_INVALID");
  }
  if (expectedTeamIdentity !== undefined &&
    (typeof expectedTeamIdentity !== "string" || !expectedTeamIdentity)) {
    throw probeError("account", "EXPECTED_TEAM_INVALID");
  }
  if (typeof allowSendPrompt !== "boolean") {
    throw probeError("transport", "SEND_POLICY_INVALID");
  }

  const appVersion = readAppVersion(profileReader);
  const pinned = readAccountPin(profileReader);
  if (expectedAccountIdentity !== undefined &&
    pinned.activeIdentity !== expectedAccountIdentity) {
    throw probeError("account", "EXPECTED_ACCOUNT_MISMATCH");
  }
  readAppVersion(profileReader);
  const wrappedKey = readDpapiWrappedKey(profileReader);
  let storageKey;
  try {
    storageKey = dpapiUnprotect(wrappedKey);
  } catch (error) {
    if (error instanceof ProbeError) throw error;
    throw probeError("dpapi", "DPAPI_UNPROTECT_FAILED");
  }
  if (!Buffer.isBuffer(storageKey) || storageKey.length !== 32) {
    throw probeError("dpapi", "DPAPI_KEY_INVALID");
  }

  const teamId =
    pinned.teamCiphertext === null
      ? undefined
      : decryptV10Secret(
          pinned.teamCiphertext,
          storageKey,
          "SELECTED_TEAM_DECRYPT_FAILED",
        );
  if (
    teamId !== undefined &&
    (!/^[1-9][0-9]*$/.test(teamId) || !Number.isSafeInteger(Number(teamId)))
  ) {
    throw probeError("account", "SELECTED_TEAM_INVALID");
  }
  const teamIdentity = teamId ?? NO_SELECTED_TEAM;
  if (expectedTeamIdentity !== undefined && teamIdentity !== expectedTeamIdentity) {
    throw probeError("account", "EXPECTED_TEAM_MISMATCH");
  }

  // Confirm account and selected team before decrypting the access token.
  assertAccountPin(profileReader, pinned);
  const accessToken = decryptV10Secret(
    pinned.accessTokenCiphertext,
    storageKey,
    "ACCESS_TOKEN_DECRYPT_FAILED",
  );
  if (!accessToken.trim()) throw probeError("account", "ACCESS_TOKEN_EMPTY");

  assertAccountPin(profileReader, pinned);
  const ensureHeaders = {
    "content-type": "application/json",
    "connect-protocol-version": "1",
    authorization: `Bearer ${accessToken}`,
    "x-cursor-client-type": "sand",
    "x-cursor-client-version": appVersion,
    "x-sand-box-namespace": "prod",
    ...(teamId === undefined ? {} : { "x-cursor-team-id": teamId }),
  };
  const ensureResult = await postJson(
    fetchImpl,
    ENSURE_SANDBOX_URL,
    ensureHeaders,
    "ensure_sandbox",
  );
  assertAccountPin(profileReader, pinned);
  const route = ensureRoute(ensureResult.body);

  assertAccountPin(profileReader, pinned);
  const listUrl = `${route.gatewayUrl}/api/listAgents`;
  const listHeaders = {
    "content-type": "application/json",
    authorization: `Bearer ${route.gatewayToken}`,
    ...route.gatewayHeaders,
  };
  const listResult = await postJson(
    fetchImpl,
    listUrl,
    listHeaders,
    "list_agents",
  );
  assertAccountPin(profileReader, pinned);

  if (onAuthenticatedList) {
    await onAuthenticatedList(Object.freeze({
      accountIdentity: pinned.activeIdentity,
      teamIdentity,
      agents: listResult.body,
      async gatewayRequestDetailed(method, body, stage) {
        const permittedMethods = allowSendPrompt
          ? AUTHENTICATED_GATEWAY_METHODS
          : READ_ONLY_GATEWAY_METHODS;
        if (!permittedMethods.has(method)) {
          throw probeError("transport", "GATEWAY_METHOD_NOT_ALLOWED");
        }
        if (!body || typeof body !== "object" || Array.isArray(body)) {
          throw probeError("transport", "GATEWAY_REQUEST_INVALID");
        }
        if (typeof stage !== "string" || !/^[a-z_]{1,32}$/.test(stage)) {
          throw probeError("transport", "GATEWAY_STAGE_INVALID");
        }
        assertAccountPin(profileReader, pinned);
        const result = await postJson(
          fetchImpl,
          `${route.gatewayUrl}/api/${method}`,
          listHeaders,
          stage,
          body,
        );
        assertAccountPin(profileReader, pinned);
        return { body: result.body, httpStatus: result.status };
      },
      async gatewayRequest(method, body, stage) {
        const result = await this.gatewayRequestDetailed(method, body, stage);
        return result.body;
      },
    }));
  }

  return {
    ok: true,
    stage: "complete",
    httpStatus: {
      ensureSandbox: ensureResult.status,
      listAgents: listResult.status,
    },
    ...countAgentKinds(listResult.body),
  };
}

export async function runAuthenticatedListAction({ action, ...dependencies } = {}) {
  if (typeof action !== "function") {
    throw probeError("transport", "ACTION_ADAPTER_REQUIRED");
  }
  let result;
  await runListProbe({
    ...dependencies,
    onAuthenticatedList: async (session) => {
      result = await action(session);
    },
  });
  return result;
}

function safeFailure(error) {
  if (error instanceof ProbeError) {
    return {
      ok: false,
      stage: error.stage,
      code: error.code,
      ...(error.status === undefined ? {} : { status: error.status }),
      ...(safeRemoteErrorCode(error.errorCode) === undefined
        ? {}
        : { errorCode: safeRemoteErrorCode(error.errorCode) }),
      ...(safeCorrelationId(error.requestId) === undefined
        ? {}
        : { requestId: safeCorrelationId(error.requestId) }),
    };
  }
  return { ok: false, stage: "probe", code: "INTERNAL_ERROR" };
}

export async function main({
  argv = process.argv.slice(2),
  dependencies,
  stdout = process.stdout,
} = {}) {
  if (argv.length !== 1 || argv[0] !== "--execute") {
    stdout.write(
      `${JSON.stringify({ ok: false, stage: "arguments", code: "EXECUTE_FLAG_REQUIRED" })}\n`,
    );
    return 2;
  }

  try {
    const deps =
      dependencies ??
      (process.platform === "win32"
        ? {
            profileReader: createWindowsProfileReader(),
            dpapiUnprotect: createWindowsDpapiUnprotect(),
            fetchImpl: globalThis.fetch,
          }
        : null);
    if (!deps) throw probeError("profile", "WINDOWS_REQUIRED");
    const result = await runListProbe(deps);
    stdout.write(`${JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    stdout.write(`${JSON.stringify(safeFailure(error))}\n`);
    return 1;
  }
}

if (
  process.argv[1] &&
  pathToFileURL(resolve(process.argv[1])).href === import.meta.url
) {
  process.exitCode = await main();
}
