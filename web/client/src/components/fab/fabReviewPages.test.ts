import { describe, expect, it, vi } from "vitest";
import { refreshFabReviewPages } from "./fabReviewPages";

describe("refreshFabReviewPages", () => {
  it("limits refresh concurrency and preserves page order despite completion order", async () => {
    const pending = new Map<number, () => void>();
    const fetchPage = vi.fn((offset: number) => new Promise<{ workItems: unknown[] }>(resolve => {
      pending.set(offset, () => resolve({ workItems: [{ id: `doc-${offset}` }] }));
    }));
    const result = refreshFabReviewPages({}, [1, 2, 3, 4, 5, 6], 1, fetchPage);
    expect(fetchPage).toHaveBeenCalledTimes(4);
    pending.get(4)!();
    pending.get(2)!();
    await vi.waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(6));
    for (const offset of [6, 5, 3, 1]) pending.get(offset)!();
    expect((await result).workItems.map(item => item.id)).toEqual([1, 2, 3, 4, 5, 6].map(id => `doc-${id}`));
  });

  it("does not dispatch queued pages after the refresh is superseded", async () => {
    let current = true;
    const pending: Array<() => void> = [];
    const fetchPage = vi.fn(() => new Promise<{}>(resolve => pending.push(() => resolve({}))));
    const result = refreshFabReviewPages({}, [1, 2, 3, 4, 5, 6], 1, fetchPage, { isCurrent: () => current });
    const rejected = expect(result).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchPage).toHaveBeenCalledTimes(4);
    current = false;
    pending.forEach(resolve => resolve());
    await rejected;
    expect(fetchPage).toHaveBeenCalledTimes(4);
  });

  it("never starts a refresh that is already stale", async () => {
    const fetchPage = vi.fn();
    await expect(refreshFabReviewPages({}, [1], 1, fetchPage, { isCurrent: () => false }))
      .rejects.toMatchObject({ name: "AbortError" });
    expect(fetchPage).not.toHaveBeenCalled();
  });

  it("stops queued work after a fetch error and rejects instead of merging partial results", async () => {
    const pending: Array<() => void> = [];
    const error = new Error("synthetic page failure");
    const fetchPage = vi.fn().mockRejectedValueOnce(error).mockImplementation(
      () => new Promise<{}>(resolve => pending.push(() => resolve({}))),
    );
    const result = refreshFabReviewPages({}, [1, 2, 3, 4, 5, 6], 1, fetchPage);
    await expect(result).rejects.toBe(error);
    pending.forEach(resolve => resolve());
    await Promise.resolve();
    expect(fetchPage).toHaveBeenCalledTimes(4);
  });

  it("refetches loaded offsets and merges refreshed pages in order", async () => {
    const fetchPage = vi.fn(async (offset: number, limit: number) => ({
      workItems:
        offset === 2
          ? [
              { id: "doc-2", vendor: "Updated vendor" },
              { vendor: "Missing id" },
            ]
          : [{ id: "doc-3" }],
      pagination: { nextOffset: offset + limit, hasMore: false },
    }));

    const result = await refreshFabReviewPages(
      { workItems: [{ id: "doc-1" }, { id: "doc-2", vendor: "Old vendor" }] },
      [2, 4],
      2,
      fetchPage
    );

    expect(fetchPage).toHaveBeenNthCalledWith(1, 2, 2);
    expect(fetchPage).toHaveBeenNthCalledWith(2, 4, 2);
    expect(result.workItems).toEqual([
      { id: "doc-1" },
      { id: "doc-2", vendor: "Updated vendor" },
      { vendor: "Missing id" },
      { id: "doc-3" },
    ]);
    expect(result.pagination).toEqual({ nextOffset: 6, hasMore: false });
  });

  it("keeps the first-page result when no additional pages were loaded", async () => {
    const fetchPage = vi.fn();
    const firstPage = {
      workItems: [{ id: "doc-1" }],
      pagination: { nextOffset: 1, hasMore: true },
    };

    await expect(
      refreshFabReviewPages(firstPage, [], 1, fetchPage)
    ).resolves.toEqual({
      workItems: [{ id: "doc-1" }],
      pagination: { nextOffset: 1, hasMore: true },
    });
    expect(fetchPage).not.toHaveBeenCalled();
  });
});
