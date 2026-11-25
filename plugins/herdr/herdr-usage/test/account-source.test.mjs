import { test } from "node:test";
import assert from "node:assert/strict";
import { accountSource, applyPoll } from "../lib/snapshot.mjs";

const capture = {
  source: "claude", account: "claude-test", account_label: "개인",
  quota_observed_at: 100, seen_at: 110,
  quota: { windows: [{ id: "seven_day", used_percent: 90, remaining_percent: 10, resets_at: 10000 }] },
};
const fresh = { key: "claude-test", observed_at: 200, windows: [{ id: "seven_day", used_percent: 20, remaining_percent: 80, resets_at: 10000 }] };

test("direct quota supersedes older high-usage session values", () => {
  const state = { claude: applyPoll(null, { status: "ok", method: "oauth-api", message_ko: "실시간 조회", groups: [fresh] }, 200) };
  const result = accountSource("claude", state, [capture], true, 210);
  assert.equal(result.groups[0].windows[0].used_percent, 20);
  assert.equal(result.observed_at, 200);
});

test("failed query keeps original successful observation, without marking it fresh", () => {
  const good = applyPoll(null, { status: "ok", method: "oauth-api", groups: [fresh] }, 200);
  const state = { claude: applyPoll(good, { status: "error", method: "oauth-api", message_ko: "조회 실패", groups: [], last_error: "HTTP 429", retry_until: 600 }, 300) };
  const result = accountSource("claude", state, [capture], true, 310);
  assert.equal(result.observed_at, 200);
  assert.equal(result.fetched_at, 300);
  assert.equal(result.status, "error");
  assert.equal(state.claude.retry_until, 600);
});

test("first direct failure shows session fallback with explicit error", () => {
  const state = { claude: applyPoll(null, { status: "connect_needed", method: "oauth-api", message_ko: "로그인 필요", groups: [] }, 300) };
  const result = accountSource("claude", state, [capture], true, 310);
  assert.equal(result.observed_at, 100);
  assert.equal(result.method, "statusline");
  assert.equal(result.status, "connect_needed");
  assert.match(result.message_ko, /로그인 필요.*마지막 세션/);
});

test("partial API response preserves missing windows with their old observation", () => {
  // Claude keeps a session-only window even when it predates the direct answer.
  const old = { ...capture, quota: { windows: [
    { id: "seven_day", used_percent: 90, remaining_percent: 10, resets_at: 10000 },
    { id: "five_hour", used_percent: 10, remaining_percent: 90, resets_at: 5000 },
  ] } };
  const state = { claude: applyPoll(null, { status: "ok", method: "oauth-api", message_ko: "실시간 조회", groups: [fresh] }, 200) };
  const result = accountSource("claude", state, [old], true, 210);
  assert.equal(result.observed_at, 100);
  assert.deepEqual(result.groups[0].windows.map((w) => [w.id, w.observed_at]), [["seven_day", 200], ["five_hour", 100]]);
  assert.match(result.message_ko, /일부 한도/);
});

// Antigravity: the 1.5.17 /usage answer lists Argon inside the shared Gemini group.
const argonCapture = (observed) => ({ source: "agy", account: "agy-default", quota_observed_at: observed, seen_at: observed,
  quota: { windows: [
    { id: "gemini-weekly", used_percent: 40, remaining_percent: 60, resets_at: 90000 },
    { id: "gemini-argon-wtu-weekly", used_percent: 6.2, remaining_percent: 93.8, resets_at: 80000 },
  ] } });
const agyDirect = (observed, extra = []) => ({ status: "ok", method: "quota-api", message_ko: "실시간 조회", groups: [
  { key: "agy-default", label: "기본", label_key: "default", observed_at: observed, windows: [
    { id: "3p-5h", used_percent: 0, remaining_percent: 100, observed_at: observed },
    { id: "gemini-weekly", used_percent: 57, remaining_percent: 43, resets_at: 90000, observed_at: observed },
    ...extra,
  ] },
] });

