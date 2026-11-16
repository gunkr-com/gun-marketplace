// Folder listing, the folder check behind Path, and the self-test's scratch folder:
// programs run through an `exec` with herdr's signature. No herdr/gpui imports, so
// Node tests can drive it with a fake exec, or with real PowerShell and sh.
//
// macOS, Linux and remote POSIX machines: sh, test, mktemp, mkdir, rm (as before).
// Windows, here or on a remote machine whose folder is a Windows path: Windows
// PowerShell with .NET calls. A path is handed to PowerShell as UTF-8 Base64 and
// decoded there, so quotes, $, `, ;, & and any Unicode in it are never parsed as code.
import { isWindowsPath } from "./core.js";

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/** UTF-8 bytes of text, in Base64 (no Buffer or btoa in the mod runtime). */
export function utf8Base64(text) {
  const bytes = [];
  for (const ch of String(text)) {
    const c = ch.codePointAt(0);
    if (c < 0x80) bytes.push(c);
    else if (c < 0x800) bytes.push(0xc0 | (c >> 6), 0x80 | (c & 63));
    else if (c < 0x10000) bytes.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    else bytes.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
  }
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + (i + 1 < bytes.length ? B64[(n >> 6) & 63] : "=") + (i + 2 < bytes.length ? B64[n & 63] : "=");
  }
  return out;
}

// exec("powershell", psArgs(...)): the script sees the path as $p. No double quotes in
// any script, so Windows argument quoting never has to escape one.
export function psArgs(body, path) {
  const p = path == null ? "" : `$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${utf8Base64(path)}')); `;
  return ["-NoProfile", "-NonInteractive", "-Command",
    "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + p + body];
}

// Get-ChildItem lists a file as itself, so the folder is checked first.
// Links keep directory attributes even when dangling; check their target.
// Other reparse points (OneDrive's folders) have no LinkType and are folders.
const PS_LIST = "if (-not [IO.Directory]::Exists($p)) { [Console]::Error.Write('Not a folder: ' + $p); exit 1 }; " +
  "@(Get-ChildItem -LiteralPath $p -Force | Where-Object Name -ne '.git' | Select-Object -First 501 | ForEach-Object { @{ name=$_.Name; " +
  "dir=($_.PSIsContainer -and (-not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -or -not $_.LinkType -or ($_.Target -and " +
  "[IO.Directory]::Exists(([IO.Path]::Combine([IO.Path]::GetDirectoryName($_.FullName), @($_.Target)[0])))))) } }) | ConvertTo-Json -Compress";
const PS_IS_FOLDER = "if ([IO.Directory]::Exists($p)) { exit 0 }; exit 1";
const PS_TEMP = "$d = Join-Path ([IO.Path]::GetTempPath()) ('herdr-files-selftest.' + [Guid]::NewGuid().ToString('N').Substring(0, 12)); " +
  "[void][IO.Directory]::CreateDirectory($d); [Console]::Out.Write($d)";
const SH_LIST = 'n=0; for p in "$1"/* "$1"/.[!.]* "$1"/..?*; do [ -e "$p" ] || [ -L "$p" ] || continue; name=${p##*/}; [ "$name" = .git ] && continue; if [ -d "$p" ]; then kind=d; else kind=f; fi; printf "%s\\0%s\\0" "$kind" "$name"; n=$((n+1)); [ "$n" -gt 500 ] && break; done; test -d "$1"';
export const TEMP_NAME = /[\\/]herdr-files-selftest\.[A-Za-z0-9]+$/;

const missingProgram = (e) => /cannot start \S+: program not found/.test(String(e));

/**
 * exec: herdr's exec(command, args, { on }). platform: () => window().platform
 * ("windows" | "macos" | "linux"). Every call is on this machine unless `on` names one.
 */
export function makeFolders({ exec, platform }) {
  // Here: the machine decides. Elsewhere: the folder's own path style decides.
  const windows = (path, on) => (on ? isWindowsPath(path) : platform() === "windows");
  const ps = (body, path, on) => exec("powershell", psArgs(body, path), { on });
  // A POSIX program, or PowerShell (ps: true) when a remote machine has no such program.
  const posix = async (command, args, on, body, path) => {
    try { return { r: await exec(command, args, { on }), ps: false }; }
    catch (e) { if (on && missingProgram(e)) return { r: await ps(body, path, on), ps: true }; throw e; }
  };
  const parsePs = (r) => {
    if (r.code !== 0) throw new Error(r.stderr || "Cannot list this folder");
    const result = JSON.parse((r.stdout || "").trim() || "[]");
    return Array.isArray(result) ? result : [result];
  };
  const must = (r, what) => { if (r.code !== 0) throw new Error(`${what} failed: ${r.stderr || `exit ${r.code}`}`); return r; };

  return {
    /** Entries of a folder: [{ name, dir }], at most 501. Rejects when it is not a folder. */
    async list(path, on) {
      if (windows(path, on)) return parsePs(await ps(PS_LIST, path, on));
      const { r, ps: viaPs } = await posix("sh", ["-c", SH_LIST, "files", path], on, PS_LIST, path);
      if (viaPs) return parsePs(r);
      if (r.code !== 0) throw new Error(r.stderr || "Cannot list this folder");
      const fields = r.stdout.split("\0"), out = [];
      for (let i = 0; i + 1 < fields.length; i += 2) out.push({ name: fields[i + 1], dir: fields[i] === "d" });
      return out;
    },

    /** true only for an existing folder (a file, a missing path or an error is false). */
    async isFolder(path, on) {
      try {
        const r = windows(path, on) ? await ps(PS_IS_FOLDER, path, on) : (await posix("test", ["-d", path], on, PS_IS_FOLDER, path)).r;
        return r.code === 0;
      } catch { return false; }
    },

    // ---- for the self-test, on this machine only ----

    /** A new empty folder under the system temp folder, named herdr-files-selftest.<random>. */
    async makeTemp() {
      const r = platform() === "windows" ? await ps(PS_TEMP) : await exec("mktemp", ["-d", "/tmp/herdr-files-selftest.XXXXXX"]);
      const dir = r.stdout.trim();
      if (r.code !== 0 || !TEMP_NAME.test(dir)) throw new Error("Cannot make the self-test folder: " + (r.stderr || dir));
      return dir;
    },
    async makeFolder(path) {
      must(platform() === "windows" ? await ps("[void][IO.Directory]::CreateDirectory($p)", path) : await exec("mkdir", ["--", path]), "mkdir");
    },
    async removeFile(path) {
      must(platform() === "windows" ? await ps("[IO.File]::Delete($p)", path) : await exec("rm", ["-f", "--", path]), "rm");
    },
    /** A file holding the bytes a, NUL, b. */
    async writeBinary(path) {
      must(platform() === "windows" ? await ps("[IO.File]::WriteAllBytes($p, [byte[]](97, 0, 98))", path) : await exec("sh", ["-c", 'printf "a\\000b" > "$1"', "sh", path]), "binary fixture");
    },
  };
}
