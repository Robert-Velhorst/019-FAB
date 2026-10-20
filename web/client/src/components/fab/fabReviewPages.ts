import { asRecord, records, text, type FabRecord } from "./fabView";

export type FabReviewPage = {
  workItems?: unknown;
  pagination?: unknown;
};

export async function refreshFabReviewPages(
  firstPage: FabReviewPage,
  offsets: number[],
  limit: number,
  fetchPage: (offset: number, limit: number) => Promise<FabReviewPage>,
  options: { isCurrent?: () => boolean } = {},
): Promise<{ workItems: FabRecord[]; pagination: FabRecord }> {
  const ensureCurrent = () => {
    if (options.isCurrent?.() === false) {
      const error = new Error("FAB review refresh was superseded.");
      error.name = "AbortError";
      throw error;
    }
  };
  const loadedPages: FabReviewPage[] = new Array(offsets.length);
  let nextIndex = 0;
  let failed = false;
  const worker = async () => {
    try {
      while (!failed) {
        ensureCurrent();
        const index = nextIndex++;
        if (index >= offsets.length) return;
        loadedPages[index] = await fetchPage(offsets[index], limit);
      }
    } catch (error) {
      failed = true;
      throw error;
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(4, offsets.length) }, worker),
  );
  ensureCurrent();
  const pages = [firstPage, ...loadedPages];
  const merged = new Map<string, FabRecord>();
  pages.forEach((page, pageIndex) =>
    records(page.workItems).forEach((item, itemIndex) => {
      const id = text(item.id, "");
      merged.set(id || `page:${pageIndex}:item:${itemIndex}`, item);
    })
  );
  return {
    workItems: Array.from(merged.values()),
    pagination: asRecord(pages[pages.length - 1]?.pagination),
  };
}
