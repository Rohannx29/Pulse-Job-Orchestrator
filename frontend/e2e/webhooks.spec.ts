import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Outbound Webhooks Flow", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("registers webhook subscription, verifies signing secret, and inspects delivery console", async ({ page }) => {
    // 1. Navigate to Webhooks console
    await page.goto("/webhooks");
    await expect(page.locator("h1")).toContainText("Outbound Webhooks");
    await expect(page.locator("text=Active Subscriptions")).toBeVisible();

    // 2. Register a new webhook endpoint
    const webhookUrl = `https://httpbin.org/post?spec=${Date.now()}`;
    await page.fill("input[placeholder='https://api.example.com/webhooks/pulse']", webhookUrl);
    await page.fill("input[placeholder='*, job.completed, job.failed']", "job.completed, job.failed");
    await page.fill("input[placeholder='Primary Production Ingestion']", "E2E Automated Receiver");
    await page.click("button:has-text('Add Subscription')");

    // Expect success message
    await expect(page.locator("text=Webhook subscription created successfully")).toBeVisible();

    // 3. Verify subscription row in the table
    const subRow = page.locator(`tr:has-text('${webhookUrl}')`);
    await expect(subRow).toBeVisible();

    // Verify secret is initially masked with bullet points
    await expect(subRow.locator("text=••••••••••••••••")).toBeVisible();

    // Click "Show" to reveal HMAC secret starting with whsec_
    await subRow.locator("button:has-text('Show')").click();
    await expect(subRow.locator("code:has-text('whsec_')")).toBeVisible();

    // 4. Trigger "Test Ping"
    const testPingBtn = subRow.locator("button:has-text('Test Ping')");
    await testPingBtn.click();

    // Expect test ping banner outcome
    await expect(page.locator("text=Test Ping Outcome:")).toBeVisible({ timeout: 15000 });

    // 5. Click "Logs" to open delivery audit drawer
    await subRow.locator("button:has-text('Logs')").click();
    await expect(page.locator("h3:has-text('Delivery History & Audit Logs')")).toBeVisible();
  });
});
