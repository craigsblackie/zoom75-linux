#!/usr/bin/env bash
#
# Installs the Zoom75 screen daemon as a root-owned system service.
#
# Security model:
#   * Code lives in /opt/zoom75-screen owned by root:root. It runs as root, so
#     it must not be writable by an unprivileged user -- otherwise editing it
#     would be a local privilege escalation.
#   * Config lives in /etc/zoom75/config.toml, root-owned. Changing what the
#     screen shows therefore requires sudo.
#   * NO udev rule is installed. The daemon opens /dev/hidraw* as root and
#     those nodes stay crw------- root root, so no unprivileged process can
#     read the keyboard's input interfaces. If a previous version of this
#     project installed a udev rule, this script removes it.
#   * The unit itself is sandboxed (see systemd/zoom75-screen.service):
#     all capabilities dropped, read-only filesystem, /dev restricted to
#     hidraw, syscall filter, egress limited.
#
# Usage:  sudo ./install-service.sh
#         sudo ./install-service.sh --uninstall

set -euo pipefail

PREFIX=/opt/zoom75-screen
CONFDIR=/etc/zoom75
UNIT=/etc/systemd/system/zoom75-screen.service
OLD_UDEV=/etc/udev/rules.d/99-zoom75.rules
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "This must run as root: sudo $0" >&2
    exit 1
fi

uninstall() {
    echo "==> stopping and disabling the service"
    systemctl disable --now zoom75-screen.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    echo "==> left $CONFDIR in place (delete it yourself if you want it gone)"
    echo "Done."
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

echo "==> source: $SRC"
[[ -d "$SRC/zoom75" ]] || { echo "zoom75/ package not found next to this script" >&2; exit 1; }

# A root-run daemon must not load code from a directory others can write to.
if [[ -n "$(find "$SRC/zoom75" -perm -o+w -print -quit)" ]]; then
    echo "Refusing: $SRC/zoom75 contains world-writable files." >&2
    exit 1
fi

if [[ -e "$OLD_UDEV" ]]; then
    echo "==> removing the old udev rule ($OLD_UDEV); the daemon does not need it"
    rm -f "$OLD_UDEV"
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=hidraw || true
fi

echo "==> installing code to $PREFIX"
rm -rf "$PREFIX"
install -d -o root -g root -m 0755 "$PREFIX" "$PREFIX/zoom75"
install -o root -g root -m 0644 "$SRC"/zoom75/*.py "$PREFIX/zoom75/"
for doc in README.md PROTOCOL.md; do
    [[ -f "$SRC/$doc" ]] && install -o root -g root -m 0644 "$SRC/$doc" "$PREFIX/"
done

echo "==> building the virtualenv (root-owned)"
/usr/bin/python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
"$PREFIX/.venv/bin/pip" install -q --disable-pip-version-check bleak pillow
chown -R root:root "$PREFIX"
find "$PREFIX" -type d -exec chmod 755 {} +
find "$PREFIX" -type f -not -path "*/.venv/bin/*" -exec chmod 644 {} +
chmod 755 "$PREFIX/.venv/bin/"*

echo "==> installing config to $CONFDIR/config.toml"
install -d -o root -g root -m 0755 "$CONFDIR"
if [[ -e "$CONFDIR/config.toml" ]]; then
    echo "    existing config kept; new defaults written to config.toml.new"
    install -o root -g root -m 0644 "$SRC/systemd/config.toml" "$CONFDIR/config.toml.new"
else
    install -o root -g root -m 0644 "$SRC/systemd/config.toml" "$CONFDIR/config.toml"
fi

echo "==> installing the unit"
install -o root -g root -m 0644 "$SRC/systemd/zoom75-screen.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now zoom75-screen.service

echo
echo "Installed. Verify with:"
echo "  systemctl status zoom75-screen"
echo "  journalctl -u zoom75-screen -f"
echo "  ls -l /dev/hidraw*      # keyboard nodes must stay crw------- root root"
echo
echo "Edit /etc/zoom75/config.toml (needs sudo), then:"
echo "  sudo systemctl restart zoom75-screen"
