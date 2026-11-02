import assert from "node:assert/strict";
import { createCipheriv } from "node:crypto";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  EXPECTED_REPLY,
  OPERATION_ID,
  PROMPT,
  createLedgerCall,
  main,
  resolveGrokBot,
  runGrokBotMarkerReconciliation,
  runGrokBotMarkerSmoke,
} from "../scripts/grok_bot_marker_smoke.mjs";
import { runAuthenticatedListAction } from "../scripts/grok_bot_list_probe.mjs";


const ENSURE_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox";
const STORAGE_KEY = Buffer.alloc(32, 0x45);
const WRAPPED_KEY = Buffer.from("synthetic-wrapped-dpapi-key");
const CLIENT_NONCE = "e5dd8c58-bd07-4c46-9f22-e5a19a8a0f17";
let cipherCounter = 0;

function encryptV10(text) {
  const nonce = Buffer.alloc(12);
  nonce.writeUInt32BE(++cipherCounter, 8);
  const cipher = createCipheriv("aes-256-gcm", STORAGE_KEY, nonce);
  const ciphertext = Buffer.concat([cipher.update(text, "utf8"), cipher.final()]);
  return Buffer.concat([Buffer.from("v10"), nonce, ciphertext, cipher.getAuthTag()]).toString("base64");
}

