# ClipForge web image: build the SPA, serve it from nginx, proxy the API.
#
# The browser only ever talks to one origin. nginx serves the built assets and
# forwards /api, /ws and /healthz to the API container, which is what keeps the
# HttpOnly session cookie working without CORS exceptions.
FROM node:20-alpine AS build

WORKDIR /web

# Dependency layer first for better caching.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite compiles the SPA; API traffic stays relative so no build-time host is baked in.
ARG VITE_DEV_API_TARGET=""
RUN npm run build


FROM nginx:1.27-alpine AS runtime

RUN apk add --no-cache curl

COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
