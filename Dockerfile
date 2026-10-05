# Pinned so rebuilding an old release cannot jump an Alpine major; bump deliberately.
ARG ALPINE_VERSION=3.24
ARG GO_VERSION=1.26.8

############################
# Build: amneziawg-go
############################
FROM golang:${GO_VERSION}-alpine AS awg_go_builder

# Pin by default for reproducibility; override with --build-arg AWG_GO_REF=...
ARG AWG_GO_REF=v3.1.20260828

RUN apk add --no-cache git make build-base

WORKDIR /src/amneziawg-go
# The x/* pins must be >= what upstream go.mod requires: Go's minimal version
# selection takes the max of both, so pinning below upstream is a no-op.
RUN git clone https://github.com/amnezia-vpn/amneziawg-go.git . \
    && git checkout "${AWG_GO_REF}" \
    && go get golang.org/x/crypto@v0.56.0 \
    && go get golang.org/x/net@v0.57.0 \
    && go get golang.org/x/sys@v0.47.0 \
    && go mod tidy \
    && make

RUN install -Dm755 ./amneziawg-go /out/usr/bin/amneziawg-go


############################
# Build: amneziawg-tools
############################
FROM alpine:${ALPINE_VERSION} AS awg_tools_builder

# Must match the AmneziaWG protocol generation built above: AWG 3.0 changed the
# UAPI wire format (range-valued keepalive, header protection, timings), so v1.x
# tools cannot configure a v3 daemon. Override with --build-arg AWG_TOOLS_REF=...
ARG AWG_TOOLS_REF=v3.1.20260812

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

# Versions are pinned in web-ui/requirements.txt so builds are reproducible.
COPY web-ui/requirements.txt /tmp/requirements.txt

# pip/wheel/setuptools are build-time only: the app imports none of them, so they
# are removed to keep them out of the runtime image (and out of CVE scans).
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && rm -f /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.* \
    && rm -rf /opt/venv/lib/python*/site-packages/pip \
              /opt/venv/lib/python*/site-packages/pip-*.dist-info \
              /opt/venv/lib/python*/site-packages/wheel \
              /opt/venv/lib/python*/site-packages/wheel-*.dist-info \
              /opt/venv/lib/python*/site-packages/setuptools \
              /opt/venv/lib/python*/site-packages/setuptools-*.dist-info \
              /opt/venv/lib/python*/site-packages/pkg_resources


############################
# Build: Tailwind CSS
############################
# Tailwind's standalone CLI (statically linked, no Node), downloaded and
# checksum-verified by build_css.sh. Runs on the build platform: the output is plain
# CSS, the same for every target architecture.
FROM --platform=$BUILDPLATFORM alpine:${ALPINE_VERSION} AS css_builder

RUN apk add --no-cache bash curl

WORKDIR /src
COPY build_css.sh tailwind.config.js tailwind.input.css ./
COPY web-ui/templates web-ui/templates
COPY web-ui/static/js web-ui/static/js
RUN ./build_css.sh


############################
# Runtime
############################
# Named so publish_dockerhub.sh can rebuild just this stage (--no-cache-filter runtime).
FROM alpine:${ALPINE_VERSION} AS runtime

# apk upgrade: every Alpine fix released since the base image was built.
# Runtime deps:
# - bash/openresolv/iproute2/iptables: required by awg-quick and our iptables scripts
# - ca-certificates: required for external IP/Geo lookups
# - openssl: generates the nginx Basic Auth hash in scripts/start.sh. Replaces
#   apache2-utils/htpasswd, which pulled in apr-util (CVE-2026-34191,
#   CVE-2026-32327, both critical and unfixed in Alpine as of 3.24).
#   Since 2.4 the hash is SHA-512 crypt (`openssl passwd -6`), which musl's crypt
#   and so nginx read; the panel's settings drawer writes the same format.
RUN apk upgrade --no-cache \
    && apk add --no-cache \
    python3 \
    nginx \
    supervisor \
    openssl \
    bash \
    iproute2 \
    iptables \
    openresolv \
    ca-certificates \
    && rm -f /usr/lib/python*/ensurepip/_bundled/pip-*.whl \
    && rm -rf /usr/lib/python*/site-packages/setuptools \
              /usr/lib/python*/site-packages/setuptools-*.dist-info \
              /usr/lib/python*/site-packages/pkg_resources

COPY --from=python_deps_builder /opt/venv /opt/venv

# Install AmneziaWG components built from source
COPY --from=awg_go_builder /out/usr/bin/amneziawg-go /usr/bin/amneziawg-go
COPY --from=awg_tools_builder /out/usr/ /usr/

# Compatibility: many scripts (and this Web UI) expect standard wg/wg-quick names.
# amneziawg-tools provides awg/awg-quick; expose wg/wg-quick as symlinks.
RUN ln -sf /usr/bin/awg /usr/bin/wg \
    && ln -sf /usr/bin/awg-quick /usr/bin/wg-quick

RUN mkdir -p /app/web-ui /var/log/amnezia /var/log/nginx /etc/amnezia/amneziawg /run/nginx

COPY web-ui /app/web-ui/
COPY --from=css_builder /src/web-ui/static/css/tailwind.css /app/web-ui/static/css/tailwind.css

# Build label shown under the page heading. run.sh and publish_dockerhub.sh pass it
# (version from git tags, see version.sh); a plain `docker build` gets none and the
# page shows "dev". Declared late so a new label only rebuilds this layer.
ARG BUILD_LABEL=""
RUN if [ -n "${BUILD_LABEL}" ]; then printf '%s\n' "${BUILD_LABEL}" > /app/web-ui/BUILD; fi

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

# Health check: /status answers 503 when a server that should run is down (after a
# grace period from boot). http.client, unlike urlopen, does not raise on a 503, so the
# body naming the server is printed into the health log (docker inspect).
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import os, sys, http.client; c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('NGINX_PORT', '80')), timeout=10); c.request('GET', '/status'); r = c.getresponse(); print(r.read().decode()); sys.exit(r.status != 200)" || exit 1

ENTRYPOINT ["/app/scripts/start.sh"]