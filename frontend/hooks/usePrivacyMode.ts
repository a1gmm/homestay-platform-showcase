"use client";

import { useSyncExternalStore } from "react";
import { readPrivacyMode } from "@/lib/privacy-mode";

// The mode is per-tab and toggling it reloads the app. Reading after hydration
// keeps server markup stable while making mutation controls visibly read-only.
const subscribe = () => () => {};
const serverSnapshot = () => false;

export function usePrivacyMode(): boolean {
  return useSyncExternalStore(subscribe, readPrivacyMode, serverSnapshot);
}
