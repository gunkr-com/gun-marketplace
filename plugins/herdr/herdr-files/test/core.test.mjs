// Behavioral tests for mod/herdr-files/core.js with fake IO: async loads, dirty state, guarded saves.
//   node --test test/
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const CORE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../mod/herdr-files/core.js");
const core = await import(pathToFileURL(CORE).href);
const { DocController, classify, resolveInput, parentDir, joinPath, trimSlash, baseName, rememberedFile, keepsDoc, MAX_CHARS } = core;

// A fake disk with writeTextFile's contract: write only if current === expected (null: must not exist).
function fakeDisk(files = {}) {
  const disk = new Map(Object.entries(files));
  const gates = [];
  const io = {
    disk, writes: [], reads: 0,
    // When set, reads/writes wait for release() so tests can interleave them.
    hold: false,
    gate() { return io.hold ? new Promise(r => gates.push(r)) : Promise.resolve(); },
    release() { const g = gates.shift(); g?.(); return Boolean(g); },
    async read(p, on) { io.reads++; await io.gate(); if (on) return disk.has(`${on}:${p}`) ? disk.get(`${on}:${p}`) : null; return disk.has(p) ? disk.get(p) : null; },
    async write(p, text, expected) {
      await io.gate();
      const cur = disk.has(p) ? disk.get(p) : null;
      if (cur !== expected) throw new Error("stale snapshot");
      disk.set(p, text); io.writes.push({ p, text, expected });
      return null;
    },
    states: [],
    makeState({ value, readonly }) {
      const handlers = [];
      const s = { v: value, readonly, released: false,
        value() { if (s.released) throw new Error("released EditorState used"); return s.v; }, set_value(v) { s.v = v; },
        on(ev, fn) { if (ev === "change") handlers.push(fn); return true; },
        release() { s.released = true; return true; },
        type(v) { if (s.readonly) return; s.v = v; for (const h of handlers) h({}, cx); } };
      const cx = { notified: 0, notify() { cx.notified++; } };
      s.cx = cx;
      io.states.push(s);
      return s;
    },
  };
  return io;
}
const tick = () => new Promise(r => setImmediate(r));

test("paths: join, parent, trailing slashes, root", () => {
  assert.equal(joinPath("/", "a"), "/a");
  assert.equal(joinPath("/x", "a b/c"), "/x/a b/c");
  assert.equal(joinPath("/x", ""), "/x");
  assert.equal(parentDir("/a/b c/"), "/a");
  assert.equal(parentDir("/a"), "/");
  assert.equal(parentDir("/"), null);
  assert.equal(trimSlash("/a//"), "/a");
  assert.equal(trimSlash("/"), "/");
  assert.equal(baseName("/a/Work Dir/"), "Work Dir");
});

test("resolveInput: spaces, quotes, escapes, ~, relative, ..", () => {
  const home = "/Users/me";
  assert.equal(resolveInput("/Users/me/Work Dir/project", "/x", home), "/Users/me/Work Dir/project");
  assert.equal(resolveInput('  "/a/Work Dir"  ', "/x", home), "/a/Work Dir");
  assert.equal(resolveInput("'/a/b c'", "/x", home), "/a/b c");
  assert.equal(resolveInput("/a/Work\\ Dir", "/x", home), "/a/Work Dir");
  assert.equal(resolveInput("~/dev", "/x", home), "/Users/me/dev");
  assert.equal(resolveInput("~", "/x", home), "/Users/me");
  assert.equal(resolveInput("~/dev", "/x", null), null, "remote: no local ~");
  assert.equal(resolveInput("sub dir/../other", "/x/y", home), "/x/y/other");
  assert.equal(resolveInput("../..", "/x/y", home), "/");
  assert.equal(resolveInput("   ", "/x", home), null);
  assert.equal(resolveInput("rel", null, home), null);
});

