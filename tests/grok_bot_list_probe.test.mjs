import assert from "node:assert/strict";
import { createCipheriv } from "node:crypto";
import test from "node:test";

import {
  createWindowsDpapiUnprotect,
  main,
  runListProbe,
} from "../scripts/grok_bot_list_probe.mjs";

const ENSURE_URL =
  "https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox";
const STORAGE_KEY = Buffer.alloc(32, 0x45);
const WRAPPED_KEY = Buffer.from("synthetic-wrapped-dpapi-key");
let nonceCounter = 0;

function encryptV10(text) {
  const nonce = Buffer.alloc(12);
  nonce.writeUInt32BE(++nonceCounter, 8);
  const cipher = createCipheriv("aes-256-gcm", STORAGE_KEY, nonce);
  const ciphertext = Buffer.concat([
    cipher.update(text, "utf8"),
    cipher.final(),
  ]);
  return Buffer.concat([
    Buffer.from("v10"),
    nonce,
    ciphertext,
    cipher.getAuthTag(),
  ]).toString("base64");
}

function accountRecord(label, teamId = "1234") {
  return {
    "cursor-access-token": encryptV10(`synthetic-access-${label}`),
    ...(teamId == null ? {} : { "cursor-selected-team-id": encryptV10(teamId) }),
  };
}

function makeSecrets({ activeIdentity = "acct-a", selectedTeam = "1234" } = {}) {
  const accounts = {
    "acct-a": accountRecord("a", selectedTeam),
    "acct-b": accountRecord("b", "5678"),
  };
  return {
    version: 1,
    "cursor-accounts": JSON.stringify({
      ...(activeIdentity == null ? {} : { active: activeIdentity }),
      accounts,
    }),
  };
}

function syntheticProfile({
  appVersion = "0.75.5",
  signedIn = true,
  omitSignedIn = false,
  secrets = makeSecrets(),
} = {}) {
  let currentSecrets = secrets;
  let currentSignedIn = signedIn;
  let omitCurrentSignedIn = omitSignedIn;
  const counts = new Map();
  let beforeRead = null;
  const reader = {
    read(name) {
      const count = (counts.get(name) ?? 0) + 1;
      counts.set(name, count);
      beforeRead?.(name, count, reader);
      if (name === "sand-secrets.json") return JSON.stringify(currentSecrets);
      if (name === "desktop-status.json") {
        return JSON.stringify({
          appVersion,
          ...(omitCurrentSignedIn ? {} : { signedIn: currentSignedIn }),
        });
      }
      if (name === "Local State") {
        return JSON.stringify({
          os_crypt: {
            encrypted_key: Buffer.concat([
              Buffer.from("DPAPI"),
              WRAPPED_KEY,
            ]).toString("base64"),
          },
        });
      }
      throw new Error("synthetic profile file not allowed");
    },
    readCount(name) {
      return counts.get(name) ?? 0;
    },
    setSecrets(value) {
      currentSecrets = value;
    },
    getSecrets() {
      return currentSecrets;
    },
    setBeforeRead(callback) {
      beforeRead = callback;
    },
    setSignedIn(value) {
      currentSignedIn = value;
      omitCurrentSignedIn = false;
    },
    omitSignedIn() {
      omitCurrentSignedIn = true;
    },
  };
  return reader;
}

function changeActive(profile, identity) {
  const current = JSON.parse(profile.getSecrets()["cursor-accounts"]);
  current.active = identity;
  profile.setSecrets({
    ...profile.getSecrets(),
    "cursor-accounts": JSON.stringify(current),
  });
}

function changeTeam(profile, identity, teamId) {
  const current = JSON.parse(profile.getSecrets()["cursor-accounts"]);
  current.accounts[identity]["cursor-selected-team-id"] = encryptV10(teamId);
  profile.setSecrets({
    ...profile.getSecrets(),
    "cursor-accounts": JSON.stringify(current),
  });
}

