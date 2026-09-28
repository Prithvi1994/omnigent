"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { Writable } = require("node:stream");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createRequire } = require("node:module");

const ORIGIN = "https://workspace.cloud.databricks.com";
const COOKIE = {
  name: "DBAUTH",
  domain: new URL(ORIGIN).hostname,
  hostOnly: true,
  path: "/",
  value: "new",
};

function harness({ respond, follow, oldCookie = false, stored } = {}) {
  const requests = [];
  const logs = [];
  const calls = { browser: 0, stored: 0, storedOrigins: [] };
  let jar = oldCookie ? [{ ...COOKIE, value: "old" }] : [];
  const cookies = Object.assign(new EventEmitter(), { get: async () => jar });
  const ses = { cookies };
  function response(request, status = 200, writeCookie = true, { body = "", headers = {} } = {}) {
    if (writeCookie) {
      jar = [COOKIE];
      cookies.emit("changed", {}, COOKIE, "explicit", false);
    }
    const res = new EventEmitter();
    res.statusCode = status;
    res.headers = headers;
    request.emit("response", res);
    if (body) res.emit("data", body);
    res.emit("end");
    res.emit("close");
  }
  const net = {
    request: (options) => {
      // Electron closes its Writable after end(), before the network response arrives.
      const req = Object.assign(
        new Writable({
          autoDestroy: true,
          final(callback) {
            callback();
            setImmediate(() => {
              if (this.aborted) return;
              if (respond) respond(this, response);
              else this.emit("redirect", 302, "GET", `${ORIGIN}/omnigent`, {});
            });
          },
        }),
        {
          options,
          events: [],
          aborted: false,
          followed: 0,
          setHeader() {},
          abort() {
            this.aborted = true;
            this.emit("abort");
            this.destroy();
          },
          followRedirect() {
            this.followed++;
            if (follow) follow(this, response);
            else response(this);
          },
        },
      );
      for (const event of ["close", "redirect", "response"]) {
        req.on(event, () => req.events.push(event));
      }
      requests.push(req);
      return req;
    },
  };
  const file = path.join(__dirname, "../src/databricks-session.js");
  const realRequire = createRequire(file);
  const module = { exports: {} };
  vm.runInNewContext(
    fs.readFileSync(file, "utf8"),
    {
      module,
      URL,
      console: { log: (...args) => logs.push(args), warn: (...args) => logs.push(args) },
      setTimeout,
      clearTimeout,
      require: (specifier) => {
        if (specifier === "electron") return { net };
        if (specifier === "./databricks-oauth")
          return {
            isTrustedDatabricksOrigin: realRequire("./url").isDatabricksOAuthServerUrl,
            runInteractiveLogin: async () => {
              calls.browser++;
              return { tokens: { access_token: "token" }, issuerOrigin: ORIGIN };
            },
            getValidStoredToken: async (origin) => {
              calls.stored++;
              calls.storedOrigins.push(origin);
              return stored ? stored(origin) : "token";
            },
            saveWorkspaceToken() {},
          };
        if (specifier === "./databricks-account") return { parseAccountFromToken: () => null };
        return realRequire(specifier);
      },
    },
    { filename: file },
  );
  return { ...module.exports, requests, ses, calls, logs };
}

