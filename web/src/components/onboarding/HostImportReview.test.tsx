import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { requestImportReview, resetImportReviewSessionForTests } from "@/lib/importReviewState";

const authenticatedFetchMock = vi.hoisted(() => vi.fn());
const features = vi.hoisted(() => ({ import_review: true }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features }) }));
vi.mock("@/lib/nativeBridge", () => ({ isIOSShell: () => false }));

import { ImportReviewGate, ReviewImportsPanel } from "./HostImportReview";

function host(id: string, overrides: Partial<Host> = {}): Host {
  return {
    host_id: id,
    name: `${id}-machine`,
    owner: "me",
    status: "online",
    configured_harnesses: { "claude-native": true },
    ...overrides,
  };
}

/** Serve hosts plus per-host skill names; MCP inventories are empty. */
function serve(
  hosts: Host[],
  skillsByHost: Record<string, string[]>,
  { absentSkillCalls = 0 }: { absentSkillCalls?: number } = {},
) {
  let absent = absentSkillCalls;
  authenticatedFetchMock.mockImplementation(async (url: string) => {
    const parsed = new URL(url, "http://test");
    if (parsed.pathname === "/v1/hosts") return Response.json({ hosts });
    if (parsed.pathname === "/v1/skills") {
      if (absent > 0) {
        absent -= 1;
        return Response.json({ detail: "host not connected" }, { status: 409 });
      }
      const names = skillsByHost[parsed.searchParams.get("host_id") ?? ""] ?? [];
      return Response.json({ skills: names.map((name) => ({ name, description: "" })) });
    }
    if (parsed.pathname.endsWith("/mcp-servers")) return Response.json({ mcp_servers: [] });
    throw new Error(`unexpected request ${url}`);
  });
}

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

function skillRequestsFor(hostId: string) {
  return authenticatedFetchMock.mock.calls.filter(([url]) =>
    String(url).startsWith(`/v1/skills?host_id=${hostId}&`),
  );
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
  window.localStorage.clear();
  resetImportReviewSessionForTests();
  features.import_review = true;
});

const TITLE = "Your imports are ready";

/** Waits long enough for another host's inventory to load and open the modal. */
async function expectStaysClosed() {
  await new Promise((resolve) => {
    setTimeout(resolve, 100);
  });
  expect(screen.queryByText(TITLE)).toBeNull();
}

const CLOSE_METHODS: [string, () => void][] = [
  ["the X button", () => fireEvent.click(screen.getByRole("button", { name: "Close" }))],
  ["Confirm", () => fireEvent.click(screen.getByRole("button", { name: "Confirm" }))],
  ["Escape", () => fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })],
  [
    "an overlay click",
    () => {
      const overlay = document.querySelector('[data-slot="dialog-overlay"]');
      if (!overlay) throw new Error("no dialog overlay");
      // Radix dismisses on the click that follows an outside pointerdown.
      fireEvent.pointerDown(overlay);
      fireEvent.click(overlay);
    },
  ],
];

afterEach(cleanup);