function fakeDpapi() {
  let calls = 0;
  const callback = (wrapped) => {
    calls += 1;
    assert.deepEqual(wrapped, WRAPPED_KEY);
    return Buffer.from(STORAGE_KEY);
  };
  callback.callCount = () => calls;
  return callback;
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function routeBody(overrides = {}) {
  return {
    gatewayUrl: "https://box-123.us12.cursorvm.com",
    gatewayToken: "synthetic-gateway-token",
    gatewayHeaders: { "x-anyrun-network-token": "synthetic-route-header" },
    ...overrides,
  };
}

function listBody() {
  return {
    agents: [
      { id: "bot-a", name: "Synthetic bot" },
      { id: "group-a", isGroup: true, memberIds: ["bot-a"] },
    ],
  };
}

test("existing active account recovers a route then calls only listAgents", async () => {
  const profile = syntheticProfile();
  const dpapi = fakeDpapi();
  const requests = [];
  const result = await runListProbe({
    profileReader: profile,
    dpapiUnprotect: dpapi,
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      assert.equal(options.method, "POST");
      assert.equal(options.redirect, "error");
      assert.equal(options.body, "{}");
      assert.ok(options.signal instanceof AbortSignal);
      if (url === ENSURE_URL) {
        assert.equal(options.headers.authorization, "Bearer synthetic-access-a");
        assert.equal(options.headers["x-cursor-client-type"], "sand");
        assert.equal(options.headers["x-cursor-client-version"], "0.75.5");
        assert.equal(options.headers["x-sand-box-namespace"], "prod");
        assert.equal(options.headers["x-cursor-team-id"], "1234");
        return jsonResponse(routeBody());
      }
      if (url === "https://box-123.us12.cursorvm.com/api/listAgents") {
        assert.equal(options.headers.authorization, "Bearer synthetic-gateway-token");
        assert.equal(options.headers["x-anyrun-network-token"], "synthetic-route-header");
        assert.equal(options.headers["x-cursor-team-id"], undefined);
        assert.equal(options.headers["x-cursor-client-version"], undefined);
        return jsonResponse(listBody());
      }
      throw new Error("synthetic request outside allowlist");
    },
  });

  assert.equal(dpapi.callCount(), 1);
  assert.deepEqual(
    requests.map(({ url }) => url),
    [ENSURE_URL, "https://box-123.us12.cursorvm.com/api/listAgents"],
  );
  assert.deepEqual(result, {
    ok: true,
    stage: "complete",
    httpStatus: { ensureSandbox: 200, listAgents: 200 },
    botCount: 1,
    groupCount: 1,
  });
  const output = JSON.stringify(result);
  for (const secret of [
    "synthetic-access-a",
    "synthetic-gateway-token",
    "synthetic-route-header",
    "box-123",
    "acct-a",
  ]) {
    assert.equal(output.includes(secret), false);
  }
});

test("DPAPI adapter uses hidden bounded child pipes and keeps wrapped bytes off argv", () => {
  let invocation;
  const adapter = createWindowsDpapiUnprotect(
    (executable, args, options) => {
      invocation = { executable, args, options };
      return Buffer.from(STORAGE_KEY.toString("base64"), "ascii");
    },
    { systemRoot: "C:\\SyntheticWindows" },
  );
  const wrapped = Buffer.from("synthetic-dpapi-input");

  assert.deepEqual(adapter(wrapped), STORAGE_KEY);
  assert.match(invocation.executable, /SyntheticWindows.*powershell\.exe$/i);
  assert.equal(invocation.options.windowsHide, true);
  assert.equal(invocation.options.timeout, 5_000);
  assert.equal(invocation.options.maxBuffer, 64 * 1024);
  assert.deepEqual(invocation.options.stdio, ["pipe", "pipe", "pipe"]);
  assert.equal(
    invocation.options.input.toString("ascii"),
    wrapped.toString("base64"),
  );
  assert.equal(invocation.args.some((arg) => arg.includes(wrapped.toString("base64"))), false);
  assert.match(invocation.args.at(-1), /DataProtectionScope\]::CurrentUser/);
});

