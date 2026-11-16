// mod/herdr-files/folders.js: which program each platform and path style uses (fake exec),
// and the real commands against a fresh temporary folder (PowerShell on Windows, sh on
// macOS and Linux). Nothing outside that temporary folder is touched.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const MOD = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../mod/herdr-files");
const { makeFolders, psArgs, utf8Base64, TEMP_NAME } = await import(pathToFileURL(path.join(MOD, "folders.js")).href);
const { joinPath, parentDir, resolveInput, samePath, isWindowsPath } = await import(pathToFileURL(path.join(MOD, "core.js")).href);

// A fake exec that records calls and answers from `reply(command, args, on)`.
function fakeExec(reply = () => ({ code: 0, stdout: "", stderr: "" })) {
  const calls = [];
  const exec = async (command, args, opts) => { calls.push({ command, args, on: opts?.on ?? null }); return reply(command, args, opts?.on ?? null); };
  return { exec, calls };
}

test("utf8Base64 matches Node's UTF-8 Base64", () => {
  for (const s of ["", "a", "ab", "abc", "C:\\Users\\me\\it's ‘q’ 폴더", "$x `y; & \"z\"", "emoji 😀 ñ", "\u07ff\u0800\uffff"]) {
    assert.equal(utf8Base64(s), Buffer.from(s, "utf8").toString("base64"), s);
  }
});

test("psArgs: the path travels only as Base64; no double quotes in the script", () => {
  const p = "C:\\it's ‘q’ \"x\" $(evil) `y; & 폴더";
  const args = psArgs("[void]$p", p);
  assert.deepEqual(args.slice(0, 3), ["-NoProfile", "-NonInteractive", "-Command"]);
  assert.ok(!args[3].includes("evil") && !args[3].includes("폴더"), "path not in the script text");
  assert.ok(args[3].includes(Buffer.from(p, "utf8").toString("base64")));
  assert.ok(!args[3].includes('"'));
});

test("which program: local Windows uses PowerShell; macOS/Linux and remote POSIX keep sh and test", async () => {
  const win = fakeExec((c) => ({ code: c === "powershell" ? 0 : 1, stdout: "", stderr: "" }));
  const W = makeFolders({ exec: win.exec, platform: () => "windows" });
  assert.deepEqual(await W.list("C:\\x", null), []);
  assert.equal(await W.isFolder("C:\\x", null), true);
  assert.deepEqual(win.calls.map((c) => c.command), ["powershell", "powershell"]);

  // Windows host, remote POSIX folder: the folder's style decides, not the host.
  const remote = fakeExec((c) => (c === "sh" ? { code: 0, stdout: "d\0sub\0f\0a.md\0", stderr: "" } : { code: 0, stdout: "", stderr: "" }));
  const R = makeFolders({ exec: remote.exec, platform: () => "windows" });
  assert.deepEqual(await R.list("/Users/me/dev", "vps"), [{ name: "sub", dir: true }, { name: "a.md", dir: false }]);
  assert.equal(await R.isFolder("/Users/me/dev", "vps"), true);
  assert.deepEqual(remote.calls.map((c) => [c.command, c.on]), [["sh", "vps"], ["test", "vps"]]);
  assert.deepEqual(remote.calls[1].args, ["-d", "/Users/me/dev"]);
  await R.isFolder("//tmp/dir", "vps");
  assert.deepEqual([remote.calls[2].command, remote.calls[2].args], ["test", ["-d", "//tmp/dir"]], "POSIX // is not a UNC share");

  // macOS host: the same commands as 0.1.0.
  const mac = fakeExec();
  const M = makeFolders({ exec: mac.exec, platform: () => "macos" });
  await M.list("/a b", null);
  await M.isFolder("/a b", null);
  assert.equal(mac.calls[0].command, "sh");
  assert.deepEqual(mac.calls[0].args.slice(2), ["files", "/a b"]);
  assert.deepEqual(mac.calls[1], { command: "test", args: ["-d", "/a b"], on: null });

  // macOS host, remote Windows folder: PowerShell there.
  const rw = fakeExec();
  const RW = makeFolders({ exec: rw.exec, platform: () => "macos" });
  await RW.list("C:\\Users\\x", "win-box");
  assert.deepEqual([rw.calls[0].command, rw.calls[0].on], ["powershell", "win-box"]);
});