function syntheticProfile() {
  let signedIn = true;
  let dpapiCalls = 0;
  const secrets = {
    version: 1,
    "cursor-accounts": JSON.stringify({
      active: "synthetic-account",
      accounts: {
        "synthetic-account": {
          "cursor-access-token": encryptV10("synthetic-access-token"),
          "cursor-selected-team-id": encryptV10("1234"),
        },
      },
    }),
  };
  return {
    reader: {
      read(name) {
        if (name === "sand-secrets.json") return JSON.stringify(secrets);
        if (name === "desktop-status.json") return JSON.stringify({ signedIn, appVersion: "0.75.5" });
        if (name === "Local State") {
          return JSON.stringify({
            os_crypt: {
              encrypted_key: Buffer.concat([Buffer.from("DPAPI"), WRAPPED_KEY]).toString("base64"),
            },
          });
        }
        throw new Error("unexpected synthetic profile read");
      },
      signOut() { signedIn = false; },
      changeTeam(teamId) {
        const root = JSON.parse(secrets["cursor-accounts"]);
        root.accounts["synthetic-account"]["cursor-selected-team-id"] = encryptV10(teamId);
        secrets["cursor-accounts"] = JSON.stringify(root);
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

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeLedger({ existing = false, nonce = CLIENT_NONCE } = {}) {
  let record = existing
    ? { client_nonce: nonce, transport_state: "sending" }
    : null;
  const calls = [];
  const ledgerCall = (payload) => {
    calls.push(payload);
    if (payload.action === "begin") {
      if (!record) {
        record = { client_nonce: nonce, transport_state: "sending" };
        return { ok: true, created: true, ...record };
      }
      return { ok: true, created: false, ...record };
    }
    assert.equal(payload.operation_id, OPERATION_ID);
    record.transport_state = payload.state;
    return { ok: true, transport_state: record.transport_state };
  };
  return { ledgerCall, calls, get record() { return record; } };
}

function makeFetch({
  agents = [{ id: "opaque-bot-id", name: "GrokBot" }],
  sendBody = { messageId: "synthetic-receipt-id" },
  transcript = [],
  thread = { entries: [] },
  afterSend,
  afterList,
  beforeResponse,
} = {}) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    const body = JSON.parse(options.body);
    calls.push({ url, path: parsed.pathname, body, options });
    if (url === ENSURE_URL) {
      return response({
        gatewayUrl: "https://box-123.us12.cursorvm.com",
        gatewayToken: "synthetic-route-token",
        gatewayHeaders: { "x-anyrun-network-token": "synthetic-route-header" },
      });
    }
    if (parsed.pathname === "/api/listAgents") {
      afterList?.();
      return response({ agents });
    }
    if (parsed.pathname === "/api/sendPrompt") {
      afterSend?.();
      return response(sendBody);
    }
    if (parsed.pathname === "/api/getAgentTranscriptTail") {
      const result = await beforeResponse?.(parsed.pathname, body);
      if (result) return result;
      return response({ entries: typeof transcript === "function" ? transcript(calls) : transcript });
    }
    if (parsed.pathname === "/api/getAgentThread") {
      const result = await beforeResponse?.(parsed.pathname, body);
      if (result) return result;
      return response(thread);
    }
    throw new Error("synthetic request outside the reviewed gateway methods");
  };
  return { fetchImpl, calls };
}

function dependencies(profile, fetchImpl) {
  return {
    profileReader: profile.reader,
    dpapiUnprotect: profile.dpapiUnprotect,
    fetchImpl,
  };
}

function seedReconciliationRow(ledgerCall, {
  accountIdentity = "synthetic-account",
  teamIdentity = "1234",
  targetIdentity = "opaque-bot-id",
  state = "unknown",
} = {}) {
  const admission = ledgerCall({
    action: "begin",
    operation_id: OPERATION_ID,
    account_identity: accountIdentity,
    team_identity: teamIdentity,
    target_identity: targetIdentity,
    prompt: PROMPT,
  });
  if (state !== "sending") {
    ledgerCall({ action: "state", operation_id: OPERATION_ID, state });
  }
  return admission;
}

test("exact resolver excludes groups and fails closed for missing, ambiguous, or malformed lists", () => {
  assert.deepEqual(
    resolveGrokBot({ agents: [
      { id: "group-id", name: "GrokBot", isGroup: true },
      { id: "bot-id", name: "GrokBot" },
      { id: "other-id", name: "Other" },
    ] }),
    { id: "bot-id", name: "GrokBot", isGroup: false },
  );
  assert.throws(() => resolveGrokBot({ agents: [{ id: "group-id", name: "GrokBot", isGroup: true }] }),
    (error) => error.code === "TARGET_NOT_FOUND");
  assert.throws(() => resolveGrokBot({ agents: [
    { id: "bot-a", name: "GrokBot" },
    { id: "bot-b", name: "GrokBot", isGroup: false },
  ] }), (error) => error.code === "TARGET_AMBIGUOUS");
  assert.throws(() => resolveGrokBot({ agents: [{ id: "bot-id", name: 42 }] }),
    (error) => error.code === "AGENT_SCHEMA_INVALID");
});

test("send uses the durable nonce and only exact rooted reply marker reports success", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const tail = [{
    id: "request-id",
    kind: "user",
    clientNonce: CLIENT_NONCE,
    requestId: "expected-run",
  }];
  const { fetchImpl, calls } = makeFetch({
    agents: [
      { id: "opaque-bot-id", name: "GrokBot", isGroup: false },
      { id: "group-id", name: "GrokBot", isGroup: true, memberIds: ["opaque-bot-id"] },
    ],
    transcript: tail,
    thread: { rootId: "request-id", entries: [
      { id: "old-reply", role: "assistant", rootId: "old-root", text: EXPECTED_REPLY },
      { id: "request-id", role: "user", text: EXPECTED_REPLY },
      { id: "reply-id", kind: "send-message", role: "assistant", rootId: "request-id", requestId: "expected-run", text: EXPECTED_REPLY },
    ] },
  });

  const result = await runGrokBotMarkerSmoke({
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall: ledger.ledgerCall,
    sleepImpl: async () => {},
  });

  assert.equal(result.ok, true);
  assert.equal(result.operation_id, OPERATION_ID);
  assert.equal(result.send_attempts, 1);
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.reply_status, "observed");
  assert.equal(result.remote_terminal, "unsupported");
  assert.equal(ledger.record.transport_state, "accepted");
  assert.deepEqual(ledger.calls[0], {
    action: "begin",
    operation_id: OPERATION_ID,
    account_identity: "synthetic-account",
    team_identity: "1234",
    target_identity: "opaque-bot-id",
    prompt: PROMPT,
  });
  const send = calls.find((call) => call.path === "/api/sendPrompt");
  assert.deepEqual(send.body, {
    agentId: "opaque-bot-id",
    prompt: PROMPT,
    clientNonce: CLIENT_NONCE,
  });
  assert.deepEqual(calls.find((call) => call.path === "/api/getAgentThread").body, {
    id: "opaque-bot-id",
    rootId: "request-id",
  });
  const serialized = JSON.stringify(result);
  for (const hidden of [
    "synthetic-access-token",
    "synthetic-route-token",
    "synthetic-route-header",
    "box-123",
    "opaque-bot-id",
    EXPECTED_REPLY,
  ]) assert.equal(serialized.includes(hidden), false);
});

