import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BridgeSetupApp, type OmnigentSetup } from "./server-selector-v2";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup(overrides: Partial<OmnigentSetup> = {}) {
  const bridge: OmnigentSetup = {
    getServerUrl: async () => "https://team.example.com/",
    getManagedServers: async () => ["https://team.example.com/"],
    getManagedServerNames: async () => ({ "https://team.example.com/": "Engineering" }),
    getRecentServers: async () => [],
    getSetupCapabilities: async () => ({ connectedBefore: true }),
    getCliStatus: async () => ({ installed: true }),
    setServerUrl: vi.fn(),
    copyText: vi.fn(),
    startLocalServer: async () => ({ ok: true, url: "http://localhost:6767/" }),
    ...overrides,
  };
  Object.assign(window, { omnigentSetup: bridge });
  render(<BridgeSetupApp />);
  return bridge;
}

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, "omnigentSetup");
  vi.useRealTimers();
});

describe("native setup loading", () => {
  it("shows feedback until all routing data arrives, then opens the correct step", async () => {
    const recents = deferred<string[]>();
    setup({ getManagedServers: async () => [], getRecentServers: () => recents.promise });
    expect(screen.getByRole("status")).toHaveTextContent("Loading your setup…");
    expect(screen.queryByText("Meet Omnigent")).not.toBeInTheDocument();
    await act(async () => {
      recents.resolve(["https://old.example.com/"]);
    });
    expect(screen.getByText("Recents")).toBeInTheDocument();
    expect(screen.queryByText("Loading your setup…")).not.toBeInTheDocument();
  });

  it("allows retry of a stalled bootstrap and ignores the old snapshot", async () => {
    vi.useFakeTimers();
    const old = deferred<string[]>();
    const getRecentServers = vi
      .fn()
      .mockReturnValueOnce(old.promise)
      .mockResolvedValue(["https://new.example.com/"]);
    setup({ getRecentServers, getManagedServers: async () => [] });
    await act(async () => {
      vi.advanceTimersByTime(15_000);
    });
    fireEvent.click(screen.getByRole("button", { name: "Retry loading setup" }));
    await act(async () => {});
    expect(screen.getByText("new.example.com")).toBeInTheDocument();
    await act(async () => {
      old.resolve(["https://old.example.com/"]);
    });
    expect(screen.queryByText("old.example.com")).not.toBeInTheDocument();
  });

  it("shows authentication, cancels, retries, and ignores an old attempt's late failure", async () => {
    const old = deferred<unknown>();
    const next = deferred<unknown>();
    let progress!: Parameters<NonNullable<OmnigentSetup["onConnectionProgress"]>>[0];
    const unsubscribe = vi.fn();
    const setServerUrl = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
    const cancelServerConnection = vi.fn().mockResolvedValue(true);
    setup({
      setServerUrl,
      cancelServerConnection,
      onConnectionProgress: (cb) => {
        progress = cb;
        return unsubscribe;
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Join your team/ }));
    const firstId = setServerUrl.mock.calls[0][1].requestId;
    expect(screen.getByRole("status")).toHaveTextContent("Connecting to your server…");
    expect(screen.getByRole("status")).toHaveTextContent("Engineering");
    expect(screen.queryByRole("button", { name: /Join your team/ })).not.toBeInTheDocument();
    act(() => progress({ requestId: firstId, phase: "authenticating" }));
    expect(screen.getByRole("status")).toHaveTextContent("Signing in…");
    fireEvent.click(screen.getByRole("button", { name: "Cancel sign-in" }));
    fireEvent.click(await screen.findByRole("button", { name: /Join your team/ }));
    const secondId = setServerUrl.mock.calls[1][1].requestId;
    expect(secondId).not.toBe(firstId);
    expect(cancelServerConnection).toHaveBeenCalledWith(firstId);
    await act(async () => {
      progress({ requestId: secondId, phase: "authenticating" });
      progress({ requestId: firstId, phase: "connecting" });
      old.reject(new Error("old failure"));
    });
    expect(screen.getByRole("status")).toHaveTextContent("Signing in…");
    expect(screen.queryByText("old failure")).not.toBeInTheDocument();
    await act(async () => {
      next.reject(new Error("Please retry"));
    });
    expect(screen.getByRole("alert")).toHaveTextContent("Please retry");
    cleanup();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it("keeps the connection busy if cancellation fails", async () => {
    const pending = deferred<unknown>();
    setup({
      setServerUrl: () => pending.promise,
      cancelServerConnection: async () => {
        throw new Error("Try cancelling again");
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Join your team/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel connection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try cancelling again");
    expect(screen.getByRole("button", { name: "Cancel connection" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Join your team/ })).not.toBeInTheDocument();
  });

  it("also covers Open from recents on an older shell without cancellation", async () => {
    setup({
      getManagedServers: async () => [],
      getRecentServers: async () => ["https://recent.example.com/"],
      setServerUrl: () => new Promise(() => {}),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Open Omnigent" }));
    expect(screen.getByRole("status")).toHaveTextContent("recent.example.com");
    expect(screen.queryByRole("button", { name: /Cancel/ })).not.toBeInTheDocument();
  });

  it("cancelling a terminal's final navigation does not restart the runner", async () => {
    const pending = deferred<unknown>();
    const connectRunner = vi.fn().mockResolvedValue({ ok: true });
    setup({
      getSetupCapabilities: async () => ({ connectedBefore: false }),
      getRunnerOptions: async () => ({ remote: false }),
      connectRunner,
      setServerUrl: () => pending.promise,
      cancelServerConnection: async () => {
        pending.resolve({ cancelled: true });
        return true;
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: /Join your team/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Omnigent" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel connection" }));
    await waitFor(() => expect(screen.getByText(/Connection cancelled/)).toBeVisible());
    expect(connectRunner).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });
  it.each(["alternate preset", "recent", "typed URL"])(
    "shows a cancellable connection from the MDM dropdown's %s",
    async (choice) => {
      const pending = deferred<unknown>();
      const setServerUrl = vi.fn(() => pending.promise);
      setup({
        getManagedServers: async () => [
          "https://team.example.com/",
          "https://alternate.example.com/",
        ],
        getRecentServers: async () => ["https://recent.example.com/"],
        setServerUrl,
        cancelServerConnection: async () => true,
      });
      fireEvent.pointerDown(await screen.findByRole("button", { name: "Choose team URL" }), {
        button: 0,
      });
      if (choice === "typed URL") {
        fireEvent.change(screen.getByRole("textbox", { name: "Server URL" }), {
          target: { value: "https://typed.example.com/" },
        });
        fireEvent.keyDown(screen.getByRole("textbox", { name: "Server URL" }), { key: "Enter" });
      } else {
        fireEvent.click(
          screen.getByRole("menuitem", {
            name: choice === "recent" ? "recent.example.com" : "alternate.example.com",
          }),
        );
      }
      expect(await screen.findByRole("button", { name: "Cancel connection" })).toBeVisible();
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
      expect(setServerUrl).toHaveBeenCalledOnce();
    },
  );
});
