import { describe, expect, it, vi } from "vitest";
import { appendIntakeItems, completeIntakeItem, finishIntakeBatch, tryBeginIntakeBatch } from "./fabIntakeQueue";

describe("completeIntakeItem", () => {
  it("releases the source File reference but keeps safe display metadata and result", () => {
    const file = { name: "receipt.pdf", size: 2048 };
    const result = { documentId: "doc-1" };
    const completed = completeIntakeItem({
      id: "item-1",
      file,
      fileName: file.name,
      fileSize: file.size,
      status: "uploading" as const,
      error: "old error",
    }, result);

    expect(completed).toMatchObject({
      file: null,
      fileName: "receipt.pdf",
      fileSize: 2048,
      status: "complete",
      result,
    });
    expect(completed.error).toBeUndefined();
  });
});

describe("tryBeginIntakeBatch", () => {
  it("reserves synchronously so rapid activation cannot start a duplicate upload loop", () => {
    const busyRef = { current: false };

    expect(tryBeginIntakeBatch(busyRef, true, false, 1)).toBe(true);
    expect(tryBeginIntakeBatch(busyRef, true, false, 1)).toBe(false);
    expect(busyRef.current).toBe(true);
  });

  it.each([
    [false, false, false, 1],
    [true, true, false, 1],
    [true, false, true, 1],
    [true, false, false, 0],
  ])("does not reserve when the batch cannot start", (connected, busy, alreadyReserved, queuedCount) => {
    const busyRef = { current: alreadyReserved };

    expect(tryBeginIntakeBatch(busyRef, connected, busy, queuedCount)).toBe(false);
    expect(busyRef.current).toBe(alreadyReserved);
  });
});

describe("finishIntakeBatch", () => {
  it("reports a failed refresh without rejecting after successful uploads", async () => {
    const onFinished = vi.fn().mockRejectedValue(new Error("refresh failed"));
    const onFailure = vi.fn();

    await expect(finishIntakeBatch(2, onFinished, onFailure)).resolves.toBeUndefined();

    expect(onFinished).toHaveBeenCalledWith(2);
    expect(onFailure).toHaveBeenCalledOnce();
  });

  it("does not start completion when no upload succeeded", async () => {
    const onFinished = vi.fn();
    const onFailure = vi.fn();

    await finishIntakeBatch(0, onFinished, onFailure);

    expect(onFinished).not.toHaveBeenCalled();
    expect(onFailure).not.toHaveBeenCalled();
  });
});

describe("appendIntakeItems", () => {
  it("preserves separate files even when their browser metadata matches", () => {
    const first = { name: "receipt.pdf", size: 200, lastModified: 10 };
    const second = { name: "receipt.pdf", size: 200, lastModified: 10 };
    const result = appendIntakeItems([first], [second], (file) => ({ file }), 10);

    expect(result.items).toEqual([first, { file: second }]);
    expect(result.rejectedCount).toBe(0);
  });

  it("bounds retained items and reports how many additions were rejected", () => {
    const result = appendIntakeItems(["kept"], ["one", "two", "three"], (item) => item, 3);

    expect(result.items).toEqual(["kept", "one", "two"]);
    expect(result.rejectedCount).toBe(1);
  });

  it("accepts no additional files when already at the limit", () => {
    const result = appendIntakeItems(["one", "two"], ["three"], (item) => item, 2);

    expect(result.items).toEqual(["one", "two"]);
    expect(result.rejectedCount).toBe(1);
  });

  it("does not construct queue items for rejected entries", () => {
    const createItem = (item: string) => item.toUpperCase();
    const createItemSpy = vi.fn(createItem);
    const result = appendIntakeItems(["kept"], ["one", "two", "three"], createItemSpy, 2);

    expect(result.items).toEqual(["kept", "ONE"]);
    expect(result.rejectedCount).toBe(2);
    expect(createItemSpy).toHaveBeenCalledTimes(1);
  });

  it("uses the 100-file production default", () => {
    const input = Array.from({ length: 125 }, (_, index) => index);
    const result = appendIntakeItems([], input, (item) => item);

    expect(result.items).toHaveLength(100);
    expect(result.rejectedCount).toBe(25);
  });
});
