const configuredToken = process.env.CHATBI_COMPOSE_E2E_TOKEN?.trim();

if (!configuredToken) {
  throw new Error(
    "CHATBI_COMPOSE_E2E_TOKEN must be exported by scripts/compose_test_env.sh",
  );
}

export const E2E_TOKEN = configuredToken;
export const AUTH_HEADERS = { Authorization: `Bearer ${E2E_TOKEN}` } as const;
