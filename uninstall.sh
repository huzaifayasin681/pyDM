#!/usr/bin/env bash
# Remove PyDM. Your downloads and settings (~/.config/pydm) are kept unless you pass --purge.
set -u
systemctl --user disable --now pydm.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/pydm.service"
systemctl --user daemon-reload 2>/dev/null
rm -f "$HOME/.local/share/applications/pydm.desktop" "$HOME/.local/share/applications/local.pydm.app.desktop" \
      "$HOME/.config/autostart/local.pydm.app.desktop" "$HOME/.local/share/icons/hicolor/scalable/apps/local.pydm.app.svg" "$HOME/.local/bin/pydm" "$HOME/.local/bin/pydm-gui"
rm -rf "$HOME/.local/share/pydm"
if [ "${1:-}" = "--purge" ]; then rm -rf "$HOME/.config/pydm"; echo "Settings removed."; fi
echo "PyDM uninstalled. Remove the browser extension from chrome://extensions yourself."
