// CI check: both marketplace catalogs list herdr-usage, every local catalog source
// exists with a manifest of the same name, every remote source is well formed,
// and herdr-usage's versions agree.
// The Claude catalog may also list GitHub sources ({ source: "github", repo: "owner/repo" }).
// Their manifests live in another repository, so only their shape is checked here.
//   node plugins/herdr/herdr-usage/test/catalog-check.mjs   (from the repository root or anywhere)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const plugin = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.resolve(plugin, "../../..");
const errors = [];
const json = (f) => { try { return JSON.parse(fs.readFileSync(f, "utf8")); } catch (e) { errors.push(`${path.relative(root, f)}: ${e.message}`); return null; } };

// Returns { local: "<path>" }, { remote: "<description>" } or { error: "<reason>" }.
const GITHUB_REPO = /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\/[A-Za-z0-9._-]+$/;
const localPath = (v) => typeof v === "string" && v.startsWith("./") && v.length > 2 ? { local: v } : { error: `local source must be a "./" path, got ${JSON.stringify(v)}` };
const claudeSource = (s) => {
  if (typeof s === "string") return localPath(s);
  if (s && typeof s === "object" && !Array.isArray(s) && s.source === "github") {
    if (typeof s.repo !== "string" || !GITHUB_REPO.test(s.repo) || /^\.\.?$/.test(s.repo.split("/")[1])) return { error: `github source needs repo "owner/repo", got ${JSON.stringify(s.repo)}` };
    for (const k of ["ref", "sha"]) if (k in s && (typeof s[k] !== "string" || !s[k])) return { error: `github source ${k} must be a non-empty string` };
    return { remote: `github:${s.repo}` };
  }
  return { error: `unsupported source ${JSON.stringify(s)}` };
};
const codexSource = (s) => s && typeof s === "object" && s.source === "local" ? localPath(s.path) : { error: `unsupported source ${JSON.stringify(s)}` };

const catalogs = [
  { file: ".claude-plugin/marketplace.json", src: claudeSource, manifest: ".claude-plugin/plugin.json" },
  { file: ".agents/plugins/marketplace.json", src: codexSource, manifest: ".codex-plugin/plugin.json" },
];
const remote = [];
for (const c of catalogs) {
  const cat = json(path.join(root, c.file));
  if (!cat) continue;
  const names = new Set();
  for (const p of cat.plugins ?? []) {
    if (names.has(p.name)) errors.push(`${c.file}: duplicate ${p.name}`);
    names.add(p.name);
    if (typeof p.name !== "string" || !p.name) { errors.push(`${c.file}: entry without a name`); continue; }
    const src = c.src(p.source);
    if (src.error) { errors.push(`${c.file}: ${p.name}: ${src.error}`); continue; }
    if (src.remote) { remote.push(`${p.name} (${src.remote})`); continue; }
    const dir = path.join(root, src.local);
    const man = fs.existsSync(path.join(dir, c.manifest)) ? json(path.join(dir, c.manifest)) : null;
    if (!man) errors.push(`${c.file}: ${p.name} has no ${c.manifest} at ${src.local}`);
    else if (man.name !== p.name) errors.push(`${c.file}: ${p.name} manifest is named ${man.name}`);
  }
  if (!names.has("herdr-usage")) errors.push(`${c.file}: herdr-usage missing`);
}

const versions = {
  ".claude-plugin/plugin.json": json(path.join(plugin, ".claude-plugin/plugin.json"))?.version,
  ".codex-plugin/plugin.json": json(path.join(plugin, ".codex-plugin/plugin.json"))?.version,
  "package.json": json(path.join(plugin, "package.json"))?.version,
  "lib/setup.mjs": fs.readFileSync(path.join(plugin, "lib/setup.mjs"), "utf8").match(/VERSION = "([^"]+)"/)?.[1],
};
if (new Set(Object.values(versions)).size !== 1) errors.push(`herdr-usage versions differ: ${JSON.stringify(versions)}`);

const skill = fs.readFileSync(path.join(plugin, "skills/herdr-usage/SKILL.md"), "utf8").replaceAll("\r\n", "\n");
const fm = skill.match(/^---\n([\s\S]*?)\n---\n/);
if (!fm || !/^name: herdr-usage$/m.test(fm[1]) || !/^description: \S/m.test(fm[1])) errors.push("skills/herdr-usage/SKILL.md: frontmatter needs name: herdr-usage and a description");

if (errors.length) { for (const e of errors) console.error(`error: ${e}`); process.exit(1); }
console.log(`catalogs ok; herdr-usage ${versions["package.json"]}`);
if (remote.length) console.log(`remote sources (shape checked; manifest not checked here): ${remote.join(", ")}`);
