// `herdr-files.selftest`: drives a separate DocController with the real readTextFile,
// writeTextFile and EditorState, and the panel's folder listing and folder check, against
// fixtures in a fresh scratch folder: /tmp/herdr-files-selftest.XXXXXX (mktemp -d) on
// macOS and Linux, %TEMP%\herdr-files-selftest.<random> on Windows.
// It writes only inside that folder, never anywhere a caller names.
// Result: <scratch folder>/result.json, and a toast.
import { exec, readTextFile, writeTextFile, notify, window } from "herdr";
import { DocController, joinPath, parentDir, resolveInput, samePath, isWindowsPath } from "./core.js";
import { makeFolders } from "./folders.js";

export async function selftest(_cx, makeState) {
  const folders = makeFolders({ exec, platform: () => window()?.platform });
  const dir = await folders.makeTemp();
  const results = [];
  const check = (name, ok, detail) => results.push({ name, ok: Boolean(ok), detail });
  const states = [];
  const docs = new DocController({
    read: (path, on) => readTextFile(path, { on }),
    write: (path, text, expected) => writeTextFile(path, text, expected),
    makeState: (o) => { const s = makeState(o); states.push(s); return s; },
  });
  const type = (text) => { docs.doc.state.set_value(text); docs.changed(docs.doc, null); };
  const f = (name) => joinPath(dir, name);
  try {
    await writeTextFile(f("note one.md"), "# one\n", null);
    await writeTextFile(f("b.md"), "b\n", null);
    await writeTextFile(f("big.md"), "x".repeat(400001), null);
    await writeTextFile(f("code.js"), "let a = 1;\n", null);
    await folders.writeBinary(f("bin.md"));

    check("open editable (path with space)", await docs.open(f("note one.md"), null) && docs.editable && !docs.dirty);
    type("# one\nedited\n");
    check("edit marks dirty", docs.dirty);
    check("close is held while dirty", !docs.request({ kind: "close" }) && docs.pendingNav?.kind === "close");
    check("cancel keeps document", (await docs.resolve("cancel")) === null && docs.dirty);
    let r = await docs.save();
    check("save writes", r === "saved" && !docs.dirty && (await readTextFile(f("note one.md"))) === "# one\nedited\n", r);

    await writeTextFile(f("note one.md"), "external\n", "# one\nedited\n");
    type("mine\n");
    r = await docs.save();
    check("external change -> conflict, disk kept", r === "conflict" && docs.doc.conflict?.disk === "external\n" && (await readTextFile(f("note one.md"))) === "external\n", r);
    r = await docs.save("overwrite");
    check("explicit overwrite", r === "saved" && (await readTextFile(f("note one.md"))) === "mine\n", r);

    await folders.removeFile(f("note one.md"));
    type("again\n");
    r = await docs.save();
    check("deleted -> deleted, not recreated", r === "deleted" && (await readTextFile(f("note one.md"))) === null, r);
    r = await docs.save("recreate");
    check("explicit recreate", r === "saved" && (await readTextFile(f("note one.md"))) === "again\n", r);

    const first = docs.open(f("note one.md"), null), second = docs.open(f("b.md"), null);
    const [a, b] = await Promise.all([first, second]);
    check("stale load discarded", a === false && b === true && docs.doc.path === f("b.md"));

    await docs.open(f("big.md"), null);
    check("truncated is read-only", docs.doc.readonly === "truncated" && (await docs.save()) === "failed");
    await docs.open(f("bin.md"), null);
    check("binary has no editor", !docs.doc.state && /Binary/.test(docs.doc.message ?? ""));
    await docs.open(f("code.js"), null);
    check("non-text preview read-only", docs.doc.readonly === "preview");
    check("files untouched", (await readTextFile(f("big.md")))?.length === 400001 && (await readTextFile(f("code.js"))) === "let a = 1;\n");

    // Folders: the Path check, the listing and parent navigation, with a name that
    // needs quoting care (apostrophes, curly quotes, $, `, ;, &, spaces, Unicode).
    const name = "it's ‘q’ 폴더 $x `y; & ñ", sub = joinPath(dir, name);
    await folders.makeFolder(sub);
    check("folder check: scratch folder", await folders.isFolder(dir, null));
    check("folder check: quoted Unicode name", await folders.isFolder(sub, null));
    check("folder check: a file is not a folder", !(await folders.isFolder(f("b.md"), null)));
    check("folder check: missing is not a folder", !(await folders.isFolder(f("missing"), null)));
    const listed = await folders.list(dir, null);
    check("listing: folders and files", listed.some(e => e.name === name && e.dir) && listed.some(e => e.name === "b.md" && !e.dir), JSON.stringify(listed));
    check("listing: a file is refused", await folders.list(f("b.md"), null).then(() => false, () => true));
    const win = isWindowsPath(dir);
    check("parent of the subfolder", samePath(parentDir(sub), dir), parentDir(sub));
    check("typed .. and quoted path", samePath(resolveInput("..", sub, null, win), dir) && samePath(resolveInput(`"${sub}"`, dir, null, win), sub));

    const inner = joinPath(sub, "노트 it's.md");
    await writeTextFile(inner, "u\n", null);
    check("open editable (Unicode folder and name)", await docs.open(inner, null) && docs.editable);
    type("u2\n");
    r = await docs.save();
    check("save in Unicode folder", r === "saved" && (await readTextFile(inner)) === "u2\n", r);

    await writeTextFile(f("crlf.md"), "a\r\nb\r\n", null);
    await docs.open(f("crlf.md"), null);
    check("CRLF file opens unmodified", docs.editable && !docs.dirty && docs.doc.state.value() === "a\r\nb\r\n", JSON.stringify(docs.doc.state?.value()));
  } catch (e) {
    check("no exception", false, String(e?.stack ?? e));
  } finally {
    docs.close();
    for (const s of states) try { s.release(); } catch {}
  }
  const passed = results.filter(x => x.ok).length;
  const summary = { ok: passed === results.length, passed, total: results.length, dir, results };
  const result = joinPath(dir, "result.json");
  await writeTextFile(result, JSON.stringify(summary, null, 2), null).catch(() => {});
  notify({ title: "herdr-files selftest", body: `${passed}/${results.length} passed · ${result}`, level: summary.ok ? "info" : "error" });
  return summary;
}