test("bare acknowledgment plus exact requestId and no reply rootId proves the marker reply", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const { fetchImpl, calls } = makeFetch({
    sendBody: { accepted: true },
    transcript: [{ id: "root-for-nonce", kind: "user", clientNonce: CLIENT_NONCE, requestId: "run-for-nonce" }],
    thread: { entries: [{ id: "reply", kind: "send-message", requestId: "run-for-nonce", message: { type: "text", content: EXPECTED_REPLY } }] },
  });
  const result = await runGrokBotMarkerSmoke({
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall: ledger.ledgerCall,
    sleepImpl: async () => {},
  });
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.reply_status, "observed");
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 1);
});

test("observed message/user tail correlation accepts exact same-request reply without rooted membership", async (t) => {
  const rootedResponses = [
    { name: "rooted endpoint empty", entries: [] },
    { name: "rooted endpoint only has user request", entries: [{
      id: "request-message-id",
      kind: "message",
      role: "user",
      requestId: "request-run-id",
    }] },
  ];
  for (const rooted of rootedResponses) {
    await t.test(rooted.name, async () => {
      const profile = syntheticProfile();
      const ledger = makeLedger();
      const { fetchImpl, calls } = makeFetch({
        sendBody: { accepted: true },
        transcript: [
          {
            id: "request-message-id",
            kind: "message",
            role: "user",
            content: "synthetic request body",
            isStreaming: false,
            timestampMs: 1,
            clientNonce: CLIENT_NONCE,
            requestId: "request-run-id",
            seq: 1,
          },
          {
            id: "reply-message-id",
            kind: "send-message",
            message: { type: "text", content: EXPECTED_REPLY },
            timestampMs: 2,
            requestId: "request-run-id",
            seq: 2,
          },
        ],
        thread: rooted,
      });
      const result = await runGrokBotMarkerSmoke({
        dependencies: dependencies(profile, fetchImpl),
        ledgerCall: ledger.ledgerCall,
        sleepImpl: async () => {},
      });
      assert.equal(result.ok, true);
      assert.equal(result.delivery_status, "accepted");
      assert.equal(result.reply_status, "observed");
      assert.equal(calls.filter((call) => call.path === "/api/getAgentThread").length, 0);
    });
  }
});

test("foreign nonce and old rooted reply never count; repeated operation only reads", async () => {
  const scenarios = [
    {
      name: "foreign nonce",
      transcript: [{ id: "other-request", kind: "user", clientNonce: "another-nonce", requestId: "other-run" }],
      thread: { entries: [] },
      expectedDelivery: "unknown",
      expectedThreadReads: 0,
    },
    {
      name: "old reply from another root",
      transcript: [{ id: "request-id", kind: "user", clientNonce: CLIENT_NONCE, requestId: "expected-run" }],
      thread: { entries: [{ id: "old-reply", role: "assistant", rootId: "old-root", text: EXPECTED_REPLY }] },
      expectedDelivery: "accepted",
      expectedThreadReads: 8,
    },
    {
      name: "reply tagged with another nonce",
      transcript: [{ id: "request-id", kind: "user", clientNonce: CLIENT_NONCE, requestId: "expected-run" }],
      thread: { entries: [{ id: "foreign-reply", kind: "send-message", role: "assistant", rootId: "request-id", requestId: "expected-run", clientNonce: "another-nonce", text: EXPECTED_REPLY }] },
      expectedDelivery: "accepted",
      expectedThreadReads: 8,
    },
  ];

  for (const scenario of scenarios) {
    const profile = syntheticProfile();
    const ledger = makeLedger();
    const { fetchImpl, calls } = makeFetch({
      sendBody: { accepted: true },
      transcript: scenario.transcript,
      thread: scenario.thread,
    });
    const deps = dependencies(profile, fetchImpl);
    const first = await runGrokBotMarkerSmoke({ dependencies: deps, ledgerCall: ledger.ledgerCall, sleepImpl: async () => {} });
    const second = await runGrokBotMarkerSmoke({ dependencies: deps, ledgerCall: ledger.ledgerCall, sleepImpl: async () => {} });
    assert.equal(first.delivery_status, scenario.expectedDelivery, scenario.name);
    assert.equal(first.reply_status, "pending", scenario.name);
    assert.equal(first.send_attempts, 1, scenario.name);
    assert.equal(second.send_attempts, 0, scenario.name);
    assert.equal(second.reply_status, "pending", scenario.name);
    assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 1, scenario.name);
    assert.equal(calls.filter((call) => call.path === "/api/getAgentThread").length, scenario.expectedThreadReads, scenario.name);
  }
});

