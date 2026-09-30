import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { ConnectionProgress } from "./useServerConnection";

export function ConnectionStatus({
  progress,
  name,
}: {
  progress: ConnectionProgress;
  name?: string;
}) {
  const signingIn = progress.phase === "authenticating";
  const cancelling = progress.phase === "cancelling";
  return (
    <div
      className="flex flex-1 flex-col items-center justify-center gap-4 px-4 py-6 text-center"
      aria-busy="true"
    >
      <Spinner className="size-6 motion-reduce:animate-none" aria-hidden="true" />
      <div role="status" aria-live="polite" className="space-y-2">
        <h1 className="text-xl font-medium">
          {cancelling ? "Cancelling…" : signingIn ? "Signing in…" : "Connecting to your server…"}
        </h1>
        <p className="break-all text-sm text-muted-foreground">
          {name ?? new URL(progress.url).host}
        </p>
        {signingIn && (
          <p className="text-sm text-muted-foreground">
            If a browser sign-in window opened, finish signing in there.
          </p>
        )}
      </div>
      {progress.error && (
        <p role="alert" className="text-sm text-destructive">
          {progress.error}
        </p>
      )}
      {progress.onCancel && (
        <Button autoFocus variant="outline" disabled={cancelling} onClick={progress.onCancel}>
          {signingIn ? "Cancel sign-in" : "Cancel connection"}
        </Button>
      )}
    </div>
  );
}
