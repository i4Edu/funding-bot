import { expect, test } from "playwright/test";

test("unauthenticated requests are challenged", async ({ browser, baseURL }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const response = await page.goto(`${baseURL}/dashboard`);
  expect(response?.status()).toBe(401);
  await expect(page.locator("body")).toContainText("Authentication required");
  await context.close();
});

test("admin can navigate dashboard and settings", async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    httpCredentials: { username: "admin", password: "admin-secret" },
  });
  const page = await context.newPage();

  await page.goto(`${baseURL}/dashboard`);
  await expect(page.getByRole("heading", { name: "Operations Dashboard" })).toBeAttached();
  await expect(page.getByText("Education Innovation Grant")).toBeVisible();

  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  await page.getByRole("link", { name: "My Tasks" }).click();
  await expect(page.getByRole("heading", { name: "Task Board" })).toBeVisible();

  await page.getByRole("link", { name: "Dashboard" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await context.close();
});
