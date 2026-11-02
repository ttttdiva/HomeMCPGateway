import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { resolve as resolvePath } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  createWindowsDpapiUnprotect,
  createWindowsProfileReader,
  runAuthenticatedListAction,
} from "./grok_bot_list_probe.mjs";


export const OPERATION_ID = "GROK-MARKER-20261024-1652";
export const PROMPT =
  "Reply with exactly GROK-HARNESS-20261024-1652-OK. " +
  "Do not use tools, files, commands, or external services.";
export const EXPECTED_REPLY = "GROK-HARNESS-20261024-1652-OK";
const PROMPT_SHA256 = createHash("sha256").update(PROMPT, "utf8").digest("hex");

const POLL_ATTEMPTS = 4;
const POLL_DELAY_MS = 1_000;
const TRANSCRIPT_LIMIT = 40;
const THREAD_ENTRY_LIMIT = 200;
const NONCE_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const PROFILE_ROOT = resolvePath(fileURLToPath(new URL("..", import.meta.url)));
const PYTHON_EXE = resolvePath(PROFILE_ROOT, ".venv", "Scripts", "python.exe");


class SmokeError extends Error {
  constructor(stage, code, status) {
    super(code);
    this.name = "SmokeError";
    this.stage = stage;
    this.code = code;
    if (Number.isInteger(status) && status >= 100 && status <= 599) {
      this.status = status;
    }
  }
}