test("Windows paths: drive and UNC roots, both separators, parent, base name", () => {
  const { isWindowsPath, isRootPath, pathKey, samePath, extension } = core;
  for (const p of ["C:\\", "C:/", "c:", "C:\\Users", "D:/x/y", "\\\\srv\\share", "\\\\srv/share/x", "\\\\?\\C:\\x", "\\\\?\\UNC\\srv\\share"]) assert.ok(isWindowsPath(p), p);
  for (const p of ["/", "/Users/me", "rel", "C:rel", "\\\\srv", "//srv", "//tmp/dir", "//srv/share/x", "", null]) assert.ok(!isWindowsPath(p), String(p));
  assert.equal(trimSlash("C:\\Users\\me\\\\"), "C:\\Users\\me");
  assert.equal(trimSlash("c:/Users/me/"), "C:\\Users\\me", "slashes and drive case normalized");
  assert.equal(trimSlash("C:"), "C:\\", "a bare drive is its root, not drive-relative");
  assert.equal(trimSlash("C:/"), "C:\\");
  assert.equal(trimSlash("\\\\srv\\share\\"), "\\\\srv\\share");
  assert.equal(joinPath("C:", ""), "C:\\", "the shown folder itself: a drive root, never drive-relative C:");
  assert.equal(joinPath("c:/Users/me/", ""), "C:\\Users\\me");
  assert.equal(trimSlash("\\\\?\\UNC\\srv\\share\\x"), "\\\\srv\\share\\x");
  assert.equal(trimSlash("\\\\?\\D:\\x"), "D:\\x");
  assert.equal(joinPath("C:\\", "a b/c.md"), "C:\\a b\\c.md");
  assert.equal(joinPath("C:\\x", "a"), "C:\\x\\a");
  assert.equal(joinPath("\\\\srv\\share", "a/b"), "\\\\srv\\share\\a\\b");
  assert.equal(joinPath("E:/Shared Docs/repo", "sub/f.md"), "E:\\Shared Docs\\repo\\sub\\f.md", "git's forward-slash top level");
  assert.equal(parentDir("C:\\Users\\me"), "C:\\Users");
  assert.equal(parentDir("C:\\Users"), "C:\\");
  assert.equal(parentDir("C:\\"), null);
  assert.equal(parentDir("\\\\srv\\share\\sub"), "\\\\srv\\share");
  assert.equal(parentDir("\\\\srv\\share"), null, "never above the share");
  assert.equal(baseName("C:\\a\\Work Dir\\"), "Work Dir");
  assert.equal(baseName("C:\\"), "C:\\");
  assert.equal(baseName("\\\\srv\\share"), "\\\\srv\\share");
  assert.ok(isRootPath("C:\\") && isRootPath("D:/") && isRootPath("\\\\srv\\share\\") && isRootPath("/"));
  assert.ok(!isRootPath("C:\\x") && !isRootPath("/x"));
  assert.equal(extension("C:\\dir.md\\README"), "txt", "a dotted folder does not make an extension");
  assert.equal(extension("C:\\a\\Notes.MD"), "md");
  assert.equal(classify("C:\\dir.md\\README", "x", null).readonly, null, "extensionless stays editable");
  assert.equal(classify("C:\\a\\b.js", "x", null).readonly, "preview");
  assert.ok(samePath("c:/Users/Me/", "C:\\users\\me"), "Windows: case and separators ignored");
  assert.ok(!samePath("/Users/Me", "/users/me"), "POSIX: case matters");
  assert.equal(pathKey("E:/Shared Docs/x/a.md"), pathKey("e:\\shared docs\\x\\a.md"), "git marks match the panel's paths");
  // POSIX paths keep "\" as part of a name.
  assert.equal(baseName("/a/b\\c.md"), "b\\c.md");
  assert.equal(parentDir("/a/b\\c"), "/a");
});

