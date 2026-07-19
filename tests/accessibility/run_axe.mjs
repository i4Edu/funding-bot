import { Buffer } from "node:buffer";
import AxeBuilder from "@axe-core/playwright";
import { chromium } from "playwright";

const baseUrl = process.env.ACCESSIBILITY_BASE_URL || "http://127.0.0.1:5001";
const authToken = Buffer.from(
  `${process.env.ACCESSIBILITY_USERNAME || "admin"}:${process.env.ACCESSIBILITY_PASSWORD || "admin-secret"}`,
).toString("base64");
const routes = ["/dashboard", "/dashboard/tasks", "/settings"];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  extraHTTPHeaders: {
    Authorization: `Basic ${authToken}`,
  },
});

let hasViolations = false;

try {
  for (const route of routes) {
    const page = await context.newPage();
    const url = `${baseUrl}${route}`;

    await page.goto(url, { waitUntil: "networkidle" });
    await page.locator("#main-content").waitFor();

    const results = await new AxeBuilder({ page }).analyze();
    if (results.violations.length > 0) {
      hasViolations = true;
      console.error(`Accessibility violations found on ${url}`);
      for (const violation of results.violations) {
        console.error(`- ${violation.id}: ${violation.help}`);
        for (const node of violation.nodes) {
          console.error(`  • ${node.target.join(", ")}`);
          if (node.failureSummary) {
            console.error(`    ${node.failureSummary.replace(/\n/g, " ").trim()}`);
          }
        }
      }
    } else {
      console.log(`No accessibility violations found on ${url}`);
    }

    await page.close();
  }
} finally {
  await context.close();
  await browser.close();
}

if (hasViolations) {
  process.exit(1);
}
