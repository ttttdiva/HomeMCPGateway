import assert from "node:assert/strict";
import { createCipheriv, createHash } from "node:crypto";
import { createServer } from "node:http";
import { Readable } from "node:stream";
import test from "node:test";

import {
  OPERATION_ID as MARKER_OPERATION_ID,
  PROMPT as MARKER_PROMPT,
} from "../scripts/grok_bot_marker_smoke.mjs";
import {
  main,
  parseTaskEnvelope,
  runGrokBotTask,
  validateTaskEnvelope,
} from "../scripts/grok_bot_task.mjs";
import { runAuthenticatedListAction } from "../scripts/grok_bot_list_probe.mjs";


const ENSURE_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox";
const STORAGE_KEY = Buffer.alloc(32, 0x45);
const WRAPPED_KEY = Buffer.from("synthetic-wrapped-dpapi-key");
const ACCOUNT = "synthetic-approved-account";
const TEAM = "2468";
const TARGET = "synthetic-approved-bot-id";
const TASK_ID = "GROK-TASK-SYNTHETIC-001";
const TASK_PROMPT = "Review this synthetic fixture for a missing null check.";
const CLIENT_NONCE = "6b6356d7-79de-43ac-a31c-373fd7ec190a";
const MARKER_HASH = createHash("sha256").update(MARKER_PROMPT, "utf8").digest("hex");
let cipherCounter = 0;


function encryptV10(text) {
  const nonce = Buffer.alloc(12);
  nonce.writeUInt32BE(++cipherCounter, 8);
  const cipher = createCipheriv("aes-256-gcm", STORAGE_KEY, nonce);
  const encrypted = Buffer.concat([cipher.update(text, "utf8"), cipher.final()]);
  return Buffer.concat([Buffer.from("v10"), nonce, encrypted, cipher.getAuthTag()]).toString("base64");
}

function syntheticProfile({ accountIdentity = ACCOUNT, teamIdentity = TEAM } = {}) {
  const secrets = {
    version: 1,
    "cursor-accounts": JSON.stringify({
      active: accountIdentity,
      accounts: {
        [accountIdentity]: {
          "cursor-access-token": encryptV10("synthetic-access-token"),
          "cursor-selected-team-id": encryptV10(teamIdentity),
        },
      },
    }),
  };
  let dpapiCalls = 0;
  return {
    reader: {
      read(name) {
        if (name === "sand-secrets.json") return JSON.stringify(secrets);
        if (name === "desktop-status.json") return JSON.stringify({ signedIn: true, appVersion: "0.75.5" });
        if (name === "Local State") {
          return JSON.stringify({
            os_crypt: { encrypted_key: Buffer.concat([Buffer.from("DPAPI"), WRAPPED_KEY]).toString("base64") },
          });
        }
        throw new Error("unexpected synthetic profile read");
      },
    },
    dpapiUnprotect(wrapped) {
      dpapiCalls += 1;
      assert.deepEqual(wrapped, WRAPPED_KEY);
      return Buffer.from(STORAGE_KEY);
    },
    get dpapiCalls() { return dpapiCalls; },
  };
}

function httpResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function observedUser(clientNonce, overrides = {}) {
  return {
    kind: "message",
    role: "user",
    id: "synthetic-user-message-id",
    content: "synthetic prompt omitted from result",
    clientNonce,
    requestId: "synthetic-request-run-id",
    rootId: "synthetic-user-message-id",
    ...overrides,
  };
}

function observedReply(content = "Reviewed the fixture; the null check is missing.", overrides = {}) {
  return {
    kind: "send-message",
    id: "synthetic-reply-id",
    requestId: "synthetic-request-run-id",
    message: { type: "text", content },
    ...overrides,
  };
}

function makeFetch({
  agents = [{ id: TARGET, name: "GrokBot" }],
  sendBody = { messageId: "synthetic-send-receipt-id" },
  transcript,
  thread = { entries: [] },
  sendFailure,
  sendStatus,
  transcriptStatus,
  threadStatus,
} = {}) {
  const calls = [];
  let sentNonce = CLIENT_NONCE;
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    const body = options.body ? JSON.parse(options.body) : {};
    calls.push({ url, method: parsed.pathname, body, options });
    if (url === ENSURE_URL) {
      return httpResponse({
        gatewayUrl: "https://box-123.us12.cursorvm.com",
        gatewayToken: "synthetic-route-token",
      });
    }
    if (parsed.pathname === "/api/listAgents") return httpResponse({ agents });
    if (parsed.pathname === "/api/sendPrompt") {
      sentNonce = body.clientNonce;
      if (sendFailure) throw sendFailure;
      if (sendStatus !== undefined) return httpResponse({}, sendStatus);
      return httpResponse(sendBody);
    }
    if (parsed.pathname === "/api/getAgentTranscriptTail") {
      if (transcriptStatus !== undefined) return httpResponse({}, transcriptStatus);
      const entries = typeof transcript === "function"
        ? transcript(sentNonce)
        : transcript ?? [observedUser(sentNonce), observedReply()];
      return httpResponse({ entries });
    }
    if (parsed.pathname === "/api/getAgentThread") {
      if (threadStatus !== undefined) return httpResponse({}, threadStatus);
      return httpResponse(thread);
    }
    throw new Error("synthetic request outside the approved gateway methods");
  };
  return { fetchImpl, calls };
}

