import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { collectClaude, claudeLogin, groupsFromUsage, keychainAccount, readKeychain, retryAt } from "../lib/sources/claude.mjs";

function account(t, oauth) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "herdr-claude-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, ".credentials.json");
  fs.writeFileSync(file, JSON.stringify({ untouched: { enabled: true }, claudeAiOauth: oauth }));
  return { file, settingsPath: path.join(dir, "settings.json") };
}
const response = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers });

test("quota excludes absent windows and keeps missing usage unknown", () => {
  const [g] = groupsFromUsage({ five_hour: null, seven_day: { utilization: 25, resets_at: "2026-11-20T00:00:00Z" }, seven_day_sonnet: { resets_at: "2026-11-20T00:00:00Z" } }, { dir: "account", observed: 123 });
  assert.equal(g.observed_at, 123);
  assert.equal(g.windows.length, 2);
  assert.equal(g.windows[0].remaining_percent, 75);
  assert.equal(g.windows[1].used_percent, null);
  assert.deepEqual(groupsFromUsage({}, { dir: "account" }), []);
});

test("expired credentials renew once, preserve metadata and persist rotating token", async (t) => {
  const a = account(t, { accessToken: "old", refreshToken: "refresh", expiresAt: 1, subscriptionType: "max" });
  const calls = [];
  const r = await collectClaude({ settingsPath: a.settingsPath, fetcher: async (url, options) => {
    calls.push(url);
    if (url.includes("/v1/oauth/token")) {
      assert.equal(JSON.parse(options.body).grant_type, "refresh_token");
      return response({ access_token: "new", refresh_token: "rotated", expires_in: 3600 });
    }
    assert.equal(options.headers.authorization, "Bearer new");
    return response({ seven_day: { utilization: 25 } });
  } });
  assert.equal(r.status, "ok");
  assert.equal(calls.length, 2);
  const saved = JSON.parse(fs.readFileSync(a.file));
  assert.equal(saved.claudeAiOauth.refreshToken, "rotated");
  assert.equal(saved.claudeAiOauth.subscriptionType, "max");
  assert.deepEqual(saved.untouched, { enabled: true });
  assert.equal(fs.existsSync(`${a.file}.herdr-usage-refresh.lock`), false);
});

test("a concurrent CLI login is never overwritten", async (t) => {
  const a = account(t, { accessToken: "old", refreshToken: "refresh", expiresAt: 1 });
  const r = await collectClaude({ settingsPath: a.settingsPath, fetcher: async (url, options) => {
    if (url.includes("/v1/oauth/token")) {
      fs.writeFileSync(a.file, JSON.stringify({ claudeAiOauth: { accessToken: "cli-token", refreshToken: "cli-refresh" }, added: true }));
      return response({ access_token: "collector-token", refresh_token: "collector-refresh", expires_in: 3600 });
    }
    assert.equal(options.headers.authorization, "Bearer cli-token");
    return response({ seven_day: { utilization: 25 } });
  } });
  assert.equal(r.status, "ok");
  assert.equal(JSON.parse(fs.readFileSync(a.file)).claudeAiOauth.refreshToken, "cli-refresh");
  assert.equal(JSON.parse(fs.readFileSync(a.file)).added, true);
});

test("rate limit returns retry deadline, no raw server text", async (t) => {
  const a = account(t, { accessToken: "token" });
  const r = await collectClaude({ settingsPath: a.settingsPath, fetcher: async () => response({ secret: "never log" }, 429, { "retry-after": "600" }) });
  assert.equal(r.status, "error");
  assert.equal(r.last_error, "http 429");
  assert.ok(r.retry_until >= Math.floor(Date.now() / 1000) + 599);
  assert.equal(JSON.stringify(r).includes("never log"), false);
  assert.equal(retryAt(response({}, 429, { "retry-after": "Wed, 21 Oct 2015 07:28:00 GMT" }), 100), 86500);
});

test("failed refresh preserves credentials and returns login status", async (t) => {
  const a = account(t, { accessToken: "old", refreshToken: "refresh", expiresAt: 1 });
  const before = fs.readFileSync(a.file, "utf8");
  const r = await collectClaude({ settingsPath: a.settingsPath, fetcher: async () => response({}, 401) });
  assert.equal(r.status, "connect_needed");
  assert.equal(fs.readFileSync(a.file, "utf8"), before);
});

