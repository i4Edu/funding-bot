import { expect, test } from "playwright/test";

test("admin can run settings, connector, task, and export workflows", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    httpCredentials: { username: "admin", password: "admin-secret" },
  });
  const page = await context.newPage();

  await page.goto(`${baseURL}/settings`);
  await page.getByLabel("Organization name").fill("i4Edu Labs");
  await page.getByLabel("Mission").fill("Expand access to equitable education and digital learning.");
  await page.getByRole("button", { name: "Save organization profile" }).click();
  await expect(page.locator("#organization-result")).toHaveText("Profile saved.");

  await page.getByLabel("Keyword filters (comma-separated)").fill("education, community, innovation");
  await page.getByRole("button", { name: "Save donation search settings" }).click();
  await expect(page.locator("#search-result")).toHaveText("Search settings saved.");

  await page.getByLabel("Credential alias").fill("grants-api");
  await page.getByLabel("Credential environment variable name").fill("GRANTS_API_TOKEN");
  await page.getByRole("button", { name: "Add credential alias" }).click();
  await page.waitForLoadState("networkidle");
  await expect(page.getByText("grants-api")).toBeVisible();

  await page.getByRole("button", { name: "Run donation discovery now" }).click();
  await expect(page.locator("#discovery-result")).toContainText("\"count\":");
  await expect(page.locator("#discovery-result")).toContainText("Education");

  await page.getByLabel("Effective date").fill("2026-07-19");
  await page.getByRole("button", { name: "Generate privacy policy" }).click();
  await page.waitForLoadState("networkidle");
  await expect(page.getByText(/EU/)).toBeVisible();

  await page.getByLabel("Donor email address").fill("donor@example.org");
  await page.getByLabel("Donor name").fill("Donor Name");
  await page.getByRole("button", { name: "Send donor outreach test" }).click();
  await expect(page.locator("#outreach-result")).toContainText("donor@example.org");

  await page.getByRole("link", { name: "Open my tasks dashboard" }).click();
  await expect(page.getByRole("heading", { name: "Task Board" })).toBeVisible();

  await page.getByLabel("Task title").fill("Playwright created task");
  await page.getByLabel("Task assignee").selectOption("admin");
  await page.getByLabel("Task due date").fill("2026-07-24");
  await page.getByLabel("Task description").fill("Created from the Playwright flow.");
  await page.getByRole("button", { name: "Create task" }).click();
  await page.waitForLoadState("networkidle");
  await expect(page.getByText("Playwright created task")).toBeVisible();

  await page.locator('[data-task-title="Playwright created task"]').click();
  await page.getByLabel("Edit task title").fill("Playwright edited task");
  await page.getByLabel("Edit task description").fill("Updated from the Playwright flow.");
  await page.getByLabel("Edit task status").selectOption("in-progress");
  await page.getByRole("button", { name: "Save task changes" }).click();
  await page.waitForLoadState("networkidle");
  await expect(page.getByText("Playwright edited task")).toBeVisible();

  const editedTask = page.locator('[data-task-title="Playwright edited task"]');
  await editedTask.focus();
  await page.keyboard.press("ArrowRight");
  await page.waitForLoadState("networkidle");

  const [exportPage] = await Promise.all([
    page.waitForEvent("popup"),
    page.getByRole("link", { name: "Export filtered tasks as JSON" }).click(),
  ]);
  await exportPage.waitForLoadState("domcontentloaded");
  await expect(exportPage.locator("body")).toContainText("Playwright edited task");

  await page.getByRole("link", { name: "Open dashboard page" }).click();
  await expect(page.getByText("Education Innovation Grant")).toBeVisible();

  await context.close();
});
