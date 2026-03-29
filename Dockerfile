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
# Build: Python dependencies
############################
FROM alpine:${ALPINE_VERSION} AS python_deps_builder

RUN apk add --no-cache python3 py3-pip

RUN python3 -m venv /opt/venv

RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir \
        flask \
        flask_socketio \
        flask-wtf \
        requests \
        python-socketio \
        eventlet \
    && rm -f /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.* \
    && rm -rf /opt/venv/lib/python*/site-packages/pip \
              /opt/venv/lib/python*/site-packages/pip-*.dist-info \
              /opt/venv/lib/python*/site-packages/wheel \
              /opt/venv/lib/python*/site-packages/wheel-*.dist-info


############################
# Runtime
############################
FROM alpine:${ALPINE_VERSION}

# Runtime deps:
# - bash/openresolv/iproute2/iptables: required by awg-quick and our iptables scripts
# - ca-certificates: required for external IP/Geo lookups
RUN apk upgrade --no-cache expat zlib \
    && apk add --no-cache \
    python3 \
    nginx \
    supervisor \
    apache2-utils \
    bash \
    iproute2 \
    iptables \
    nftables \
    openresolv \
    ca-certificates \
    && rm -f /usr/lib/python*/ensurepip/_bundled/pip-*.whl \
    && rm -rf /usr/lib/python*/site-packages/setuptools/_vendor/wheel \
              /usr/lib/python*/site-packages/setuptools/_vendor/wheel-*.dist-info

COPY --from=python_deps_builder /opt/venv /opt/venv

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
ENV PATH="/opt/venv/bin:${PATH}"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import os, sys, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"NGINX_PORT\", \"80\")}/status', timeout=10).read(); sys.exit(0)" || exit 1

ENTRYPOINT ["/app/scripts/start.sh"]