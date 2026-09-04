"use client";

import { useEffect, type RefObject } from "react";

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])", "select:not([disabled])",
  "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
].join(",");

const inertStates = new Map<HTMLElement, { count: number; inert: boolean; ariaHidden: string | null }>();

export function useModalDialog({
  open,
  dialogRef,
  initialFocusRef,
  onClose,
  closeDisabled = false,
  instanceKey,
}: {
  open: boolean;
  dialogRef: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose(): void;
  closeDisabled?: boolean;
  instanceKey?: string;
}) {
  useEffect(() => {
    if (!open || !dialogRef.current) return;
    const dialog = dialogRef.current;
    const restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const hidden: HTMLElement[] = [];
    let branch: HTMLElement | null = dialog;
    while (branch?.parentElement) {
      for (const sibling of Array.from(branch.parentElement.children)) {
        if (sibling === branch || !(sibling instanceof HTMLElement)) continue;
        const existing = inertStates.get(sibling);
        if (existing) existing.count += 1;
        else inertStates.set(sibling, { count: 1, inert: sibling.hasAttribute("inert"), ariaHidden: sibling.getAttribute("aria-hidden") });
        hidden.push(sibling);
        sibling.setAttribute("inert", "");
        sibling.setAttribute("aria-hidden", "true");
      }
      branch = branch.parentElement;
      if (branch === document.body) break;
    }

    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE))
      .filter((element) => !element.hasAttribute("disabled") && element.getAttribute("aria-hidden") !== "true");
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !closeDisabled) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    queueMicrotask(() => (initialFocusRef?.current || focusable()[0] || dialog).focus());
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      hidden.forEach((element) => {
        const state = inertStates.get(element);
        if (!state) return;
        state.count -= 1;
        if (state.count > 0) return;
        inertStates.delete(element);
        if (!state.inert) element.removeAttribute("inert");
        if (state.ariaHidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", state.ariaHidden);
      });
      queueMicrotask(() => {
        if (!document.querySelector('[role="dialog"][aria-modal="true"]')) {
          inertStates.forEach((state, element) => {
            if (!state.inert) element.removeAttribute("inert");
            if (state.ariaHidden === null) element.removeAttribute("aria-hidden");
            else element.setAttribute("aria-hidden", state.ariaHidden);
          });
          inertStates.clear();
        }
        restoreFocus?.focus();
      });
    };
  }, [closeDisabled, dialogRef, initialFocusRef, instanceKey, onClose, open]);
}
