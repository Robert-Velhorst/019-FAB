import { describe, expect, it, vi } from "vitest";
import { appendIntakeItems } from "./fabIntakeQueue";

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
