import { defineConfig, devices } from "@playwright/test";

// 某些开发/CI 环境全局注入 HTTP(S)_PROXY；本地 Vite 就绪探测必须绕过代理。
for (const key of ["NO_PROXY", "no_proxy"]) {
  const values = new Set((process.env[key] ?? "").split(",").filter(Boolean));
  values.add("localhost");
  values.add("127.0.0.1");
  process.env[key] = [...values].join(",");
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : "list",
  use: {
    baseURL: "http://localhost:4173",
    launchOptions: { args: ["--no-proxy-server"] },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "pnpm dev --host 127.0.0.1 --port 4173",
    // localhost 会遵循常见 NO_PROXY 配置；直接写 127.0.0.1 在公司代理环境可能被转发成 502。
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
