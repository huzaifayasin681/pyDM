#!/usr/bin/env bash
# Install PyDM for the current user (no root needed):
#   ~/.local/share/pydm/            app files + browser extension
#   ~/.config/systemd/user/pydm.service   daemon, started automatically at login
#   ~/.local/share/applications/pydm.desktop   app-menu entry
#   ~/.local/bin/pydm, pydm-gui      commands
# Re-run after editing the source to update the installed copy.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/pydm"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"
PY=/usr/bin/python3
RUN_PATH="$BIN:/usr/local/bin:/usr/bin:/bin"

echo "==> Checking requirements"
$PY -c 'import sys; assert sys.version_info >= (3, 8)' || { echo "Python 3.8+ required"; exit 1; }
if ! PATH="$RUN_PATH" command -v yt-dlp >/dev/null; then
  echo "    yt-dlp not found - installing it into ~/.local/share/pydm-venv"
  $PY -m venv "$HOME/.local/share/pydm-venv"
  "$HOME/.local/share/pydm-venv/bin/pip" install -q -U yt-dlp
  mkdir -p "$BIN"; ln -sf "$HOME/.local/share/pydm-venv/bin/yt-dlp" "$BIN/yt-dlp"
elif [ -x "$HOME/.local/share/pydm-venv/bin/pip" ]; then
  echo "    updating yt-dlp (sites like TikTok/Instagram change often)"
  "$HOME/.local/share/pydm-venv/bin/pip" install -q -U yt-dlp || echo "    (update skipped: offline?)"
fi
PATH="$RUN_PATH" command -v ffmpeg >/dev/null || echo "    WARNING: ffmpeg not found - best-quality video+audio merging needs it (sudo apt install ffmpeg)"
$PY -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
  || echo "    WARNING: GTK4/libadwaita missing - desktop app won't open (sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1); web UI still works"

# Only restart the service (interrupting downloads) when the daemon code changed
OLD_SUM="$(cat "$DEST/pydmd.py" "$DEST/pydm.py" 2>/dev/null | sha256sum)"
NEW_SUM="$(cat "$SRC/pydmd.py" "$SRC/pydm.py" | sha256sum)"

echo "==> Copying files to $DEST"
mkdir -p "$DEST" "$BIN" "$UNIT_DIR" "$APPS"
install -m 644 "$SRC"/pydm.py "$SRC"/pydmd.py "$SRC"/pydm_app.py "$SRC"/ui.html "$SRC"/README.md "$DEST"/
rm -rf "$DEST/extension"; cp -r "$SRC/extension" "$DEST/extension"
install -m 755 "$SRC/uninstall.sh" "$DEST/uninstall.sh"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
install -m 644 "$SRC/data/local.pydm.app.svg" "$ICON_DIR/local.pydm.app.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "==> Installing commands"
cat > "$BIN/pydm" <<EOF
#!/bin/sh
# PyDM command-line downloader:  pydm URL [-c 8] ...
export PATH="$RUN_PATH:\$PATH"
exec $PY "$DEST/pydm.py" "\$@"
EOF
cat > "$BIN/pydm-gui" <<EOF
#!/bin/sh
export PATH="$RUN_PATH:\$PATH"
exec $PY "$DEST/pydm_app.py" "\$@"
EOF
chmod 755 "$BIN/pydm" "$BIN/pydm-gui"

echo "==> Installing background service (starts at login)"
cat > "$UNIT_DIR/pydm.service" <<EOF
[Unit]
Description=PyDM download manager daemon
After=network-online.target

[Service]
Type=simple
Environment=PATH=$RUN_PATH
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$DEST
ExecStart=$PY $DEST/pydmd.py
KillSignal=SIGINT
TimeoutStopSec=15
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

echo "==> Installing app-menu entry"
rm -f "$APPS/pydm.desktop"            # old name; the file name must match the app id
cat > "$APPS/local.pydm.app.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PyDM
GenericName=Download Manager
Comment=Multi-connection download manager with video grabbing
Exec=$BIN/pydm-gui
Icon=local.pydm.app
Terminal=false
Categories=Network;FileTransfer;
Keywords=download;video;youtube;tiktok;instagram;
StartupNotify=true
StartupWMClass=local.pydm.app
EOF

# Start the app hidden at login so browser downloads show progress popups.
# Done once; afterwards the "Start at Login" switch in Preferences decides.
AUTOSTART="$HOME/.config/autostart/local.pydm.app.desktop"
if [ ! -e "$HOME/.config/pydm/.autostart-chosen" ]; then
  mkdir -p "$HOME/.config/autostart" "$HOME/.config/pydm"
  printf '[Desktop Entry]\nType=Application\nName=PyDM\nExec=%s --background\nIcon=local.pydm.app\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n' "$BIN/pydm-gui" > "$AUTOSTART"
  touch "$HOME/.config/pydm/.autostart-chosen"
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

# Stop an old, manually started daemon so the service can take the port
for pid in $(pgrep -f '^[^ ]*python[0-9.]* [^ ]*pydmd\.py' || true); do
  case "$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)" in
    *"$DEST/pydmd.py"*) ;;                      # the service itself
    *) kill -INT "$pid" 2>/dev/null && sleep 2 ;;
  esac
done
systemctl --user daemon-reload
systemctl --user enable pydm.service >/dev/null 2>&1
busy() {   # true if the running service has active downloads
  local tok; tok="$(cat "$HOME/.config/pydm/token" 2>/dev/null)" || return 1
  curl -s -m 3 -H "X-PyDM-Token: $tok" http://127.0.0.1:6800/api/jobs 2>/dev/null | grep -q '"status": "downloading"'
}
if ! systemctl --user is-active -q pydm.service; then
  systemctl --user restart pydm.service
elif [ "$OLD_SUM" != "$NEW_SUM" ] && busy && [ "${FORCE_RESTART:-0}" != 1 ]; then
  echo "    downloads are running - the updated service will be used after:"
  echo "        systemctl --user restart pydm     (paused downloads resume automatically)"
elif [ "$OLD_SUM" != "$NEW_SUM" ]; then
  systemctl --user restart pydm.service
else
  echo "    service already up to date - not restarting (running downloads continue)"
fi

for _ in $(seq 1 20); do
  [ -s "$HOME/.config/pydm/token" ] && systemctl --user is-active -q pydm.service && break
  sleep 0.3
done

echo
if systemctl --user is-active -q pydm.service; then
  echo "PyDM installed and running."
else
  echo "PyDM installed, but the service did not start:  journalctl --user -u pydm -n 30"
fi
cat <<EOF

  Open the app:   "PyDM" in your app menu  (or: pydm-gui)
  Web UI:         http://127.0.0.1:6800/
  Command line:   pydm URL
  Service:        systemctl --user status|restart|stop pydm
  Uninstall:      $DEST/uninstall.sh

  Browser extension (one-time, Chrome/Chromium/Brave/Edge):
    1. chrome://extensions -> turn on Developer mode -> Load unpacked
    2. select:  $DEST/extension
    3. open the extension's Options and paste this token:
       $(cat "$HOME/.config/pydm/token" 2>/dev/null || echo "(see ~/.config/pydm/token)")
EOF