test("resolveInput, Windows: absolute, relative, .., ~, quotes, roots, refusals", () => {
  const base = "C:\\Users\\me\\Work Dir", home = "C:\\Users\\me";
  assert.equal(resolveInput("D:\\data\\x", base, home), "D:\\data\\x", "drive path is absolute, not relative");
  assert.equal(resolveInput("d:/data/x/", base, home), "D:\\data\\x");
  assert.equal(resolveInput('"E:\\Shared Docs\\it\'s 폴더"', base, home), "E:\\Shared Docs\\it's 폴더", "Explorer's quoted copy");
  assert.equal(resolveInput("'C:\\a b'", base, home), "C:\\a b");
  assert.equal(resolveInput("C:\\a\\ b", base, home), "C:\\a\\ b", "\\ is a separator, not an escape");
  assert.equal(resolveInput("sub\\deeper/x", base, home), "C:\\Users\\me\\Work Dir\\sub\\deeper\\x");
  assert.equal(resolveInput("..", base, home), "C:\\Users\\me");
  assert.equal(resolveInput("..\\..\\..\\..\\..", base, home), "C:\\", ".. stops at the drive");
  assert.equal(resolveInput(".", base, home), base);
  assert.equal(resolveInput("\\Windows", base, home), "C:\\Windows", "root-relative: the current drive");
  assert.equal(resolveInput("/", base, home), "C:\\");
  assert.equal(resolveInput("~", base, home), "C:\\Users\\me");
  assert.equal(resolveInput("~\\dev", base, home), "C:\\Users\\me\\dev");
  assert.equal(resolveInput("~/dev", base, home), "C:\\Users\\me\\dev");
  assert.equal(resolveInput("~", base, null), null, "remote: no local ~");
  assert.equal(resolveInput("\\\\srv\\share\\a\\..\\..\\..", base, home), "\\\\srv\\share", ".. stops at the share");
  assert.equal(resolveInput("x", "\\\\srv\\share", home), "\\\\srv\\share\\x");
  assert.equal(resolveInput("//srv/share/a", base, home), "\\\\srv\\share\\a", "forward-slash UNC once the folder is Windows");
  assert.equal(resolveInput("\\top", "\\\\srv\\share\\a", home), "\\\\srv\\share\\top");
  assert.equal(resolveInput("C:rel", base, home), null, "drive-relative refused");
  assert.equal(resolveInput("\\\\srv", base, home), null, "server without share refused");
  assert.equal(resolveInput("C:\\a|b", base, home), null, "reserved character refused");
  assert.equal(resolveInput("C:\\a\nb", base, home), null, "control character refused");
  assert.equal(resolveInput("C:\\a\u0000b", base, home), null);
  assert.equal(resolveInput("rel", null, home, true), null, "no base folder: relative refused");
  assert.equal(resolveInput("C:\\x", null, home), "C:\\x", "no base folder: the input's style");
  assert.equal(resolveInput("\\x", null, home, true), "C:\\x", "no base folder on Windows: home's drive");
});

test("resolveInput: a remote POSIX folder keeps POSIX rules on a Windows host", () => {
  // main.js passes windows = isWindowsPath(root) when a folder is shown, and home only locally.
  const root = "/Users/me/dev";
  assert.equal(resolveInput("..", root, null, false), "/Users/me");
  assert.equal(resolveInput("Work\\ Dir/x", root, null, false), "/Users/me/dev/Work Dir/x", "shell escape still unescaped");
  assert.equal(resolveInput("/opt/x", root, null, false), "/opt/x");
  assert.equal(resolveInput("~/x", root, null, false), null);
  assert.equal(resolveInput("../..", root, null), "/Users", "default style is the base folder's");
  assert.equal(resolveInput("/a/b\nc", root, null), null, "control characters refused for POSIX too");
  // POSIX "//": not a UNC share, with or without a shown folder.
  assert.equal(resolveInput("//tmp/dir", null, null), "/tmp/dir");
  assert.equal(resolveInput("//tmp/dir", root, null), "/tmp/dir");
  assert.equal(trimSlash("//tmp/dir/"), "//tmp/dir");
  assert.equal(parentDir("//tmp/dir"), "//tmp");
  assert.equal(core.isRootPath("//tmp/dir"), false);
});

