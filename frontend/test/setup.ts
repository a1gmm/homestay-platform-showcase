import "@ant-design/v5-patch-for-react-19";
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

// antd 组件依赖的浏览器 API，jsdom 未实现
if (typeof window !== "undefined") {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
  if (!window.ResizeObserver) {
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!window.IntersectionObserver) {
    window.IntersectionObserver = class {
      root = null;
      rootMargin = "";
      thresholds = [];
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    } as unknown as typeof IntersectionObserver;
  }
  // Node 26 exposes an experimental global localStorage that can shadow
  // jsdom's storage. Install one deterministic Storage-prototype-backed object
  // so bare/window access and quota/CAS prototype spies all share the same data.
  const storagePrototype = window.Storage.prototype;
  const stores = new WeakMap<Storage, Map<string, string>>();
  const storeFor = (storage: Storage) => {
    let store = stores.get(storage);
    if (!store) {
      store = new Map<string, string>();
      stores.set(storage, store);
    }
    return store;
  };
  Object.defineProperties(storagePrototype, {
    length: {
      configurable: true,
      get: function length(this: Storage) { return storeFor(this).size; },
    },
    clear: {
      configurable: true,
      writable: true,
      value: function clear(this: Storage) { storeFor(this).clear(); },
    },
    getItem: {
      configurable: true,
      writable: true,
      value: function getItem(this: Storage, key: string) {
        const store = storeFor(this);
        return store.has(String(key)) ? store.get(String(key))! : null;
      },
    },
    key: {
      configurable: true,
      writable: true,
      value: function key(this: Storage, index: number) {
        return Array.from(storeFor(this).keys())[index] ?? null;
      },
    },
    removeItem: {
      configurable: true,
      writable: true,
      value: function removeItem(this: Storage, key: string) {
        storeFor(this).delete(String(key));
      },
    },
    setItem: {
      configurable: true,
      writable: true,
      value: function setItem(this: Storage, key: string, value: string) {
        storeFor(this).set(String(key), String(value));
      },
    },
  });
  const storage = Object.create(storagePrototype) as Storage;
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true });
  Object.defineProperty(globalThis, "localStorage", { value: storage, configurable: true });
  Object.defineProperty(globalThis, "Storage", { value: window.Storage, configurable: true });
}
