ARG IMAGE_REGISTRY=docker.io
FROM ${IMAGE_REGISTRY}/library/node:20-bookworm-slim AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/product/package.json apps/product/package.json
COPY apps/console/package.json apps/console/package.json
COPY packages/shared/package.json packages/shared/package.json
RUN npm ci

FROM dependencies AS build
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
ARG REHAB_BACKEND_BASE=http://localhost:8000
ENV REHAB_BACKEND_BASE=${REHAB_BACKEND_BASE} \
    NEXT_PUBLIC_API_BASE=${REHAB_BACKEND_BASE}
RUN npm run build:product

FROM ${IMAGE_REGISTRY}/library/node:20-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "run", "start", "--workspace", "@rehab/product"]
