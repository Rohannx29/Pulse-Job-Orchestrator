import { test, expect } from "@playwright/test";

test.describe("Authentication & Route Protection Flow", () => {
  test("unauthenticated visitor to protected route is redirected to login", async ({ page }) => {
    // Attempt to access dashboard directly
    await page.goto("/");
    await expect(page).toHaveURL(/\/login\?next=%2F|\/login/);

    // Attempt to access jobs page directly
    await page.goto("/jobs");
    await expect(page).toHaveURL(/\/login\?next=%2Fjobs/);

    // Verify login form is rendered
    await expect(page.locator("button[type='submit']")).toContainText("Sign in");
  });

  test("shows clear error message on invalid credentials", async ({ page }) => {
    await page.goto("/login");
    await page.fill("input[type='email']", "nonexistent-user@pulse.test");
    await page.fill("input[type='password']", "wrong-password-1234");
    await page.click("button[type='submit']");

    // Expect invalid credentials alert
    const errorMsg = page.locator("text=Invalid email or password.");
    await expect(errorMsg).toBeVisible();
  });

  test("signup form validates password length", async ({ page }) => {
    await page.goto("/signup");
    await page.fill("input[type='email']", "new-user@pulse.test");
    await page.fill("input[placeholder='Jane Smith']", "Test User");
    await page.fill("input[placeholder='Acme Corp']", "Acme Test Org");
    await page.fill("input[type='password']", "short");
    await page.click("button[type='submit']");

    // Expect minimum password length warning
    const lengthError = page.locator("text=Password must be at least 8 characters.");
    await expect(lengthError).toBeVisible();
  });

  test("end-to-end user registration, dashboard navigation, and logout", async ({ page }) => {
    const uniqueEmail = `e2e-user-${Date.now()}@pulse.test`;
    const uniqueOrg = `Org-${Date.now()}`;

    // Navigate to signup
    await page.goto("/signup");
    await page.fill("input[type='email']", uniqueEmail);
    await page.fill("input[placeholder='Jane Smith']", "E2E Lead");
    await page.fill("input[placeholder='Acme Corp']", uniqueOrg);
    await page.fill("input[type='password']", "supersecretpassword123");
    await page.click("button[type='submit']");

    // Expect automatic redirect to authenticated dashboard
    await expect(page).toHaveURL("/", { timeout: 10000 });
    await expect(page.locator("h1")).toContainText("System overview");

    // Verify navigation bar shows authenticated user and live SSE indicator
    const navBar = page.locator("nav");
    await expect(navBar).toBeVisible();
    await expect(page.locator("text=ORG ADMIN")).toBeVisible();

    // Sign out
    const signOutBtn = page.locator("button:has-text('Sign out')");
    await signOutBtn.click();

    // Verify redirection to login page
    await expect(page).toHaveURL(/\/login/);
  });
});
