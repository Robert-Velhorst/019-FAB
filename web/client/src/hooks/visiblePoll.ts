type VisibilitySource = Pick<
  Document,
  "visibilityState" | "addEventListener" | "removeEventListener"
>;

export function startVisiblePoll(
  callback: () => Promise<void> | void,
  options: {
    intervalMs: number;
    initialDelayMs?: number;
    onError?: (error: unknown) => void | Promise<void>;
    visibilitySource?: VisibilitySource | null;
  }
): () => void {
  const initialDelayMs = options.initialDelayMs ?? options.intervalMs;
  if (!Number.isFinite(options.intervalMs) || options.intervalMs <= 0) {
    throw new RangeError(
      "intervalMs must be a finite number greater than zero"
    );
  }
  if (!Number.isFinite(initialDelayMs) || initialDelayMs < 0) {
    throw new RangeError(
      "initialDelayMs must be a finite, non-negative number"
    );
  }

  const source =
    options.visibilitySource === undefined
      ? typeof document === "undefined"
        ? null
        : document
      : options.visibilitySource;
  let disposed = false;
  let inFlight = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const isVisible = () => !source || source.visibilityState === "visible";
  const schedule = (delayMs: number) => {
    if (!disposed && isVisible()) {
      timer = setTimeout(() => void poll(), delayMs);
    }
  };
  const poll = async () => {
    timer = undefined;
    if (disposed || !isVisible()) return;
    if (inFlight) return;
    inFlight = true;
    try {
      await callback();
    } catch (error) {
      if (!disposed) {
        try {
          const report = options.onError?.(error);
          if (report) void Promise.resolve(report).catch(() => {});
        } catch {
          // A reporting callback must not turn a handled poll failure into an unhandled rejection.
        }
      }
    } finally {
      inFlight = false;
      schedule(options.intervalMs);
    }
  };
  const onVisibilityChange = () => {
    if (timer) clearTimeout(timer);
    timer = undefined;
    if (isVisible()) schedule(0);
  };

  source?.addEventListener("visibilitychange", onVisibilityChange);
  schedule(initialDelayMs);

  return () => {
    disposed = true;
    if (timer) clearTimeout(timer);
    source?.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
