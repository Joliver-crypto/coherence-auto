/**
 * ============================================================================
 *  preload.js  —  Electron Preload Script
 * ============================================================================
 *
 * Overview
 * --------
 * Runs in the renderer context *before* the page scripts but with access to
 * Node.js APIs.  We expose a minimal ``window.api`` bridge so the renderer
 * can interact with the backend without having full Node access (security
 * best-practice: contextIsolation = true, nodeIntegration = false).
 *
 * Exposed API
 * -----------
 *   window.api.backendUrl   — base URL of the Python backend ("http://…:5000")
 *
 * ============================================================================
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  backendUrl: "http://127.0.0.1:5000",
  openFile: (filePath) => ipcRenderer.invoke("open-file", filePath),
});
