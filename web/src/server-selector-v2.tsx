// Standalone entry for the gated server selector v2 (Electron shell).
//
// Loaded from a file:// window when OMNIGENT_SERVER_SELECTOR_V2=1 (see
// electron/src/main.js `setupPagePath`). Renders the ServerSelectorV2, wiring it
// to the shell's `omnigentSetup` preload bridge (server URL, recent / managed
// servers, connect, start-local). Theme follows the OS via index.css.

import { type CSSProperties, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AnimatedOmnigentPanel } from "./components/onboarding/AnimatedOmnigentPanel";
import { Button } from "./components/ui/button";
import { Spinner } from "./components/ui/spinner";
import { useServerConnection, type ConnectionBridge } from "./pages/onboarding/useServerConnection";
import type { Runner } from "./pages/onboarding/RunnerStep";
import { ServerSelectorV2, type ServerSelectorV2Setup } from "./pages/onboarding/ServerSelectorV2";
import { maybeMockSetup } from "./pages/onboarding/mockSetup";
import "./index.css";

const DEFAULT_URL = "http://localhost:6767";
const CLOUD_DOCS_URL = "https://omnigent.ai/docs/deploy/overview";

/** The `omnigentSetup` preload bridge (see electron/src/preload.js). */
export interface OmnigentSetup extends ConnectionBridge {
  getServerUrl: () => Promise<string | null>;
  getManagedServers: () => Promise<string[]>;
  getManagedServerNames?: () => Promise<Record<string, string>>;
  getRecentServers: () => Promise<string[]>;
  forgetRecentServer?: (url: string) => Promise<string[]>;
  getRunnerOptions?: (url: string) => Promise<{ remote?: boolean; bundledCli?: boolean } | null>;
  connectRunner?: (url: string, runner: Runner) => Promise<{ ok?: boolean; error?: string }>;
  onRunnerConnectLog?: (cb: (line: string) => void) => () => void;
  checkServer?: (url: string) => Promise<{ status: "ok" | "reachable" | "unreachable" }>;
  copyText: (text: string) => Promise<unknown>;
  setServerSelectorV2?: (enabled: boolean) => Promise<unknown>;
  getSetupCapabilities?: () => Promise<{ v2Forced?: boolean; connectedBefore?: boolean }>;
  setColorScheme?: (scheme: "light" | "dark" | "system") => void;
  getColorScheme?: () => Promise<{ source: string; effective: "light" | "dark" } | null>;
  onColorScheme?: (cb: (theme: "light" | "dark") => void) => () => void;
  getCliStatus: () => Promise<{
    installed?: boolean;
    installSupported?: boolean;
    localServerRunning?: boolean;
  }>;
  startLocalServer: () => Promise<{ ok?: boolean; url?: string; error?: string }>;
  onLocalServerSetupLog?: (cb: (line: string) => void) => () => void;
  installCli?: () => Promise<{ ok?: boolean; error?: string; installed?: boolean }>;
  onCliInstallLog?: (cb: (line: string) => void) => () => void;
}

function setupBridge(): OmnigentSetup | undefined {
  return (window as unknown as { omnigentSetup?: OmnigentSetup }).omnigentSetup;
}

// Mock-or-real router: with `?mock=1` render the URL-param mock (dev only, see
// mockSetup.ts), otherwise the real bridge-wired flow. Split so each branch's
// hooks run unconditionally (rules of hooks).
function SetupApp() {
  const mock = maybeMockSetup(new URLSearchParams(window.location.search));
  if (mock) {
    return <ServerSelectorV2 setup={mock} />;
  }
  return <BridgeSetupApp />;
}

