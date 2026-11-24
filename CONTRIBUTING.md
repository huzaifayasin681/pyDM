# Contributing to PyDM

Thanks for your interest in PyDM. Contributions of all sizes are welcome: bug reports, ideas, docs and code.

## Good places to start

- Firefox extension support (the extension is Chrome MV3 only today)
- Release packaging (.deb, AppImage)
- Support for more Linux distributions and install testing
- Tests for the download engine (segments, resume, retries)
- Translations and UI polish for the GTK app

## Project layout

- `pydm.py`: download engine and CLI
- `pydmd.py`: background service (queue, scheduler, video, HTTP API)
- `pydm_app.py`: GTK4 / libadwaita desktop app
- `ui.html`: web view
- `extension/`: Chrome extension

## Development setup

1. Install dependencies (Ubuntu/Debian): `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg`
2. Run `./install.sh` (per user, no root).
3. After code changes, re-run the installer, fully quit PyDM from the main menu, and reopen it.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Keep changes focused, one topic per PR.
3. Describe what changed and how you tested it.
4. The engine uses only the Python standard library, so please avoid adding new dependencies unless really needed.

## Reporting bugs

Open an issue with your distro and version, Python version, what you expected, what happened, and any logs.

By contributing you agree that your contributions are licensed under the MIT License.