describe("ImportReviewGate", () => {
  it("stays hidden and loads nothing while the import_review feature is off", async () => {
    features.import_review = false;
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ImportReviewGate />);

    await expectStaysClosed();
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });

  it.each(CLOSE_METHODS)(
    "closes on %s and doesn't reopen for the next unreviewed host",
    async (_name, close) => {
      serve([host("a"), host("b")], { a: ["from-a"], b: ["from-b"] });
      renderWithClient(<ImportReviewGate />);

      expect(await screen.findByText("/from-a")).toBeTruthy();
      close();

      await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());
      await expectStaysClosed();
      expect(window.localStorage.getItem("omnigent:imports-reviewed:a")).not.toBeNull();
      expect(window.localStorage.getItem("omnigent:imports-reviewed:b")).toBeNull();
    },
  );

  it("opens at most once per page load, even across remounts", async () => {
    serve([host("a"), host("b")], { a: ["from-a"], b: ["from-b"] });
    renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("/from-a")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());

    cleanup();
    renderWithClient(<ImportReviewGate />);
    await expectStaysClosed();
    expect(skillRequestsFor("b")).toHaveLength(0);
  });

  it("keeps a dismissed host dismissed after a reload", async () => {
    serve([host("a"), host("b")], { a: ["from-a"], b: ["from-b"] });
    renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("/from-a")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());

    // A reload starts a new page session with the same localStorage.
    cleanup();
    resetImportReviewSessionForTests();
    renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("/from-b")).toBeTruthy();
    expect(screen.queryByText("/from-a")).toBeNull();
  });

  it("opens once for a new host and remembers the review", async () => {
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("Your imports are ready")).toBeTruthy();
    expect(screen.getByText("/review")).toBeTruthy();
    // A single host isn't named.
    expect(screen.queryByText(/on a-machine/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(screen.queryByText("Your imports are ready")).toBeNull());
    expect(window.localStorage.getItem("omnigent:imports-reviewed:a")).not.toBeNull();

    cleanup();
    renderWithClient(<ImportReviewGate />);
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("Your imports are ready")).toBeNull();
  });

  it("skips offline, reviewed, and empty hosts, and names the host among several", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:reviewed", "x");
    serve(
      [host("offline", { status: "offline" }), host("reviewed"), host("empty"), host("fresh")],
      { offline: ["a"], reviewed: ["b"], fresh: ["c"] },
    );
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(/Found in your harnesses on fresh-machine\./)).toBeTruthy();
    expect(screen.getByText("/c")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText("Your imports are ready")).toBeNull());
    expect(window.localStorage.getItem("omnigent:imports-reviewed:fresh")).not.toBeNull();
    expect(window.localStorage.getItem("omnigent:imports-reviewed:empty")).toBeNull();
  });
});

describe("ImportReviewGate with a requested host", () => {
  it("waits for an offline target without showing another host's imports", async () => {
    serve([host("target", { status: "offline" }), host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target", runner: "remote" });
    renderWithClient(<ImportReviewGate />);

    // A listed host is named rather than described by its runner.
    expect(await screen.findByText("Connecting to target-machine…")).toBeTruthy();
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("/o")).toBeNull();
    expect(screen.queryByText(/is offline/)).toBeNull();
    expect(skillRequestsFor("other")).toHaveLength(0);
  });

  it("shows the target's imports once it connects", async () => {
    // Not registered yet (Arca's tunnel lags its daemon), then online.
    serve([host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target", runner: "remote" });
    const { client } = renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("Connecting to Arca…")).toBeTruthy();

    serve([host("other"), host("target")], { target: ["t"], other: ["o"] });
    await client.invalidateQueries({ queryKey: ["hosts"] });

    expect(await screen.findByText("/t")).toBeTruthy();
    expect(screen.getByText(/Found in your harnesses on target-machine\./)).toBeTruthy();
    expect(screen.queryByText("/o")).toBeNull();
  });

  it("treats 409 as still connecting, not an error", async () => {
    serve([host("target")], { target: ["t"] }, { absentSkillCalls: 1 });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("Checking your harnesses…")).toBeTruthy();
    await waitFor(() => expect(skillRequestsFor("target")).toHaveLength(1));
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
    expect(await screen.findByText("/t", {}, { timeout: 5_000 })).toBeTruthy();
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
  });

  it.each([
    ["local", "Connecting this Mac…"],
    [undefined, "Connecting…"],
  ] as const)("describes an unlisted %s runner while it connects", async (runner, copy) => {
    serve([host("other")], { other: ["o"] });
    requestImportReview({ hostId: "target", runner });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(copy)).toBeTruthy();
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("/o")).toBeNull();
  });

  it("names a single targeted host", async () => {
    serve([host("target")], { target: ["t"] });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(/Found in your harnesses on target-machine\./)).toBeTruthy();
  });

  it("shows a reviewed target and clears the request on dismiss", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:target", "x");
    serve([host("target"), host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("/t")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    // Closing ends this load's auto-open, so another unreviewed host waits.
    await waitFor(() => expect(screen.queryByText(TITLE)).toBeNull());
    await expectStaysClosed();
    expect(skillRequestsFor("other")).toHaveLength(0);
  });
});

describe("ReviewImportsPanel", () => {
  it("reopens the modal for a reviewed host", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:a", "x");
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ReviewImportsPanel />);

    fireEvent.click(await screen.findByRole("button", { name: "Review imports on a-machine" }));
    expect(await screen.findByText("/review")).toBeTruthy();
  });

  it("explains when no machine is online", async () => {
    serve([host("a", { status: "offline" })], {});
    renderWithClient(<ReviewImportsPanel />);
    expect(await screen.findByText(/None of your machines are online/)).toBeTruthy();
  });
});
