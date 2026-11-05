// Desktop window for Fahis. Starts the Python backend, which owns the adapter, and passes the
// page's requests to it through a private pipe. There's no web server and no open network port.
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { pathToFileURL } = require("node:url");

const ROOT = path.join(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
// The installed app carries its own backend, and saves in the user's folder rather than beside it.
const BUNDLED_BACKEND = path.join(process.resourcesPath, "backend", "fahis-backend.exe");
const PAGE = path.join(__dirname, "index.html");
const ICON = path.join(__dirname, "icon.ico");
const DEMO = process.argv.includes("--demo");
const REQUEST_KINDS = new Set(["state", "i18n", "api"]);
const REQUEST_TIMEOUT_MS = 30000;

let backend = null;
let quitting = false;
let nextRequest = 1;
const waiting = new Map();

function message(english, arabic) {
  return app.getLocale().startsWith("ar") ? arabic : english;
}

function fail(english, arabic) {
  dialog.showErrorBox(message("Fahis", "فاحص"), message(english, arabic));
  app.quit();
}

function finishRequest(reply) {
  const request = waiting.get(reply.id);
  if (!request) return;
  waiting.delete(reply.id);
  clearTimeout(request.timer);
  request.resolve({ status: reply.status, data: reply.data });
}

function startBackend() {
  const args = ["app"];
  if (DEMO) args.push("--demo");
  const stdio = ["pipe", "pipe", "ignore"];
  if (app.isPackaged) {
    const dataFolder = path.join(app.getPath("userData"), "data");
    fs.mkdirSync(dataFolder, { recursive: true });
    backend = spawn(BUNDLED_BACKEND, args, { cwd: dataFolder, windowsHide: true, stdio,
                                             env: { ...process.env, FAHIS_DATA_DIR: dataFolder } });
  } else {
    backend = spawn(PYTHON, ["obd_tool.py", ...args], { cwd: ROOT, windowsHide: true, stdio });
  }
  readline.createInterface({ input: backend.stdout }).on("line", line => {
    try {
      finishRequest(JSON.parse(line));
    } catch (error) {
      // A line that isn't a reply can't be matched to a request, so its timeout handles it.
    }
  });
  backend.stdin.on("error", () => {});
  backend.once("exit", () => {
    backend = null;
    for (const request of waiting.values()) {
      clearTimeout(request.timer);
      request.reject(new Error("backend stopped"));
    }
    waiting.clear();
    if (!quitting) {
      fail("The Fahis backend stopped unexpectedly.", "توقفت الخدمة الخلفية لبرنامج فاحص بشكل غير متوقع.");
    }
  });
}

function sendToBackend(kind, payload) {
  return new Promise((resolve, reject) => {
    if (!backend) {
      reject(new Error("backend stopped"));
      return;
    }
    const id = nextRequest++;
    const timer = setTimeout(() => {
      waiting.delete(id);
      reject(new Error("backend did not answer"));
    }, REQUEST_TIMEOUT_MS);
    waiting.set(id, { resolve, reject, timer });
    backend.stdin.write(`${JSON.stringify({ id, kind, payload })}\n`);
  });
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 380,
    minHeight: 500,
    backgroundColor: "#101418",
    title: "Fahis",
    icon: ICON,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  const pageUrl = pathToFileURL(PAGE).href;

  // Only the app's own page loads in the window. Web links open in the normal browser.
  window.webContents.on("will-navigate", (event, target) => {
    if (target.split("#")[0] === pageUrl) return;
    event.preventDefault();
    if (target.startsWith("https://")) shell.openExternal(target);
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  window.loadFile(PAGE);
}

function start() {
  Menu.setApplicationMenu(null);
  if (!app.isPackaged && !fs.existsSync(PYTHON)) {
    fail("Python setup is missing. Follow the Setup steps in the README first.",
         "إعداد Python غير موجود. اتبع خطوات التثبيت في ملف README أولًا.");
    return;
  }
  ipcMain.handle("fahis:request", (event, kind, payload) => {
    const sender = event.senderFrame ? event.senderFrame.url.split("#")[0] : "";
    if (sender !== pathToFileURL(PAGE).href || !REQUEST_KINDS.has(kind)) {
      throw new Error("request refused");
    }
    return sendToBackend(kind, payload);
  });
  startBackend();
  createWindow();
}

if (!app.requestSingleInstanceLock()) {
  // The adapter serves one program at a time, so a second copy could never connect.
  app.quit();
} else {
  app.on("second-instance", () => {
    const [window] = BrowserWindow.getAllWindows();
    if (window) {
      if (window.isMinimized()) window.restore();
      window.focus();
    }
  });
  app.setAppUserModelId("com.alequab.fahis"); // lets the Windows taskbar show Fahis's own icon
  app.whenReady().then(start);
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => { quitting = true; });
  app.on("quit", () => {
    if (backend) {
      backend.stdin.end(); // the backend disconnects from the adapter when its pipe closes
      backend.kill();
    }
  });
}