test("agy direct answer retires an older session-only Argon bucket", () => {
  const state = { agy: applyPoll(null, agyDirect(500), 500) };
  const result = accountSource("agy", state, [argonCapture(100)], true, 510);
  assert.equal(result.status, "ok");
  assert.equal(result.observed_at, 500);
  assert.deepEqual(result.groups[0].windows.map((w) => w.id), ["3p-5h", "gemini-weekly"]);
  assert.equal(result.groups[0].windows[1].remaining_percent, 43);
  assert.doesNotMatch(result.message_ko, /일부 한도/);
  assert.ok(!result.message_codes?.includes("partial_session"));
});

test("agy poll failure after a direct answer does not bring Argon back", () => {
  const good = applyPoll(null, agyDirect(500), 500);
  const state = { agy: applyPoll(good, { status: "error", method: "quota-api", groups: [], last_error: "quota_command_failed" }, 900) };
  const result = accountSource("agy", state, [argonCapture(100)], true, 910);
  assert.equal(result.status, "error");
  assert.equal(result.observed_at, 500);
  assert.equal(result.fetched_at, 900);
  assert.deepEqual(result.groups[0].windows.map((w) => w.id), ["3p-5h", "gemini-weekly"]);
});

test("agy separate Argon bucket is kept when the direct answer returns it", () => {
  const argon = { id: "gemini-argon-wtu-weekly", used_percent: 20, remaining_percent: 80, resets_at: 80000, observed_at: 500 };
  const state = { agy: applyPoll(null, agyDirect(500, [argon]), 500) };
  const result = accountSource("agy", state, [argonCapture(100)], true, 510);
  assert.equal(result.observed_at, 500);
  assert.deepEqual(result.groups[0].windows.find((w) => w.id === argon.id).remaining_percent, 80);
});

test("agy session re-emitting Argon after the direct answer does not add it back", () => {
  // A long-lived session can keep sending the old bucket; its newer time is not membership.
  const good = applyPoll(null, agyDirect(500), 500);
  const ok = accountSource("agy", { agy: good }, [argonCapture(700)], true, 710);
  assert.equal(ok.observed_at, 500);
  assert.deepEqual(ok.groups[0].windows.map((w) => w.id), ["3p-5h", "gemini-weekly"]);
  assert.doesNotMatch(ok.message_ko, /일부 한도/);
  const failed = { agy: applyPoll(good, { status: "error", method: "quota-api", groups: [], last_error: "quota_command_failed" }, 900) };
  const r = accountSource("agy", failed, [argonCapture(100), argonCapture(850)], true, 910);
  assert.equal(r.observed_at, 500);
  assert.equal(r.method, "quota-api");
  assert.deepEqual(r.groups[0].windows.map((w) => w.id), ["3p-5h", "gemini-weekly"]);
});

test("agy without any direct answer keeps the session fallback, Argon included", () => {
  const ids = (r) => r.groups[0].windows.map((w) => w.id).sort();
  const none = accountSource("agy", {}, [argonCapture(100)], true, 110);
  assert.equal(none.method, "statusline");
  assert.deepEqual(ids(none), ["gemini-argon-wtu-weekly", "gemini-weekly"]);
  const failed = { agy: applyPoll(null, { status: "error", method: "quota-api", message_ko: "조회 실패", groups: [] }, 300) };
  const r = accountSource("agy", failed, [argonCapture(100)], true, 310);
  assert.equal(r.status, "error");
  assert.equal(r.method, "statusline");
  assert.equal(r.observed_at, 100);
  assert.deepEqual(ids(r), ["gemini-argon-wtu-weekly", "gemini-weekly"]);
  assert.match(r.message_ko, /마지막 세션/);
});

test("agy collector state written by 0.1.6 is read the same way", () => {
  // 0.1.6 state: last_good with group observed_at, windows without their own time, no message_codes.
  const st = { agy: { fetched_at: 900, status: "error", method: "quota-api", message_ko: "조회 실패", last_error: "quota_command_failed", retry_until: null,
    last_good: { method: "quota-api", observed_at: 500, groups: [{ key: "agy-default", label: "기본", observed_at: 500,
      windows: [{ id: "gemini-weekly", used_percent: 57, remaining_percent: 43, resets_at: 90000 }] }] } } };
  const result = accountSource("agy", st, [argonCapture(100)], true, 910);
  assert.equal(result.observed_at, 500);
  assert.equal(result.message_codes, null);
  assert.deepEqual(result.groups[0].windows.map((w) => w.id), ["gemini-weekly"]);
});