test("keepsDoc: the remembered file matches across Windows case and separators", () => {
  const doc = { path: "C:\\Users\\me\\a.md", on: null };
  assert.equal(keepsDoc(false, doc, { path: "c:/users/me/a.md", on: null }), true);
  assert.equal(keepsDoc(false, doc, { path: "C:\\Users\\me\\b.md", on: null }), false);
  assert.equal(keepsDoc(false, doc, { path: "C:\\Users\\me\\a.md", on: "box" }), false, "another machine is another file");
  assert.equal(keepsDoc(false, { path: "/a/A.md", on: null }, { path: "/a/a.md", on: null }), false, "POSIX case matters");
});

test("classify: editable only for local, whole, Markdown/text", () => {
  assert.equal(classify("/a.md", "x", null).readonly, null);
  assert.equal(classify("/a.markdown", "x", null).readonly, null);
  assert.equal(classify("/a.txt", "x", null).readonly, null);
  assert.equal(classify("/README", "x", null).readonly, null, "no extension counts as text");
  assert.equal(classify("/a.md", "x", "vps").readonly, "remote");
  assert.equal(classify("/a.js", "x", null).readonly, "preview");
  assert.equal(classify("/a.md", "x".repeat(MAX_CHARS + 1), null).readonly, "truncated");
  assert.equal(classify("/a.md", "한".repeat(349526), null).readonly, "truncated", "near 1 MiB of UTF-8 may be cut");
  assert.equal(classify("/a.md", "a\0b", null).kind, "binary");
  assert.equal(classify("/a.md", null, null).kind, "missing");
});

test("edit -> dirty -> save writes with expected = loaded text", async () => {
  const io = fakeDisk({ "/p/a.md": "old" });
  const d = new DocController(io);
  assert.equal(await d.open("/p/a.md", null), true);
  assert.equal(d.editable, true);
  const s = d.doc.state;
  s.type("new");
  assert.equal(d.dirty, true);
  assert.equal(s.cx.notified, 1, "notifies when dirty flips");
  s.type("newer");
  assert.equal(s.cx.notified, 1, "no redraw per keystroke while still dirty");
  s.type("old");
  assert.equal(d.dirty, false, "typing back to the saved text is clean");
  s.type("new");
  assert.equal(await d.save(), "saved");
  assert.deepEqual(io.writes, [{ p: "/p/a.md", text: "new", expected: "old" }]);
  assert.equal(d.dirty, false);
  assert.equal(await d.save(), "clean", "nothing to save");
  assert.equal(io.writes.length, 1);
});

test("typing during an in-flight save stays dirty; second save is refused", async () => {
  const io = fakeDisk({ "/a.md": "v0" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("v1");
  io.hold = true;
  const p = d.save();
  assert.equal(d.doc.saving, true);
  assert.equal(await d.save(), "busy", "no overlapping save");
  assert.equal(d.request({ kind: "close" }), false, "no close while saving");
  d.doc.state.type("v2");
  io.release();
  assert.equal(await p, "saved");
  io.hold = false;
  assert.equal(io.disk.get("/a.md"), "v1");
  assert.equal(d.doc.saved, "v1");
  assert.equal(d.dirty, true, "v2 typed during save is still unsaved");
  assert.equal(await d.save(), "saved");
  assert.equal(io.disk.get("/a.md"), "v2");
});

test("external change: conflict, disk untouched, explicit overwrite uses disk snapshot", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  io.disk.set("/a.md", "theirs");
  assert.equal(await d.save(), "conflict");
  assert.equal(io.disk.get("/a.md"), "theirs");
  assert.equal(d.doc.conflict.disk, "theirs");
  assert.equal(d.dirty, true);
  assert.equal(d.held, true);
  // A further change on disk after the conflict was shown: overwrite is refused again.
  io.disk.set("/a.md", "theirs2");
  assert.equal(await d.save("overwrite"), "conflict");
  assert.equal(io.disk.get("/a.md"), "theirs2");
  assert.equal(await d.save("overwrite"), "saved");
  assert.equal(io.disk.get("/a.md"), "mine");
  assert.equal(d.doc.conflict, null);
  assert.equal(d.dirty, false);
});

test("normal save after a conflict never silently overwrites", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  io.disk.set("/a.md", "theirs");
  assert.equal(await d.save(), "conflict");
  assert.equal(await d.save(), "conflict", "plain Save keeps guarding against the original text");
  assert.equal(io.disk.get("/a.md"), "theirs");
});

