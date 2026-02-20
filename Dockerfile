ARG ALPINE_VERSION=latest
ARG GO_VERSION=1.26

############################
# Build: amneziawg-go
############################
FROM golang:${GO_VERSION}-alpine AS awg_go_builder

# Pin by default for reproducibility; override with --build-arg AWG_GO_REF=...
ARG AWG_GO_REF=449d7cf

RUN apk add --no-cache git make build-base

WORKDIR /src/amneziawg-go
RUN git clone https://github.com/amnezia-vpn/amneziawg-go.git . \
    && git checkout "${AWG_GO_REF}" \
    && go get golang.org/x/crypto@v0.47.0 \
    && go mod tidy \
    && make

RUN install -Dm755 ./amneziawg-go /out/usr/bin/amneziawg-go


############################
# Build: amneziawg-tools
############################
FROM alpine:${ALPINE_VERSION} AS awg_tools_builder

# Latest release at the time of writing; override with --build-arg AWG_TOOLS_REF=...
ARG AWG_TOOLS_REF=v1.0.20250903

RUN apk add --no-cache git make build-base bash linux-headers

WORKDIR /src/amneziawg-tools
RUN git clone https://github.com/amnezia-vpn/amneziawg-tools.git . \
    && git checkout "${AWG_TOOLS_REF}" \
    && make -C src \
    && make -C src install \
        PREFIX=/usr \
        DESTDIR=/out \
        WITH_WGQUICK=yes \
        WITH_BASHCOMPLETION=no \
        WITH_SYSTEMDUNITS=no


############################
# Runtime
############################
FROM alpine:${ALPINE_VERSION}

# Runtime deps:
# - bash/openresolv/iproute2/iptables: required by awg-quick and our iptables scripts
# - ca-certificates/curl: healthcheck + external IP/Geo lookups
RUN apk add --no-cache \
    python3 \
    py3-pip \
    nginx \
    supervisor \
    curl \
    apache2-utils \
    bash \
    iproute2 \
    iptables \
    nftables \
    openresolv \
    ca-certificates

RUN pip3 install --no-cache-dir --break-system-packages \
        flask \
        flask_socketio \
        flask-wtf \
        requests \
        python-socketio \
        eventlet \
    && apk del --no-network py3-pip py3-wheel py3-setuptools || true \
    && rm -f /usr/lib/python*/ensurepip/_bundled/wheel-*.whl \
    && rm -rf /usr/lib/python*/site-packages/setuptools/_vendor/wheel \
              /usr/lib/python*/site-packages/setuptools/_vendor/wheel-*.dist-info

# Install AmneziaWG components built from source
COPY --from=awg_go_builder /out/usr/bin/amneziawg-go /usr/bin/amneziawg-go
COPY --from=awg_tools_builder /out/usr/ /usr/

# Compatibility: many scripts (and this Web UI) expect standard wg/wg-quick names.
# amneziawg-tools provides awg/awg-quick; expose wg/wg-quick as symlinks.
RUN ln -sf /usr/bin/awg /usr/bin/wg \
    && ln -sf /usr/bin/awg-quick /usr/bin/wg-quick

RUN mkdir -p /app/web-ui /var/log/supervisor /var/log/webui /var/log/amnezia /var/log/nginx /etc/amnezia/amneziawg /run/nginx

COPY web-ui /app/web-ui/

COPY config/nginx.conf /etc/nginx/http.d/default.conf
COPY config/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh

# Optional: wrapper that redirects amneziawg-go stdout/stderr to a log file.
# Enabled by setting AWG_LOG_LEVEL (see scripts/start.sh).
COPY scripts/amneziawg-go-logged.sh /usr/local/bin/amneziawg-go-logged
RUN chmod +x /usr/local/bin/amneziawg-go-logged

# Expose default ports
EXPOSE 80
EXPOSE 51820/udp

ENV NGINX_PORT=80

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:$NGINX_PORT/status || exit 1

ENTRYPOINT ["/app/scripts/start.sh"]