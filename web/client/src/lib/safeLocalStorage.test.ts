import { afterEach, describe, expect, it, vi } from "vitest";
import { readLocalStorage, writeLocalStorage } from "./safeLocalStorage";

describe("safe local storage access", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns an empty result when no browser window exists", () => {
    vi.stubGlobal("window", undefined);

    expect(readLocalStorage("fab-lang")).toBeNull();
    expect(writeLocalStorage("fab-lang", "nl")).toBe(false);
  });

  it("treats inaccessible storage as unavailable", () => {
    vi.stubGlobal("window", {
      get localStorage() {
        throw new DOMException("Storage access is denied", "SecurityError");
      },
    });

    expect(readLocalStorage("fab-lang")).toBeNull();
    expect(writeLocalStorage("fab-lang", "nl")).toBe(false);
  });

  it("keeps storage method failures from escaping", () => {
    vi.stubGlobal("window", {
      localStorage: {
        getItem: () => {
          throw new DOMException("Storage is unavailable", "SecurityError");
        },
        setItem: () => {
          throw new DOMException("Storage quota is exhausted", "QuotaExceededError");
        },
      },
    });

    expect(readLocalStorage("fab-lang")).toBeNull();
    expect(writeLocalStorage("fab-lang", "nl")).toBe(false);
  });

  it("reads and writes when storage is available", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => store.set(key, value),
      },
    });

    expect(writeLocalStorage("fab-lang", "nl")).toBe(true);
    expect(readLocalStorage("fab-lang")).toBe("nl");
  });
});