describe("Databricks session preparation", () => {
  it("rejects non-Databricks and Apps URLs before opening a browser or reading tokens", async () => {
    const h = harness();
    await Promise.all(
      ["https://server.example", "https://app.databricksapps.com", "http://localhost:8000"].map(
        async (origin) => {
          await assert.rejects(h.ensureDatabricksSession(h.ses, origin), /Databricks workspace/);
          await assert.rejects(
            h.ensureDatabricksSession(h.ses, origin, { interactive: false }),
            /Databricks workspace/,
          );
        },
      ),
    );
    assert.equal(h.calls.browser, 0);
    assert.equal(h.calls.stored, 0);
    assert.equal(h.requests.length, 0);
  });
  it("connects with stored credentials without opening the browser", async () => {
    const h = harness();
    assert.equal(await h.ensureDatabricksSession(h.ses, ORIGIN), ORIGIN);
    assert.equal(h.calls.browser, 0);
    assert.equal(h.calls.stored, 1);
    assert.equal(h.requests.length, 1);
    const output = JSON.stringify(h.logs);
    assert.match(output, /connected with stored credentials/);
    assert.doesNotMatch(output, /"token"/);
  });
  it("skips stored credentials when the caller requires a browser sign-in", async () => {
    const h = harness();
    assert.equal(
      await h.ensureDatabricksSession(h.ses, ORIGIN, { useStoredCredentials: false }),
      ORIGIN,
    );
    assert.equal(h.calls.stored, 0);
    assert.equal(h.calls.browser, 1);
  });
  for (const [label, stored] of [
    [
      "no stored token",
      () => {
        throw Object.assign(new Error("no stored Databricks token"), {
          errorCode: "NO_STORED_TOKEN",
        });
      },
    ],
    [
      "a dead refresh grant",
      () => {
        throw Object.assign(new Error("token endpoint 400: invalid_grant"), {
          status: 400,
          errorCode: "invalid_grant",
        });
      },
    ],
    [
      "a network failure",
      () => {
        throw new TypeError("fetch failed");
      },
    ],
  ]) {
    it(`falls back to browser sign-in on connect with ${label}`, async () => {
      const h = harness({ stored });
      assert.equal(await h.ensureDatabricksSession(h.ses, ORIGIN), ORIGIN);
      assert.equal(h.calls.stored, 1);
      assert.equal(h.calls.browser, 1);
      assert.match(JSON.stringify(h.logs), /stored credentials unusable; signing in/);
    });
  }
  it("falls back to browser sign-in when minting from stored credentials fails", async () => {
    let requests = 0;
    const h = harness({
      respond(req) {
        if (++requests === 1) req.emit("error", new Error("net::ERR_NAME_NOT_RESOLVED"));
        else req.emit("redirect", 302, "GET", `${ORIGIN}/omnigent`, {});
      },
    });
    assert.equal(await h.ensureDatabricksSession(h.ses, ORIGIN), ORIGIN);
    assert.equal(h.calls.stored, 1);
    assert.equal(h.calls.browser, 1);
    assert.equal(h.requests.length, 2);
  });
  it("sends an account URL through browser sign-in since tokens are stored per workspace", async () => {
    const account = "https://accounts.cloud.databricks.com";
    const h = harness({
      stored: (origin) => {
        if (origin !== ORIGIN) {
          throw Object.assign(new Error("no stored Databricks token"), {
            errorCode: "NO_STORED_TOKEN",
          });
        }
        return "workspace-token";
      },
    });
    await h.ensureDatabricksSession(h.ses, account);
    assert.deepEqual(h.calls.storedOrigins, [account]);
    assert.equal(h.calls.browser, 1);
  });
  it("stops at cancellation instead of falling back to browser sign-in", async () => {
    const controller = new AbortController();
    const h = harness({
      stored: () => {
        controller.abort();
        return "token";
      },
    });
    await assert.rejects(
      h.ensureDatabricksSession(h.ses, ORIGIN, { signal: controller.signal }),
      (error) => error.name === "AbortError",
    );
    assert.equal(h.calls.browser, 0);
    assert.equal(h.requests.length, 0);
  });
  it("stops at cancellation during a failed stored lookup", async () => {
    const controller = new AbortController();
    const h = harness({
      stored: () => {
        controller.abort();
        throw new TypeError("fetch failed");
      },
    });
    await assert.rejects(
      h.ensureDatabricksSession(h.ses, ORIGIN, { signal: controller.signal }),
      (error) => error.name === "AbortError",
    );
    assert.equal(h.calls.browser, 0);
  });
  it("uses stored credentials for silent restoration without interactive login", async () => {
    const h = harness();
    assert.equal(await h.ensureDatabricksSession(h.ses, ORIGIN, { interactive: false }), ORIGIN);
    assert.equal(h.calls.browser, 0);
    assert.equal(h.calls.stored, 1);
  });
});