async function withLocalFakeServer(handler, callback) {
  const server = createServer(handler);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.equal(typeof address, "object");
  const origin = `http://127.0.0.1:${address.port}`;
  try {
    return await callback({
      origin,
      fetchImpl: (url, options) => {
        const requested = new URL(url);
        // The wrapper maps every dependency request to the local server.  The
        // test therefore cannot accidentally contact the real provider.
        return fetch(`${origin}${requested.pathname}${requested.search}`, options).then((response) => {
          // postJson verifies that the no-redirect response URL is unchanged;
          // preserve the logical URL after the local mapping.
          Object.defineProperty(response, "url", { value: String(url) });
          return response;
        });
      },
    });
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

function syntheticDependencies(profile, fetchImpl) {
  return {
    profileReader: profile.reader,
    dpapiUnprotect: profile.dpapiUnprotect,
    fetchImpl,
  };
}

function makeLedger({ anchor = true, initialRecord } = {}) {
  const rows = new Map(initialRecord ? [[initialRecord.operation_id, { ...initialRecord }]] : []);
  const actions = [];
  const marker = anchor ? {
    ok: true,
    operation_id: MARKER_OPERATION_ID,
    account_identity: ACCOUNT,
    team_identity: TEAM,
    target_identity: TARGET,
    prompt_sha256: MARKER_HASH,
    client_nonce: "a73f3d51-bb3c-4c01-9d9d-09bdd95c4ae0",
    transport_state: "accepted",
  } : null;
  const ledgerCall = (payload) => {
    actions.push(payload);
    if (payload.action === "anchor") {
      if (!marker) throw Object.assign(new Error("missing synthetic anchor"), {
        stage: "ledger", code: "OPERATION_NOT_FOUND",
      });
      return { ...marker };
    }
    if (payload.action === "task_read") {
      const record = rows.get(payload.operation_id);
      if (!record) throw Object.assign(new Error("missing synthetic operation"), {
        stage: "ledger", code: "OPERATION_NOT_FOUND",
      });
      return { ok: true, ...record };
    }
    if (payload.action === "task_begin") {
      if (!marker || payload.account_identity !== marker.account_identity ||
        payload.team_identity !== marker.team_identity || payload.target_identity !== marker.target_identity) {
        throw Object.assign(new Error("synthetic anchor mismatch"), {
          stage: "ledger", code: "MARKER_ANCHOR_MISMATCH",
        });
      }
      const existing = rows.get(payload.operation_id);
      const promptHash = createHash("sha256").update(payload.prompt, "utf8").digest("hex");
      if (existing) {
        if (existing.account_identity !== payload.account_identity ||
          existing.team_identity !== payload.team_identity ||
          existing.target_identity !== payload.target_identity || existing.prompt_sha256 !== promptHash) {
          throw Object.assign(new Error("synthetic operation conflict"), {
            stage: "ledger", code: "OPERATION_CONFLICT",
          });
        }
        return { ok: true, created: false, ...existing };
      }
      const record = {
        operation_id: payload.operation_id,
        account_identity: payload.account_identity,
        team_identity: payload.team_identity,
        target_identity: payload.target_identity,
        prompt_sha256: promptHash,
        client_nonce: CLIENT_NONCE,
        transport_state: "sending",
      };
      rows.set(payload.operation_id, record);
      return { ok: true, created: true, ...record };
    }
    if (payload.action === "task_state") {
      const record = rows.get(payload.operation_id);
      if (!record) throw Object.assign(new Error("missing synthetic operation"), {
        stage: "ledger", code: "OPERATION_NOT_FOUND",
      });
      if (Object.hasOwn(payload, "request_message_id") || Object.hasOwn(payload, "request_id") ||
        Object.hasOwn(payload, "root_id")) {
        for (const key of [
          "account_identity", "team_identity", "target_identity", "client_nonce", "prompt_sha256",
          "request_message_id", "request_id", "root_id",
        ]) {
          if (!Object.hasOwn(payload, key) || typeof payload[key] !== "string" || !payload[key].trim()) {
            throw Object.assign(new Error("invalid synthetic correlation"), {
              stage: "ledger", code: "OPERATION_RECORD_INVALID",
            });
          }
        }
        if (payload.account_identity !== record.account_identity ||
          payload.team_identity !== record.team_identity ||
          payload.target_identity !== record.target_identity ||
          payload.client_nonce !== record.client_nonce ||
          payload.prompt_sha256 !== record.prompt_sha256) {
          throw Object.assign(new Error("synthetic correlation binding mismatch"), {
            stage: "ledger", code: "OPERATION_BINDING_MISMATCH",
          });
        }
        record.request_message_id = payload.request_message_id;
        record.request_id = payload.request_id;
        record.root_id = payload.root_id;
      }
      if (record.transport_state === "accepted" && payload.state !== "accepted") {
        return { ok: true, operation_id: payload.operation_id, transport_state: "accepted" };
      }
      record.transport_state = payload.state;
      if (payload.diagnostic) {
        record.diagnostics = [...(record.diagnostics || []), payload.diagnostic];
      }
      return { ok: true, operation_id: payload.operation_id, transport_state: record.transport_state,
        ...(record.request_message_id ? {
          request_message_id: record.request_message_id,
          request_id: record.request_id,
          root_id: record.root_id,
        } : {}),
      };
    }
    throw new Error("unexpected synthetic ledger action");
  };
  return { ledgerCall, actions, rows, marker };
}

function taskInput(overrides = {}) {
  return { operation_id: TASK_ID, prompt: TASK_PROMPT, ...overrides };
}

async function execute({
  profile = syntheticProfile(),
  fetchOptions,
  ledger = makeLedger(),
  input = taskInput(),
  authenticatedAction = runAuthenticatedListAction,
} = {}) {
  const network = makeFetch(fetchOptions);
  const result = await runGrokBotTask({
    mode: "execute",
    input,
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
    authenticatedAction,
  });
  return { result, profile, network, ledger };
}

test("stdin envelope is strict, bounded UTF-8 JSON and cannot take prompt from argv", async () => {
  assert.deepEqual(parseTaskEnvelope("execute", Buffer.from(JSON.stringify(taskInput()))), {
    mode: "execute", ...taskInput(),
  });
  assert.deepEqual(parseTaskEnvelope("reconcile", Buffer.from(JSON.stringify({ operation_id: TASK_ID }))), {
    mode: "reconcile", operation_id: TASK_ID,
  });
  assert.throws(() => parseTaskEnvelope("execute", Buffer.from("{\"operation_id\":\"x\",\"prompt\":\"p\",\"extra\":true}")),
    (error) => error.code === "INPUT_SCHEMA_INVALID");
  assert.throws(() => parseTaskEnvelope("execute", Buffer.from([0xff])),
    (error) => error.code === "INPUT_UTF8_INVALID");
  assert.throws(() => parseTaskEnvelope("execute", Buffer.alloc(64 * 1024 + 1, 0x20)),
    (error) => error.code === "INPUT_TOO_LARGE");
  assert.throws(() => validateTaskEnvelope("execute", { ...taskInput(), prompt: "" }),
    (error) => error.code === "PROMPT_INVALID");

  let authenticated = false;
  let output = "";
  const secretPrompt = "secret-prompt-must-not-echo";
  const code = await main({
    argv: ["--execute", secretPrompt],
    stdin: Readable.from([Buffer.from("{}")]),
    stdout: { write(value) { output += value; } },
    dependencies: {},
    authenticatedAction() { authenticated = true; },
  });
  assert.equal(code, 2);
  assert.equal(authenticated, false);
  assert.equal(output.includes(secretPrompt), false);
  assert.match(output, /MODE_REQUIRED/);
});

test("ordinary execute sends once with the durable nonce and returns only correlated text", async () => {
  const { result, network, ledger } = await execute({
    fetchOptions: {
      agents: [
        { id: TARGET, name: "GrokBot", isGroup: false },
        { id: "synthetic-group-id", name: "GrokBot", isGroup: true, memberIds: [TARGET] },
      ],
      transcript: (nonce) => [
        { kind: "send-message", id: "old-reply", requestId: "old-run", message: { type: "text", content: "unrelated old transcript" } },
        observedUser(nonce),
        observedReply(),
      ],
    },
  });
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.send_attempted, true);
  assert.equal(result.inspection_status, "observed");
  assert.equal(result.ok, true);
  assert.equal(result.operation_completed, false);
  assert.equal(result.completion_status, "nonterminal");
  assert.equal(result.reply_observed, true);
  assert.equal(result.reply_entries_observed, 1);
  assert.equal(result.tool_entries_observed, 0);
  assert.equal(result.response_available, true);
  assert.deepEqual(result.replies, ["Reviewed the fixture; the null check is missing."]);
  assert.equal(result.remote_terminal, "unsupported");
  assert.equal(result.remote_terminal_supported, false);
  assert.ok(result.diagnostics.some((diagnostic) =>
    diagnostic.phase === "send_receipt" &&
    diagnostic.reason === "RECEIPT_REQUEST_ID_MISSING" &&
    diagnostic.response_type === "object" && diagnostic.http_status === 200 &&
    diagnostic.present_keys.includes("messageId")));
  assert.deepEqual(result.correlation, {
    request_message_id: "synthetic-user-message-id",
    request_id: "synthetic-request-run-id",
    root_id: "synthetic-user-message-id",
  });
  assert.equal(result.operation_id, TASK_ID);
  assert.equal(JSON.stringify(result).includes(TASK_PROMPT), false);
  assert.equal(Object.hasOwn(result, "account_identity"), false);
  assert.equal(Object.hasOwn(result, "target_identity"), false);
  const sent = network.calls.filter((call) => call.method === "/api/sendPrompt");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.prompt, TASK_PROMPT);
  assert.equal(sent[0].body.clientNonce, CLIENT_NONCE);
  assert.equal(ledger.actions.filter((action) => action.action === "task_begin").length, 1);
});