test("network exception never exposes its message", async (t) => {
  const a = account(t, { accessToken: "token" });
  const r = await collectClaude({ settingsPath: a.settingsPath, fetcher: async () => { throw new Error("Bearer secret-token"); } });
  assert.equal(r.last_error, "network_error");
});

test("abandoned refresh lock recovers, a recent lock prevents a second renewal", async (t) => {
  const a = account(t, { accessToken: "old", refreshToken: "refresh", expiresAt: 1 });
  const lock = `${a.file}.herdr-usage-refresh.lock`;
  fs.mkdirSync(lock);
  let calls = 0;
  const fetcher = async (url) => {
    calls++;
    return response(url.includes("/v1/oauth/token") ? { access_token: "new", expires_in: 3600 } : { seven_day: { utilization: 25 } });
  };
  const busy = await collectClaude({ settingsPath: a.settingsPath, fetcher });
  assert.equal(busy.last_error, "refresh_busy");
  assert.equal(calls, 0);
  const old = new Date(Date.now() - 120000);
  fs.utimesSync(lock, old, old);
  assert.equal((await collectClaude({ settingsPath: a.settingsPath, fetcher })).status, "ok");
  assert.equal(calls, 2);
});

test("401 after proactive renewal does not rotate credentials twice", async (t) => {
  const a = account(t, { accessToken: "old", refreshToken: "refresh", expiresAt: 1 });
  let calls = 0;
  const r = await collectClaude({ settingsPath: a.settingsPath, fetcher: async (url) => {
    calls++;
    return url.includes("/v1/oauth/token") ? response({ access_token: "new", expires_in: 3600 }) : response({}, 401);
  } });
  assert.equal(r.status, "connect_needed");
  assert.equal(calls, 2);
});


test("macOS default login prefers Keychain over a stale credential file", async () => {
  const file = path.join(os.homedir(), ".claude", ".credentials.json");
  const oauth = { accessToken: "current-keychain-token" };
  const result = await claudeLogin(file, { platform: "darwin", keychain: async () => oauth });
  assert.deepEqual(result, { oauth, keychain: true });
});

test("custom config never reads the default account Keychain", async (t) => {
  const a = account(t, { accessToken: "custom-token" });
  const result = await claudeLogin(a.file, { platform: "darwin", keychain: async () => assert.fail("wrong account") });
  assert.equal(result.oauth.accessToken, "custom-token");
  assert.equal(result.keychain, false);
});

test("Keychain credentials are queried without rotating or copying them", async (t) => {
  const a = account(t, { accessToken: "stale-file-token" });
  const before = fs.readFileSync(a.file, "utf8");
  let calls = 0;
  const result = await collectClaude({ settingsPath: a.settingsPath,
    login: async () => ({ keychain: true, oauth: { accessToken: "current-token", refreshToken: "secret", expiresAt: 1 } }),
    fetcher: async (url, options) => {
      calls++;
      assert.ok(url.endsWith("/api/oauth/usage"));
      assert.equal(options.headers.authorization, "Bearer current-token");
      return response({}, 401);
    } });
  assert.equal(result.status, "connect_needed");
  assert.equal(calls, 1);
  assert.equal(fs.readFileSync(a.file, "utf8"), before);
});

test("Keychain account follows Claude Code: $USER, then the OS user name, then a fixed fallback", () => {
  const noInfo = () => assert.fail("userInfo must not be read when $USER is set");
  assert.equal(keychainAccount({ env: { USER: "dev.user_1-a" }, userInfo: noInfo }), "dev.user_1-a");
  assert.equal(keychainAccount({ env: {}, userInfo: () => ({ username: "osuser" }) }), "osuser");
  assert.equal(keychainAccount({ env: { USER: "" }, userInfo: () => ({ username: "osuser" }) }), "osuser");
  // A name Claude Code rejects is not passed on; it uses the fixed account instead.
  assert.equal(keychainAccount({ env: { USER: "two words" }, userInfo: noInfo }), "claude-code-user");
  assert.equal(keychainAccount({ env: { USER: "../x;rm" }, userInfo: noInfo }), "claude-code-user");
  assert.equal(keychainAccount({ env: {}, userInfo: () => ({ username: "us\u00e9r" }) }), "claude-code-user");
  assert.equal(keychainAccount({ env: {}, userInfo: () => { throw new Error("no passwd entry"); } }), "claude-code-user");
  assert.equal(keychainAccount({ env: {}, userInfo: () => ({ username: "" }) }), "claude-code-user");
});

