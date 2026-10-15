import { afterEach, describe, expect, it, vi } from "vitest";
import { startVisiblePoll } from "./visiblePoll";

class TestVisibilitySource extends EventTarget {
  visibilityState: DocumentVisibilityState = "visible";

  setVisibility(visibilityState: DocumentVisibilityState) {
    this.visibilityState = visibilityState;
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

afterEach(() => vi.useRealTimers());

describe("startVisiblePoll", () => {
  it("does not overlap a slow callback and waits the full interval after it finishes", async () => {
    vi.useFakeTimers();
    const source = new TestVisibilitySource();
    let finish: (() => void) | undefined;
    const callback = vi.fn(
      () =>
        new Promise<void>(resolve => {
          finish = resolve;
        })
    );
    const stop = startVisiblePoll(callback, {
      intervalMs: 10_000,
      initialDelayMs: 1_000,
      visibilitySource: source,
    });

    await vi.advanceTimersByTimeAsync(1_000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(callback).toHaveBeenCalledOnce();

    finish?.();
    await vi.advanceTimersByTimeAsync(9_999);
    expect(callback).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(callback).toHaveBeenCalledTimes(2);
    stop();
  });

  it("pauses while hidden and resumes immediately when visible", async () => {
    vi.useFakeTimers();
    const source = new TestVisibilitySource();
    const callback = vi.fn();
    const stop = startVisiblePoll(callback, {
      intervalMs: 10_000,
      initialDelayMs: 1_000,
      visibilitySource: source,
    });

    source.setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(callback).not.toHaveBeenCalled();
    source.setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(callback).toHaveBeenCalledOnce();
    stop();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(callback).toHaveBeenCalledOnce();
  });

  it("reports callback failures and continues polling without unhandled rejections", async () => {
    vi.useFakeTimers();
    const source = new TestVisibilitySource();
    const error = new Error("refresh failed");
    const callback = vi
      .fn()
      .mockRejectedValueOnce(error)
      .mockResolvedValue(undefined);
    const onError = vi.fn();
    const stop = startVisiblePoll(callback, {
      intervalMs: 1_000,
      onError,
      visibilitySource: source,
    });

    await vi.advanceTimersByTimeAsync(1_000);
    expect(onError).toHaveBeenCalledWith(error);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(callback).toHaveBeenCalledTimes(2);
    stop();
  });

  it.each([
    [
      "synchronously",
      () => {
        throw new Error("reporting failed");
      },
    ],
    [
      "asynchronously",
      async () => {
        throw new Error("reporting failed");
      },
    ],
  ])(
    "keeps polling if the error reporter fails %s",
    async (_mode, reportFailure) => {
      vi.useFakeTimers();
      const source = new TestVisibilitySource();
      const callback = vi
        .fn()
        .mockRejectedValueOnce(new Error("refresh failed"))
        .mockResolvedValue(undefined);
      const onError = vi.fn(reportFailure);
      const stop = startVisiblePoll(callback, {
        intervalMs: 1_000,
        onError,
        visibilitySource: source,
      });

      await vi.advanceTimersByTimeAsync(1_000);
      expect(onError).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(1_000);
      expect(callback).toHaveBeenCalledTimes(2);
      stop();
    }
  );

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    "rejects invalid interval %s",
    intervalMs => {
      expect(() => startVisiblePoll(() => undefined, { intervalMs })).toThrow(
        RangeError
      );
    }
  );

  it.each([-1, Number.NaN, Number.POSITIVE_INFINITY])(
    "rejects invalid initial delay %s",
    initialDelayMs => {
      expect(() =>
        startVisiblePoll(() => undefined, { intervalMs: 1_000, initialDelayMs })
      ).toThrow(RangeError);
    }
  );
});
