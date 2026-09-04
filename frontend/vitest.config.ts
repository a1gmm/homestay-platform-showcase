import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: "unit",
          environment: "node",
          include: ["**/*.test.ts"],
        },
      },
      {
        extends: true,
        test: {
          name: "components",
          environment: "jsdom",
          include: ["**/*.test.tsx"],
          setupFiles: ["./test/setup.ts"],
          // Ant Design + jsdom component suites are memory-heavy. Running test
          // files concurrently causes deterministic 5s userEvent timeouts on
          // otherwise passing cases, so keep this project file-serial in CI.
          fileParallelism: false,
        },
      },
    ],
  },
});
