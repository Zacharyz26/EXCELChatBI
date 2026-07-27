import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(webRoot, "../..");
const dataRoot = path.join(repositoryRoot, ".data", "e2e");
const apiPort = "18100";
const webPort = "4174";
const e2eToken = "chatbi-fullstack-e2e-token-00000001";

for (const key of ["NO_PROXY", "no_proxy"]) {
  const values = new Set((process.env[key] ?? "").split(",").filter(Boolean));
  values.add("localhost");
  values.add("127.0.0.1");
  process.env[key] = [...values].join(",");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["full-stack.spec.ts"],
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: "list",
  timeout: 60_000,
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
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
  webServer: [
    {
      command: (
        ".venv/bin/python scripts/prepare_fullstack_e2e.py"
        + ` && .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port ${apiPort}`
      ),
      cwd: repositoryRoot,
      env: {
        ...process.env,
        APP_ENV: "development",
        AUTH_MODE: "bearer",
        AUTH_TOKENS_JSON: JSON.stringify({
          [e2eToken]: {
            user_id: "e2e-user",
            tenant_id: "e2e-tenant",
            roles: ["kb_admin"],
          },
        }),
        MODEL_REGISTRY_PATH: path.join(repositoryRoot, "config", "models.example.yaml"),
        CHAT_DB_PATH: path.join(dataRoot, "chatbi.db"),
        UPLOAD_DIR: path.join(dataRoot, "uploads"),
        DATASET_DIR: path.join(dataRoot, "datasets"),
        REPORT_DIR: path.join(dataRoot, "reports"),
        KB_INDEX_DIR: path.join(dataRoot, "kb-index"),
        KB_BACKUP_DIR: path.join(dataRoot, "kb-backups"),
        RAG_EMBEDDER: "hashing",
        RAG_RERANKER: "lexical",
        RAG_STORE: "local",
      },
      url: `http://127.0.0.1:${apiPort}/health/ready`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `pnpm dev --host 127.0.0.1 --port ${webPort}`,
      cwd: webRoot,
      env: {
        ...process.env,
        VITE_API_PROXY_TARGET: `http://127.0.0.1:${apiPort}`,
      },
      url: `http://127.0.0.1:${webPort}/`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