test("receipt without a message ID stays unknown with a fixed receipt diagnostic", async () => {
  const { result, ledger } = await execute({
    fetchOptions: { sendBody: {}, transcript: [] },
  });

  assert.equal(result.delivery_status, "unknown");
  assert.equal(result.inspection_status, "pending");
  assert.ok(result.diagnostics.some((diagnostic) =>
    diagnostic.phase === "send_receipt" &&
    diagnostic.reason === "RECEIPT_MESSAGE_ID_MISSING"));
  assert.ok(ledger.rows.get(TASK_ID).diagnostics.some((diagnostic) =>
    diagnostic.reason === "RECEIPT_MESSAGE_ID_MISSING"));
});

test("secret receipt and transcript values never enter diagnostics, ledger diagnostics, or stdout", async () => {
  const privatePrompt = "synthetic private prompt text";
  const privateToken = "Bearer synthetic-secret-token-value";
  const hiddenReceiptKey = "unapprovedReceiptFieldName";
  const hiddenTranscriptKey = "unapprovedTranscriptFieldName";
  const network = makeFetch({
    sendBody: { [hiddenReceiptKey]: privateToken },
    transcript: (nonce) => [observedUser("foreign-nonce", {
      id: "private-transcript-message-id",
      clientNonce: "foreign-nonce",
      content: privatePrompt,
      [hiddenTranscriptKey]: privateToken,
    })],
  });
  const ledger = makeLedger();
  let stdout = "";
  const code = await main({
    argv: ["--execute"],
    stdin: Readable.from([Buffer.from(JSON.stringify(taskInput({ prompt: privatePrompt })))]),
    stdout: { write(value) { stdout += value; } },
    dependencies: syntheticDependencies(syntheticProfile(), network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
  });
  const persistedDiagnostics = ledger.rows.get(TASK_ID).diagnostics || [];
  const diagnosticText = JSON.stringify({ diagnostics: JSON.parse(stdout).diagnostics, persistedDiagnostics });

  assert.equal(code, 1);
  for (const secret of [privatePrompt, privateToken, hiddenReceiptKey, hiddenTranscriptKey]) {
    assert.equal(stdout.includes(secret), false);
    assert.equal(diagnosticText.includes(secret), false);
  }
  assert.ok(JSON.parse(stdout).diagnostics.some((diagnostic) => diagnostic.unknown_key_count > 0));
  assert.ok(persistedDiagnostics.length >= 2);
});

test("transcript nonce absence reports pending with a fixed request lookup diagnostic", async () => {
  const { result, ledger } = await execute({
    fetchOptions: { transcript: [{ kind: "message", role: "user", clientNonce: "other-nonce" }] },
  });

  assert.equal(result.inspection_status, "pending");
  assert.ok(result.diagnostics.some((diagnostic) =>
    diagnostic.phase === "request_lookup" &&
    diagnostic.reason === "REQUEST_NONCE_NOT_FOUND" &&
    diagnostic.tail_entries === 1 && diagnostic.nonce_matches === 0));
  assert.ok(ledger.rows.get(TASK_ID).diagnostics.some((diagnostic) =>
    diagnostic.reason === "REQUEST_NONCE_NOT_FOUND"));
});

test("accepted receipt saves validated correlation IDs and uses the thread when transcript tail fails", async () => {
  const correlation = {
    messageId: "receipt-message-id",
    requestId: "receipt-request-id",
    rootId: "receipt-root-id",
  };
  const { result, network, ledger } = await execute({
    fetchOptions: {
      sendBody: {
        messageId: correlation.messageId,
        requestId: correlation.requestId,
        rootId: correlation.rootId,
        agentId: TARGET,
        clientNonce: CLIENT_NONCE,
        accountIdentity: ACCOUNT,
        teamIdentity: TEAM,
      },
      transcriptStatus: 500,
      thread: {
        rootId: correlation.rootId,
        entries: [observedReply("reply after tail failure", {
          id: "receipt-reply-id",
          requestId: correlation.requestId,
          rootId: correlation.rootId,
        })],
      },
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "observed");
  assert.deepEqual(result.replies, ["reply after tail failure"]);
  assert.equal(result.send_attempted, true);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentTranscriptTail").length, 0);
  assert.deepEqual(network.calls.find((call) => call.method === "/api/getAgentThread").body, {
    id: TARGET,
    rootId: correlation.rootId,
  });
  assert.deepEqual(ledger.rows.get(TASK_ID), {
    operation_id: TASK_ID,
    account_identity: ACCOUNT,
    team_identity: TEAM,
    target_identity: TARGET,
    prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
    client_nonce: CLIENT_NONCE,
    transport_state: "accepted",
    request_message_id: correlation.messageId,
    request_id: correlation.requestId,
    root_id: correlation.rootId,
  });
});

test("saved correlation reads the requested thread after the item leaves the transcript tail without resending", async () => {
  const correlation = {
    request_message_id: "saved-message-id",
    request_id: "saved-request-id",
    root_id: "saved-root-id",
  };
  const ledger = makeLedger({
    initialRecord: {
      operation_id: TASK_ID,
      account_identity: ACCOUNT,
      team_identity: TEAM,
      target_identity: TARGET,
      prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
      client_nonce: CLIENT_NONCE,
      transport_state: "accepted",
      ...correlation,
    },
  });
  const { result, network } = await execute({
    ledger,
    fetchOptions: {
      transcript: [{ kind: "message", role: "user", id: "unrelated-tail", clientNonce: "other-nonce" }],
      thread: {
        rootId: correlation.root_id,
        entries: [observedReply("saved thread reply", {
          id: "saved-reply-id",
          requestId: correlation.request_id,
          rootId: correlation.root_id,
        })],
      },
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "observed");
  assert.deepEqual(result.replies, ["saved thread reply"]);
  assert.equal(result.send_attempted, false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentTranscriptTail").length, 0);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 1);
  assert.equal(ledger.rows.get(TASK_ID).transport_state, "accepted");
});

test("foreign target or nonce evidence cannot create a saved correlation", async () => {
  const { result, network, ledger } = await execute({
    fetchOptions: {
      transcript: [
        observedUser(CLIENT_NONCE, { targetId: "foreign-target-id" }),
        observedUser("foreign-nonce", { id: "foreign-nonce-message-id" }),
        observedReply("foreign reply", { requestId: "synthetic-request-run-id" }),
      ],
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "pending");
  assert.equal(result.reply_observed, false);
  assert.equal(result.send_attempted, true);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 0);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_id"), false);
});

test("accepted transport survives a receipt carrying a foreign target or nonce without saving its IDs", async () => {
  const { result, network, ledger } = await execute({
    fetchOptions: {
      sendBody: {
        messageId: "foreign-receipt-message-id",
        requestId: "foreign-receipt-request-id",
        rootId: "foreign-receipt-root-id",
        agentId: "foreign-target-id",
        clientNonce: "foreign-nonce",
      },
      transcriptStatus: 500,
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "unknown");
  assert.equal(result.send_attempted, true);
  assert.deepEqual(result.inspection_error, {
    stage: "transcript", code: "HTTP_STATUS", status: 500,
  });
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 0);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_id"), false);
});

test("receipt without an explicit root ID remains accepted but cannot trigger a thread read", async () => {
  const { result, network, ledger } = await execute({
    fetchOptions: {
      sendBody: {
        messageId: "message-without-root-id",
        requestId: "request-without-root-id",
        agentId: TARGET,
        clientNonce: CLIENT_NONCE,
        accountIdentity: ACCOUNT,
        teamIdentity: TEAM,
      },
      transcriptStatus: 500,
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "unknown");
  assert.equal(result.send_attempted, true);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentTranscriptTail").length, 1);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 0);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_message_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "root_id"), false);
});

test("reconcile derives a missing root ID only from its unique nonce-matched user message ID", async () => {
  const request = observedUser(CLIENT_NONCE, {
    id: "own-nonce-matched-message-id",
    requestId: "own-nonce-matched-request-id",
  });
  delete request.rootId;
  const ledger = makeLedger({
    initialRecord: {
      operation_id: TASK_ID,
      account_identity: ACCOUNT,
      team_identity: TEAM,
      target_identity: TARGET,
      prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
      client_nonce: CLIENT_NONCE,
      transport_state: "unknown",
    },
  });
  const profile = syntheticProfile();
  const network = makeFetch({
    transcript: [request],
    thread: {
      rootId: request.id,
      entries: [observedReply("synthetic correlated reply", {
        clientNonce: CLIENT_NONCE,
        requestId: request.requestId,
        rootId: request.id,
      })],
    },
  });
  const result = await runGrokBotTask({
    mode: "reconcile",
    input: { operation_id: TASK_ID },
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
    authenticatedAction(options) {
      assert.equal(options.allowSendPrompt, false);
      return runAuthenticatedListAction(options);
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "observed");
  assert.equal(result.reply_observed, true);
  assert.equal(result.send_attempted, false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentTranscriptTail").length, 1);
  const threadCall = network.calls.find((call) => call.method === "/api/getAgentThread");
  assert.equal(threadCall.body.rootId, request.id);
  assert.equal(ledger.rows.get(TASK_ID).request_message_id, request.id);
  assert.equal(ledger.rows.get(TASK_ID).request_id, request.requestId);
  assert.equal(ledger.rows.get(TASK_ID).root_id, request.id);
});

test("missing own message ID remains uncorrelated and cannot synthesize a root", async () => {
  const request = observedUser(CLIENT_NONCE, { requestId: "request-without-own-id" });
  delete request.id;
  delete request.rootId;
  const ledger = makeLedger({
    initialRecord: {
      operation_id: TASK_ID,
      account_identity: ACCOUNT,
      team_identity: TEAM,
      target_identity: TARGET,
      prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
      client_nonce: CLIENT_NONCE,
      transport_state: "unknown",
    },
  });
  const profile = syntheticProfile();
  const network = makeFetch({ transcript: [request] });
  const result = await runGrokBotTask({
    mode: "reconcile",
    input: { operation_id: TASK_ID },
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
    authenticatedAction(options) {
      assert.equal(options.allowSendPrompt, false);
      return runAuthenticatedListAction(options);
    },
  });

  assert.equal(result.delivery_status, "unknown");
  assert.equal(result.inspection_status, "pending");
  assert.equal(result.send_attempted, false);
  assert.ok(result.diagnostics.some((diagnostic) =>
    diagnostic.phase === "request_lookup" && diagnostic.reason === "REQUEST_MESSAGE_ID_UNOBSERVED"));
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 0);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_message_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "root_id"), false);
});

test("an invalid explicit root field blocks the own-ID fallback", async () => {
  const request = observedUser(CLIENT_NONCE, { requestId: "request-with-invalid-root" });
  delete request.rootId;
  request.root_id = "";
  const ledger = makeLedger({
    initialRecord: {
      operation_id: TASK_ID,
      account_identity: ACCOUNT,
      team_identity: TEAM,
      target_identity: TARGET,
      prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
      client_nonce: CLIENT_NONCE,
      transport_state: "unknown",
    },
  });
  const profile = syntheticProfile();
  const network = makeFetch({ transcript: [request] });
  const result = await runGrokBotTask({
    mode: "reconcile",
    input: { operation_id: TASK_ID },
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
    authenticatedAction(options) {
      assert.equal(options.allowSendPrompt, false);
      return runAuthenticatedListAction(options);
    },
  });

  assert.equal(result.delivery_status, "unknown");
  assert.equal(result.inspection_status, "unknown");
  assert.equal(result.send_attempted, false);
  assert.deepEqual(result.inspection_error, {
    stage: "transcript", code: "REQUEST_SCHEMA_INVALID",
  });
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 0);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_message_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "root_id"), false);
});

test("nonce collisions stay ambiguous even when only one candidate matches the request context", async () => {
  const { result, network, ledger } = await execute({
    fetchOptions: {
      transcript: (nonce) => {
        const missingRoot = observedUser(nonce, {
          id: "request-missing-root",
          requestId: "run-missing-root",
        });
        delete missingRoot.rootId;
        return [
          missingRoot,
          observedUser(nonce, {
            id: "request-with-root",
            requestId: "run-with-root",
            rootId: "root-with-root",
            targetIdentity: "foreign-target",
          }),
        ];
      },
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "pending");
  assert.ok(result.diagnostics.some((diagnostic) =>
    diagnostic.phase === "request_lookup" && diagnostic.reason === "REQUEST_NONCE_AMBIGUOUS" &&
    diagnostic.nonce_matches === 2));
  assert.equal(result.send_attempted, true);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 0);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_message_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "request_id"), false);
  assert.equal(Object.hasOwn(ledger.rows.get(TASK_ID), "root_id"), false);
});

test("first ordinary operation is pinned to the accepted marker account, team, and Bot", async () => {
  const accountProfile = syntheticProfile({ accountIdentity: "different-account" });
  const accountNetwork = makeFetch();
  const accountLedger = makeLedger();
  await assert.rejects(runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: syntheticDependencies(accountProfile, accountNetwork.fetchImpl),
    ledgerCall: accountLedger.ledgerCall,
  }), (error) => error.code === "EXPECTED_ACCOUNT_MISMATCH");
  assert.equal(accountProfile.dpapiCalls, 0);
  assert.equal(accountNetwork.calls.filter((call) => call.url === ENSURE_URL).length, 0);
  assert.equal(accountNetwork.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);

  const teamProfile = syntheticProfile({ teamIdentity: "9753" });
  const teamNetwork = makeFetch();
  const teamLedger = makeLedger();
  await assert.rejects(runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: syntheticDependencies(teamProfile, teamNetwork.fetchImpl),
    ledgerCall: teamLedger.ledgerCall,
  }), (error) => error.code === "EXPECTED_TEAM_MISMATCH");
  assert.equal(teamNetwork.calls.filter((call) => call.url === ENSURE_URL).length, 0);
  assert.equal(teamNetwork.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);

  const targetProfile = syntheticProfile();
  const targetNetwork = makeFetch({ agents: [{ id: "different-bot-id", name: "GrokBot" }] });
  const targetLedger = makeLedger();
  await assert.rejects(runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: syntheticDependencies(targetProfile, targetNetwork.fetchImpl),
    ledgerCall: targetLedger.ledgerCall,
  }), (error) => error.code === "TARGET_IDENTITY_MISMATCH");
  assert.equal(targetNetwork.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
  assert.equal(targetLedger.actions.some((action) => action.action === "task_begin"), false);
});

test("missing or malformed anchor stops before authentication", async () => {
  const network = makeFetch();
  const profile = syntheticProfile();
  const ledger = makeLedger({ anchor: false });
  let authenticationCalls = 0;
  await assert.rejects(runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
    authenticatedAction() { authenticationCalls += 1; },
  }), (error) => error.code === "OPERATION_NOT_FOUND");
  assert.equal(authenticationCalls, 0);
  assert.equal(profile.dpapiCalls, 0);
  assert.equal(network.calls.length, 0);
});

