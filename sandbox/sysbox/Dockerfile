# Builds the sysbox runtime (sysbox-runc, sysbox-mgr, sysbox-fs) from its sources, for the agent's
# Docker engine (agent_docker.mode: sysbox). `pibox up` runs it once in the VM's Docker, with the
# sources (github.com/nestybox/sysbox and its submodules, cloned on the Mac) as the build context,
# and writes the result to ~/.pi-sandbox/generated/sysbox: bin/ and the systemd/sysctl files of
# sysbox's own package. install.sh then installs them into the VM.
# Downloads: Debian packages (repository.apt_debian), Go modules (repository.goproxy, your
# Artifactory's Go remote), through the proxy on your Mac like the other image builds. Nothing is
# downloaded from GitHub here.
ARG GOLANG_IMAGE=golang:1.25-bookworm
FROM ${GOLANG_IMAGE} AS build
ARG APT_DEBIAN=
ARG APT_SECURITY=
RUN set -eux; src=/etc/apt/sources.list.d/debian.sources; \
    if [ -n "$APT_DEBIAN" ]; then sed -i "s#^URIs: http://deb.debian.org/debian\$#URIs: $APT_DEBIAN#" "$src"; fi; \
    if [ -n "$APT_SECURITY" ]; then sed -i "s#^URIs: http://deb.debian.org/debian-security\$#URIs: $APT_SECURITY#" "$src"; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev libseccomp-dev pkg-config make git; \
    rm -rf /var/lib/apt/lists/*
# Go modules (sources) from your repository: repository.goproxy, repository.gosumdb
ARG GOPROXY=https://proxy.golang.org,direct
ARG GOSUMDB=sum.golang.org
ENV GOPROXY=${GOPROXY} GOSUMDB=${GOSUMDB} GOTOOLCHAIN=local
# protoc's Go plugin that still understands the "plugins=grpc" option sysbox-ipc uses
RUN go install github.com/golang/protobuf/protoc-gen-go@v1.5.4
COPY . /src
WORKDIR /src
RUN git config --global --add safe.directory '*' && make sysbox-local HOST_UID=0 HOST_GID=0
RUN set -eux; mkdir -p /out/bin /out/systemd; \
    cp sysbox-runc/build/*/sysbox-runc sysbox-mgr/build/*/sysbox-mgr sysbox-fs/build/*/sysbox-fs /out/bin/; \
    cp sysbox-pkgr/systemd/sysbox.service sysbox-pkgr/systemd/sysbox-mgr.service sysbox-pkgr/systemd/sysbox-fs.service \
       sysbox-pkgr/systemd/99-sysbox-sysctl.conf sysbox-pkgr/systemd/50-sysbox-mod.conf /out/systemd/; \
    /out/bin/sysbox-runc --version | sed -n 's/.*version:[[:space:]]*//p' > /out/VERSION

FROM scratch
COPY --from=build /out/ /
