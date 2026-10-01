import {
  devices,
  expect,
  request,
  test as base,
  type APIRequestContext,
  type BrowserContext,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";

const apiOrigin = process.env.E2E_API_ORIGIN || "http://127.0.0.1:8000";
const canonicalSeed = { status: "seeded", billing_month: "2025-12" };

type AttemptResetFixture = {
  attemptReset: void;
};

export const test = base.extend<AttemptResetFixture>({
  attemptReset: [async ({ request: api }, use) => {
    const response = await api.post(
      `${apiOrigin.replace(/\/$/, "")}/__e2e__/monthly-close/reset-task12`,
    );
    const body = await response.text();

    expect(response.ok(), `attempt reset failed: ${body}`).toBeTruthy();
    expect(JSON.parse(body), "attempt reset returned an unexpected seed").toEqual(canonicalSeed);
    await use();
  }, { auto: true }],
});

export {
  devices,
  expect,
  request,
  type APIRequestContext,
  type BrowserContext,
  type Locator,
  type Page,
  type Route,
};
