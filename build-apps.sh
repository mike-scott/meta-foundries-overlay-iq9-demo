#!/usr/bin/env bash
# Build the apps' images as OCI tarballs (images/<app>.oci.tar) for
# stage-app.py, on a docker-container buildx builder.
#
# qip-gesture's base images are chained through OCI layouts in build/oci:
# a docker-container builder cannot see images in the local docker store.
# apps/qip-gesture/sdk-tools is cloned at the pin and patched only when
# missing, so local edits survive; delete it to start over.
set -euo pipefail
cd "$(dirname "$0")"

BUILDER=${BUILDX_BUILDER:-host-builder}
docker buildx inspect "$BUILDER" | grep -q '^Driver: *docker-container' ||
  { echo "buildx builder '$BUILDER' must use the docker-container driver" >&2; exit 1; }

APP=apps/qip-gesture
CTX=$APP/sdk-tools/qimsdk-debian
OCI=$PWD/build/oci
BLD=imsdk-builder:qairt2.47.0-imsdk1.0.2
RT=imsdk-runtime:latest

mkdir -p images "$OCI"
# Each build is skipped when its output exists; delete the output to rebuild.

# shellhttpd (arm64)
if [ ! -e images/shellhttpd.oci.tar ]; then
  docker buildx build --builder "$BUILDER" --platform linux/arm64 --provenance=false --sbom=false \
    --output type=oci,dest=images/shellhttpd.oci.tar,name=local-server/shellhttpd:latest \
    apps/shellhttpd
fi

if [ ! -d $APP/sdk-tools ]; then
  git clone -b imsdk-tools.lnx.1.0 https://git.codelinaro.org/clo/le/sdk-tools.git $APP/sdk-tools
  git -C $APP/sdk-tools checkout --detach 513e0a3fe1e67e131fa4f65694eeb07294ce8356
  git -C $APP/sdk-tools apply "$PWD/$APP"/sdk-tools-patches/*.patch
fi

# imsdk-builder (x86 host image; cross-compiles to arm64)
if [ ! -e "$OCI/imsdk-builder/index.json" ]; then
  docker buildx build --builder "$BUILDER" -f $APP/Dockerfile.builder \
    --output type=docker,name=$BLD \
    --output type=oci,dest="$OCI/imsdk-builder",tar=false,name=$BLD \
    $CTX
fi

# imsdk-runtime (arm64) FROM imsdk-builder
if [ ! -e "$OCI/imsdk-runtime/index.json" ]; then
  docker buildx build --builder "$BUILDER" -f $APP/Dockerfile.runtime --target imsdk_runtime_arm64 \
    --build-arg IMSDK_BUILDER_IMAGE=$BLD \
    --build-context $BLD=oci-layout://"$OCI/imsdk-builder":${BLD#*:} \
    --output type=docker,name=$RT \
    --output type=oci,dest="$OCI/imsdk-runtime",tar=false,name=$RT \
    $CTX
fi

# qip-gesture (arm64) FROM imsdk-runtime, compiled in imsdk-builder
if [ ! -e images/qip-gesture.oci.tar ]; then
  docker buildx build --builder "$BUILDER" --platform linux/arm64 -f $APP/Dockerfile \
    --build-arg IMSDK_BUILDER_IMAGE=$BLD \
    --build-arg IMSDK_RUNTIME_IMAGE=$RT \
    --build-context $BLD=oci-layout://"$OCI/imsdk-builder":${BLD#*:} \
    --build-context $RT=oci-layout://"$OCI/imsdk-runtime":${RT#*:} \
    --output type=oci,dest=images/qip-gesture.oci.tar,name=qip.local/qip-gesture:v1 \
    $APP
fi