test("parent reproducer: foreign requestId without reply rootId cannot report success", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger({ nonce: "11111111-1111-4111-8111-111111111111" });
  const { fetchImpl, calls } = makeFetch({
    sendBody: { accepted: true },
    transcript: [{
      id: "request-entry-1",
      kind: "user",
      clientNonce: "11111111-1111-4111-8111-111111111111",
      requestId: "expected-run",
    }],
    thread: { entries: [{
      id: "foreign-reply",
      kind: "send-message",
      requestId: "DIFFERENT-run",
      message: { type: "text", content: EXPECTED_REPLY },
    }] },
  });
  const result = await runGrokBotMarkerSmoke({
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall: ledger.ledgerCall,
    sleepImpl: async () => {},
  });
  assert.equal(result.ok, false);
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.reply_status, "pending");
  assert.deepEqual(calls.find((call) => call.path === "/api/getAgentThread").body, {
    id: "opaque-bot-id",
    rootId: "request-entry-1",
  });
});

test("missing reply requestId and foreign requestId with matching root stay pending", async (t) => {
  const cases = [
    {
      name: "missing requestId",
      reply: { id: "reply", kind: "send-message", rootId: "request-entry", text: EXPECTED_REPLY },
    },
    {
      name: "foreign requestId with matching root",
      reply: { id: "reply", kind: "send-message", rootId: "request-entry", requestId: "foreign-run", text: EXPECTED_REPLY },
    },
  ];
  for (const scenario of cases) {
    await t.test(scenario.name, async () => {
      const profile = syntheticProfile();
      const ledger = makeLedger();
      const { fetchImpl } = makeFetch({
        sendBody: { accepted: true },
        transcript: [{ id: "request-entry", kind: "user", clientNonce: CLIENT_NONCE, requestId: "expected-run" }],
        thread: { entries: [scenario.reply] },
      });
      const result = await runGrokBotMarkerSmoke({
        dependencies: dependencies(profile, fetchImpl),
        ledgerCall: ledger.ledgerCall,
        sleepImpl: async () => {},
      });
      assert.equal(result.delivery_status, "accepted");
      assert.equal(result.reply_status, "pending");
      assert.equal(result.ok, false);
    });
  }
});

test("contradictory user and assistant roles cannot provide request or reply correlation", async (t) => {
  await t.test("nonce entry marked user and assistant", async () => {
    const profile = syntheticProfile();
    const ledger = makeLedger();
    const { fetchImpl } = makeFetch({
      sendBody: { accepted: true },
      transcript: [{ id: "request-entry", kind: "user", role: "assistant", clientNonce: CLIENT_NONCE, requestId: "expected-run" }],
    });
    const result = await runGrokBotMarkerSmoke({
      dependencies: dependencies(profile, fetchImpl),
      ledgerCall: ledger.ledgerCall,
      sleepImpl: async () => {},
    });
    assert.equal(result.delivery_status, "unknown");
    assert.equal(result.reply_status, "unknown");
    assert.equal(result.code, "REQUEST_SCHEMA_INVALID");
  });

  await t.test("assistant role on user-kind reply", async () => {
    const profile = syntheticProfile();
    const ledger = makeLedger();
    const { fetchImpl } = makeFetch({
      sendBody: { accepted: true },
      transcript: [{ id: "request-entry", kind: "user", clientNonce: CLIENT_NONCE, requestId: "expected-run" }],
      thread: { entries: [{ id: "reply", kind: "user", role: "assistant", requestId: "expected-run", text: EXPECTED_REPLY }] },
    });
    const result = await runGrokBotMarkerSmoke({
      dependencies: dependencies(profile, fetchImpl),
      ledgerCall: ledger.ledgerCall,
      sleepImpl: async () => {},
    });
    assert.equal(result.delivery_status, "accepted");
    assert.equal(result.reply_status, "pending");
    assert.equal(result.ok, false);
  });
});

