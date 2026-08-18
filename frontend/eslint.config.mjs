import { defineConfig } from "eslint/config";
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";

export default defineConfig([
  ...nextCoreWebVitals,
  {
    // Next 16 enables stricter React Compiler-oriented rules. The existing
    // application is not compiled with React Compiler, so keep the previous
    // lint contract while the related state-management code is migrated.
    rules: {
      "react-hooks/immutability": "off",
      "react-hooks/purity": "off",
      "react-hooks/set-state-in-effect": "off",
    },
  },
]);
