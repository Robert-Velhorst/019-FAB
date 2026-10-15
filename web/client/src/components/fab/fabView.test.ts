import { describe, expect, it } from "vitest";
import { commandDetails, commandOutcome } from "./fabView";

describe("commandOutcome", () => {
  it("treats an HTTP-success response with an explicit failed result as a failure", () => {
    expect(commandOutcome({ success: false, status: "failed" })).toBe("failure");
  });

  it("treats attention states as warnings rather than successes", () => {
    expect(commandOutcome({ success: true, status: "prepared_needs_review" })).toBe("attention");
    expect(commandOutcome({ status: "attention_required" })).toBe("attention");
  });

  it("keeps completed commands successful and disabled commands non-failing", () => {
    expect(commandOutcome({ success: true, status: "completed" })).toBe("success");
    expect(commandOutcome({ success: true, status: "disabled" })).toBe("attention");
  });

  it("keeps summary fields from direct gateway responses and merges wrapped results", () => {
    expect(commandDetails({ status: "completed", documentsProcessed: 4 })).toMatchObject({
      status: "completed",
      documentsProcessed: 4,
    });
    expect(commandDetails({ status: "completed", result: { status: "failed", failed: 2 } })).toMatchObject({
      status: "failed",
      failed: 2,
    });
  });
});
