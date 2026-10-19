import { Page } from "@playwright/test";

/**
 * Creates a unique test user and organization through the UI, ensuring an
 * isolated session cookie for each test spec.
 */
export async function registerAndLogin(
  page: Page,
  email = `e2e-${Date.now()}-${Math.floor(Math.random() * 10000)}@pulse.test`,
  password = "password1234",
  orgName = `Org-${Date.now()}`
) {
  await page.goto("/signup");
  await page.fill("input[type='email']", email);
  await page.fill("input[placeholder='Jane Smith']", "Test Operator");
  await page.fill("input[placeholder='Acme Corp']", orgName);
  await page.fill("input[type='password']", password);
  await page.click("button[type='submit']");
  await page.waitForURL("/", { timeout: 10000 });
  return { email, password, orgName };
}