test("account signout after send stops further reads and leaves the send uncertain", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const { fetchImpl, calls } = makeFetch({ afterSend: () => profile.reader.signOut() });
  const result = await runGrokBotMarkerSmoke({
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall: ledger.ledgerCall,
    sleepImpl: async () => {},
  });
  assert.equal(result.delivery_status, "unknown");
  assert.equal(result.reply_status, "unknown");
  assert.equal(result.stage, "app_version");
  assert.equal(result.code, "APP_NOT_SIGNED_IN");
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 1);
  assert.equal(calls.filter((call) => call.path === "/api/getAgentTranscriptTail").length, 0);
});

test("selected-team drift after list fails before operation admission or send", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const { fetchImpl, calls } = makeFetch({ afterList: () => profile.reader.changeTeam("5678") });
  await assert.rejects(
    runGrokBotMarkerSmoke({
      dependencies: dependencies(profile, fetchImpl),
      ledgerCall: ledger.ledgerCall,
      sleepImpl: async () => {},
    }),
    (error) => error.code === "SELECTED_TEAM_CHANGED",
  );
  assert.equal(ledger.calls.length, 0);
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 0);
});

test("HTTP policy failure is sanitized and does not switch methods", async () => {
  const profile = syntheticProfile();
  const ledger = makeLedger();
  const { fetchImpl, calls } = makeFetch({
    transcript: [{ id: "request-id", kind: "user", clientNonce: CLIENT_NONCE, requestId: "expected-run" }],
    beforeResponse: (path) => path === "/api/getAgentTranscriptTail"
      ? response({ error: "PRIVATE_RESPONSE_BODY" }, 403)
      : null,
  });
  const result = await runGrokBotMarkerSmoke({
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall: ledger.ledgerCall,
    sleepImpl: async () => {},
  });
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.reply_status, "unknown");
  assert.equal(result.stage, "transcript");
  assert.equal(result.code, "HTTP_STATUS");
  assert.equal(JSON.stringify(result).includes("PRIVATE_RESPONSE_BODY"), false);
  assert.equal(calls.filter((call) => call.path.startsWith("/api/")).every((call) =>
    ["/api/listAgents", "/api/sendPrompt", "/api/getAgentTranscriptTail", "/api/getAgentThread"].includes(call.path)), true);
});

test("Node to Python ledger bridge admits one concurrent send and survives restart read-only", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "grok-marker-ledger-test-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const ledgerPath = join(directory, "synthetic.sqlite3");
  const ledgerCall = createLedgerCall({ ledgerPath });
  const profile = syntheticProfile();
  let exposeCorrelatedRequest = false;
  const { fetchImpl, calls } = makeFetch({
    sendBody: { accepted: true },
    transcript: (requests) => {
      if (!exposeCorrelatedRequest) return [];
      const send = requests.find((call) => call.path === "/api/sendPrompt");
      return send ? [{ id: "request-id", kind: "user", clientNonce: send.body.clientNonce, requestId: "expected-run" }] : [];
    },
    thread: { entries: [{ id: "reply-id", kind: "send-message", role: "assistant", rootId: "request-id", requestId: "expected-run", text: EXPECTED_REPLY }] },
  });
  const deps = dependencies(profile, fetchImpl);
  const [first, concurrent] = await Promise.all([
    runGrokBotMarkerSmoke({ dependencies: deps, ledgerCall, sleepImpl: async () => {} }),
    runGrokBotMarkerSmoke({ dependencies: deps, ledgerCall, sleepImpl: async () => {} }),
  ]);
  const nonce = calls.find((call) => call.path === "/api/sendPrompt").body.clientNonce;
  exposeCorrelatedRequest = true;
  const second = await runGrokBotMarkerSmoke({ dependencies: deps, ledgerCall, sleepImpl: async () => {} });
  const persisted = ledgerCall({
    action: "begin",
    operation_id: OPERATION_ID,
    account_identity: "synthetic-account",
    team_identity: "1234",
    target_identity: "opaque-bot-id",
    prompt: PROMPT,
  });

  assert.match(nonce, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.deepEqual([first.send_attempts, concurrent.send_attempts].sort(), [0, 1]);
  assert.equal(first.delivery_status, "unknown");
  assert.equal(concurrent.delivery_status, "unknown");
  assert.equal(second.ok, true);
  assert.equal(second.send_attempts, 0);
  assert.equal(persisted.created, false);
  assert.equal(persisted.client_nonce, nonce);
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 1);
});

