import { asRecord, records, text, type FabRecord } from "./fabView";

export type FabReviewPage = {
  workItems?: unknown;
  pagination?: unknown;
};

export async function refreshFabReviewPages(
  firstPage: FabReviewPage,
  offsets: number[],
  limit: number,
  fetchPage: (offset: number, limit: number) => Promise<FabReviewPage>
): Promise<{ workItems: FabRecord[]; pagination: FabRecord }> {
  const loadedPages = await Promise.all(
    offsets.map(offset => fetchPage(offset, limit))
  );
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
