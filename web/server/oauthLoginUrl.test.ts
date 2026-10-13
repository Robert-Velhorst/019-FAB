import { describe, expect, it } from "vitest";
import { getLoginUrl } from "../client/src/const";

describe("shared sign-in link", () => {
  it("starts on FAB without depending on browser globals or exposing state", () => {
    expect(getLoginUrl()).toBe("/api/oauth/start");
    expect(getLoginUrl()).toBe(getLoginUrl());
  });
});
