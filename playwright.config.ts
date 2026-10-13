import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/ui",
  timeout: 20000,
  fullyParallel: true,
  use: {
    channel: process.env.DUO_UI_BROWSER_CHANNEL || undefined,
    baseURL: "http://127.0.0.1:1438",
    viewport: { width: 1180, height: 800 },
    reducedMotion: "reduce",
  },
  webServer: {
    command:
      process.env.DUO_UI_FIXTURE_ONLY === "1"
        ? "pnpm dev --port 1438"
        : "pnpm dev:web --no-open --port 1438 --data-dir artifacts/ui-workspace",
    url: "http://127.0.0.1:1438",
    reuseExistingServer: false,
    timeout: 120000,
  },
  outputDir: "artifacts/ui-tests",
  reporter: "list",
});
