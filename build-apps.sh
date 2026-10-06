#!/usr/bin/env bash
# Build qip-gesture as an OCI image tarball for stage-app.py, in three buildx
# steps on a docker-container builder:
#   1. imsdk-builder (x86 host image; cross-compiles to arm64)
#   2. imsdk-runtime (arm64) FROM imsdk-builder
#   3. qip-gesture   (arm64) FROM imsdk-runtime, compiled in imsdk-builder
# Steps 1-2 also load into the local docker store. Each step writes an OCI
# layout that the next one consumes as a named build context, because a
# docker-container builder cannot see images in the local store.
#
# Build context for 1-2 = apps/qip-gesture/sdk-tools/qimsdk-debian: upstream
# sdk-tools at the pinned commit plus apps/qip-gesture/sdk-tools-patches (the
# Dockerfiles need its scripts/, cmake/, g-ir-scripts/, entrypoint.sh,
# .bash_aliases). Upstream documents 64 GB RAM for the TFLite compile in
# step 1.
set -euo pipefail

SDK_TOOLS_URL=${SDK_TOOLS_URL:-https://git.codelinaro.org/clo/le/sdk-tools.git}
SDK_TOOLS_BRANCH=${SDK_TOOLS_BRANCH:-imsdk-tools.lnx.1.0}
SDK_TOOLS_COMMIT=${SDK_TOOLS_COMMIT:-513e0a3fe1e67e131fa4f65694eeb07294ce8356}
BUILDER_TAG=${BUILDER_TAG:-imsdk-builder:qairt2.47.0-imsdk1.0.2}
RUNTIME_TAG=${RUNTIME_TAG:-imsdk-runtime:latest}
# Must match the image: in apps/qip-gesture/docker-compose.yml.
QIP_GESTURE_TAG=${QIP_GESTURE_TAG:-qip.local/qip-gesture:v1}
BUILDX_BUILDER=${BUILDX_BUILDER:-host-builder}

ROOT=$(cd "$(dirname "$0")" && pwd)
APP="$ROOT/apps/qip-gesture"
WORK=${WORK:-$APP/sdk-tools}
# Pin args + deb version bump; without the bump the patched GStreamer -base
# deb never lands in the runtime.
PATCH_DIR=${PATCH_DIR:-$APP/sdk-tools-patches}
OCI_DIR=${OCI_DIR:-$ROOT/build/oci}
OUT=${OUT:-$ROOT/images/qip-gesture.oci.tar}

driver=$(docker buildx inspect "$BUILDX_BUILDER" 2>/dev/null | awk '/^Driver:/{print $2}' || true)
if [ "$driver" != "docker-container" ]; then
  echo "buildx builder '$BUILDX_BUILDER' must use the docker-container driver" \
       "(OCI export); create one with: docker buildx create --name $BUILDX_BUILDER" \
       "--driver docker-container" >&2
  exit 1
fi
# Pin and patch only on first checkout; an existing checkout is used as-is so
# local sdk-tools edits survive. Delete it to start over from the pin.
if [ ! -d "$WORK/.git" ]; then
  git clone -b "$SDK_TOOLS_BRANCH" "$SDK_TOOLS_URL" "$WORK"
  git -C "$WORK" checkout --detach "$SDK_TOOLS_COMMIT"
  for p in "$PATCH_DIR"/*.patch; do
    git -C "$WORK" apply "$p"
  done
fi

CTX="$WORK/qimsdk-debian"
BUILD=(docker buildx build --builder "$BUILDX_BUILDER")
# oci-layout://<dir>:<tag> selects the image by its ref.name annotation.
BUILDER_CTX="$BUILDER_TAG=oci-layout://$OCI_DIR/imsdk-builder:${BUILDER_TAG##*:}"
RUNTIME_CTX="$RUNTIME_TAG=oci-layout://$OCI_DIR/imsdk-runtime:${RUNTIME_TAG##*:}"
mkdir -p "$OCI_DIR" "$(dirname "$OUT")"

echo "== 1/3: imsdk-builder ($BUILDER_TAG) =="
"${BUILD[@]}" -f "$APP/Dockerfile.builder" \
  --output "type=docker,name=$BUILDER_TAG" \
  --output "type=oci,dest=$OCI_DIR/imsdk-builder,tar=false,name=$BUILDER_TAG" \
  "$CTX"

echo "== 2/3: imsdk-runtime ($RUNTIME_TAG) from $BUILDER_TAG =="
"${BUILD[@]}" -f "$APP/Dockerfile.runtime" --target imsdk_runtime_arm64 \
  --build-arg IMSDK_BUILDER_IMAGE="$BUILDER_TAG" \
  --build-context "$BUILDER_CTX" \
  --output "type=docker,name=$RUNTIME_TAG" \
  --output "type=oci,dest=$OCI_DIR/imsdk-runtime,tar=false,name=$RUNTIME_TAG" \
  "$CTX"

echo "== 3/3: qip-gesture ($QIP_GESTURE_TAG) -> $OUT =="
"${BUILD[@]}" --platform linux/arm64 -f "$APP/Dockerfile" \
  --build-arg IMSDK_BUILDER_IMAGE="$BUILDER_TAG" \
  --build-arg IMSDK_RUNTIME_IMAGE="$RUNTIME_TAG" \
  --build-context "$BUILDER_CTX" \
  --build-context "$RUNTIME_CTX" \
  --output "type=oci,dest=$OUT,name=$QIP_GESTURE_TAG" \
  "$APP"

echo "stage with: stage-app.py --image $QIP_GESTURE_TAG=$OUT ..."
