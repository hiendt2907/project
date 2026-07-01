# Provider Portal — release artifact RIÊNG. Build context = ui/ (workspace monorepo).
#   docker build -t aoip-provider-web:latest -f apps/provider-portal/Dockerfile .
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/provider-portal/package.json apps/provider-portal/package.json
COPY apps/tenant-portal/package.json apps/tenant-portal/package.json
COPY packages ./packages
RUN npm ci

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY packages ./packages
COPY apps/provider-portal ./apps/provider-portal
# rewrites() được "bake" lúc build → backend URL phải đúng service cluster tại đây.
ARG AOIP_BACKEND_URL=http://aoip-provider-portal:8081
ENV AOIP_BACKEND_URL=$AOIP_BACKEND_URL
RUN npm run build -w @aoip/provider-portal

FROM node:20-alpine AS run
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3001
RUN addgroup -S nodegrp && adduser -S nodeusr -G nodegrp
# Next standalone giữ nguyên cấu trúc workspace (apps/provider-portal/server.js).
COPY --from=build --chown=nodeusr:nodegrp /app/apps/provider-portal/.next/standalone ./
COPY --from=build --chown=nodeusr:nodegrp /app/apps/provider-portal/.next/static ./apps/provider-portal/.next/static
USER nodeusr
EXPOSE 3001
CMD ["node", "apps/provider-portal/server.js"]