test("missing or ambiguous active account fails before DPAPI or network", async (t) => {
  const inactive = makeSecrets();
  const inactiveAccounts = JSON.parse(inactive["cursor-accounts"]);
  delete inactiveAccounts.accounts["acct-a"]["cursor-access-token"];
  inactive["cursor-accounts"] = JSON.stringify(inactiveAccounts);
  for (const [name, secrets, expectedCode] of [
    ["multiple accounts without selection", makeSecrets({ activeIdentity: null }), "ACTIVE_ACCOUNT_AMBIGUOUS"],
    ["stale active account key", makeSecrets({ activeIdentity: "missing-account" }), "ACTIVE_ACCOUNT_NOT_FOUND"],
    ["inactive selected account", inactive, "ACTIVE_ACCESS_TOKEN_MISSING"],
  ]) {
    await t.test(name, async () => {
      const dpapi = fakeDpapi();
      let networkCalls = 0;
      await assert.rejects(
        runListProbe({
          profileReader: syntheticProfile({ secrets }),
          dpapiUnprotect: dpapi,
          fetchImpl: async () => {
            networkCalls += 1;
            return jsonResponse({});
          },
        }),
        (error) => error.code === expectedCode,
      );
      assert.equal(dpapi.callCount(), 0);
      assert.equal(networkCalls, 0);
    });
  }
});

test("active account and selected team drift stop at each network boundary", async (t) => {
  const cases = [
    {
      name: "before EnsureSandBox",
      readNumber: 3,
      mutate: changeActive,
      mutateArgs: ["acct-b"],
      expectedCode: "ACTIVE_ACCOUNT_CHANGED",
      ensureCalls: 0,
      listCalls: 0,
    },
    {
      name: "after EnsureSandBox",
      readNumber: 4,
      mutate: changeTeam,
      mutateArgs: ["acct-a", "9999"],
      expectedCode: "SELECTED_TEAM_CHANGED",
      ensureCalls: 1,
      listCalls: 0,
    },
    {
      name: "before listAgents",
      readNumber: 5,
      mutate: changeActive,
      mutateArgs: ["acct-b"],
      expectedCode: "ACTIVE_ACCOUNT_CHANGED",
      ensureCalls: 1,
      listCalls: 0,
    },
    {
      name: "after listAgents",
      readNumber: 6,
      mutate: changeTeam,
      mutateArgs: ["acct-a", "9999"],
      expectedCode: "SELECTED_TEAM_CHANGED",
      ensureCalls: 1,
      listCalls: 1,
    },
  ];

  for (const scenario of cases) {
    await t.test(scenario.name, async () => {
      const profile = syntheticProfile();
      profile.setBeforeRead((name, count, reader) => {
        if (name === "sand-secrets.json" && count === scenario.readNumber) {
          scenario.mutate(reader, ...scenario.mutateArgs);
        }
      });
      let ensureCalls = 0;
      let listCalls = 0;
      await assert.rejects(
        runListProbe({
          profileReader: profile,
          dpapiUnprotect: fakeDpapi(),
          fetchImpl: async (url) => {
            if (url === ENSURE_URL) {
              ensureCalls += 1;
              return jsonResponse(routeBody());
            }
            if (url.endsWith("/api/listAgents")) {
              listCalls += 1;
              return jsonResponse(listBody());
            }
            throw new Error("synthetic forbidden URL");
          },
        }),
        (error) => error.code === scenario.expectedCode,
      );
      assert.equal(ensureCalls, scenario.ensureCalls);
      assert.equal(listCalls, scenario.listCalls);
    });
  }
});

test("invalid desktop app version fails before credential decryption", async () => {
  const dpapi = fakeDpapi();
  let networkCalls = 0;
  await assert.rejects(
    runListProbe({
      profileReader: syntheticProfile({ appVersion: "unknown-version" }),
      dpapiUnprotect: dpapi,
      fetchImpl: async () => {
        networkCalls += 1;
        return jsonResponse({});
      },
    }),
    (error) => error.code === "APP_VERSION_INVALID",
  );
  assert.equal(dpapi.callCount(), 0);
  assert.equal(networkCalls, 0);
});

test("missing or false signedIn fails before DPAPI or network", async (t) => {
  for (const [name, profile] of [
    ["false", syntheticProfile({ signedIn: false })],
    ["missing", syntheticProfile({ omitSignedIn: true })],
  ]) {
    await t.test(name, async () => {
      const dpapi = fakeDpapi();
      let networkCalls = 0;
      await assert.rejects(
        runListProbe({
          profileReader: profile,
          dpapiUnprotect: dpapi,
          fetchImpl: async () => {
            networkCalls += 1;
            return jsonResponse({});
          },
        }),
        (error) =>
          error.stage === "app_version" && error.code === "APP_NOT_SIGNED_IN",
      );
      assert.equal(dpapi.callCount(), 0);
      assert.equal(networkCalls, 0);
    });
  }
});