test("partial saved correlation is rejected before authentication or gateway inspection", async () => {
  const ledger = makeLedger({
    initialRecord: {
      operation_id: TASK_ID,
      account_identity: ACCOUNT,
      team_identity: TEAM,
      target_identity: TARGET,
      prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
      client_nonce: CLIENT_NONCE,
      transport_state: "accepted",
      request_id: "partial-request-id",
    },
  });
  const profile = syntheticProfile();
  const network = makeFetch();

  await assert.rejects(runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
  }), (error) => error.code === "OPERATION_RECORD_INVALID");
  assert.equal(profile.dpapiCalls, 0);
  assert.equal(network.calls.length, 0);
});

test("execute rejects a conflicting prior prompt before authentication", async () => {
  const record = {
    operation_id: TASK_ID,
    account_identity: ACCOUNT,
    team_identity: TEAM,
    target_identity: TARGET,
    prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
    client_nonce: CLIENT_NONCE,
    transport_state: "unknown",
  };
  const ledger = makeLedger({ initialRecord: record });
  let authenticationCalls = 0;
  await assert.rejects(runGrokBotTask({
    mode: "execute",
    input: taskInput({ prompt: "a different prompt" }),
    dependencies: {},
    ledgerCall: ledger.ledgerCall,
    authenticatedAction() { authenticationCalls += 1; },
  }), (error) => error.code === "OPERATION_CONFLICT");
  assert.equal(authenticationCalls, 0);
  assert.deepEqual(ledger.actions.map((action) => action.action), ["anchor", "task_read"]);
});