test("--reconcile with missing ledger fails before profile, DPAPI, HTTP, or ledger creation", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "grok-marker-reconcile-missing-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const ledgerPath = join(directory, "missing.sqlite3");
  const adapter = createLedgerCall({ ledgerPath });
  const ledgerActions = [];
  const ledgerCall = (payload) => {
    ledgerActions.push(payload.action);
    return adapter(payload);
  };
  const profile = syntheticProfile();
  let profileReads = 0;
  const read = profile.reader.read;
  profile.reader.read = (name) => {
    profileReads += 1;
    return read(name);
  };
  const { fetchImpl, calls } = makeFetch();
  const output = { value: "", write(chunk) { this.value += chunk; } };
  const code = await main({
    argv: ["--reconcile"],
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall,
    stdout: output,
  });

  assert.equal(code, 1);
  assert.equal(JSON.parse(output.value).code, "OPERATION_NOT_FOUND");
  assert.deepEqual(ledgerActions, ["read"]);
  assert.equal(profileReads, 0);
  assert.equal(profile.dpapiCalls, 0);
  assert.equal(calls.length, 0);
  assert.equal(existsSync(ledgerPath), false);
});

test("--reconcile reads an unknown row, confirms only correlated evidence, and never admits or sends", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "grok-marker-reconcile-existing-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const ledgerPath = join(directory, "synthetic.sqlite3");
  const adapter = createLedgerCall({ ledgerPath });
  const admission = seedReconciliationRow(adapter, { state: "unknown" });
  const ledgerActions = [];
  const ledgerCall = (payload) => {
    ledgerActions.push(payload.action);
    return adapter(payload);
  };
  const profile = syntheticProfile();
  const { fetchImpl, calls } = makeFetch({
    // The named Bot now has another ID. Reconciliation must use the persisted opaque ID.
    agents: [{ id: "different-current-id", name: "GrokBot" }],
    transcript: [{
      id: "live-request-message-id",
      kind: "message",
      role: "user",
      clientNonce: admission.client_nonce,
      requestId: "live-request-run-id",
    }, {
      id: "live-reply-message-id",
      kind: "send-message",
      message: { type: "text", content: EXPECTED_REPLY },
      requestId: "live-request-run-id",
    }],
    thread: { entries: [{
      id: "live-request-message-id",
      kind: "message",
      role: "user",
      requestId: "live-request-run-id",
    }] },
  });
  const output = { value: "", write(chunk) { this.value += chunk; } };
  const code = await main({
    argv: ["--reconcile"],
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall,
    stdout: output,
  });
  const result = JSON.parse(output.value);
  const readAfter = adapter({ action: "read", operation_id: OPERATION_ID });

  assert.equal(code, 0);
  assert.equal(result.ok, true);
  assert.equal(result.send_attempts, 0);
  assert.equal(result.remote_terminal, "unsupported");
  assert.equal(readAfter.transport_state, "accepted");
  assert.equal(readAfter.client_nonce, admission.client_nonce);
  assert.deepEqual(ledgerActions, ["read", "confirm"]);
  assert.deepEqual(calls.find((call) => call.path === "/api/getAgentTranscriptTail").body, {
    id: "opaque-bot-id",
    limit: 40,
  });
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 0);
  assert.equal(calls.filter((call) => call.path === "/api/getAgentThread").length, 0);

  ledgerActions.length = 0;
  const acceptedOutput = { value: "", write(chunk) { this.value += chunk; } };
  const acceptedCode = await main({
    argv: ["--reconcile"],
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall,
    stdout: acceptedOutput,
  });
  assert.equal(acceptedCode, 0);
  assert.equal(JSON.parse(acceptedOutput.value).ok, true);
  assert.deepEqual(ledgerActions, ["read"]);
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 0);
});

