import { useEffect, useRef, useState } from "react";
import type { ConnectResult } from "./ServerSelectorV2";

type Phase = "connecting" | "authenticating";
export interface ConnectionProgress {
  phase: Phase | "cancelling";
  url: string;
  error?: string;
  onCancel?: () => void;
}

export interface ConnectionBridge {
  setServerUrl: (url: string, options?: { requestId: string }) => Promise<unknown>;
  cancelServerConnection?: (requestId: string) => Promise<boolean>;
  onConnectionProgress?: (
    callback: (progress: { requestId: string; phase: Phase }) => void,
  ) => () => void;
}

export function useServerConnection(bridge: ConnectionBridge | undefined) {
  const [progress, setProgress] = useState<ConnectionProgress | null>(null);
  const active = useRef<{ requestId: string; phase: Phase; cancelling: boolean } | null>(null);

  useEffect(() => {
    const unsubscribe = bridge?.onConnectionProgress?.((update) => {
      const attempt = active.current;
      if (!attempt || attempt.requestId !== update.requestId || attempt.cancelling) return;
      if (update.phase !== "connecting" && update.phase !== "authenticating") return;
      attempt.phase = update.phase;
      setProgress((prev) => prev && { ...prev, phase: update.phase });
    });
    return () => {
      active.current = null;
      unsubscribe?.();
    };
  }, [bridge]);

  async function connect(url: string): Promise<ConnectResult> {
    if (!bridge) return { error: "The desktop shell is unavailable." };
    if (active.current) return { cancelled: true };
    const attempt = {
      requestId: crypto.randomUUID(),
      phase: "connecting" as Phase,
      cancelling: false,
    };
    active.current = attempt;

    const cancel = async () => {
      if (active.current !== attempt || attempt.cancelling || !bridge.cancelServerConnection)
        return;
      attempt.cancelling = true;
      setProgress((prev) => prev && { ...prev, phase: "cancelling", error: undefined });
      try {
        const cancelled = await bridge.cancelServerConnection(attempt.requestId);
        if (active.current !== attempt) return;
        if (cancelled) {
          active.current = null;
          setProgress(null);
          return;
        }
        throw new Error("The connection is still finishing. Please try again.");
      } catch (error) {
        if (active.current !== attempt) return;
        attempt.cancelling = false;
        setProgress(
          (prev) =>
            prev && {
              ...prev,
              phase: attempt.phase,
              error: error instanceof Error ? error.message : "Could not cancel the connection.",
            },
        );
      }
    };

    setProgress({
      url,
      phase: "connecting",
      onCancel: bridge.cancelServerConnection ? cancel : undefined,
    });
    try {
      const result = await bridge.setServerUrl(url, { requestId: attempt.requestId });
      if (active.current !== attempt) return { cancelled: true };
      if (result && typeof result === "object" && "cancelled" in result && result.cancelled)
        return { cancelled: true };
      return {};
    } catch (error) {
      if (active.current !== attempt) return { cancelled: true };
      return {
        error: error instanceof Error ? error.message : "Could not connect to that server.",
      };
    } finally {
      if (active.current === attempt) {
        active.current = null;
        setProgress(null);
      }
    }
  }

  return { connect, progress };
}
