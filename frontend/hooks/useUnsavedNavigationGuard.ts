"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { APP_NAVIGATION_REQUEST, type AppNavigationRequestDetail } from "@/lib/app-navigation";

export interface PendingNavigation {
  id: number;
  href: string;
  navigate?: () => void;
}

export function useUnsavedNavigationGuard({
  hasPendingWork,
  saveBeforeLeave,
  persistBeforeUnload,
  navigateTo,
}: {
  hasPendingWork(): boolean;
  saveBeforeLeave(): Promise<unknown>;
  persistBeforeUnload?(): void;
  navigateTo(href: string): void;
}) {
  const [pendingNavigation, setPendingNavigationState] = useState<PendingNavigation | null>(null);
  const [navigationConfirming, setNavigationConfirming] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  const pendingRef = useRef<PendingNavigation | null>(null);
  const sequenceRef = useRef(0);
  const confirmingRef = useRef<number | null>(null);
  const completedRef = useRef(new Set<number>());
  const callbacksRef = useRef({ hasPendingWork, saveBeforeLeave, persistBeforeUnload, navigateTo });
  useEffect(() => {
    callbacksRef.current = { hasPendingWork, saveBeforeLeave, persistBeforeUnload, navigateTo };
  }, [hasPendingWork, navigateTo, persistBeforeUnload, saveBeforeLeave]);
  const armRef = useRef<() => void>(() => undefined);
  const releaseRef = useRef<(navigate: () => void) => void>((navigate) => navigate());

  const setPendingNavigation = useCallback((next: PendingNavigation | null) => {
    pendingRef.current = next;
    setPendingNavigationState(next);
  }, []);
  const requestNavigation = useCallback((href: string, navigate?: () => void) => {
    if (!callbacksRef.current.hasPendingWork()) return false;
    const next = { id: ++sequenceRef.current, href, navigate };
    confirmingRef.current = null;
    setNavigationConfirming(false);
    setNavigationError(null);
    setPendingNavigation(next);
    return true;
  }, [setPendingNavigation]);

  useEffect(() => {
    const editorHref = `${location.pathname}${location.search}${location.hash}`;
    const token = `content-editor-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const guardedState = { ...(history.state && typeof history.state === "object" ? history.state : {}), __ownerEditorNavigationGuard: token };
    let guardActive = false;
    const arm = () => {
      if (guardActive) return;
      history.pushState(guardedState, "", editorHref);
      guardActive = true;
    };
    const release = (navigate: () => void) => {
      if (!guardActive) return navigate();
      guardActive = false;
      addEventListener("popstate", navigate, { once: true });
      history.back();
    };
    armRef.current = arm;
    releaseRef.current = release;
    if (callbacksRef.current.hasPendingWork()) arm();

    const click = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target instanceof Element ? event.target.closest<HTMLElement>("a[href], [data-navigation-href]") : null;
      const raw = target instanceof HTMLAnchorElement ? target.getAttribute("href") : target?.dataset.navigationHref;
      if (!raw) return;
      const destination = new URL(raw, location.href);
      if (destination.origin !== location.origin || !requestNavigation(`${destination.pathname}${destination.search}${destination.hash}`)) return;
      event.preventDefault(); event.stopPropagation();
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const target = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-navigation-href]") : null;
      if (!target?.dataset.navigationHref || !requestNavigation(target.dataset.navigationHref)) return;
      event.preventDefault(); event.stopPropagation();
    };
    const programmatic = (event: Event) => {
      const detail = (event as CustomEvent<AppNavigationRequestDetail>).detail;
      if (!detail?.href || typeof detail.navigate !== "function" || !requestNavigation(detail.href, detail.navigate)) return;
      event.preventDefault();
    };
    const popstate = () => {
      if (!guardActive) return;
      guardActive = false;
      if (!callbacksRef.current.hasPendingWork()) return history.back();
      arm();
      requestNavigation("返回上一页", () => history.back());
    };
    const unload = (event: BeforeUnloadEvent) => {
      if (!callbacksRef.current.hasPendingWork()) return;
      callbacksRef.current.persistBeforeUnload?.();
      event.preventDefault(); event.returnValue = "";
    };
    document.addEventListener("click", click, true);
    document.addEventListener("keydown", keydown, true);
    addEventListener(APP_NAVIGATION_REQUEST, programmatic);
    addEventListener("popstate", popstate, true);
    addEventListener("beforeunload", unload);
    return () => {
      document.removeEventListener("click", click, true); document.removeEventListener("keydown", keydown, true);
      removeEventListener(APP_NAVIGATION_REQUEST, programmatic); removeEventListener("popstate", popstate, true); removeEventListener("beforeunload", unload);
      armRef.current = () => undefined; releaseRef.current = (navigate) => navigate();
    };
  }, [requestNavigation]);

  const confirmNavigation = useCallback(async () => {
    const destination = pendingRef.current;
    if (!destination || confirmingRef.current !== null) return;
    confirmingRef.current = destination.id; setNavigationConfirming(true); setNavigationError(null);
    try {
      await callbacksRef.current.saveBeforeLeave();
      if (pendingRef.current?.id !== destination.id || confirmingRef.current !== destination.id) return;
      setPendingNavigation(null);
      releaseRef.current(() => {
        if (completedRef.current.has(destination.id)) return;
        completedRef.current.add(destination.id);
        if (destination.navigate) destination.navigate(); else callbacksRef.current.navigateTo(destination.href);
      });
    } catch {
      setNavigationError("保存没有完成，内容仍保留在当前页面，请重试。");
    } finally {
      if (confirmingRef.current === destination.id) confirmingRef.current = null;
      setNavigationConfirming(false);
    }
  }, [setPendingNavigation]);

  return {
    pendingNavigation, navigationConfirming, navigationError,
    requestNavigation, confirmNavigation,
    cancelNavigation: useCallback(() => { if (!navigationConfirming) setPendingNavigation(null); }, [navigationConfirming, setPendingNavigation]),
    armHistoryGuard: useCallback(() => armRef.current(), []),
  };
}
