import { Buffer } from "node:buffer";
import AxeBuilder from "@axe-core/playwright";
import { chromium } from "playwright";

const baseUrl = process.env.ACCESSIBILITY_BASE_URL || "http://127.0.0.1:5001";
const routes = ["/dashboard", "/dashboard/tasks", "/settings"];
const colorSchemes = ["light", "dark"];
const headers = {};

if (process.env.ACCESSIBILITY_USERNAME && process.env.ACCESSIBILITY_PASSWORD) {
  headers.Authorization = `Basic ${Buffer.from(
    `${process.env.ACCESSIBILITY_USERNAME}:${process.env.ACCESSIBILITY_PASSWORD}`,
  ).toString("base64")}`;
}

const browser = await chromium.launch({ headless: true });

let hasViolations = false;

try {
  for (const colorScheme of colorSchemes) {
    const context = await browser.newContext({
      colorScheme,
      extraHTTPHeaders: headers,
    });

    try {
      for (const route of routes) {
        const page = await context.newPage();
        const url = `${baseUrl}${route}`;

        await page.goto(url, { waitUntil: "networkidle" });
        await page.locator("#main-content").waitFor();

        const results = await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa"])
          .analyze();
        const contrastIssues = await page.evaluate(() => {
          const targets = [
            { selector: ".app-role-chip", minimum: 4.5 },
            { selector: ".text-muted", minimum: 4.5 },
            { selector: ".badge.text-bg-primary", minimum: 4.5 },
            { selector: ".badge.text-bg-secondary", minimum: 4.5 },
            { selector: ".badge.text-bg-success", minimum: 4.5 },
            { selector: ".badge.text-bg-warning", minimum: 4.5 },
            { selector: ".badge.text-bg-light", minimum: 4.5 },
            { selector: ".badge.text-bg-info", minimum: 4.5 },
          ];

          const parseColorValue = (cssColor) => {
            const normalized = cssColor.trim().toLowerCase();
            if (normalized === "transparent") {
              return { r: 0, g: 0, b: 0, a: 0 };
            }
            const parts = normalized.match(/[\d.]+/g)?.map(Number) || [];
            return {
              r: parts[0] ?? 0,
              g: parts[1] ?? 0,
              b: parts[2] ?? 0,
              a: parts[3] ?? 1,
            };
          };

          const composite = (foreground, background) => {
            const alpha = foreground.a + background.a * (1 - foreground.a);
            if (alpha <= 0) {
              return { r: 0, g: 0, b: 0, a: 0 };
            }
            return {
              r: ((foreground.r * foreground.a) + (background.r * background.a * (1 - foreground.a))) / alpha,
              g: ((foreground.g * foreground.a) + (background.g * background.a * (1 - foreground.a))) / alpha,
              b: ((foreground.b * foreground.a) + (background.b * background.a * (1 - foreground.a))) / alpha,
              a: alpha,
            };
          };

          const luminance = (color) => {
            const [red, green, blue] = [color.r, color.g, color.b].map((channel) => {
              const normalized = channel / 255;
              return normalized <= 0.03928
                ? normalized / 12.92
                : ((normalized + 0.055) / 1.055) ** 2.4;
            });
            return (0.2126 * red) + (0.7152 * green) + (0.0722 * blue);
          };

          const ratio = (foreground, background) => {
            const lighter = Math.max(luminance(foreground), luminance(background));
            const darker = Math.min(luminance(foreground), luminance(background));
            return (lighter + 0.05) / (darker + 0.05);
          };

          const rootBackground = parseColorValue(getComputedStyle(document.body).backgroundColor);

          return targets.flatMap(({ selector, minimum }) => Array.from(document.querySelectorAll(selector))
            .filter((element) => element.textContent?.trim())
            .map((element) => {
              const computedStyle = getComputedStyle(element);
              const foreground = parseColorValue(computedStyle.color);
              foreground.a *= Number(computedStyle.opacity || "1");

              let background = rootBackground;
              const ancestors = [];
              for (let current = element; current; current = current.parentElement) {
                ancestors.push(current);
              }

              for (let index = ancestors.length - 1; index >= 0; index -= 1) {
                const layer = parseColorValue(getComputedStyle(ancestors[index]).backgroundColor);
                if (layer.a > 0) {
                  background = composite(layer, background);
                }
              }

              const measuredRatio = ratio(
                foreground.a < 1 ? composite(foreground, background) : foreground,
                background,
              );

              return measuredRatio < minimum ? {
                selector,
                text: element.textContent.trim().slice(0, 80),
                ratio: Number(measuredRatio.toFixed(2)),
                minimum,
              } : null;
            })
            .filter(Boolean));
        });

        if (results.violations.length > 0 || contrastIssues.length > 0) {
          hasViolations = true;
          console.error(`Accessibility violations found on ${url} (${colorScheme})`);
          for (const violation of results.violations) {
            console.error(`- ${violation.id}: ${violation.help}`);
            for (const node of violation.nodes) {
              console.error(`  • ${node.target.join(", ")}`);
              if (node.failureSummary) {
                console.error(`    ${node.failureSummary.replace(/\n/g, " ").trim()}`);
              }
            }
          }
          for (const issue of contrastIssues) {
            console.error(
              `- contrast: ${issue.selector} "${issue.text}" ratio ${issue.ratio} (minimum ${issue.minimum})`,
            );
          }
        }
        else {
          console.log(`No accessibility violations found on ${url} (${colorScheme})`);
        }

        await page.close();
      }
    } finally {
      await context.close();
    }
  }
} finally {
  await browser.close();
}

if (hasViolations) {
  process.exit(1);
}
