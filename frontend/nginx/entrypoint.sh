#!/bin/sh
set -eu

: "${API_UPSTREAM:=http://api:8000}"
: "${EXTRA_CONNECT_SRC:=}"

# Render only our own placeholders and leave NGINX's own $variables intact.
envsubst '${API_UPSTREAM} ${EXTRA_CONNECT_SRC}' \
    < /etc/nginx/app.conf.template \
    > /etc/nginx/conf.d/default.conf
