export const APP_NAVIGATION_REQUEST = "app:navigation-request";

export interface AppNavigationRequestDetail {
  href: string;
  navigate: () => void;
}

export function requestAppNavigation(href: string, navigate: () => void): boolean {
  const event = new CustomEvent<AppNavigationRequestDetail>(APP_NAVIGATION_REQUEST, {
    cancelable: true,
    detail: { href, navigate },
  });
  const allowed = window.dispatchEvent(event);
  if (allowed) navigate();
  return allowed;
}