test("Keychain lookup selects the entry by service and current-user account", async () => {
  const calls = [];
  const oauth = await readKeychain({ account: "someone", run: async (cmd, args, options) => {
    calls.push({ cmd, args, options });
    return { stdout: JSON.stringify({ claudeAiOauth: { accessToken: "tok" } }) };
  } });
  assert.deepEqual(oauth, { accessToken: "tok" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].cmd, "/usr/bin/security");
  assert.deepEqual(calls[0].args, ["find-generic-password", "-s", "Claude Code-credentials", "-a", "someone", "-w"]);
  assert.equal(calls[0].options.timeout, 5000);
});

test("Keychain lookup defaults to the current user's account", async (t) => {
  const saved = process.env.USER;
  t.after(() => { if (saved === undefined) delete process.env.USER; else process.env.USER = saved; });
  process.env.USER = "current.user";
  let args;
  await readKeychain({ run: async (_cmd, a) => { args = a; return { stdout: "{}" }; } });
  assert.deepEqual(args.slice(3, 5), ["-a", "current.user"]);
  process.env.USER = "bad name";
  await readKeychain({ run: async (_cmd, a) => { args = a; return { stdout: "{}" }; } });
  assert.deepEqual(args.slice(3, 5), ["-a", "claude-code-user"]);
});

// Regression: duplicate "Claude Code-credentials" entries. A service-only
// lookup returned an old entry without a token, so the bar showed "connect".
function duplicateKeychain(account) {
  return async (_cmd, args) => {
    const i = args.indexOf("-a");
    if (i < 0) return { stdout: JSON.stringify({ claudeAiOauth: { accessToken: "" } }) };
    if (args[i + 1] === account) return { stdout: JSON.stringify({ claudeAiOauth: { accessToken: "live-token", subscriptionType: "max" } }) };
    const e = new Error("The specified item could not be found in the keychain."); e.code = 44; throw e;
  };
}

// A temporary home so the default-folder branch never touches real
// credentials. os.homedir() reads HOME on POSIX and USERPROFILE on Windows.
function tempHome(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "herdr-claude-home-"));
  const saved = { HOME: process.env.HOME, USERPROFILE: process.env.USERPROFILE };
  t.after(() => {
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
    fs.rmSync(home, { recursive: true, force: true });
  });
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  const dir = path.join(home, ".claude");
  fs.mkdirSync(dir);
  assert.equal(dir, path.join(os.homedir(), ".claude"));
  return dir;
}

test("duplicate Keychain entries: the current-user entry wins and usage loads", async (t) => {
  const run = duplicateKeychain("me");
  const dir = tempHome(t);
  const file = path.join(dir, ".credentials.json");
  const login = (f) => claudeLogin(f, { platform: "darwin", keychain: () => readKeychain({ run, account: "me" }) });
  const current = await login(file);
  assert.equal(current.keychain, true);
  assert.equal(current.oauth.accessToken, "live-token");
  const r = await collectClaude({ settingsPath: path.join(dir, "settings.json"), login,
    fetcher: async (url, options) => {
      assert.ok(url.endsWith("/api/oauth/usage"));
      assert.equal(options.headers.authorization, "Bearer live-token");
      return response({ five_hour: { utilization: 10 } });
    } });
  assert.equal(r.status, "ok");
  assert.equal(r.method, "oauth-api");
  assert.ok(!JSON.stringify(r).includes("live-token"));
});

test("a missing or unreadable Keychain entry falls back to the credential file", async (t) => {
  const notFound = duplicateKeychain("someone-else");
  assert.equal(await readKeychain({ run: notFound, account: "me" }), null);
  assert.equal(await readKeychain({ account: "me", run: async () => ({ stdout: "not json" }) }), null);
  const file = path.join(tempHome(t), ".credentials.json");
  fs.writeFileSync(file, JSON.stringify({ claudeAiOauth: { accessToken: "file-token" } }));
  const result = await claudeLogin(file, { platform: "darwin", keychain: () => readKeychain({ run: notFound, account: "me" }) });
  assert.deepEqual(result, { oauth: { accessToken: "file-token" }, keychain: false });
});

test("custom config isolation holds: no Keychain lookup at all, whatever the account", async (t) => {
  const a = account(t, { accessToken: "custom-token" });
  let ran = false;
  const result = await claudeLogin(a.file, { platform: "darwin",
    keychain: () => readKeychain({ account: "me", run: async () => { ran = true; return { stdout: "{}" }; } }) });
  assert.equal(ran, false);
  assert.equal(result.oauth.accessToken, "custom-token");
  assert.equal(result.keychain, false);
});
