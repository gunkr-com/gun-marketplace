// catalog-check.mjs against disposable marketplace trees in a temporary directory.
// Each case copies the real script and herdr-usage's manifests into a fresh tree,
// writes its own catalogs, and checks the exit code and message.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const plugin = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const COPY = [".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "package.json", "lib/setup.mjs", "skills/herdr-usage/SKILL.md", "test/catalog-check.mjs"];
const USAGE = "./plugins/herdr/herdr-usage";

function tree({ claude, codex, extra = {} }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "catalog-check-"));
  const write = (rel, data) => {
    const f = path.join(root, rel);
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, typeof data === "string" ? data : JSON.stringify(data, null, 2));
  };
  for (const rel of COPY) write(`plugins/herdr/herdr-usage/${rel}`, fs.readFileSync(path.join(plugin, rel), "utf8"));
  write(".claude-plugin/marketplace.json", { name: "m", plugins: claude });
  write(".agents/plugins/marketplace.json", { name: "m", plugins: codex ?? [{ name: "herdr-usage", source: { source: "local", path: USAGE } }] });
  for (const [rel, data] of Object.entries(extra)) write(rel, data);
  return root;
}

function run(opts) {
  const root = tree(opts);
  try {
    const r = spawnSync(process.execPath, [path.join(root, "plugins/herdr/herdr-usage/test/catalog-check.mjs")], { encoding: "utf8" });
    return { code: r.status, out: r.stdout + r.stderr };
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

const usage = { name: "herdr-usage", source: USAGE };
const local = { name: "other", source: "./plugins/x/other" };
const otherManifest = { "plugins/x/other/.claude-plugin/plugin.json": { name: "other", version: "1.0.0" } };
const gh = (source) => ({ name: "cab-of-one", source });

test("local and GitHub sources pass; the GitHub source is reported as not checked locally", () => {
  const r = run({ claude: [usage, local, gh({ source: "github", repo: "gunkr-com/cab-of-one" })], extra: otherManifest });
  assert.equal(r.code, 0, r.out);
  assert.match(r.out, /catalogs ok; herdr-usage /);
  assert.match(r.out, /remote sources \(shape checked; manifest not checked here\): cab-of-one \(github:gunkr-com\/cab-of-one\)/);
});

test("a GitHub source with ref and sha strings passes", () => {
  const r = run({ claude: [usage, gh({ source: "github", repo: "o/r", ref: "main", sha: "0123456789abcdef0123456789abcdef01234567" })] });
  assert.equal(r.code, 0, r.out);
});

for (const [label, source, msg] of [
  ["no repo", { source: "github" }, /github source needs repo "owner\/repo"/],
  ["empty repo", { source: "github", repo: "" }, /github source needs repo/],
  ["owner only", { source: "github", repo: "gunkr-com" }, /github source needs repo/],
  ["empty owner", { source: "github", repo: "/cab-of-one" }, /github source needs repo/],
  ["empty name", { source: "github", repo: "gunkr-com/" }, /github source needs repo/],
  ["three parts", { source: "github", repo: "a/b/c" }, /github source needs repo/],
  ["dot repo", { source: "github", repo: "a/.." }, /github source needs repo/],
  ["non-string repo", { source: "github", repo: ["a", "b"] }, /github source needs repo/],
  ["empty ref", { source: "github", repo: "o/r", ref: "" }, /ref must be a non-empty string/],
  ["unknown source type", { source: "npm", package: "x" }, /unsupported source/],
  ["object without type", { repo: "o/r" }, /unsupported source/],
  ["number", 42, /unsupported source/],
  ["null", null, /unsupported source/],
  ["path without ./", "plugins/x/other", /local source must be a "\.\/" path/],
]) {
  test(`malformed Claude source fails: ${label}`, () => {
    const r = run({ claude: [usage, gh(source)] });
    assert.equal(r.code, 1, r.out);
    assert.match(r.out, msg);
  });
}

test("a local source without a manifest fails", () => {
  const r = run({ claude: [usage, { name: "missing", source: "./plugins/x/missing" }] });
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /missing has no \.claude-plugin\/plugin\.json at \.\/plugins\/x\/missing/);
});

test("a manifest with another name fails", () => {
  const r = run({ claude: [usage, { name: "renamed", source: "./plugins/x/other" }], extra: otherManifest });
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /renamed manifest is named other/);
});

test("a duplicate name fails, also when one copy is remote", () => {
  const r = run({ claude: [usage, gh({ source: "github", repo: "o/r" }), gh({ source: "github", repo: "o/r" })] });
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /duplicate cab-of-one/);
});

test("a Codex source that is not local fails", () => {
  const r = run({ claude: [usage], codex: [{ name: "herdr-usage", source: { source: "github", repo: "o/r" } }] });
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /\.agents\/plugins\/marketplace\.json: herdr-usage: unsupported source/);
});

test("herdr-usage missing from the Claude catalog fails", () => {
  const r = run({ claude: [gh({ source: "github", repo: "o/r" })] });
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /herdr-usage missing/);
});

test("disagreeing herdr-usage versions fail", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(plugin, "package.json"), "utf8"));
  const r = run({ claude: [usage], extra: { "plugins/herdr/herdr-usage/package.json": { ...pkg, version: "9.9.9" } } });
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /herdr-usage versions differ/);
});