test("duplicate execute and timed out send reconcile with the same nonce and never replay", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const timeout = new Error("synthetic transport timeout with private data");
  const network = makeFetch({
    sendFailure: timeout,
    transcript: (nonce) => [observedUser(nonce, { requestId: undefined })],
  });
  const dependencies = syntheticDependencies(profile, network.fetchImpl);
  const first = await runGrokBotTask({
    mode: "execute", input: taskInput(), dependencies, ledgerCall: ledger.ledgerCall,
  });
  const second = await runGrokBotTask({
    mode: "execute", input: taskInput(), dependencies, ledgerCall: ledger.ledgerCall,
  });
  assert.equal(first.delivery_status, "accepted");
  assert.equal(first.reply_observed, false);
  assert.equal(first.inspection_status, "pending");
  assert.equal(first.send_attempted, true);
  assert.equal(second.send_attempted, false);
  assert.equal(second.delivery_status, "accepted");
  assert.equal(Object.hasOwn(first, "send_error"), true);
  assert.equal(Object.hasOwn(second, "send_error"), false);
  assert.equal(Object.hasOwn(second, "inspection_error"), false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
  assert.equal(ledger.rows.get(TASK_ID).client_nonce, CLIENT_NONCE);
  assert.equal(JSON.stringify(first).includes("private data"), false);
});

