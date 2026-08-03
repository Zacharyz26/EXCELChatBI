import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(webRoot, "../..");

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["compose-full-stack.spec.ts", "compose-recovery.spec.ts"],
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: "list",
  timeout: 180_000,
  use: {
    baseURL: process.env.CHATBI_COMPOSE_BASE_URL ?? "http://127.0.0.1:8080",
    launchOptions: { args: ["--no-proxy-server"] },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  outputDir: path.join(repositoryRoot, ".data", "e2e", "test-results"),
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
