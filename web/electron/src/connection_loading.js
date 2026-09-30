"use strict";

const path = require("node:path");

/** A shell-owned loading indicator that survives replacement of the server document. */
function createConnectionLoading({ BrowserWindow, platform = process.platform }) {
  const entries = new Map();

  function show(parent, attempt, label) {
    if (parent.isDestroyed()) return;
    let entry = entries.get(parent);
    if (!entry) {
      const window = new BrowserWindow({
        parent,
        frame: false,
        transparent: true,
        hasShadow: false,
        show: false,
        resizable: false,
        movable: false,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        ...(platform === "darwin" ? { focusable: false, hiddenInMissionControl: true } : {}),
        width: 340,
        height: 64,
        webPreferences: {
          preload: path.join(__dirname, "connection_loading_preload.js"),
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
        },
      });
      if (platform === "darwin") window.excludedFromShownWindowsMenu = true;
      window.setIgnoreMouseEvents(true, { forward: true });
      entry = { window, attempt, label, ready: false };
      entries.set(parent, entry);
      const position = () => {
        if (parent.isDestroyed() || window.isDestroyed()) return;
        const bounds = parent.getContentBounds();
        window.setBounds({
          x: bounds.x + Math.round((bounds.width - 340) / 2),
          y: bounds.y + 40,
          width: 340,
          height: 64,
        });
      };
      const close = () => {
        if (!window.isDestroyed()) window.destroy();
      };
      parent.on("move", position);
      parent.on("resize", position);
      parent.on("closed", close);
      window.on("closed", () => {
        entries.delete(parent);
        parent.removeListener("move", position);
        parent.removeListener("resize", position);
        parent.removeListener("closed", close);
      });
      window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      window.webContents.on("will-navigate", (event) => event.preventDefault());
      window.webContents.on("did-finish-load", () => {
        if (window.isDestroyed() || entries.get(parent) !== entry) return;
        entry.ready = true;
        window.webContents.send("omnigent:connection-loading", entry.label);
        window.showInactive();
      });
      position();
      void window
        .loadFile(path.join(__dirname, "..", "connection-loading", "index.html"))
        .catch(close);
    }
    entry.attempt = attempt;
    entry.label = label;
    if (entry.ready) entry.window.webContents.send("omnigent:connection-loading", label);
  }

  function hide(parent, attempt) {
    const entry = entries.get(parent);
    if (entry?.attempt === attempt && !entry.window.isDestroyed()) entry.window.destroy();
  }

  return { show, hide };
}

module.exports = { createConnectionLoading };
