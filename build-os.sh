#!/usr/bin/env bash
set -euo pipefail

cd $(dirname $(readlink -f $0))

action="build"
builddir=`pwd`/build
dl_dir="${builddir}/downloads"
kas_workdir="${builddir}/work"
kasfile=""
sstate_dir="${builddir}/sstate-cache"
target=qcom-multimedia-proprietary-image

help()
{
    echo "Usage parameters:"
    echo "(Required)"
    echo "--kasfile: initial kas yaml file"
    echo "(Optional)"
    echo "--action: build|shell (default: ${action})"
    echo "--builddir: location for build files (default: ${builddir})"
    echo "--dl-dir: location for Yocto/OE downloads (default: ${dl_dir})"
    echo "--kas-workdir: location for kas layers (default: ${kas_workdir})"
    echo "--sstate-dir: location for Yocto/OE sstate cache (default: ${sstate_dir})"
    echo "--target: Yocto/OE image (default: ${target})"
}

parse_args()
{
    while [ $# -gt 0 ]
    do
        case $1 in
        --action)
            action="$2"
            shift
            shift
            ;;
        --builddir)
            builddir="$2"
            shift
            shift
            ;;
        --dl-dir)
            dl_dir="$2"
            shift
            shift
            ;;
        --kasfile)
            kasfile="$2"
            shift
            shift
            ;;
        --kas-workdir)
            kas_workdir="$2"
            shift
            shift
            ;;
        --sstate-dir)
            sstate_dir="$2"
            shift
            shift
            ;;
        --target)
            target="$2"
            shift
            shift
            ;;
        --help)
            help
            exit 0
            ;;
        *)
            shift
            ;;
        esac
    done
}

parse_args "$@"

export DL_DIR="${dl_dir}"
export KAS_WORK_DIR="${kas_workdir}"
export SSTATE_DIR="${sstate_dir}"
export KAS_TARGET="${target}"

mkdir -p "${builddir}" "${DL_DIR}" "${SSTATE_DIR}" "${KAS_WORK_DIR}"

if ! command -v kas-container >/dev/null 2>&1; then
    echo "Error: kas-container is not installed." >&2
    exit 1
fi

if [ -z "${kasfile}" ] ; then
    echo "ERROR: --kasfile not set" >&2
    exit 1
fi

if [ ! -f "${kasfile}" ] ; then
    echo "ERROR: No such file ${kasfile}" >&2
    exit 1
fi

echo "Settings summary:"
echo "action     : ${action}"
echo "builddir   : ${builddir}"
echo "dl-dir     : ${DL_DIR}"
echo "sstate-dir : ${SSTATE_DIR}"
echo "kas-workdir: ${KAS_WORK_DIR}"
echo "kasfile    : ${kasfile}"
echo "target     : ${KAS_TARGET}"
echo ""

exec kas-container "${action}" "${kasfile}"