test("reconcile reads before auth, is send-incapable, and leaves missing operations absent", async () => {
  let authenticationCalls = 0;
  const missingLedger = makeLedger();
  const missingProfile = syntheticProfile();
  const missingNetwork = makeFetch();
  await assert.rejects(runGrokBotTask({
    mode: "reconcile",
    input: { operation_id: TASK_ID },
    dependencies: syntheticDependencies(missingProfile, missingNetwork.fetchImpl),
    ledgerCall: missingLedger.ledgerCall,
    authenticatedAction() { authenticationCalls += 1; },
  }), (error) => error.code === "OPERATION_NOT_FOUND");
  assert.deepEqual(missingLedger.actions.map((action) => action.action), ["task_read"]);
  assert.equal(authenticationCalls, 0);

  const record = {
    operation_id: TASK_ID,
    account_identity: ACCOUNT,
    team_identity: TEAM,
    target_identity: TARGET,
    prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
    client_nonce: CLIENT_NONCE,
    transport_state: "unknown",
  };
  const ledger = makeLedger({ initialRecord: record });
  const profile = syntheticProfile();
  const network = makeFetch();
  let readonlyPolicy;
  const result = await runGrokBotTask({
    mode: "reconcile",
    input: { operation_id: TASK_ID },
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
    authenticatedAction(options) {
      readonlyPolicy = options.allowSendPrompt;
      return runAuthenticatedListAction(options);
    },
  });
  assert.equal(readonlyPolicy, false);
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.reply_observed, true);
  assert.equal(result.send_attempted, false);
  assert.equal(Object.hasOwn(result, "send_error"), false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
  assert.equal(ledger.rows.get(TASK_ID).transport_state, "accepted");

  const failedInspectionLedger = makeLedger({
    initialRecord: {
      operation_id: TASK_ID,
      account_identity: ACCOUNT,
      team_identity: TEAM,
      target_identity: TARGET,
      prompt_sha256: createHash("sha256").update(TASK_PROMPT, "utf8").digest("hex"),
      client_nonce: CLIENT_NONCE,
      transport_state: "unknown",
    },
  });
  const failedInspectionNetwork = makeFetch({ transcriptStatus: 500 });
  const failedInspection = await runGrokBotTask({
    mode: "reconcile",
    input: { operation_id: TASK_ID },
    dependencies: syntheticDependencies(syntheticProfile(), failedInspectionNetwork.fetchImpl),
    ledgerCall: failedInspectionLedger.ledgerCall,
    authenticatedAction(options) {
      return runAuthenticatedListAction(options);
    },
  });
  assert.equal(failedInspection.send_attempted, false);
  assert.equal(failedInspection.delivery_status, "unknown");
  assert.equal(Object.hasOwn(failedInspection, "send_error"), false);
  assert.deepEqual(failedInspection.inspection_error, {
    stage: "transcript", code: "HTTP_STATUS", status: 500,
  });
  assert.equal(failedInspectionNetwork.calls.filter((call) => call.method === "/api/sendPrompt").length, 0);
});

test("send and transcript 5xx diagnostics remain distinct with top-level inspection compatibility", async () => {
  const { result, network } = await execute({
    fetchOptions: { sendStatus: 503, transcriptStatus: 500 },
  });

  assert.equal(result.delivery_status, "unknown");
  assert.equal(result.send_attempted, true);
  assert.equal(result.inspection_status, "unknown");
  assert.equal(result.stage, "transcript");
  assert.equal(result.code, "HTTP_STATUS");
  assert.equal(result.status, 500);
  assert.deepEqual(result.send_error, {
    stage: "send_prompt", code: "HTTP_STATUS", status: 503,
  });
  assert.deepEqual(result.inspection_error, {
    stage: "transcript", code: "HTTP_STATUS", status: 500,
  });
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
});