test("deleted on disk: reported; only explicit recreate writes", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  io.disk.delete("/a.md");
  assert.equal(await d.save(), "deleted");
  assert.equal(io.disk.has("/a.md"), false);
  assert.equal(await d.save("overwrite"), "failed");
  assert.equal(await d.save("recreate"), "saved");
  assert.equal(io.disk.get("/a.md"), "mine");
});

test("write error that is not a conflict surfaces saveError and keeps edits", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  io.write = async () => { throw new Error("EACCES"); };
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  assert.equal(await d.save(), "failed");
  assert.match(d.doc.saveError, /EACCES/);
  assert.equal(d.dirty, true);
  d.doc.state.type("mine2");
  assert.equal(d.doc.saveError, null, "typing clears the error");
});

test("stale async load: A then B, A finishing last is dropped", async () => {
  const io = fakeDisk({ "/a.md": "A", "/b.md": "B" });
  io.hold = true;
  const d = new DocController(io);
  const pa = d.open("/a.md", null);
  const pb = d.open("/b.md", null);
  await tick();
  io.release(); // A's read
  io.release(); // B's read
  assert.equal(await pa, false);
  assert.equal(await pb, true);
  assert.equal(d.doc.path, "/b.md");
  assert.equal(d.doc.state.value(), "B");
  assert.equal(io.states.length, 1, "no editor made for the stale load");
});

test("same file reopened while loading: only the newest load wins", async () => {
  const io = fakeDisk({ "/a.md": "one" });
  io.hold = true;
  const d = new DocController(io);
  const p1 = d.open("/a.md", null);
  await tick();
  io.disk.set("/a.md", "two");
  const p2 = d.open("/a.md", null);
  await tick();
  io.release(); io.release();
  assert.equal(await p1, false);
  assert.equal(await p2, true);
  assert.equal(io.states.length, 1);
});

test("close while loading drops the result", async () => {
  const io = fakeDisk({ "/a.md": "A" });
  io.hold = true;
  const d = new DocController(io);
  const p = d.open("/a.md", null);
  d.close();
  await tick();
  io.release();
  assert.equal(await p, false);
  assert.equal(d.doc, null);
});

test("navigation guard: dirty asks; cancel keeps; discard runs; save then runs", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  assert.equal(d.request({ kind: "close" }), true, "clean closes at once");
  d.doc.state.type("mine");
  const nav = { kind: "open", path: "/b.md" };
  assert.equal(d.request(nav), false);
  assert.equal(d.pendingNav, nav);
  assert.equal(await d.resolve("cancel"), null);
  assert.equal(d.pendingNav, null);
  assert.equal(d.dirty, true);
  d.request(nav);
  assert.equal(await d.resolve("save"), nav);
  assert.equal(io.disk.get("/a.md"), "mine");
  d.doc.state.type("mine2");
  d.request(nav);
  assert.equal(await d.resolve("discard"), nav);
  assert.equal(io.disk.get("/a.md"), "mine", "discard writes nothing");
});

test("navigation guard: Save that conflicts does not navigate", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  io.disk.set("/a.md", "theirs");
  d.request({ kind: "close" });
  assert.equal(await d.resolve("save"), null);
  assert.equal(d.doc.conflict.disk, "theirs");
  assert.equal(d.held, true);
  assert.equal(d.request({ kind: "close" }), false, "conflicted doc still held");
});