test("--reconcile can confirm delivered user message without inventing a missing requestId", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "grok-marker-reconcile-no-run-id-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const ledgerPath = join(directory, "synthetic.sqlite3");
  const adapter = createLedgerCall({ ledgerPath });
  const admission = seedReconciliationRow(adapter, { state: "unknown" });
  const ledgerActions = [];
  const ledgerCall = (payload) => {
    ledgerActions.push(payload);
    return adapter(payload);
  };
  const profile = syntheticProfile();
  const { fetchImpl, calls } = makeFetch({
    transcript: [{
      id: "request-message-without-run-id",
      kind: "message",
      role: "user",
      clientNonce: admission.client_nonce,
    }],
    thread: { entries: [{
      id: "uncorrelated-reply",
      kind: "send-message",
      requestId: "not-inferred-from-message-id",
      message: { type: "text", content: EXPECTED_REPLY },
    }] },
  });
  const output = { value: "", write(chunk) { this.value += chunk; } };
  const code = await main({
    argv: ["--reconcile"],
    dependencies: dependencies(profile, fetchImpl),
    ledgerCall,
    stdout: output,
  });
  const result = JSON.parse(output.value);
  const record = adapter({ action: "read", operation_id: OPERATION_ID });

  assert.equal(code, 1);
  assert.equal(result.delivery_status, "accepted");
  assert.equal(result.reply_status, "pending");
  assert.equal(result.ok, false);
  assert.equal(record.transport_state, "accepted");
  assert.equal(record.client_nonce, admission.client_nonce);
  assert.deepEqual(ledgerActions.map((call) => call.action), ["read", "confirm"]);
  assert.equal(ledgerActions[1].request_id, null);
  assert.equal(calls.filter((call) => call.path === "/api/getAgentThread").length, 0);
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 0);
});

test("--reconcile account and team drift fail before EnsureSandBox/listAgents", async (t) => {
  for (const scenario of [
    { name: "account", accountIdentity: "different-account", code: "EXPECTED_ACCOUNT_MISMATCH", expectedDpapi: 0 },
    { name: "team", teamIdentity: "5678", code: "EXPECTED_TEAM_MISMATCH", expectedDpapi: 1 },
  ]) {
    await t.test(scenario.name, async () => {
      const directory = mkdtempSync(join(tmpdir(), `grok-marker-reconcile-${scenario.name}-`));
      t.after(() => rmSync(directory, { recursive: true, force: true }));
      const ledgerPath = join(directory, "synthetic.sqlite3");
      const adapter = createLedgerCall({ ledgerPath });
      seedReconciliationRow(adapter, {
        accountIdentity: scenario.accountIdentity ?? "synthetic-account",
        teamIdentity: scenario.teamIdentity ?? "1234",
      });
      const ledgerActions = [];
      const ledgerCall = (payload) => {
        ledgerActions.push(payload.action);
        return adapter(payload);
      };
      const profile = syntheticProfile();
      const { fetchImpl, calls } = makeFetch();
      const output = { value: "", write(chunk) { this.value += chunk; } };
      const code = await main({
        argv: ["--reconcile"],
        dependencies: dependencies(profile, fetchImpl),
        ledgerCall,
        stdout: output,
      });
      assert.equal(code, 1);
      assert.equal(JSON.parse(output.value).code, scenario.code);
      assert.equal(profile.dpapiCalls, scenario.expectedDpapi);
      assert.equal(calls.length, 0);
      assert.deepEqual(ledgerActions, ["read"]);
    });
  }
});

test("reconciliation gateway session refuses sendPrompt even if requested", async () => {
  const profile = syntheticProfile();
  const { fetchImpl, calls } = makeFetch();
  await assert.rejects(
    runAuthenticatedListAction({
      ...dependencies(profile, fetchImpl),
      allowSendPrompt: false,
      action: (session) => session.gatewayRequest(
        "sendPrompt",
        { agentId: "opaque-bot-id", prompt: PROMPT, clientNonce: CLIENT_NONCE },
        "send_prompt",
      ),
    }),
    (error) => error.code === "GATEWAY_METHOD_NOT_ALLOWED",
  );
  assert.equal(calls.filter((call) => call.path === "/api/sendPrompt").length, 0);
});

test("execute gate and import default perform no profile or network access", async () => {
  const output = { value: "" , write(chunk) { this.value += chunk; } };
  let called = false;
  const code = await main({
    argv: [],
    dependencies: { get profileReader() { called = true; throw new Error("should not read profile"); } },
    stdout: output,
  });
  assert.equal(code, 2);
  assert.equal(called, false);
  assert.deepEqual(JSON.parse(output.value), {
    ok: false,
    stage: "arguments",
    code: "EXECUTE_OR_RECONCILE_FLAG_REQUIRED",
  });
});
