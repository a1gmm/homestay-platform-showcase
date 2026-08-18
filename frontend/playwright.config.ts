import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:3000";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer:
    process.env.PLAYWRIGHT_NO_WEBSERVER === "1"
      ? undefined
      : [
          {
            command: "uv run --directory ../backend python tests/e2e_task8_server.py",
            url: `${process.env.E2E_API_ORIGIN || "http://127.0.0.1:8000"}/health`,
            reuseExistingServer: false,
            timeout: 120_000,
          },
          {
          command: "pnpm dev --hostname 127.0.0.1",
          url: baseURL,
          reuseExistingServer: false,
          timeout: 120_000,
          env: {
            ...process.env,
            BACKEND_URL:
              process.env.BACKEND_URL || process.env.E2E_API_ORIGIN || "http://127.0.0.1:8000",
          },
          },
        ],
});
