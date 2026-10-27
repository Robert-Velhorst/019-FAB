FROM node:26.12.1-alpine3.24@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS build

WORKDIR /app
RUN test "$(node --version)" = "v26.12.1" \
    && grep -qx 'VERSION_ID=3.24.3' /etc/os-release \
    && npm install --global npm@12.2.1 --no-audit --no-fund \
    && npm install --global pnpm@12.15.0 --no-audit --no-fund \
    && test "$(pnpm --version)" = "12.15.0"

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY patches ./patches
COPY vendor/tailwindcss-typography ./vendor/tailwindcss-typography
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM node:26.12.1-alpine3.24@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797

RUN test "$(node --version)" = "v26.12.1" \
    && grep -qx 'VERSION_ID=3.24.3' /etc/os-release \
    && rm -rf /usr/local/lib/node_modules/npm /opt/yarn-* \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/yarn /usr/local/bin/yarnpkg

ENV NODE_ENV=production
WORKDIR /app

COPY --from=build --chown=node:node /app/package.json ./package.json
COPY --from=build --chown=node:node /app/dist ./dist
COPY --from=build --chown=node:node /app/scripts/production-env.mjs ./scripts/production-env.mjs

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/api/fab/runtime').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "dist/fab-standalone.js"]
