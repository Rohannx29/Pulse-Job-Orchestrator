import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Audit Log & Operator Activity Trail Flow", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("inspects audit activity trail and validates filter controls", async ({ page }) => {
    // 1. Navigate to Audit Logs explorer
    await page.goto("/audit-logs");
    await expect(page.locator("h1")).toContainText("Audit Log & Activity Trail");
    await expect(page.locator("text=Immutable, compliance-ready record")).toBeVisible();

    // 2. Verify filter selects
    const actionSelect = page.locator("select").first();
    await expect(actionSelect).toBeVisible();

    // Select queue.pause filter
    await actionSelect.selectOption("queue.pause");
    await expect(actionSelect).toHaveValue("queue.pause");

    // Reset filter to all
    await actionSelect.selectOption("all");
    await expect(actionSelect).toHaveValue("all");

    // 3. Verify audit log table is visible
    const table = page.locator("table");
    await expect(table).toBeVisible();
    await expect(page.locator("th:has-text('TIMESTAMP')")).toBeVisible();
    await expect(page.locator("th:has-text('ACTION')")).toBeVisible();
    await expect(page.locator("th:has-text('RESOURCE')")).toBeVisible();
  });
});
