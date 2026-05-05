#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_TYPE="${BUILD_TYPE:-Release}"
BUILD_TYPE_LOWER="$(printf '%s' "${BUILD_TYPE}" | tr '[:upper:]' '[:lower:]')"
VERSION="$(sed -n 's/.*constexpr const char\* VERSION = "\(.*\)";.*/\1/p' "${ROOT_DIR}/include/VersionInfo.h" | head -n1)"

MAKE_PLUGIN_BINARY="${ROOT_DIR}/build/macos/${BUILD_TYPE_LOWER}/px4xplane/64/mac.xpl"
CMAKE_PLUGIN_BINARY="${ROOT_DIR}/build/$(uname -s)-x64-${BUILD_TYPE}/mac/${BUILD_TYPE}/px4xplane/64/mac.xpl"
CMAKE_GENERIC_PLUGIN_BINARY="${ROOT_DIR}/build/macos-cmake/mac/${BUILD_TYPE}/px4xplane/64/mac.xpl"
PACKAGE_ROOT="${ROOT_DIR}/build/package"
STAGING_DIR="${PACKAGE_ROOT}/px4xplane"
ZIP_PATH="${ROOT_DIR}/build/sprig-px4xplane-v${VERSION}-macos.zip"

if [[ -f "${MAKE_PLUGIN_BINARY}" ]]; then
  PLUGIN_BINARY="${MAKE_PLUGIN_BINARY}"
elif [[ -f "${CMAKE_PLUGIN_BINARY}" ]]; then
  PLUGIN_BINARY="${CMAKE_PLUGIN_BINARY}"
elif [[ -f "${CMAKE_GENERIC_PLUGIN_BINARY}" ]]; then
  PLUGIN_BINARY="${CMAKE_GENERIC_PLUGIN_BINARY}"
else
  echo "Missing mac.xpl build output." >&2
  echo "Checked:" >&2
  echo "  ${MAKE_PLUGIN_BINARY}" >&2
  echo "  ${CMAKE_PLUGIN_BINARY}" >&2
  echo "  ${CMAKE_GENERIC_PLUGIN_BINARY}" >&2
  echo "Build first with: cmake -S . -B build/macos-cmake -DCMAKE_BUILD_TYPE=${BUILD_TYPE} && cmake --build build/macos-cmake" >&2
  echo "or: make -f Makefile.macos BUILD_TYPE=${BUILD_TYPE}" >&2
  exit 1
fi

rm -rf "${PACKAGE_ROOT}"
mkdir -p "${STAGING_DIR}/64" "${STAGING_DIR}/px4_airframes"

cp "${PLUGIN_BINARY}" "${STAGING_DIR}/64/mac.xpl"
cp "${ROOT_DIR}/config/config.ini" "${STAGING_DIR}/64/config.ini"
cp "${ROOT_DIR}/README.md" "${STAGING_DIR}/README.md"

for airframe in \
  5001_xplane_cessna172 \
  5002_xplane_tb2 \
  5010_xplane_ehang184 \
  5020_xplane_alia250 \
  5021_xplane_qtailsitter; do
  cp "${ROOT_DIR}/config/px4_params/${airframe}" "${STAGING_DIR}/px4_airframes/${airframe}"
done

if [[ -f "${ROOT_DIR}/SPRIG.md" ]]; then
  cp "${ROOT_DIR}/SPRIG.md" "${STAGING_DIR}/SPRIG.md"
fi

rm -f "${ZIP_PATH}"
(
  cd "${PACKAGE_ROOT}"
  zip -qr "${ZIP_PATH}" px4xplane
)

echo "Created ${ZIP_PATH}"