test("initial transport failure remains visible beside a failed thread read", async () => {
  const { result, network } = await execute({
    fetchOptions: {
      sendFailure: new Error("synthetic send transport failure"),
      transcript: (nonce) => [observedUser(nonce)],
      threadStatus: 500,
    },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "unknown");
  assert.equal(result.stage, "thread");
  assert.equal(result.code, "HTTP_STATUS");
  assert.equal(result.status, 500);
  assert.deepEqual(result.send_error, {
    stage: "send_prompt", code: "REQUEST_FAILED",
  });
  assert.deepEqual(result.inspection_error, {
    stage: "thread", code: "HTTP_STATUS", status: 500,
  });
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
});

test("successful send and failed inspection reports no send error", async () => {
  const { result, network } = await execute({
    fetchOptions: { transcriptStatus: 500 },
  });

  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.send_attempted, true);
  assert.equal(result.stage, "transcript");
  assert.deepEqual(result.inspection_error, {
    stage: "transcript", code: "HTTP_STATUS", status: 500,
  });
  assert.equal(Object.hasOwn(result, "send_error"), false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
});

test("HTTP 500 inspection outcome is structured failure and exits nonzero", async () => {
  const profile = syntheticProfile();
  const network = makeFetch({ transcriptStatus: 500 });
  const ledger = makeLedger();
  let stdout = "";
  const exitCode = await main({
    argv: ["--execute"],
    stdin: Readable.from([Buffer.from(JSON.stringify(taskInput()))]),
    stdout: { write(value) { stdout += value; } },
    dependencies: syntheticDependencies(profile, network.fetchImpl),
    ledgerCall: ledger.ledgerCall,
  });
  const result = JSON.parse(stdout);

  assert.equal(exitCode, 1);
  assert.equal(result.ok, false);
  assert.equal(result.error_category, "http_status");
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.inspection_status, "unknown");
  assert.equal(result.operation_completed, false);
  assert.equal(result.completion_status, "unknown");
  assert.equal(result.response_available, false);
  assert.equal(result.stage, "transcript");
  assert.equal(result.code, "HTTP_STATUS");
  assert.equal(result.status, 500);
  assert.equal(result.remote_terminal_supported, false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
});

test("local HTTP server preserves status and safe category without any external request", async () => {
  const requests = [];
  await withLocalFakeServer((request, response) => {
    requests.push(request.url);
    response.setHeader("content-type", "application/json");
    if (request.url === "/aiserver.v1.GrokBotService/EnsureSandBox") {
      response.writeHead(200);
      response.end(JSON.stringify({
        gatewayUrl: "https://box-123.us12.cursorvm.com",
        gatewayToken: "synthetic-route-token",
      }));
      return;
    }
    if (request.url === "/api/listAgents") {
      response.writeHead(200);
      response.end(JSON.stringify({ agents: [{ id: TARGET, name: "GrokBot" }] }));
      return;
    }
    if (request.url === "/api/sendPrompt") {
      response.writeHead(200);
      response.end(JSON.stringify({ messageId: "synthetic-send-receipt-id" }));
      return;
    }
    if (request.url === "/api/getAgentTranscriptTail") {
      response.setHeader("x-error-code", "unavailable");
      response.setHeader("x-request-id", "fake-req-500");
      response.writeHead(500);
      response.end(JSON.stringify({ error: "synthetic-upstream-detail" }));
      return;
    }
    response.writeHead(404);
    response.end(JSON.stringify({ error: "unexpected synthetic route" }));
  }, async ({ fetchImpl }) => {
    const profile = syntheticProfile();
    const ledger = makeLedger();
    const result = await runGrokBotTask({
      mode: "execute",
      input: taskInput(),
      dependencies: syntheticDependencies(profile, fetchImpl),
      ledgerCall: ledger.ledgerCall,
    });
    assert.equal(result.ok, false);
    assert.equal(result.error_category, "http_status");
    assert.equal(result.stage, "transcript");
    assert.equal(result.code, "HTTP_STATUS");
    assert.equal(result.status, 500);
    assert.equal(result.remote_error_code, "unavailable");
    assert.equal(result.remote_request_id, "fake-req-500");
    assert.deepEqual(result.inspection_error, {
      stage: "transcript",
      code: "HTTP_STATUS",
      status: 500,
      remote_error_code: "unavailable",
      remote_request_id: "fake-req-500",
    });
    assert.equal(result.operation_completed, false);
    assert.equal(JSON.stringify(result).includes("synthetic-upstream-detail"), false);
  });
  assert.deepEqual(requests, [
    "/aiserver.v1.GrokBotService/EnsureSandBox",
    "/api/listAgents",
    "/api/sendPrompt",
    "/api/getAgentTranscriptTail",
  ]);
});

test("response stream disconnect is a transport error, not an HTTP success", async () => {
  const network = makeFetch();
  const fetchImpl = async (url, options) => {
    if (new URL(url).pathname !== "/api/getAgentTranscriptTail") {
      return network.fetchImpl(url, options);
    }
    return {
      status: 200,
      ok: true,
      redirected: false,
      url,
      body: {
        getReader() {
          return {
            async read() {
              throw new Error("synthetic disconnected stream");
            },
          };
        },
      },
    };
  };
  const ledger = makeLedger();
  const result = await runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: syntheticDependencies(syntheticProfile(), fetchImpl),
    ledgerCall: ledger.ledgerCall,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error_category, "transport");
  assert.equal(result.stage, "transcript");
  assert.equal(result.code, "RESPONSE_READ_FAILED");
  assert.equal(result.status, 200);
  assert.equal(result.completion_status, "unknown");
  assert.equal(result.operation_completed, false);
  assert.equal(network.calls.filter((call) => call.method === "/api/sendPrompt").length, 1);
});

test("malicious exception text and fields are allowlisted in nested diagnostics", async () => {
  const secret = "Bearer synthetic-secret prompt=private https://private.invalid/?q=x nonce=private target=private";
  const ledger = makeLedger();
  const result = await runGrokBotTask({
    mode: "execute",
    input: taskInput(),
    dependencies: {},
    ledgerCall: ledger.ledgerCall,
    authenticatedAction({ action }) {
      return action({
        agents: [{ id: TARGET, name: "GrokBot" }],
        accountIdentity: ACCOUNT,
        teamIdentity: TEAM,
        async gatewayRequest(method) {
          const error = Object.assign(new Error(secret), {
            stage: method === "sendPrompt" ? "send_prompt" : "transcript",
            code: `PRIVATE_${secret}`,
            status: method === "sendPrompt" ? 503 : 500,
          });
          throw error;
        },
      });
    },
  });

  assert.deepEqual(result.send_error, {
    stage: "send_prompt", code: "SEND_FAILED", status: 503,
  });
  assert.deepEqual(result.inspection_error, {
    stage: "transcript", code: "TRANSCRIPT_READ_FAILED", status: 500,
  });
  assert.equal(result.stage, "transcript");
  assert.equal(result.code, "TRANSCRIPT_READ_FAILED");
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes(secret), false);
  assert.equal(serialized.includes(TASK_PROMPT), false);
  assert.equal(serialized.includes(CLIENT_NONCE), false);
  assert.equal(serialized.includes(TARGET), false);
});

test("production request/reply schema accepts omitted optional reply fields and bounded thread fallback", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const network = makeFetch({
    transcript: (nonce) => [
      {
        kind: "message", role: "user", id: "synthetic-user-message-id",
        content: "prompt not returned", clientNonce: nonce, requestId: "synthetic-request-run-id",
        rootId: "synthetic-user-message-id",
      },
    ],
    thread: {
      entries: [
        observedReply("fallback reply without role, sender, root, or nonce"),
      ],
    },
  });
  const result = await runGrokBotTask({
    mode: "execute", input: taskInput(),
    dependencies: syntheticDependencies(profile, network.fetchImpl), ledgerCall: ledger.ledgerCall,
  });
  assert.deepEqual(result.replies, ["fallback reply without role, sender, root, or nonce"]);
  assert.equal(result.reply_observed, true);
  assert.equal(network.calls.filter((call) => call.method === "/api/getAgentThread").length, 1);
});

