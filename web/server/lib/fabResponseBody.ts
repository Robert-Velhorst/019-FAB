export class FabResponseTooLargeError extends Error {
  constructor() {
    super("FAB response exceeds the permitted size limit");
  }
}

export async function readBoundedFabResponseBody(response: Response, maxBytes: number): Promise<Buffer> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    throw new Error("Invalid FAB response size limit");
  }
  const advertised = response.headers.get("content-length");
  if (advertised && /^\d+$/.test(advertised) && Number(advertised) > maxBytes) {
    void response.body?.cancel().catch(() => {});
    throw new FabResponseTooLargeError();
  }
  if (!response.body) return Buffer.alloc(0);
  const reader = response.body.getReader();
  let buffer = Buffer.allocUnsafe(Math.min(maxBytes, 16 * 1024));
  let length = 0;
  let finished = false;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) {
        finished = true;
        return buffer.subarray(0, length);
      }
      if (!chunk.value.byteLength) continue;
      const nextLength = length + chunk.value.byteLength;
      if (nextLength > maxBytes) throw new FabResponseTooLargeError();
      if (nextLength > buffer.length) {
        const grown = Buffer.allocUnsafe(Math.min(maxBytes, Math.max(nextLength, buffer.length * 2)));
        buffer.copy(grown, 0, 0, length);
        buffer = grown;
      }
      buffer.set(chunk.value, length);
      length = nextLength;
    }
  } finally {
    // Cancellation must not let an upstream cleanup promise stall the request.
    if (!finished) void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
