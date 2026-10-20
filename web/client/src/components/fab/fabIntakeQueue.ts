export const MAX_INTAKE_FILES_PER_BATCH = 100;

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