test("tool-only and empty correlated entries are reported without claiming completion", async () => {
  const toolEntry = {
    kind: "send-message",
    id: "synthetic-tool-entry",
    requestId: "synthetic-request-run-id",
    message: { type: "tool-call", name: "read", status: "started" },
  };
  const toolOnly = await execute({
    fetchOptions: {
      transcript: (nonce) => [observedUser(nonce)],
      thread: { entries: [toolEntry] },
    },
  });
  assert.equal(toolOnly.result.ok, true);
  assert.equal(toolOnly.result.operation_completed, false);
  assert.equal(toolOnly.result.completion_status, "tool_only");
  assert.equal(toolOnly.result.reply_observed, false);
  assert.equal(toolOnly.result.reply_entries_observed, 1);
  assert.equal(toolOnly.result.tool_entries_observed, 1);

  const empty = await execute({
    fetchOptions: {
      transcript: (nonce) => [observedUser(nonce), observedReply("")],
    },
  });
  assert.equal(empty.result.ok, true);
  assert.equal(empty.result.operation_completed, false);
  assert.equal(empty.result.completion_status, "empty");
  assert.equal(empty.result.reply_observed, true);
  assert.equal(empty.result.response_available, false);

  const toolOnlyInTranscript = await execute({
    fetchOptions: {
      transcript: (nonce) => [observedUser(nonce), toolEntry],
      thread: { entries: [] },
    },
  });
  assert.equal(toolOnlyInTranscript.result.completion_status, "tool_only");
  assert.equal(toolOnlyInTranscript.result.tool_only, true);
  assert.equal(toolOnlyInTranscript.result.tool_entries_observed, 1);
  assert.equal(toolOnlyInTranscript.result.non_text_entries_observed, 1);

  const nonToolNonText = await execute({
    fetchOptions: {
      transcript: (nonce) => [
        observedUser(nonce),
        {
          kind: "send-message",
          id: "synthetic-image-entry",
          requestId: "synthetic-request-run-id",
          message: { type: "image", content: "synthetic image marker" },
        },
      ],
      thread: { entries: [] },
    },
  });
  assert.equal(nonToolNonText.result.completion_status, "non_text");
  assert.equal(nonToolNonText.result.tool_only, false);
  assert.equal(nonToolNonText.result.tool_entries_observed, 0);
  assert.equal(nonToolNonText.result.non_text_entries_observed, 1);
});

test("foreign or malformed run, root, nonce, and role evidence never yields a reply", async (t) => {
  const cases = [
    ["missing request ID", (nonce) => [observedUser(nonce, { requestId: undefined }), observedReply()]],
    ["missing reply request ID", (nonce) => [observedUser(nonce), observedReply("bad", { requestId: undefined })]],
    ["foreign reply request ID", (nonce) => [observedUser(nonce), observedReply("bad", { requestId: "foreign-run" })]],
    ["foreign reply root", (nonce) => [observedUser(nonce), observedReply("bad", { rootId: "foreign-root" })]],
    ["foreign reply nonce", (nonce) => [observedUser(nonce), observedReply("bad", { clientNonce: "foreign-nonce" })]],
    ["user reply role", (nonce) => [observedUser(nonce), observedReply("bad", { role: "user" })]],
    ["contradictory reply role", (nonce) => [observedUser(nonce), observedReply("bad", { role: "assistant", sender: "user" })]],
    ["contradictory user request role", (nonce) => [{ ...observedUser(nonce), role: "assistant" }, observedReply()]],
  ];
  for (const [name, transcript] of cases) {
    await t.test(name, async () => {
      const { result } = await execute({ fetchOptions: { transcript } });
      assert.equal(result.reply_observed, false);
      assert.deepEqual(result.replies, []);
      assert.equal(result.remote_terminal, "unsupported");
    });
  }
});

test("reply text output has a strict byte and message bound", async () => {
  const content = "界".repeat(5_000);
  const { result } = await execute({
    fetchOptions: {
      transcript: (nonce) => [
        observedUser(nonce),
        ...Array.from({ length: 12 }, (_, index) => observedReply(`${index}:${content}`, { id: `reply-${index}` })),
      ],
    },
  });
  assert.equal(result.reply_observed, true);
  assert.ok(result.replies.length <= 8);
  assert.ok(result.replies.length > 0);
  assert.equal(result.response_truncated, true);
  assert.ok(result.replies.reduce((sum, reply) => sum + Buffer.byteLength(reply), 0) <= 24 * 1024);
});

test("safe errors never echo secret messages or codes", async () => {
  const secret = "synthetic-secret-route-token";
  let output = "";
  const code = await main({
    argv: ["--execute"],
    stdin: Readable.from([Buffer.from(JSON.stringify(taskInput()))]),
    stdout: { write(value) { output += value; } },
    dependencies: {},
    ledgerCall(payload) {
      if (payload.action === "anchor") {
        throw Object.assign(new Error(secret), { stage: "ledger", code: `token=${secret}` });
      }
    },
  });
  assert.equal(code, 1);
  assert.equal(output.includes(secret), false);
  assert.match(output, /MARKER_ANCHOR_UNAVAILABLE/);
});

test("malformed and oversized input fails before auth and import/default has no live action", async () => {
  let authCalls = 0;
  for (const raw of [
    Buffer.from("not-json"),
    Buffer.from(JSON.stringify({ operation_id: TASK_ID, prompt: TASK_PROMPT, environment: "ignored" })),
    Buffer.alloc(64 * 1024 + 1, 0x20),
  ]) {
    let output = "";
    const code = await main({
      argv: ["--execute"],
      stdin: Readable.from([raw]),
      stdout: { write(value) { output += value; } },
      dependencies: {},
      authenticatedAction() { authCalls += 1; },
    });
    assert.equal(code, 2);
    assert.match(output, /INPUT_/);
  }
  let output = "";
  const defaultCode = await main({
    argv: [],
    stdin: Readable.from([Buffer.from("{}")]),
    stdout: { write(value) { output += value; } },
  });
  assert.equal(defaultCode, 2);
  assert.equal(authCalls, 0);
  assert.match(output, /MODE_REQUIRED/);
});