export function BridgeSetupApp() {
  const bridge = setupBridge();
  const { connect, progress } = useServerConnection(bridge);
  const params = new URLSearchParams(window.location.search);
  const failedUrl = params.get("url");
  const error = params.get("error") ?? undefined;
  const isEphemeral = params.get("ephemeral") === "1";
  // "Connect to new server…" opens straight on the server list (?step=server),
  // skipping the first-run landing/mode intro.
  const initialStep = params.get("step") === "server" ? ("server" as const) : undefined;

  // Prefill: the URL that just failed (retry is the common next step), else the
  // saved server, else the default — except in ephemeral mode, where the whole
  // point is connecting to a *different* server than the saved one.
  const [initialUrl, setInitialUrl] = useState(failedUrl ?? DEFAULT_URL);
  const [recentServers, setRecentServers] = useState<string[]>([]);
  const [managedServers, setManagedServers] = useState<string[]>([]);
  const [managedServerNames, setManagedServerNames] = useState<Record<string, string>>({});
  // Whether the `omnigent` CLI is installed — decides "Install" vs "Start"/"Open".
  // Undefined until the probe resolves.
  const [installed, setInstalled] = useState<boolean | undefined>(undefined);
  // Whether the local server is already up → local actions "Open" vs "Start".
  const [localServerRunning, setLocalServerRunning] = useState(false);
  // Has ever connected (returning user) — picks the welcome vs server-list start.
  const [connectedBefore, setConnectedBefore] = useState(false);
  // Whether in-app install is available on this platform (macOS only). Off →
  // connect/local must never route through an install step.
  const [installSupported, setInstallSupported] = useState(false);
  // Whether the env var pins the selector on → "Switch to legacy" can't take
  // effect, so the menu item is disabled.
  const [v2Forced, setV2Forced] = useState(false);
  // The shell's current color-scheme source. themeSource is process-global and
  // survives navigation, so a returning-to-setup wizard must seed the radio
  // from it (not assume "system"). Undefined → shell didn't report → "system".
  const [colorScheme, setColorScheme] = useState<"system" | "light" | "dark">("system");
  // Hold the initial paint until the CLI probe resolves, so the wizard opens on
  // the correct step (welcome vs server list) instead of flashing the wrong one.
  const [ready, setReady] = useState(false);

  const [initializationAttempt, setInitializationAttempt] = useState(0);
  const [slowInitialization, setSlowInitialization] = useState(false);

  useEffect(() => {
    if (!bridge) {
      setReady(true);
      return;
    }
    let cancelled = false;
    setReady(false);
    setSlowInitialization(false);
    const timer = setTimeout(() => setSlowInitialization(true), 15_000);
    void bridge.getManagedServerNames?.().then(
      (names) => {
        if (!cancelled) setManagedServerNames(names ?? {});
      },
      () => {},
    );
    // Decide the first step from one snapshot; late results from a retry are ignored.
    void Promise.allSettled([
      !failedUrl && !isEphemeral ? bridge.getServerUrl() : Promise.resolve(null),
      bridge.getRecentServers(),
      bridge.getManagedServers(),
      bridge.getCliStatus(),
      bridge.getSetupCapabilities?.(),
      bridge.getColorScheme?.(),
    ]).then(([saved, recents, managed, cli, caps, theme]) => {
      if (cancelled) return;
      clearTimeout(timer);
      if (saved.status === "fulfilled" && saved.value) setInitialUrl(saved.value);
      if (recents.status === "fulfilled") setRecentServers(recents.value);
      if (managed.status === "fulfilled") setManagedServers(managed.value);
      if (cli.status === "fulfilled") {
        setInstalled(cli.value?.installed === true);
        setInstallSupported(cli.value?.installSupported === true);
        setLocalServerRunning(cli.value?.localServerRunning === true);
      }
      if (caps.status === "fulfilled") {
        setV2Forced(caps.value?.v2Forced === true);
        setConnectedBefore(caps.value?.connectedBefore === true);
      }
      if (theme.status === "fulfilled" && theme.value) {
        const { source, effective } = theme.value;
        setColorScheme(source === "light" || source === "dark" ? source : "system");
        document.documentElement.classList.toggle("dark", effective === "dark");
      }
      setReady(true);
    });
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [bridge, failedUrl, isEphemeral, initializationAttempt]);

  // Sync the wizard's `.dark` class with the shell's effective theme: the dark
  // styles key off the class (index.css), not the OS media query. The shell
  // pushes on scheme change and on OS changes (so "System" tracks live).
  useEffect(() => {
    return setupBridge()?.onColorScheme?.((theme) =>
      document.documentElement.classList.toggle("dark", theme === "dark"),
    );
  }, []);

  const setup: ServerSelectorV2Setup = {
    initialUrl,
    initialStep,
    error,
    recentServers,
    managedServers,
    managedServerNames,
    installed,
    connectedBefore,
    localServerRunning,
    connection: progress,
    onConnect: connect,
    onStartLocal: async () => {
      // Start (or reuse) the local server, then navigate to it. Resolves the
      // outcome so the terminal step can show ready/failed. On success the
      // setServerUrl navigation replaces this page, so this never resolves in
      // the happy path — the terminal stays on "Ready" until the window swaps.
      if (!bridge) return { ok: false, error: "The desktop shell is unavailable." };
      try {
        const result = await bridge.startLocalServer();
        if (result?.ok && result.url) {
          const connected = await connect(result.url);
          return {
            ok: !connected.cancelled && !connected.error,
            error: connected.cancelled ? "Connection cancelled." : connected.error,
          };
        }
        return { ok: false, error: result?.error ?? "Could not start the local server." };
      } catch (e) {
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
    // Live startup-log stream from the shell, if this shell exposes it (older
    // shells / browser preview omit it → the terminal step shows phases only).
    onSetupLog: setupBridge()?.onLocalServerSetupLog
      ? (cb) => setupBridge()?.onLocalServerSetupLog?.(cb) ?? (() => {})
      : undefined,
    // Install the CLI, if the shell supports it. Offered only when NOT already
    // installed — a returning user opens rather than installs.
    onInstallCli:
      installed === false && installSupported && setupBridge()?.installCli
        ? async () => {
            if (!bridge?.installCli) return { ok: false, error: "Install is unavailable." };
            try {
              const result = await bridge.installCli();
              setInstalled(result?.installed === true);
              // The installer can exit 0 yet leave the binary unresolvable (PATH
              // issue). Treat "installed:false after a successful run" as a
              // failure rather than proceeding to start/connect with no CLI.
              if (result?.ok && result.installed !== true) {
                return {
                  ok: false,
                  error:
                    "The installer finished but the omnigent CLI still isn't detected. " +
                    "Try opening a new terminal, or install it from https://omnigent.ai/.",
                };
              }
              return { ok: result?.ok === true, error: result?.error };
            } catch (e) {
              return { ok: false, error: e instanceof Error ? e.message : String(e) };
            }
          }
        : undefined,
    onInstallLog: setupBridge()?.onCliInstallLog
      ? (cb) => setupBridge()?.onCliInstallLog?.(cb) ?? (() => {})
      : undefined,
    // Older shells omit it → the runner step offers this laptop only.
    getRunnerOptions: setupBridge()?.getRunnerOptions
      ? async (url) => {
          const options = await setupBridge()?.getRunnerOptions?.(url);
          return { remote: options?.remote === true, bundledCli: options?.bundledCli === true };
        }
      : undefined,
    onConnectRunner: setupBridge()?.connectRunner
      ? async (url, runner) => {
          try {
            const result = await setupBridge()?.connectRunner?.(url, runner);
            return { ok: result?.ok === true, error: result?.error };
          } catch (e) {
            return { ok: false, error: e instanceof Error ? e.message : String(e) };
          }
        }
      : undefined,
    onRunnerLog: setupBridge()?.onRunnerConnectLog
      ? (cb) => setupBridge()?.onRunnerConnectLog?.(cb) ?? (() => {})
      : undefined,
    // Only offered when the shell exposes the forget method (newer shells).
    onRemoveServer: setupBridge()?.forgetRecentServer
      ? (url) => {
          // Optimistic: drop it locally, then reconcile with the shell's result.
          setRecentServers((prev) => prev.filter((u) => u !== url));
          setupBridge()
            ?.forgetRecentServer?.(url)
            .then(setRecentServers)
            .catch(() => {});
        }
      : undefined,
    // Advisory reachability probe for a just-added server. Resolves a status;
    // never blocks Join. Absent bridge (browser preview) → treat as unreachable.
    onCheckServer: async (url) => {
      if (!bridge?.checkServer) return { status: "unreachable" as const };
      try {
        return await bridge.checkServer(url);
      } catch {
        return { status: "unreachable" as const };
      }
    },
    // Copy via the shell's native clipboard bridge — navigator.clipboard is
    // denied on the file:// wizard page.
    onCopy: (text) => {
      setupBridge()
        ?.copyText(text)
        .catch(() => {});
    },
    // Cloud deploy docs open in the real browser: window.open on this file://
    // page is routed out by the shell's popup policy, not opened in-window.
    onCloudSetup: () => window.open(CLOUD_DOCS_URL, "_blank", "noopener"),
    // Revert to the classic setup page; the shell persists it and reloads.
    onSwitchToLegacy: () => {
      setupBridge()
        ?.setServerSelectorV2?.(false)
        ?.catch(() => {});
    },
    switchToLegacyDisabled: v2Forced,
    // Live color-scheme override; only offered when the shell exposes it.
    onSetColorScheme: setupBridge()?.setColorScheme
      ? (scheme) => setupBridge()?.setColorScheme?.(scheme)
      : undefined,
    initialColorScheme: colorScheme,
  };

  return (
    <>
      {/* Window drag surface: with the native title bar hidden (titleBarStyle
          "hiddenInset" / frame:false, see electron/src/main.js) this strip is
          the only place the user can grab to move the window. Matches the
          static setup page's 36px .drag-strip. */}
      <div
        style={
          {
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            height: 36,
            WebkitAppRegion: "drag",
          } as CSSProperties
        }
      />
      {ready ? (
        <ServerSelectorV2 setup={setup} />
      ) : (
        <div
          className="grid min-h-screen place-items-center p-6"
          style={{ background: "var(--onboarding-wizard-background)" }}
        >
          <AnimatedOmnigentPanel height={560} panelHeight={308}>
            <div
              className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center"
              aria-busy="true"
            >
              <Spinner className="size-6 motion-reduce:animate-none" aria-hidden="true" />
              <p role="status" className="text-lg">
                Loading your setup…
              </p>
              {slowInitialization && (
                <>
                  <p className="text-sm text-muted-foreground">
                    Setup is taking longer than expected. You can retry.
                  </p>
                  <Button variant="outline" onClick={() => setInitializationAttempt((n) => n + 1)}>
                    Retry loading setup
                  </Button>
                </>
              )}
            </div>
          </AnimatedOmnigentPanel>
        </div>
      )}
    </>
  );
}

const container = document.getElementById("server-selector-v2-root");
if (container) createRoot(container).render(<SetupApp />);