test("mid-request signout is rechecked at account and team boundaries", async (t) => {
  const cases = [
    { name: "before DPAPI", statusRead: 2, ensure: 0, list: 0, dpapi: 0 },
    { name: "before access token use", statusRead: 3, ensure: 0, list: 0, dpapi: 1 },
    { name: "before EnsureSandBox", statusRead: 4, ensure: 0, list: 0, dpapi: 1 },
    { name: "after EnsureSandBox", statusRead: 5, ensure: 1, list: 0, dpapi: 1 },
    { name: "before listAgents", statusRead: 6, ensure: 1, list: 0, dpapi: 1 },
    { name: "after listAgents", statusRead: 7, ensure: 1, list: 1, dpapi: 1 },
  ];

  for (const scenario of cases) {
    await t.test(scenario.name, async () => {
      const profile = syntheticProfile();
      profile.setBeforeRead((name, count, reader) => {
        if (name === "desktop-status.json" && count === scenario.statusRead) {
          reader.setSignedIn(false);
        }
      });
      const dpapi = fakeDpapi();
      let ensureCalls = 0;
      let listCalls = 0;
      await assert.rejects(
        runListProbe({
          profileReader: profile,
          dpapiUnprotect: dpapi,
          fetchImpl: async (url) => {
            if (url === ENSURE_URL) {
              ensureCalls += 1;
              return jsonResponse(routeBody());
            }
            if (url.endsWith("/api/listAgents")) {
              listCalls += 1;
              return jsonResponse(listBody());
            }
            throw new Error("synthetic request outside allowlist");
          },
        }),
        (error) => error.code === "APP_NOT_SIGNED_IN",
      );
      assert.equal(dpapi.callCount(), scenario.dpapi);
      assert.equal(ensureCalls, scenario.ensure);
      assert.equal(listCalls, scenario.list);
    });
  }
});

test("redirects and disallowed gateway destinations fail before listAgents", async (t) => {
  const scenarios = [
    {
      name: "EnsureSandBox redirect",
      response: () => new Response("", {
        status: 302,
        headers: { location: "https://redirect.example/" },
      }),
      expectedCode: "REDIRECT_REJECTED",
    },
    {
      name: "unapproved gateway host",
      response: () => jsonResponse(routeBody({ gatewayUrl: "https://box.attacker.example" })),
      expectedCode: "GATEWAY_URL_REJECTED",
    },
    {
      name: "gateway URL userinfo",
      response: () => jsonResponse(routeBody({ gatewayUrl: "https://user@box.cursorvm.com" })),
      expectedCode: "GATEWAY_URL_REJECTED",
    },
    {
      name: "gateway URL query",
      response: () => jsonResponse(routeBody({ gatewayUrl: "https://box.cursorvm.com/?token=synthetic" })),
      expectedCode: "GATEWAY_URL_REJECTED",
    },
  ];
  for (const scenario of scenarios) {
    await t.test(scenario.name, async () => {
      let listCalls = 0;
      await assert.rejects(
        runListProbe({
          profileReader: syntheticProfile(),
          dpapiUnprotect: fakeDpapi(),
          fetchImpl: async (url) => {
            if (url === ENSURE_URL) return scenario.response();
            if (url.endsWith("/api/listAgents")) listCalls += 1;
            throw new Error("synthetic unexpected request");
          },
        }),
        (error) => error.code === scenario.expectedCode,
      );
      assert.equal(listCalls, 0);
    });
  }
});

