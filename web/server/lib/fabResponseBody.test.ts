import { describe, expect, it, vi } from "vitest";
import { FabResponseTooLargeError, readBoundedFabResponseBody } from "./fabResponseBody";

describe("bounded FAB response reader", () => {
  it("rejects an advertised oversized body without reading it", async () => {
    const cancel = vi.fn();
    const pull = vi.fn();
    const body = new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 });
    await expect(readBoundedFabResponseBody(new Response(body, {
      headers: { "content-length": "5" },
    }), 4)).rejects.toBeInstanceOf(FabResponseTooLargeError);
    expect(pull).not.toHaveBeenCalled();
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("bounds a chunked body even with a false smaller length", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2]));
        controller.enqueue(new Uint8Array([3, 4, 5]));
      },
      cancel,
    });
    await expect(readBoundedFabResponseBody(new Response(body, {
      headers: { "content-length": "1" },
    }), 4)).rejects.toBeInstanceOf(FabResponseTooLargeError);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("preserves bytes across growth, empty chunks, and an exact boundary", async () => {
    const expected = Buffer.alloc(20_000, 42);
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array());
        for (let offset = 0; offset < expected.length; offset += 100) {
          controller.enqueue(expected.subarray(offset, offset + 100));
        }
        controller.close();
      },
    });
    expect(await readBoundedFabResponseBody(new Response(body), expected.length)).toEqual(expected);
  });

  it("propagates read failures and releases the stream lock", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) { controller.error(new DOMException("aborted", "AbortError")); },
    });
    await expect(readBoundedFabResponseBody(new Response(body), 4)).rejects.toMatchObject({ name: "AbortError" });
    expect(body.locked).toBe(false);
  });

  it.each([0, -1, NaN, Infinity, 1.5])("rejects invalid size limit %s", async (limit) => {
    await expect(readBoundedFabResponseBody(new Response("{}"), limit)).rejects.toThrow("Invalid FAB response size limit");
  });
});
