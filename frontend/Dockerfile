# syntax=docker/dockerfile:1
# Multi-stage: build the Vite bundle with Node, serve it with unprivileged NGINX.
ARG NODE_IMAGE=node:24-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /app
# Optional corporate root CA so an enterprise npm registry/proxy is trusted at build time.
# Empty by default; pass --build-arg EXTRA_CA_CERT="$(cat corp-ca.pem)" on enterprise networks.
ARG EXTRA_CA_CERT=""
RUN mkdir -p /usr/local/share/ca-certificates \
    && touch /usr/local/share/ca-certificates/enterprise.crt \
    && if [ -n "$EXTRA_CA_CERT" ]; then \
         printf '%s\n' "$EXTRA_CA_CERT" > /usr/local/share/ca-certificates/enterprise.crt; \
       fi
ENV NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/enterprise.crt
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
# The unprivileged image already runs as a non-root user (uid 101) and listens on 8080.
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx/default.conf.template /etc/nginx/app.conf.template
COPY nginx/entrypoint.sh /docker-entrypoint.d/99-render-config.sh
USER root
RUN chmod +x /docker-entrypoint.d/99-render-config.sh
USER nginx
ENV API_UPSTREAM=http://api:8000 \
    EXTRA_CONNECT_SRC=""
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["/bin/sh", "-c", "wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1"]
