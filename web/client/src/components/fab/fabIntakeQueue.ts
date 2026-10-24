export const MAX_INTAKE_FILES_PER_BATCH = 100;

export type IntakeQueueItem<TFile, TResult> = {
  id: string;
  file: TFile | null;
  fileName: string;
  fileSize: number;
  status: "queued" | "uploading" | "complete" | "error";
  error?: string;
  result?: TResult;
};

export function completeIntakeItem<TFile, TResult>(
  item: IntakeQueueItem<TFile, TResult>,
  result: TResult,
): IntakeQueueItem<TFile, TResult> {
  return { ...item, file: null, status: "complete", error: undefined, result };
}

export function tryBeginIntakeBatch(
  busyRef: { current: boolean },
  connected: boolean,
  busy: boolean,
  queuedCount: number,
): boolean {
  if (!connected || busy || busyRef.current || queuedCount <= 0) return false;
  busyRef.current = true;
  return true;
}

export async function finishIntakeBatch(
  uploadedCount: number,
  onFinished: (uploadedCount: number) => Promise<void> | void,
  onFailure: () => void,
): Promise<void> {
  if (uploadedCount <= 0) return;
  try {
    await onFinished(uploadedCount);
  } catch {
    onFailure();
  }
}

export function appendIntakeItems<TInput, TOutput>(
  current: TOutput[],
  incoming: ArrayLike<TInput>,
  createItem: (input: TInput, index: number) => TOutput,
  maximum = MAX_INTAKE_FILES_PER_BATCH,
): { items: TOutput[]; rejectedCount: number } {
  const capacity = Math.max(0, Math.trunc(maximum) - current.length);
  const acceptedCount = Math.min(incoming.length, capacity);
  const items = [...current];
  for (let index = 0; index < acceptedCount; index += 1) {
    items.push(createItem(incoming[index], index));
  }
  return {
    items,
    rejectedCount: incoming.length - acceptedCount,
  };
}
