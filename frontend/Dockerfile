# Optional: run the dashboard in Docker (the API + browser agent run natively on your Mac).
# Only this folder is the build context: your profile, .env, database and resumes are never part of the image.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build && npm prune --omit=dev

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/.next ./.next
COPY --from=build --chown=node:node /app/next.config.mjs ./
# Read at run time by proxy.ts (server side only): where the API is, and the API token if you set one.
ENV PJA_BACKEND_URL=http://host.docker.internal:8000
USER node
EXPOSE 3000
# 0.0.0.0 inside the container only - docker-compose.yml publishes the port on 127.0.0.1 of your Mac
CMD ["npx", "next", "start", "-p", "3000", "-H", "0.0.0.0"]
