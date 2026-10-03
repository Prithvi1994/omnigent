// Runs src/preload.js in a VM with a scripted ipcRenderer, so bridge tests can
// script invoke replies and fire main→renderer events without Electron.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PRELOAD = fs.readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");

/**
 * @param {(channel: string, args: unknown) => unknown} [respond] Reply for
 *   `ipcRenderer.invoke`; channels it leaves undefined resolve to null.
 */
function loadPreload(respond = () => null) {
  const exposed = new Map();
  const listeners = new Map();
  const invokes = [];
  const ipcRenderer = {
    invoke: async (channel, args) => {
      invokes.push({ channel, args });
      return respond(channel, args) ?? null;
    },
    send: () => {},
    on: (channel, listener) => listeners.set(channel, listener),
    removeListener: (channel, listener) => {
      if (listeners.get(channel) === listener) listeners.delete(channel);
    },
  };
  vm.runInNewContext(PRELOAD, {
    console,
    require: (specifier) => {
      assert.equal(specifier, "electron");
      return {
        contextBridge: { exposeInMainWorld: (name, value) => exposed.set(name, value) },
        ipcRenderer,
      };
    },
  });
  return {
    desktop: exposed.get("omnigentDesktop"),
    emit: (channel, payload) => listeners.get(channel)?.({}, payload),
    hasListener: (channel) => listeners.has(channel),
    invokes,
  };
}

module.exports = { loadPreload };
