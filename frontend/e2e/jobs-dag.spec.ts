import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Job Submission & DAG Workflows Flow", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("submits jobs, links dependencies, and inspects DAG details", async ({ page }) => {
    // 1. Create a project and queue first
    await page.goto("/projects");
    const projName = `dag-proj-${Date.now()}`;
    await page.fill("input[placeholder='project-name']", projName);
    await page.click("button:has-text('Create project')");
    await expect(page.locator(`text=${projName}`)).toBeVisible();

    await page.goto("/queues");
    const qName = `dag-q-${Date.now()}`;
    await page.fill("input[placeholder='queue-name']", qName);
    await page.click("button:has-text('Create queue')");
    await expect(page.locator(`text=${qName}`)).toBeVisible();

    // 2. Navigate to Jobs explorer
    await page.goto("/jobs");
    await expect(page.locator("h1")).toContainText("Job explorer");

    // Open job creation form
    await page.click("button:has-text('+ Queue job')");
    const parentJobName = `parent-${Date.now()}`;
    await page.fill("input[placeholder='my-job']", parentJobName);
    await page.click("button:has-text('Submit job')");

    // Verify parent job appears in list
    const parentJobRow = page.locator(`tr:has-text('${parentJobName}')`);
    await expect(parentJobRow).toBeVisible();

    // Capture parent job ID from the link href (/jobs/<id>)
    const parentLink = parentJobRow.locator(`a:has-text('${parentJobName}')`);
    const parentHref = await parentLink.getAttribute("href");
    const parentId = parentHref?.split("/").pop();
    expect(parentId).toBeTruthy();

    // 3. Submit dependent child job with prerequisite parentId
    await page.click("button:has-text('+ Queue job')");
    const childJobName = `child-${Date.now()}`;
    await page.fill("input[placeholder='my-job']", childJobName);
    await page.fill("input[placeholder='e.g. uuid-1, uuid-2 (optional)']", parentId!);
    await page.click("button:has-text('Submit job')");

    // Verify child job appears
    const childJobRow = page.locator(`tr:has-text('${childJobName}')`);
    await expect(childJobRow).toBeVisible();

    // 4. Click child job to view details
    await childJobRow.locator(`a:has-text('${childJobName}')`).click();

    // Verify details page header and status
    await expect(page.locator("h1.page-title")).toContainText(childJobName);
    await expect(page.locator("text=Prerequisites (1)")).toBeVisible();
    await expect(page.locator(`a[title='${parentId}']`)).toBeVisible();

    // Verify DAG Topology panel
    await expect(page.locator("text=Directed Acyclic Graph (DAG) Topology")).toBeVisible();
  });
});
