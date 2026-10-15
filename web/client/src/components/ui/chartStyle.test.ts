import { describe, expect, it } from "vitest";

import { buildChartCss, safeChartId } from "./chartStyle";

describe("chart style generation", () => {
  it("keeps selectors, series keys, and colors inside safe CSS tokens", () => {
    const id = 'x"] { background: url(javascript:alert(1)) }';
    const css = buildChartCss(safeChartId(id), {
      expense: { color: "hsl(var(--chart-1))" },
      'bad} body { background: red': { color: "red" },
      injectedColor: { color: "red; }</style><script>alert(1)</script>" },
    });

    expect(css).toContain("data-chart=chart-x");
    expect(css).toContain("--color-expense: hsl(var(--chart-1));");
    expect(css).not.toContain("javascript:");
    expect(css).not.toContain("--color-bad");
    expect(css).not.toContain("</style><script>");
    expect(css).not.toContain("--color-injectedColor");
  });

  it("omits empty styles when no valid CSS colors remain", () => {
    expect(buildChartCss("chart-test", { invalid: { color: "red;" } })).toBe("");
  });
});
