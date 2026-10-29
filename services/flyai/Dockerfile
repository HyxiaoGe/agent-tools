# syntax=docker/dockerfile:1.7

FROM node:22.22.0-bookworm-slim AS dependencies

WORKDIR /app
COPY package.json package-lock.json ./
# 第三方 CLI 锁定版本与完整性哈希，防止依赖被替换。
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev --ignore-scripts \
    && node -e "const p=require('./package-lock.json').packages['node_modules/@fly-ai/flyai-cli'];if(p.version!=='1.0.16'||p.integrity!=='sha512-Ksi06xvJSJcdhmfoDbpAnA84K4/pF+SkLsa5ZLvNruUc3e2EpoGaI6FcJXKTODvB/evprfy5pdRzW3lVazqfkA==')process.exit(1)"

FROM node:22.22.0-bookworm-slim AS test

ENV NODE_ENV=production
WORKDIR /app
COPY --from=dependencies --chown=node:node /app/node_modules ./node_modules
COPY --chown=node:node package.json ./package.json
COPY --chown=node:node src ./src
COPY --chown=node:node test ./test
USER node
RUN npm test

# 最后一个阶段是运行镜像：流水线 docker build 不带 --target 时得到的就是它。
FROM node:22.22.0-bookworm-slim AS production

ENV NODE_ENV=production \
    HOME=/tmp/flyai-home \
    HOST=0.0.0.0 \
    PORT=8093

WORKDIR /app
COPY --from=dependencies --chown=node:node /app/node_modules ./node_modules
COPY --chown=node:node package.json ./package.json
COPY --chown=node:node src ./src

USER node
EXPOSE 8093

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:8093/health',{signal:AbortSignal.timeout(2000)}).then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]

CMD ["node", "src/index.js"]
