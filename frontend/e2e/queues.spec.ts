import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Queue Management & Pause/Resume Flow", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("creates a project, creates a queue, and toggles pause/resume", async ({ page }) => {
    // 1. Create a project
    await page.goto("/projects");
    const projName = `e2e-proj-${Date.now()}`;
    await page.fill("input[placeholder='project-name']", projName);
    await page.click("button:has-text('Create project')");

    // Expect project in list
    await expect(page.locator(`text=${projName}`)).toBeVisible();

    // 2. Navigate to Queues page
    await page.goto("/queues");
    await expect(page.locator("h1")).toContainText("Queues");

    // Select the created project if there's a selector
    const projectSelect = page.locator("select");
    if (await projectSelect.isVisible()) {
      await projectSelect.selectOption({ label: projName });
    }

    // 3. Create a new queue
    const queueName = `e2e-q-${Date.now()}`;
    await page.fill("input[placeholder='queue-name']", queueName);
    await page.click("button:has-text('Create queue')");

    // Verify queue appears with active status
    const queueRow = page.locator(`tr:has-text('${queueName}')`);
    await expect(queueRow).toBeVisible();
    await expect(queueRow.locator("span.badge")).toContainText("active");

    // 4. Pause the queue
    const pauseBtn = queueRow.locator("button:has-text('Pause')");
    await pauseBtn.click();

    // Verify status flips to paused
    await expect(queueRow.locator("span.badge")).toContainText("paused");
    await expect(queueRow.locator("button:has-text('Resume')")).toBeVisible();

    // 5. Resume the queue
    const resumeBtn = queueRow.locator("button:has-text('Resume')");
    await resumeBtn.click();

    // Verify status returns to active
    await expect(queueRow.locator("span.badge")).toContainText("active");
  });
});