function smokeError(stage, code, status) {
  return new SmokeError(stage, code, status);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nonEmptyId(value) {
  return typeof value === "string" && value.length > 0 && value.trim().length > 0 &&
    value.length <= 1024 && !/[\x00-\x1f\x7f]/.test(value);
}

function unpackAgents(body) {
  if (Array.isArray(body)) return body;
  if (!isRecord(body)) throw smokeError("resolve", "AGENT_LIST_SCHEMA_INVALID");
  const hasAgents = Object.hasOwn(body, "agents");
  const hasAgent = Object.hasOwn(body, "agent");
  if (hasAgents && hasAgent) throw smokeError("resolve", "AGENT_LIST_SCHEMA_INVALID");
  if (hasAgents) {
    if (Array.isArray(body.agents)) return body.agents;
    throw smokeError("resolve", "AGENT_LIST_SCHEMA_INVALID");
  }
  if (hasAgent && isRecord(body.agent)) return [body.agent];
  throw smokeError("resolve", "AGENT_LIST_SCHEMA_INVALID");
}

function normalizeAgent(agent) {
  if (!isRecord(agent)) throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  const hasId = Object.hasOwn(agent, "id");
  const hasAgentId = Object.hasOwn(agent, "agentId");
  const id = hasId ? agent.id : agent.agentId;
  if (!nonEmptyId(id) || (hasId && hasAgentId && agent.id !== agent.agentId)) {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  if (typeof agent.name !== "string") {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  if (agent.isGroup != null && typeof agent.isGroup !== "boolean") {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  if (agent.memberIds != null && !Array.isArray(agent.memberIds)) {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  if (agent.memberAgentIds != null && !Array.isArray(agent.memberAgentIds)) {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  const members = agent.memberIds ?? agent.memberAgentIds ?? [];
  if (!members.every((member) => nonEmptyId(member))) {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  if (
    Array.isArray(agent.memberIds) &&
    Array.isArray(agent.memberAgentIds) &&
    JSON.stringify(agent.memberIds) !== JSON.stringify(agent.memberAgentIds)
  ) {
    throw smokeError("resolve", "AGENT_SCHEMA_INVALID");
  }
  return {
    id,
    name: agent.name,
    isGroup: agent.isGroup === true || (agent.isGroup == null && members.length > 0),
  };
}

export function resolveGrokBot(agentsBody) {
  const agents = unpackAgents(agentsBody).map(normalizeAgent);
  const matches = agents.filter((agent) => agent.name === "GrokBot" && !agent.isGroup);
  if (matches.length === 0) throw smokeError("resolve", "TARGET_NOT_FOUND");
  if (matches.length !== 1) throw smokeError("resolve", "TARGET_AMBIGUOUS");
  return matches[0];
}

export function createLedgerCall({
  execFileSyncImpl = execFileSync,
  ledgerPath,
} = {}) {
  return (payload) => {
    let output;
    try {
      const safeEnv = {};
      for (const name of ["SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"]) {
        if (typeof process.env[name] === "string") safeEnv[name] = process.env[name];
      }
      const adapterArgs = ["-m", "home_mcp_gateway._grok_bot_marker_ledger", "--ledger-adapter"];
      if (ledgerPath !== undefined) {
        adapterArgs.push("--ledger-path", resolvePath(String(ledgerPath)));
      }
      output = execFileSyncImpl(
        PYTHON_EXE,
        adapterArgs,
        {
          cwd: PROFILE_ROOT,
          input: Buffer.from(JSON.stringify(payload), "utf8"),
          encoding: "utf8",
          windowsHide: true,
          timeout: 5_000,
          maxBuffer: 64 * 1024,
          stdio: ["pipe", "pipe", "pipe"],
          env: safeEnv,
        },
      );
    } catch (error) {
      if (error?.stdout == null || !String(error.stdout).trim()) {
        throw smokeError("ledger", "LEDGER_ADAPTER_FAILED");
      }
      output = error.stdout;
    }
    let result;
    try {
      result = JSON.parse(String(output).trim());
    } catch {
      throw smokeError("ledger", "LEDGER_ADAPTER_INVALID_RESPONSE");
    }
    if (!isRecord(result) || result.ok !== true) {
      const stage = isRecord(result) && typeof result.stage === "string" ? result.stage : "ledger";
      const code = isRecord(result) && typeof result.code === "string" ? result.code : "LEDGER_FAILURE";
      throw smokeError(stage, code);
    }
    return result;
  };
}

function safeError(error, fallbackStage, fallbackCode) {
  return {
    stage: typeof error?.stage === "string" ? error.stage : fallbackStage,
    code: typeof error?.code === "string" ? error.code : fallbackCode,
    ...(Number.isInteger(error?.status) ? { status: error.status } : {}),
  };
}

function isRejectedSend(error) {
  return Number.isInteger(error?.status) && error.status >= 300 && error.status < 500;
}

function receiptMessageId(body) {
  if (!isRecord(body) || typeof body.messageId !== "string") return null;
  return body.messageId.trim() ? body.messageId : null;
}

export function tailEntries(body) {
  if (!isRecord(body) || !Array.isArray(body.entries) || body.entries.length > TRANSCRIPT_LIMIT) {
    throw smokeError("transcript", "TRANSCRIPT_SCHEMA_INVALID");
  }
  if (!body.entries.every((entry) => isRecord(entry) &&
    (entry.clientNonce == null || typeof entry.clientNonce === "string"))) {
    throw smokeError("transcript", "TRANSCRIPT_SCHEMA_INVALID");
  }
  return body.entries;
}

export function findRequest(entries, clientNonce) {
  const matches = entries.filter((entry) => entry.clientNonce === clientNonce);
  if (matches.length > 1) throw smokeError("transcript", "REQUEST_CORRELATION_AMBIGUOUS");
  if (matches.length === 0) return null;
  const request = matches[0];
  const observedRequestKind = request.kind === "message" && request.role === "user";
  const legacyRequestKind = request.kind === "user" &&
    (request.role == null || request.role === "user");
  if (
    (!observedRequestKind && !legacyRequestKind) ||
    (request.sender != null && request.sender !== "user")
  ) {
    throw smokeError("transcript", "REQUEST_SCHEMA_INVALID");
  }
  if (Object.hasOwn(request, "id") && !nonEmptyId(request.id)) {
    throw smokeError("transcript", "REQUEST_SCHEMA_INVALID");
  }
  if (Object.hasOwn(request, "requestId") && !nonEmptyId(request.requestId)) {
    throw smokeError("transcript", "REQUEST_SCHEMA_INVALID");
  }
  if (Object.hasOwn(request, "rootId") && !nonEmptyId(request.rootId)) {
    throw smokeError("transcript", "REQUEST_SCHEMA_INVALID");
  }
  if (!nonEmptyId(request.id)) {
    return {
      messageIdObserved: false,
      requestIdObserved: false,
      rootIdObserved: false,
    };
  }
  return {
    messageIdObserved: true,
    requestIdObserved: nonEmptyId(request.requestId),
    rootIdObserved: true,
    messageId: request.id,
    requestId: request.requestId,
    rootId: Object.hasOwn(request, "rootId") ? request.rootId : request.id,
  };
}

export function threadEntries(body, rootId) {
  if (!isRecord(body) || !Array.isArray(body.entries) || body.entries.length > THREAD_ENTRY_LIMIT) {
    throw smokeError("thread", "THREAD_SCHEMA_INVALID");
  }
  if (body.rootId != null && body.rootId !== rootId) {
    throw smokeError("thread", "THREAD_ROOT_MISMATCH");
  }
  if (!body.entries.every((entry) => isRecord(entry) &&
    (entry.clientNonce == null || typeof entry.clientNonce === "string"))) {
    throw smokeError("thread", "THREAD_SCHEMA_INVALID");
  }
  return body.entries.filter((entry) => {
    if (entry.rootId == null) return true;
    if (!nonEmptyId(entry.rootId)) throw smokeError("thread", "THREAD_SCHEMA_INVALID");
    return entry.rootId === rootId;
  });
}

function entryText(entry) {
  if (isRecord(entry.message)) {
    if (entry.message.type !== "text" || typeof entry.message.content !== "string") {
      return null;
    }
    return entry.message.content;
  }
  if (typeof entry.text === "string") return entry.text;
  if (typeof entry.content === "string") return entry.content;
  if (Array.isArray(entry.content)) {
    const chunks = [];
    for (const part of entry.content) {
      if (typeof part === "string") chunks.push(part);
      else if (isRecord(part) && typeof part.text === "string") chunks.push(part.text);
      else if (isRecord(part) && typeof part.content === "string") chunks.push(part.content);
    }
    return chunks.join("\n");
  }
  return null;
}

export function isReply(entry, request, clientNonce) {
  if (entry.id === request.messageId) return false;
  if (entry.clientNonce != null && entry.clientNonce !== clientNonce) return false;
  if (!nonEmptyId(entry.requestId) || entry.requestId !== request.requestId) return false;
  if (entry.rootId != null &&
    (!nonEmptyId(entry.rootId) || entry.rootId !== request.rootId)) return false;
  if (entry.kind !== "send-message" || entry.role === "user" ||
    entry.sender === "user" || entry.kind === "user") return false;
  if (entry.role != null && !["assistant", "bot"].includes(entry.role)) return false;
  if (entry.sender != null && !["assistant", "bot"].includes(entry.sender)) return false;
  return true;
}

function hasExpectedReplyInTail(entries, request, clientNonce) {
  return entries.some((entry) =>
    isReply(entry, request, clientNonce) && entryText(entry) === EXPECTED_REPLY,
  );
}

function hasExpectedReplyInThread(body, request, clientNonce) {
  return threadEntries(body, request.rootId).some((entry) =>
    isReply(entry, request, clientNonce) && entryText(entry) === EXPECTED_REPLY,
  );
}

function outcome({
  deliveryStatus,
  replyStatus,
  sendAttempts,
  reconciliationReads,
  error,
}) {
  return {
    ok: deliveryStatus === "accepted" && replyStatus === "observed",
    operation_id: OPERATION_ID,
    send_attempts: sendAttempts,
    delivery_status: deliveryStatus,
    reply_status: replyStatus,
    reconciliation_reads: reconciliationReads,
    remote_terminal: "unsupported",
    remote_terminal_supported: false,
    ...(error ? error : {}),
  };
}

async function waitBeforeRetry(sleepImpl, attempt) {
  if (attempt + 1 < POLL_ATTEMPTS) await sleepImpl(POLL_DELAY_MS);
}

async function reconcile({
  session,
  target,
  clientNonce,
  ledgerCall,
  confirmRequest,
  initialDelivery,
  sendAttempts,
  sleepImpl,
}) {
  let deliveryStatus = initialDelivery;
  let replyStatus = "pending";
  let reconciliationReads = 0;
  let lastError;
  let acceptancePersisted = initialDelivery === "accepted";

  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    let request;
    let entries;
    try {
      reconciliationReads += 1;
      const tail = await session.gatewayRequest(
        "getAgentTranscriptTail",
        { id: target.id, limit: TRANSCRIPT_LIMIT },
        "transcript",
      );
      entries = tailEntries(tail);
      request = findRequest(entries, clientNonce);
    } catch (error) {
      lastError = safeError(error, "transcript", "TRANSCRIPT_READ_FAILED");
      replyStatus = "unknown";
      break;
    }

    if (!request || !request.messageIdObserved || !request.rootIdObserved) {
      replyStatus = "pending";
      await waitBeforeRetry(sleepImpl, attempt);
      continue;
    }

    deliveryStatus = "accepted";
    if (!acceptancePersisted) {
      try {
        if (confirmRequest) confirmRequest(request);
        else ledgerCall({ action: "state", operation_id: OPERATION_ID, state: "accepted" });
        acceptancePersisted = true;
      } catch (error) {
        lastError = safeError(error, "ledger", "LEDGER_STATE_WRITE_FAILED");
      }
    }

    if (request.requestIdObserved &&
      hasExpectedReplyInTail(entries, request, clientNonce)) {
      replyStatus = "observed";
      return outcome({
        deliveryStatus,
        replyStatus,
        sendAttempts,
        reconciliationReads,
        error: lastError,
      });
    }

    if (!request.requestIdObserved) {
      replyStatus = "pending";
      await waitBeforeRetry(sleepImpl, attempt);
      continue;
    }

    try {
      reconciliationReads += 1;
      const thread = await session.gatewayRequest(
        "getAgentThread",
        { id: target.id, rootId: request.rootId },
        "thread",
      );
      if (hasExpectedReplyInThread(thread, request, clientNonce)) {
        replyStatus = "observed";
        return outcome({
          deliveryStatus,
          replyStatus,
          sendAttempts,
          reconciliationReads,
          error: lastError,
        });
      }
      replyStatus = "pending";
    } catch (error) {
      lastError = safeError(error, "thread", "THREAD_READ_FAILED");
      replyStatus = "unknown";
      break;
    }
    await waitBeforeRetry(sleepImpl, attempt);
  }

  return outcome({
    deliveryStatus,
    replyStatus,
    sendAttempts,
    reconciliationReads,
    error: lastError,
  });
}

export async function runGrokBotMarkerSmoke({
  dependencies,
  ledgerCall = createLedgerCall(),
  authenticatedAction = runAuthenticatedListAction,
  sleepImpl = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
} = {}) {
  if (typeof ledgerCall !== "function") throw smokeError("ledger", "LEDGER_ADAPTER_REQUIRED");
  if (typeof sleepImpl !== "function") throw smokeError("poll", "SLEEP_ADAPTER_INVALID");
  return authenticatedAction({
    ...dependencies,
    action: async (session) => {
      const target = resolveGrokBot(session.agents);
      const admission = ledgerCall({
        action: "begin",
        operation_id: OPERATION_ID,
        account_identity: session.accountIdentity,
        team_identity: session.teamIdentity,
        target_identity: target.id,
        prompt: PROMPT,
      });
      if (
        !isRecord(admission) || admission.ok !== true ||
        typeof admission.created !== "boolean" ||
        typeof admission.client_nonce !== "string" ||
        !NONCE_PATTERN.test(admission.client_nonce) ||
        typeof admission.transport_state !== "string"
      ) {
        throw smokeError("ledger", "LEDGER_ADMISSION_INVALID");
      }

      let sendAttempts = 0;
      let deliveryStatus = admission.transport_state === "accepted"
        ? "accepted"
        : admission.transport_state === "rejected"
          ? "rejected"
          : "unknown";
      let sendError;

      if (admission.created) {
        sendAttempts = 1;
        let receipt;
        try {
          receipt = await session.gatewayRequest(
            "sendPrompt",
            {
              agentId: target.id,
              prompt: PROMPT,
              clientNonce: admission.client_nonce,
            },
            "send_prompt",
          );
        } catch (error) {
          deliveryStatus = isRejectedSend(error) ? "rejected" : "unknown";
          sendError = safeError(error, "send_prompt", "SEND_FAILED");
          try {
            ledgerCall({
              action: "state",
              operation_id: OPERATION_ID,
              state: deliveryStatus,
            });
          } catch {
            sendError = { stage: "ledger", code: "LEDGER_STATE_WRITE_FAILED" };
          }
          if (deliveryStatus === "rejected") {
            return outcome({
              deliveryStatus,
              replyStatus: "unknown",
              sendAttempts,
              reconciliationReads: 0,
              error: sendError,
            });
          }
        }

        if (!sendError) {
          const receiptObserved = receiptMessageId(receipt) !== null;
          deliveryStatus = receiptObserved ? "accepted" : "unknown";
          try {
            ledgerCall({
              action: "state",
              operation_id: OPERATION_ID,
              state: deliveryStatus,
            });
          } catch {
            sendError = { stage: "ledger", code: "LEDGER_STATE_WRITE_FAILED" };
          }
        }
      }

      const result = await reconcile({
        session,
        target,
        clientNonce: admission.client_nonce,
        ledgerCall,
        initialDelivery: deliveryStatus,
        sendAttempts,
        sleepImpl,
      });
      if (sendError && !result.stage) {
        return { ...result, ...sendError };
      }
      return result;
    },
  });
}

function validateReconciliationRecord(record) {
  if (
    !isRecord(record) ||
    record.operation_id !== OPERATION_ID ||
    !_RECORD_STATES.has(record.transport_state) ||
    record.prompt_sha256 !== PROMPT_SHA256 ||
    !nonEmptyId(record.account_identity) ||
    !nonEmptyId(record.team_identity) ||
    !nonEmptyId(record.target_identity) ||
    typeof record.client_nonce !== "string" ||
    !NONCE_PATTERN.test(record.client_nonce)
  ) {
    throw smokeError("ledger", "OPERATION_RECORD_INVALID");
  }
  return record;
}

const _RECORD_STATES = new Set(["prepared", "sending", "accepted", "unknown", "rejected"]);

export async function runGrokBotMarkerReconciliation({
  record,
  dependencies,
  ledgerCall = createLedgerCall(),
  authenticatedAction = runAuthenticatedListAction,
  sleepImpl = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
} = {}) {
  const pinned = validateReconciliationRecord(record);
  if (typeof ledgerCall !== "function") throw smokeError("ledger", "LEDGER_ADAPTER_REQUIRED");
  if (typeof sleepImpl !== "function") throw smokeError("poll", "SLEEP_ADAPTER_INVALID");

  const initialDelivery = pinned.transport_state === "accepted"
    ? "accepted"
    : pinned.transport_state === "rejected"
      ? "rejected"
      : "unknown";
  return authenticatedAction({
    ...dependencies,
    expectedAccountIdentity: pinned.account_identity,
    expectedTeamIdentity: pinned.team_identity,
    allowSendPrompt: false,
    action: (session) => reconcile({
      session,
      target: { id: pinned.target_identity },
      clientNonce: pinned.client_nonce,
      ledgerCall,
      confirmRequest: (request) => {
        if (pinned.transport_state === "accepted") return;
        ledgerCall({
          action: "confirm",
          operation_id: pinned.operation_id,
          account_identity: pinned.account_identity,
          team_identity: pinned.team_identity,
          target_identity: pinned.target_identity,
          client_nonce: pinned.client_nonce,
          prompt_sha256: pinned.prompt_sha256,
          request_message_id: request.messageId,
          request_id: request.requestIdObserved ? request.requestId : null,
          root_id: request.rootId,
        });
      },
      initialDelivery,
      sendAttempts: 0,
      sleepImpl,
    }),
  });
}

function safeFailure(error) {
  if (error instanceof SmokeError || (error && typeof error.stage === "string" && typeof error.code === "string")) {
    return {
      ok: false,
      operation_id: OPERATION_ID,
      stage: error.stage,
      code: error.code,
      ...(Number.isInteger(error.status) ? { status: error.status } : {}),
    };
  }
  return { ok: false, operation_id: OPERATION_ID, stage: "smoke", code: "INTERNAL_ERROR" };
}

export async function main({
  argv = process.argv.slice(2),
  dependencies,
  stdout = process.stdout,
  ledgerCall,
} = {}) {
  if (argv.length !== 1 || !["--execute", "--reconcile"].includes(argv[0])) {
    stdout.write(`${JSON.stringify({ ok: false, stage: "arguments", code: "EXECUTE_OR_RECONCILE_FLAG_REQUIRED" })}\n`);
    return 2;
  }

  try {
    const isReconciliation = argv[0] === "--reconcile";
    const adapter = ledgerCall ?? createLedgerCall();
    const record = isReconciliation
      ? adapter({ action: "read", operation_id: OPERATION_ID })
      : null;
    if (isReconciliation) validateReconciliationRecord(record);
    const deps = dependencies ?? (process.platform === "win32"
      ? {
          profileReader: createWindowsProfileReader(),
          dpapiUnprotect: createWindowsDpapiUnprotect(),
          fetchImpl: globalThis.fetch,
        }
      : null);
    if (!deps) throw smokeError("profile", "WINDOWS_REQUIRED");
    const result = isReconciliation
      ? await runGrokBotMarkerReconciliation({
          record,
          dependencies: deps,
          ledgerCall: adapter,
        })
      : await runGrokBotMarkerSmoke({
          dependencies: deps,
          ledgerCall: adapter,
        });
    stdout.write(`${JSON.stringify(result)}\n`);
    return result.ok ? 0 : 1;
  } catch (error) {
    stdout.write(`${JSON.stringify(safeFailure(error))}\n`);
    return 1;
  }
}

if (
  process.argv[1] &&
  pathToFileURL(resolvePath(process.argv[1])).href === import.meta.url
) {
  process.exitCode = await main();
}
