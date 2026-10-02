// Desktop-shell journey: connecting to a self-hosted server whose auth
// provider is `oidc`. The shell must hand the IdP sign-in to the system browser
// (RFC 8252 §8.12) instead of rendering the third-party IdP page in its own
// window, where a passkey (WebAuthn) ceremony has no authenticator to settle it.
//
// Run from web/electron after building the SPA (see e2e/README.md):
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_oidc_in_window_idp.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");
const { startFakeIdp, oidcServerEnv } = require("./fixtures/fakeOidcIdp");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-oidc-in-window-idp");
const IDP_NAVIGATION_WINDOW_MS = 20_000;
const PASSKEY_TIMEOUT_MS = 1_500;
// Far past both the RP timeout and Chromium's 10s floor for WebAuthn timeouts.
const PASSKEY_OBSERVATION_MS = 30_000;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

describe(
  "desktop shell — self-hosted OIDC sign-in",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let idp;
    let server;
    /** What the shell window did after Connect; filled once by the journey. */
    const observed = {
      windowUrls: [],
      inWindowIdpUrl: null,
      idpAuthorizeAgents: [],
      windowCount: 0,
      webauthn: null,
      recordings: [],
    };

    async function driveConnectJourney() {
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir: RECORD_DIR,
      });
      try {
        const urlField = window.locator("#url");
        await urlField.waitFor({ state: "visible", timeout: 15_000 });
        await urlField.fill(server.serverUrl);
        // Hold so the typed URL is legible in the recording before Connect.
        await sleep(2_500);
        await window.locator("#connect").click();

        const idpOrigin = new URL(idp.issuer).origin;
        const navigationDeadline = Date.now() + IDP_NAVIGATION_WINDOW_MS;
        /* oxlint-disable no-await-in-loop -- sequential observation of one window */
        while (Date.now() < navigationDeadline) {
          const url = window.url();
          if (observed.windowUrls.at(-1) !== url) observed.windowUrls.push(url);
          if (url.startsWith(idpOrigin)) {
            observed.inWindowIdpUrl = url;
            break;
          }
          await sleep(250);
        }
        observed.idpAuthorizeAgents = idp.requests
          .filter((r) => r.path === "/authorize")
          .map((r) => r.userAgent);
        observed.windowCount = electronApp.windows().length;

        if (observed.inWindowIdpUrl) {
          const status = window.locator("#webauthn-status");
          await status.waitFor({ state: "visible", timeout: 10_000 });
          const started = Date.now();
          while (Date.now() - started < PASSKEY_OBSERVATION_MS) {
            let state;
            try {
              state = await status.getAttribute("data-state");
            } catch {
              break; // the page moved on
            }
            if (state !== "pending") break;
            await sleep(1_000);
          }
          observed.webauthn = await window.evaluate(() => ({
            ...window.passkeyCeremony,
            observedMs: Date.now() - window.passkeyCeremony.startedAt,
            statusText: document.getElementById("webauthn-status")?.textContent ?? "",
          }));
          await window.screenshot({ path: path.join(RECORD_DIR, "in-window-idp-passkey.png") });
        }
        /* oxlint-enable no-await-in-loop */
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        observed.recordings = saveRecording(RECORD_DIR, "before-oidc-in-window-idp");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    }

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-oidc-"));
      idp = await startFakeIdp({ passkeyTimeoutMs: PASSKEY_TIMEOUT_MS });
      server = await spawnServer(tmpDir, {
        env: ({ serverUrl }) => oidcServerEnv(idp, serverUrl),
      });
      await driveConnectJourney();
      fs.writeFileSync(
        path.join(RECORD_DIR, "observed.json"),
        JSON.stringify(
          { serverUrl: server.serverUrl, idpIssuer: idp.issuer, ...observed },
          null,
          2,
        ),
      );
    });

    after(async () => {
      if (server) await server.close();
      if (idp) await idp.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("keeps the third-party IdP sign-in page out of the shell window", () => {
      assert.equal(
        observed.inWindowIdpUrl,
        null,
        `the Electron window itself navigated to the IdP: ${observed.inWindowIdpUrl}\n` +
          `window URLs after Connect: ${observed.windowUrls.join(" → ")}\n` +
          `IdP /authorize fetched by: ${observed.idpAuthorizeAgents.join(" | ") || "(nobody)"}`,
      );
    });

    it("does not leave an in-window passkey ceremony pending past its timeout", () => {
      if (!observed.inWindowIdpUrl) return;
      const w = observed.webauthn ?? {};
      assert.notEqual(
        w.state,
        "pending",
        `navigator.credentials.get({ timeout: ${PASSKEY_TIMEOUT_MS} }) still pending after ` +
          `${w.observedMs} ms with no prompt and no error ` +
          `(isUserVerifyingPlatformAuthenticatorAvailable=${w.uvpaa}; status: "${w.statusText}")`,
      );
    });
  },
);
