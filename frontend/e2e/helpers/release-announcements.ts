import { expect, type Page } from "@playwright/test";

export async function snoozeVisibleReleaseAnnouncements(page: Page): Promise<void> {
  const dialog = page.getByRole("dialog", { name: "系统更新" });
  await expect(dialog).toBeVisible();

  const snooze = dialog.getByRole("button", { name: "稍后提醒" });
  await expect(snooze).toBeEnabled();
  await snooze.click();

  await expect(dialog).toBeHidden();
}