test("remote, truncated, preview and binary never write", async () => {
  const io = fakeDisk({ "vps:/r.md": "remote", "/big.md": "x".repeat(MAX_CHARS + 5), "/a.js": "js", "/bin.md": "a\0b" });
  const d = new DocController(io);
  await d.open("/r.md", "vps");
  assert.equal(d.doc.readonly, "remote");
  assert.equal(d.doc.state.readonly, true);
  d.doc.state.type("x");
  assert.equal(await d.save(), "failed");
  await d.open("/big.md", null);
  assert.equal(d.doc.readonly, "truncated");
  assert.equal(await d.save(), "failed");
  await d.open("/a.js", null);
  assert.equal(d.doc.readonly, "preview");
  assert.equal(await d.save(), "failed");
  await d.open("/bin.md", null);
  assert.equal(d.doc.state, null);
  assert.equal(await d.save(), "failed");
  assert.equal(io.writes.length, 0);
});

test("read failure shows a message, no editor", async () => {
  const io = fakeDisk({});
  io.read = async () => { throw new Error("too large"); };
  const d = new DocController(io);
  assert.equal(await d.open("/huge.md", null), true);
  assert.equal(d.doc.state, null);
  assert.match(d.doc.message, /Cannot preview/);
});

test("revert restores saved text and releases editors on close", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  assert.equal(d.revert(), true);
  assert.equal(d.doc.state.value(), "base");
  assert.equal(d.dirty, false);
  const s = d.doc.state;
  d.close();
  assert.equal(s.released, true);
});

// Review finding 1: Discard while the pending-nav Save is writing.
test("discard/cancel-save while a save is writing is refused; doc stays intact", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  const s = d.doc.state;
  s.type("mine");
  const nav = { kind: "close" };
  d.request(nav);
  io.hold = true;
  const saving = d.resolve("save");
  await tick();
  assert.equal(d.doc.saving, true);
  assert.equal(await d.resolve("discard"), null, "discard refused mid-write");
  assert.equal(d.pendingNav, nav, "choice still pending");
  assert.equal(s.released, false);
  io.release();
  assert.equal(await saving, nav, "save finished cleanly, nav may run");
  assert.equal(io.disk.get("/a.md"), "mine");
});

test("save finishing after the document was closed does not touch the released editor", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  io.hold = true;
  const p = d.save();
  d.close(); // e.g. mod closing
  io.release();
  assert.equal(await p, "saved");
  assert.equal(d.doc, null);
});

test("conflict check finishing after close does not touch the released editor", async () => {
  const io = fakeDisk({ "/a.md": "base" });
  const d = new DocController(io);
  await d.open("/a.md", null);
  d.doc.state.type("mine");
  io.disk.set("/a.md", "theirs");
  io.hold = true;
  const p = d.save();
  await tick();
  io.release(); // write rejects
  await tick();
  d.close();
  io.release(); // conflict re-read
  assert.equal(await p, "failed");
  assert.equal(d.doc, null);
});

// Review finding 2: a held document must not be remembered by other contexts, nor reopened at home.
test("held document: remembered only by its origin context, kept on return", () => {
  const doc = { path: "/A/a.md", on: null };
  // Leaving A (where it was opened): A remembers it.
  assert.deepEqual(rememberedFile(doc, "A", "A"), { path: "/A/a.md", on: null });
  // Leaving B while A's held doc is still shown: B remembers nothing of it.
  assert.equal(rememberedFile(doc, "A", "B"), null);
  assert.equal(rememberedFile(null, "A", "A"), null);
  // Entering B with the doc held: kept.
  assert.equal(keepsDoc(true, doc, null), true);
  // Saved (no longer held) while in B, back in A, which remembers it: kept, not reopened.
  assert.equal(keepsDoc(false, doc, { path: "/A/a.md", on: null }), true);
  // Clean doc, entering a context that remembers another file or none: closed.
  assert.equal(keepsDoc(false, doc, { path: "/B/b.md", on: null }), false);
  assert.equal(keepsDoc(false, doc, null), false);
  assert.equal(keepsDoc(false, doc, { path: "/A/a.md", on: "vps" }), false, "same path on another machine is a different file");
});