test("remote without sh or test falls back to PowerShell there", async () => {
  const { exec, calls } = fakeExec();
  const missing = async (command, args, opts) => {
    if (command !== "powershell") throw new Error(`cannot start ${command}: program not found`);
    return exec(command, args, opts);
  };
  const F = makeFolders({ exec: missing, platform: () => "linux" });
  assert.deepEqual(await F.list("/odd", "box"), []);
  assert.equal(await F.isFolder("/odd", "box"), true);
  assert.deepEqual(calls.map((c) => [c.command, c.on]), [["powershell", "box"], ["powershell", "box"]]);
  // Locally, a missing program is an error, not a silent switch.
  const L = makeFolders({ exec: missing, platform: () => "linux" });
  await assert.rejects(L.list("/x", null), /program not found/);
  assert.equal(await L.isFolder("/x", null), false);
});

test("PowerShell output: empty, single object, whitespace, failure", async () => {
  const out = (stdout, code = 0, stderr = "") => makeFolders({ exec: async () => ({ code, stdout, stderr }), platform: () => "windows" });
  assert.deepEqual(await out("").list("C:\\x", null), []);
  assert.deepEqual(await out("\r\n").list("C:\\x", null), []);
  assert.deepEqual(await out('{"dir":false,"name":"a.md"}\r\n').list("C:\\x", null), [{ dir: false, name: "a.md" }]);
  assert.deepEqual(await out('[{"dir":true,"name":"a"},{"dir":false,"name":"b"}]').list("C:\\x", null), [{ dir: true, name: "a" }, { dir: false, name: "b" }]);
  await assert.rejects(out("", 1, "Not a folder: C:\\x\\f.md").list("C:\\x\\f.md", null), /Not a folder/);
  assert.equal(await out("", 1).isFolder("C:\\x", null), false);
  await assert.rejects(out("C:\\Windows").makeTemp(), /self-test folder/, "an unexpected temp folder is refused");
});

// The real programs: Windows PowerShell on Windows, sh and coreutils elsewhere.
const realExec = async (command, args) => {
  const r = spawnSync(command, args, { encoding: "utf8", windowsHide: true });
  if (r.error) throw r.error;
  return { code: r.status, stdout: r.stdout, stderr: r.stderr };
};

test("real commands: temp folder, quoted Unicode folder, listing, folder check, file removal", async () => {
  const F = makeFolders({ exec: realExec, platform: () => (process.platform === "win32" ? "windows" : process.platform === "darwin" ? "macos" : "linux") });
  const dir = await F.makeTemp();
  try {
    assert.match(dir, TEMP_NAME);
    assert.equal(isWindowsPath(dir), process.platform === "win32");
    assert.ok(fs.statSync(dir).isDirectory());
    const name = "it's ‘q’ 폴더 $x `y; & ñ", sub = joinPath(dir, name);
    await F.makeFolder(sub);
    assert.ok(fs.statSync(path.join(dir, name)).isDirectory(), "created under the exact name");
    await F.writeBinary(joinPath(dir, "bin.md"));
    assert.deepEqual([...fs.readFileSync(path.join(dir, "bin.md"))], [97, 0, 98]);
    fs.writeFileSync(path.join(dir, "노트 it's.md"), "x");

    assert.equal(await F.isFolder(dir, null), true);
    assert.equal(await F.isFolder(sub, null), true);
    assert.equal(await F.isFolder(joinPath(dir, "bin.md"), null), false, "a file is not a folder");
    assert.equal(await F.isFolder(joinPath(dir, "missing"), null), false);

    const listed = await F.list(dir, null);
    const byName = Object.fromEntries(listed.map((e) => [e.name, e.dir]));
    assert.deepEqual(byName, { [name]: true, "bin.md": false, "노트 it's.md": false });
    assert.deepEqual(await F.list(sub, null), [], "empty folder");
    await assert.rejects(F.list(joinPath(dir, "bin.md"), null), "a file is not listed as a folder");

    assert.ok(samePath(parentDir(sub), dir));
    assert.ok(samePath(resolveInput("..", sub, null, isWindowsPath(dir)), dir));
    assert.ok(samePath(resolveInput(`"${sub}"`, dir, null, isWindowsPath(dir)), sub));

    await F.removeFile(joinPath(dir, "bin.md"));
    assert.equal(fs.existsSync(path.join(dir, "bin.md")), false);
    await F.removeFile(joinPath(dir, "bin.md")); // already gone: still fine
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