describe("Databricks cookie minting", () => {
  it("follows the intended same-origin app redirect and confirms the cookie", async () => {
    const h = harness();
    await h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent");
    assert.equal(h.requests[0].options.redirect, "manual");
    assert.equal(h.requests[0].options.useSessionCookies, true);
    assert.equal(h.requests[0].followed, 1);
    assert.equal(h.ses.cookies.listenerCount("changed"), 0);
  });
  it("waits for the response when the request writable closes before the redirect", async () => {
    const h = harness();
    await h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent");
    assert.deepEqual(h.requests[0].events, ["close", "redirect", "response"]);
    assert.equal(h.ses.cookies.listenerCount("changed"), 0);
  });
  it("rejects login redirects instead of performing embedded session authentication", async () => {
    const h = harness({
      oldCookie: true,
      respond(req) {
        req.emit("redirect", 303, "GET", `${ORIGIN}/login/sso`, {});
      },
    });
    await assert.rejects(
      h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent"),
      /unexpected destination/,
    );
    assert.equal(h.requests[0].followed, 0);
    assert.equal(h.requests[0].aborted, true);
  });
  it("rejects an unchanged stale cookie even after a 200 response", async () => {
    const h = harness({ oldCookie: true, respond: (req, response) => response(req, 200, false) });
    await assert.rejects(h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent"), /no new DBAUTH/);
  });
  it("rejects HTTP failures even with an existing cookie", async () => {
    const h = harness({ oldCookie: true, respond: (req, response) => response(req, 401, false) });
    await assert.rejects(h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent"), /HTTP 401/);
  });
  it("identifies a direct session-create rejection without logging credentials or response bodies", async () => {
    const h = harness({
      respond: (req, response) =>
        response(req, 403, false, {
          body: JSON.stringify({
            error_code: "PERMISSION_DENIED",
            message: "private-response-body",
          }),
          headers: {
            "x-databricks-request-id": "trace-123",
            "set-cookie": "private-cookie-header",
          },
        }),
    });
    await assert.rejects(
      h.mintSessionCookie(h.ses, ORIGIN, "private-access-token", "/omnigent"),
      (error) => {
        assert.equal(error.phase, "session-create");
        assert.equal(error.status, 403);
        assert.equal(error.errorCode, "PERMISSION_DENIED");
        assert.equal(error.requestId, "trace-123");
        assert.match(error.message, /no redirect/);
        assert.doesNotMatch(error.message, /private-/);
        return true;
      },
    );
    const output = JSON.stringify(h.logs);
    assert.match(output, /bridge request/);
    assert.match(output, /bridge rejected/);
    assert.match(output, /PERMISSION_DENIED/);
    assert.match(output, /trace-123/);
    assert.doesNotMatch(output, /private-/);
  });
  it("distinguishes a landing-page 403 from a session-create 403", async () => {
    const h = harness({ follow: (req, response) => response(req, 403) });
    await assert.rejects(h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent"), (error) => {
      assert.equal(error.phase, "workspace landing");
      assert.match(error.message, /landing after.*redirect/);
      assert.equal(error.status, 403);
      return true;
    });
  });
  it("logs redirect destinations without their authorization query parameters", async () => {
    const h = harness({
      respond(req) {
        req.emit(
          "redirect",
          302,
          "GET",
          `${ORIGIN}/login?code=private-code&state=private-state`,
          {},
        );
      },
    });
    await assert.rejects(h.mintSessionCookie(h.ses, ORIGIN, "private-token", "/omnigent"));
    const output = JSON.stringify(h.logs);
    assert.match(output, /bridge redirect/);
    assert.match(output, /\/login/);
    assert.doesNotMatch(output, /private-/);
  });
  it("aborts a pending cookie request and removes its cookie listener", async () => {
    const h = harness({ respond() {} });
    const controller = new AbortController();
    const minting = h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent", {
      signal: controller.signal,
    });
    const rejected = assert.rejects(minting, (error) => error.name === "AbortError");
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.equal(h.requests.length, 1);
    controller.abort();
    await rejected;
    assert.equal(h.requests[0].aborted, true);
    assert.equal(h.ses.cookies.listenerCount("changed"), 0);
  });
  it("does not start cookie minting after cancellation", async () => {
    const h = harness();
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(
      h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent", { signal: controller.signal }),
      (error) => error.name === "AbortError",
    );
    assert.equal(h.requests.length, 0);
  });
  it("settles on timeout even when abort emits no error", async () => {
    const h = harness({ respond() {} });
    let timeout;
    let started;
    const timerReady = new Promise((resolve) => {
      started = resolve;
    });
    const promise = h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent", {
      setTimeoutFn: (fn) => {
        timeout = fn;
        started();
        return 1;
      },
      clearTimeoutFn() {},
    });
    const rejected = assert.rejects(promise, /timed out/);
    await timerReady;
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.deepEqual(h.requests[0].events, ["close"]);
    timeout();
    await rejected;
    assert.equal(h.requests[0].aborted, true);
    assert.equal(h.ses.cookies.listenerCount("changed"), 0);
  });
  it("rejects request errors and aborts after the writable stream has closed", async () => {
    await Promise.all(
      ["error", "abort"].map((event) => {
        const h = harness({ respond: (req) => req.emit(event, new Error("network failed")) });
        return assert.rejects(
          h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent"),
          /network failed|request aborted/,
        );
      }),
    );
  });
  it("rejects a response that errors, aborts, or closes before end", async () => {
    await Promise.all(
      ["error", "aborted", "close"].map((event) => {
        const h = harness({
          respond(req) {
            const res = new EventEmitter();
            req.emit("response", res);
            res.emit(event, new Error("response failed"));
          },
        });
        return assert.rejects(
          h.mintSessionCookie(h.ses, ORIGIN, "token", "/omnigent"),
          /response failed|response aborted|response closed/,
        );
      }),
    );
  });
  it("ships the workspace picker HTML and script", () => {
    const packageJson = require("../package.json");
    assert.ok(packageJson.build.files.includes("workspace-picker/**/*"));
    assert.ok(fs.existsSync(path.join(__dirname, "../workspace-picker/index.html")));
    assert.ok(fs.existsSync(path.join(__dirname, "../workspace-picker/picker.js")));
  });
});