test("EnsureSandBox and listAgents response bodies are capped at 2 MiB", async () => {
  let listCalls = 0;
  await assert.rejects(
    runListProbe({
      profileReader: syntheticProfile(),
      dpapiUnprotect: fakeDpapi(),
      fetchImpl: async (url) => {
        if (url === ENSURE_URL) {
          return new Response(Buffer.alloc(2 * 1024 * 1024 + 1), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        listCalls += 1;
        return jsonResponse(listBody());
      },
    }),
    (error) => error.code === "RESPONSE_TOO_LARGE",
  );
  assert.equal(listCalls, 0);
});

test("sanitized failure output omits raw errors and response bodies", async (t) => {
  const secret = "synthetic-secret-that-must-not-be-printed";
  for (const scenario of [
    {
      name: "transport exception",
      fetchImpl: async () => { throw new Error(`Bearer ${secret}`); },
      expected: { ok: false, stage: "ensure_sandbox", code: "REQUEST_FAILED" },
    },
    {
      name: "HTTP response body",
      fetchImpl: async () => new Response(`private body ${secret}`, { status: 401 }),
      expected: {
        ok: false,
        stage: "ensure_sandbox",
        code: "HTTP_STATUS",
        status: 401,
      },
    },
  ]) {
    await t.test(scenario.name, async () => {
      const stdout = { value: "", write(text) { this.value += text; } };
      const exitCode = await main({
        argv: ["--execute"],
        dependencies: {
          profileReader: syntheticProfile(),
          dpapiUnprotect: fakeDpapi(),
          fetchImpl: scenario.fetchImpl,
        },
        stdout,
      });
      assert.equal(exitCode, 1);
      assert.deepEqual(JSON.parse(stdout.value), scenario.expected);
      assert.equal(stdout.value.includes(secret), false);
      assert.equal(stdout.value.includes("Bearer"), false);
    });
  }
});

test("non-2xx diagnostics retain only allowlisted error code and correlation ID", async () => {
  const secret = "Bearer synthetic-auth-secret private response text";
  const stdout = { value: "", write(text) { this.value += text; } };
  const exitCode = await main({
    argv: ["--execute"],
    dependencies: {
      profileReader: syntheticProfile(),
      dpapiUnprotect: fakeDpapi(),
      fetchImpl: async () => new Response(JSON.stringify({
        error: {
          code: "permission_denied",
          message: secret,
          requestId: "body-request-id",
        },
        privateErrorCode: "private_backend_failure",
        authorization: secret,
      }), {
        status: 403,
        headers: {
          "content-type": "application/json",
          "x-request-id": "request-1234",
          "x-error-code": "private_backend_failure",
          authorization: `Bearer ${secret}`,
        },
      }),
    },
    stdout,
  });

  assert.equal(exitCode, 1);
  assert.deepEqual(JSON.parse(stdout.value), {
    ok: false,
    stage: "ensure_sandbox",
    code: "HTTP_STATUS",
    status: 403,
    errorCode: "permission_denied",
    requestId: "request-1234",
  });
  assert.equal(stdout.value.includes(secret), false);
  assert.equal(stdout.value.includes("private_backend_failure"), false);
  assert.equal(stdout.value.includes("body-request-id"), false);
});

test("non-2xx diagnostics accept a safe correlation header when request ID is absent", async () => {
  const stdout = { value: "", write(text) { this.value += text; } };
  const exitCode = await main({
    argv: ["--execute"],
    dependencies: {
      profileReader: syntheticProfile(),
      dpapiUnprotect: fakeDpapi(),
      fetchImpl: async () => new Response(JSON.stringify({
        errorCode: "unauthenticated",
        correlation_id: "correlation-5678",
      }), {
        status: 401,
        headers: {
          "content-type": "application/json",
          "x-correlation-id": "correlation-5678",
        },
      }),
    },
    stdout,
  });

  assert.equal(exitCode, 1);
  assert.deepEqual(JSON.parse(stdout.value), {
    ok: false,
    stage: "ensure_sandbox",
    code: "HTTP_STATUS",
    status: 401,
    errorCode: "unauthenticated",
    requestId: "correlation-5678",
  });
});

test("explicit execution flag is required before constructing live dependencies", async () => {
  const stdout = { value: "", write(text) { this.value += text; } };
  const exitCode = await main({ argv: [], stdout });
  assert.equal(exitCode, 2);
  assert.deepEqual(JSON.parse(stdout.value), {
    ok: false,
    stage: "arguments",
    code: "EXECUTE_FLAG_REQUIRED",
  });
});
