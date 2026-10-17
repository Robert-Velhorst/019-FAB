import { describe, expect, it, vi } from "vitest";
import { refreshFabReviewPages } from "./fabReviewPages";

describe("refreshFabReviewPages", () => {
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
