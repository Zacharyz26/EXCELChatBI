# syntax=docker/dockerfile:1.7

FROM node:20.20.2-alpine AS web-builder

RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
WORKDIR /app
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile
COPY apps/web ./
RUN pnpm build


FROM nginxinc/nginx-unprivileged:1.31.3-alpine3.24 AS web

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web-builder /app/dist /usr/share/nginx/html

USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
