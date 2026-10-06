SUMMARY = "CDI spec exposing the QIMSDK devices (qualcomm.com/device=qimsdk) to containers"
DESCRIPTION = "sdk-tools qimsdk-debian/cdi/qcs9075_qli_2x_no_overlay_qimsdk.json \
@ 513e0a3fe1e67e131fa4f65694eeb07294ce8356 (QCS9075, QLI 2.x, no camera)."
LICENSE = "BSD-3-Clause-Clear"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/BSD-3-Clause-Clear;md5=7a434440b651f4a472ca93716d01033a"

SRC_URI = "file://qimsdk.json file://qimsdk-cdi.conf"
S = "${UNPACKDIR}"

do_install() {
    install -D -m 0644 ${S}/qimsdk.json ${D}${sysconfdir}/cdi/qimsdk.json
    # Host paths the spec bind-mounts: a missing one fails container start.
    install -d -m 0755 ${D}${sysconfdir}/models ${D}${sysconfdir}/labels
    install -d -m 0777 ${D}${sysconfdir}/media ${D}${sysconfdir}/configs
    # The root home is not part of an ostree image; create its dirs at boot.
    install -d ${D}${nonarch_libdir}/tmpfiles.d
    sed 's#@ROOT_HOME@#${ROOT_HOME}#g' ${S}/qimsdk-cdi.conf \
        > ${D}${nonarch_libdir}/tmpfiles.d/qimsdk-cdi.conf
}

FILES:${PN} += "${nonarch_libdir}/tmpfiles.d"
