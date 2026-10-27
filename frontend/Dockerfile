# syntax=docker/dockerfile:1
# DocMind web frontend: the Next.js server (pages + BFF) as a standalone Node app.
#
#   docker build -t docmind-web:local frontend
#   docker run -e DOCMIND_API_URL=http://api:8000 -p 127.0.0.1:3080:3000 docmind-web:local
#
# Dependencies come only from package-lock.json (npm ci, install scripts disabled). No
# secrets and no backend URL are baked in: DOCMIND_API_URL is read by the server at run
# time and never reaches the browser.

ARG NODE_IMAGE=node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-fund --no-audit

FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 NEXT_OUTPUT=standalone
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# next/font downloads the Inter font here (build time only) and serves it from this origin.
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
LABEL org.opencontainers.image.title="docmind-web" \
      org.opencontainers.image.description="DocMind web frontend (Next.js server and BFF)"
WORKDIR /app
# Root-owned, read-only for the runtime user. The standalone folder carries the server and
# the traced node_modules; static assets are copied next to it so server.js serves them.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# HOSTNAME: Docker sets it to the container id, and server.js listens on $HOSTNAME.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
# Exec form: Node receives SIGTERM and Next.js drains in-flight requests before exiting.
CMD ["node", "server.js"]
