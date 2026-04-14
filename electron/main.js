/**
 * ============================================================================
 *  main.js  —  Electron Main Process
 * ============================================================================
 *
 * Overview
 * --------
 * This is the Electron main process for Coherence Auto.  It:
 *   1. Spawns the Python backend (scripts/webrtc_camera.py) as a child process.
 *   2. Waits for the backend HTTP server to come up on port 5000.
 *   3. Creates the BrowserWindow and loads index.html.
 *   4. When the window is closed, kills the Python process and exits.
 *
 * The renderer (index.html) connects to the Python backend directly via
 * WebRTC (for the live video) and WebSocket (for camera commands and saves).
 *
 * ============================================================================
 */

const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("path");
const { spawn } = require("child_process");
const http = require("http");

// ---------------------------------------------------------------------------
//  Configuration
// ---------------------------------------------------------------------------

const PYTHON_SCRIPT = path.join(__dirname, "..", "scripts", "webrtc_camera.py");
const BACKEND_URL = "http://127.0.0.1:5000";
const POLL_INTERVAL_MS = 500; // how often to check if backend is up
const POLL_TIMEOUT_MS = 15000; // give up after this long

// ---------------------------------------------------------------------------
//  Python backend process
// ---------------------------------------------------------------------------

let pyProcess = null;

function startBackend() {
  console.log("[main] Starting Python backend:", PYTHON_SCRIPT);
  pyProcess = spawn("python", [PYTHON_SCRIPT], {
    cwd: path.dirname(PYTHON_SCRIPT),
    stdio: ["ignore", "pipe", "pipe"],
  });

  pyProcess.stdout.on("data", (d) => process.stdout.write("[py] " + d));
  pyProcess.stderr.on("data", (d) => process.stderr.write("[py:err] " + d));
  pyProcess.on("exit", (code) => {
    console.log(`[main] Python exited with code ${code}`);
    pyProcess = null;
  });
}

function stopBackend() {
  if (pyProcess) {
    console.log("[main] Stopping Python backend…");
    pyProcess.kill();
    pyProcess = null;
  }
}

// ---------------------------------------------------------------------------
//  Wait for backend to respond on its HTTP port
// ---------------------------------------------------------------------------

function waitForBackend() {
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function poll() {
      const req = http.get(`${BACKEND_URL}/camera_status`, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => resolve(body));
      });
      req.on("error", () => {
        if (Date.now() - start > POLL_TIMEOUT_MS) {
          reject(new Error("Backend did not start in time"));
        } else {
          setTimeout(poll, POLL_INTERVAL_MS);
        }
      });
      req.end();
    }

    poll();
  });
}

// ---------------------------------------------------------------------------
//  Electron window
// ---------------------------------------------------------------------------

let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    title: "Coherence Auto — Live Camera",
    backgroundColor: "#1a1a1a",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "index.html"));

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// ---------------------------------------------------------------------------
//  App lifecycle
// ---------------------------------------------------------------------------

// Open a file in the system's default editor (used by "Edit Script" button)
ipcMain.handle("open-file", async (_event, filePath) => {
  return shell.openPath(filePath);
});

app.whenReady().then(async () => {
  startBackend();

  try {
    console.log("[main] Waiting for backend…");
    await waitForBackend();
    console.log("[main] Backend is up.");
  } catch (err) {
    console.error("[main]", err.message);
  }

  createWindow();
});

app.on("window-all-closed", () => {
  stopBackend();
  app.quit();
});

app.on("before-quit", () => {
  stopBackend();
});
