import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile, writeFile, rename, rm } from "node:fs/promises";
import { once } from "node:events";
import { join, resolve } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const { values } = parseArgs({
  options: {
    "no-open": { type: "boolean", default: false },
    port: { type: "string", default: "1421" },
    "data-dir": { type: "string" },
  },
});
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("端口必须是 1–65535 的整数。");
}
const origin = `http://127.0.0.1:${port}`;
const dataHome =
  process.platform === "win32"
    ? process.env.APPDATA
    : join(homedir(), "Library/Application Support");
if (!values["data-dir"] && !dataHome) throw new Error("无法确定用户目录");
const directory = resolve(
  values["data-dir"] || join(dataHome, "com.niulai.local-web-dev"),
);
const connectionFile = join(directory, ".dev-web.json");
let vite, backend, preparing;
let connection;
let stopping = false;
let stopPromise;

function openBrowser(url) {
  if (values["no-open"]) return;
  const browser =
    process.platform === "win32"
      ? spawn("rundll32.exe", ["url.dll,FileProtocolHandler", url], {
          stdio: "ignore",
        })
      : spawn("open", [url], { stdio: "ignore" });
  browser.on("error", () => console.log("请手动打开上面的本机链接。"));
}

async function reuseRunningServer() {
  try {
    const saved = JSON.parse(await readFile(connectionFile, "utf8"));
    const local = /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/;
    if (
      !local.test(saved.origin) ||
      !local.test(saved.api) ||
      !/^[a-f0-9]{64}$/.test(saved.access)
    )
      return false;
    // Check both processes with the saved token: a stale file or an occupied port
    // alone is not evidence that this workspace is still running.
    const responses = await Promise.all(
      [saved.api, saved.origin].map(async (address) => {
        const response = await fetch(`${address}/api/snapshot`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${saved.access}`,
            Origin: saved.origin,
            "Content-Type": "application/json",
          },
          body: "{}",
          signal: AbortSignal.timeout(1500),
          redirect: "error",
        });
        return response.ok && Array.isArray((await response.json()).accounts);
      }),
    );
    if (!responses.every(Boolean)) return false;
    const url = `${saved.origin}/#access=${saved.access}`;
    console.log(
      `此工作空间已在运行，使用现有页面：\n${url}\n如需重启，请在原启动终端按 Ctrl+C。`,
    );
    openBrowser(url);
    return true;
  } catch {
    return false;
  }
}

function prepare(command, args) {
  return new Promise((resolve, reject) => {
    if (stopping) return reject(new Error("已取消启动"));
    preparing = spawn(command, args, { cwd: root, stdio: "inherit" });
    preparing.once("error", reject);
    preparing.once("exit", (code) => {
      preparing = undefined;
      code === 0 ? resolve() : reject(new Error(`${command} 执行失败`));
    });
  });
}

function stop(code = 0) {
  if (stopPromise) return stopPromise;
  stopping = true;
  process.exitCode = code;
  stopPromise = (async () => {
    if (!process.stdin.isTTY) process.stdin.destroy();
    preparing?.kill();
    let exited = Promise.resolve();
    if (backend && backend.exitCode === null && backend.signalCode === null) {
      // EOF is portable, unlike forcibly killing a Windows process with SIGTERM.
      exited = once(backend, "exit").catch(() => {});
      backend.stdin.end();
      const timeout = setTimeout(() => backend.kill(), 8000);
      timeout.unref();
      exited = exited.finally(() => clearTimeout(timeout));
    }
    await Promise.all([exited, vite?.close()]);
    if (connection) {
      try {
        const saved = JSON.parse(await readFile(connectionFile, "utf8"));
        if (saved.access === connection.access)
          await rm(connectionFile, { force: true });
      } catch {
        /* Missing/stale metadata never prevents shutdown. */
      }
    }
  })();
  return stopPromise;
}
process.once("SIGINT", () => void stop());
process.once("SIGTERM", () => void stop());
if (!process.stdin.isTTY) {
  process.stdin.once("end", () => void stop());
  process.stdin.resume();
}

async function main() {
  if (await reuseRunningServer()) {
    await stop();
    return;
  }
  const python = join(
    root,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const ready =
    existsSync(python) &&
    spawnSync(
      python,
      ["-c", "import requests, srp, playwright, qrcode, keyring"],
      { stdio: "ignore" },
    ).status === 0;
  if (!ready) {
    console.log("首次启动：准备 Python 开发环境。");
    await prepare(process.execPath, [
      "scripts/python.mjs",
      "scripts/setup_worker.py",
    ]);
  }
  await prepare("cargo", [
    "build",
    "--locked",
    "--manifest-path",
    "local-web/Cargo.toml",
  ]);
  if (stopping) return;
  const executable = join(
    root,
    "local-web/target/debug",
    process.platform === "win32" ? "duo-killer-web.exe" : "duo-killer-web",
  );
  const args = [
    "--dev-origin",
    origin,
    "--no-open",
    "--port",
    "0",
    "--data-dir",
    directory,
  ];
  backend = spawn(executable, args, {
    cwd: root,
    stdio: ["pipe", "pipe", "inherit"],
  });
  backend.stdin.on("error", () => {}); // Child startup errors are reported below.
  const launch = await new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(
      () => reject(new Error("本地服务启动超时")),
      30000,
    );
    const cleanup = () => clearTimeout(timeout);
    backend.once("error", (error) => {
      cleanup();
      reject(error);
    });
    backend.once("exit", (code) => {
      cleanup();
      reject(new Error(`本地服务退出（${code}）`));
      if (!stopping) void stop(code || 1);
    });
    backend.stdout.on("data", (chunk) => {
      buffer = (buffer + chunk.toString()).slice(-10000);
      const match = buffer.match(
        /(http:\/\/127\.0\.0\.1:\d+)\/#access=([a-f0-9]+)/,
      );
      if (match) {
        cleanup();
        resolve({ api: match[1], access: match[2] });
      }
    });
  });
  if (stopping) return;
  const url = `${origin}/#access=${launch.access}`;
  vite = await createServer({
    root,
    configFile: false,
    mode: "local-web",
    clearScreen: false,
    server: {
      host: "127.0.0.1",
      port,
      strictPort: true,
      cors: false,
      open: false,
      proxy: { "/api": { target: launch.api, changeOrigin: true } },
      fs: {
        deny: [
          ".env",
          ".env.*",
          "*.{crt,pem}",
          "**/.git/**",
          "**/auth*.json",
          "**/checkout.json",
          "**/preorder.json",
          "**/preorder-address.json",
          "**/.dev-web.json",
          "**/.dev-web.json.*.tmp",
        ],
      },
      watch: {
        ignored: [
          "**/.venv/**",
          "**/local-web/target/**",
          "**/src-tauri/target/**",
          "**/local-web-release/**",
          "**/worker/{build,dist}/**",
        ],
      },
    },
  });
  if (stopping) {
    await vite.close();
    return;
  }
  await vite.listen();
  connection = { origin, api: launch.api, access: launch.access };
  const temporary = `${connectionFile}.${process.pid}.tmp`;
  try {
    await writeFile(temporary, JSON.stringify(connection), { mode: 0o600 });
    await rename(temporary, connectionFile);
  } finally {
    await rm(temporary, { force: true });
  }
  console.log(
    `\nDUO Killer · Web 开发\n${url}\n页面保存后自动更新；Python 修改在下一次操作生效。\n修改 Rust 后重启 pnpm dev:web；按 Ctrl+C 退出。\n`,
  );
  openBrowser(url);
}

main().catch(async (error) => {
  if (!stopping) console.error(`启动失败：${error.message}`);
  await stop(stopping ? process.exitCode : 1);
});
