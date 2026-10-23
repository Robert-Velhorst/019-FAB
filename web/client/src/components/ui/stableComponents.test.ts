import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Route, Router, Switch } from "wouter";
import { Calendar } from "./calendar";
import { ResizablePanel, ResizablePanelGroup } from "./resizable";

afterEach(() => vi.unstubAllGlobals());

describe("current component compatibility", () => {
  it("keeps legacy panel dimensions as percentages and direction as orientation", () => {
    const panel = ResizablePanel({
      defaultSize: 50,
      minSize: 10,
      maxSize: 80,
      collapsedSize: 0,
    });
    expect(panel.props).toMatchObject({
      defaultSize: "50%",
      minSize: "10%",
      maxSize: "80%",
      collapsedSize: "0%",
    });
    expect(ResizablePanel({ defaultSize: "120px" }).props.defaultSize).toBe(
      "120px"
    );
    const group = ResizablePanelGroup({ direction: "vertical" });
    expect(group.props.value).toBe("vertical");
    expect(group.props.children.props).toMatchObject({
      orientation: "vertical",
      "data-panel-group-direction": "vertical",
    });
  });

  it("retains the patched route registry on the actual installed router", () => {
    const fixtureWindow: { __WOUTER_ROUTES__?: string[] } = {};
    vi.stubGlobal("window", fixtureWindow);
    const app = createElement(
      Router,
      { ssrPath: "/stable" },
      createElement(
        Switch,
        null,
        createElement(
          Route,
          { path: "/stable" },
          createElement("span", null, "stable fixture")
        ),
        createElement(
          Route,
          { path: "/other" },
          createElement("span", null, "other fixture")
        )
      )
    );
    expect(renderToStaticMarkup(app)).toContain("stable fixture");
    renderToStaticMarkup(app);
    expect(fixtureWindow.__WOUTER_ROUTES__).toEqual(["/stable", "/other"]);
  });

  it("renders the current calendar grid and selected day", () => {
    const date = new Date(2026, 9, 23);
    const markup = renderToStaticMarkup(
      createElement(Calendar, {
        mode: "single",
        selected: date,
        defaultMonth: date,
      })
    );
    expect(markup).toContain("border-collapse");
    expect(markup).toContain('role="grid"');
    expect(markup).toContain('aria-selected="true"');
  });
});
