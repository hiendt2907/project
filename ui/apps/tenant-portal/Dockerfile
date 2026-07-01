# Tenant Portal — release artifact RIÊNG. Build context = ui/ (workspace monorepo).
#   docker build -t aoip-tenant-web:latest -f apps/tenant-portal/Dockerfile .
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
COPY apps/tenant-portal ./apps/tenant-portal
# rewrites() được "bake" lúc build → backend URL phải đúng service cluster tại đây.
ARG AOIP_BACKEND_URL=http://aoip-tenant-portal:8082
ENV AOIP_BACKEND_URL=$AOIP_BACKEND_URL
RUN npm run build -w @aoip/tenant-portal

FROM node:20-alpine AS run
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3002
RUN addgroup -S nodegrp && adduser -S nodeusr -G nodegrp
COPY --from=build --chown=nodeusr:nodegrp /app/apps/tenant-portal/.next/standalone ./
COPY --from=build --chown=nodeusr:nodegrp /app/apps/tenant-portal/.next/static ./apps/tenant-portal/.next/static
USER nodeusr
EXPOSE 3002
CMD ["node", "apps/tenant-portal/server.js"]
