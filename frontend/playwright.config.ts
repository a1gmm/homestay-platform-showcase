import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:3000";
const runsMonthlyCloseFullStory = process.env.PLAYWRIGHT_FULL_STORY === "1"
  || process.argv.some((argument) => argument.includes("monthly-close-assistant-full-story"));
const runsResultOutput = process.argv.some((argument) => argument.includes("monthly-close-result-output"));

const backendServer = {
  command: runsResultOutput
    ? "uv run --directory ../backend python tests/e2e_cleaning_resolution_server.py"
    : process.argv.some((argument) => argument.includes("monthly-close-cleaning-investigation"))
    ? "uv run --directory ../backend python tests/e2e_cleaning_investigation_server.py"
    : "uv run --directory ../backend python tests/e2e_task8_server.py",
  url: `${process.env.E2E_API_ORIGIN || "http://127.0.0.1:8000"}/health`,
  reuseExistingServer: false,
  timeout: 120_000,
  ...(runsResultOutput ? { env: { ...process.env, E2E_SERVER_PORT: "8000", E2E_STUB_MODEL: "1" } } : {}),
};

const killSwitchBackendServer = {
  command: "uv run --directory ../backend python tests/e2e_task8_server.py",
  url: `${process.env.E2E_KILL_SWITCH_API_ORIGIN || "http://127.0.0.1:8001"}/health`,
  reuseExistingServer: false,
  timeout: 120_000,
  env: {
    ...process.env,
    E2E_SKIP_RESET: "1",
    E2E_SERVER_PORT: "8001",
    MONTHLY_CLOSE_ASSISTANT_ENABLED: "false",
    MONTHLY_CLOSE_EXTERNAL_INTAKE_ENABLED: "false",
    MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED: "false",
    MONTHLY_CLOSE_SOURCE_ADAPTERS_ENABLED: "false",
    MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED: "false",
    MONTHLY_CLOSE_LOW_RISK_AUTOMATION_ENABLED: "false",
  },
};

const frontendServer = {
  command: `pnpm dev ${process.env.PLAYWRIGHT_WEBPACK === "1" ? "--webpack " : ""}--hostname 127.0.0.1`,
  url: baseURL,
  reuseExistingServer: false,
  timeout: 120_000,
  env: {
    ...process.env,
    BACKEND_URL:
      process.env.BACKEND_URL || process.env.E2E_API_ORIGIN || "http://127.0.0.1:8000",
  },
};

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
      : runsMonthlyCloseFullStory
        ? [backendServer, killSwitchBackendServer, frontendServer]
        : [backendServer, frontendServer],
});
