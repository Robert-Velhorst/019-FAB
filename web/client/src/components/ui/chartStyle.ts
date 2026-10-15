const THEMES = { light: "", dark: ".dark" } as const;
const SAFE_CHART_TOKEN = /^[A-Za-z_][A-Za-z0-9_-]*$/;
const SAFE_CSS_COLOR = /^(?:#[a-f0-9]{3,8}|[a-z]+|var\(--[a-z0-9_-]+\)|(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\([a-z0-9.,%/ +\-()]+\))$/i;

type ChartStyleConfig = Record<
  string,
  {
    color?: string;
    theme?: Record<keyof typeof THEMES, string>;
  }
>;

export function safeChartId(value: string): string {
  return `chart-${safeSelectorToken(value)}`;
}

export function buildChartCss(id: string, config: ChartStyleConfig): string {
  const colorConfig = Object.entries(config).filter(
    ([, item]) => item.theme || item.color,
  );

  const rules = Object.entries(THEMES)
    .map(([theme, prefix]) => {
      const declarations = colorConfig
        .map(([key, item]) => {
          if (!SAFE_CHART_TOKEN.test(key)) return null;
          const color = item.theme?.[theme as keyof typeof THEMES] || item.color;
          return color && SAFE_CSS_COLOR.test(color)
            ? `  --color-${key}: ${color};`
            : null;
        })
        .filter(Boolean)
        .join("\n");
      return declarations
        ? `${prefix} [data-chart=${safeSelectorToken(id)}] {\n${declarations}\n}`
        : null;
    })
    .filter(Boolean);
  return rules.join("\n");
}

function safeSelectorToken(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, "-");
}
