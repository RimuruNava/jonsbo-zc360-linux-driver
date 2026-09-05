#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime_dir="$HOME/.local/share/jonsbo-zc360"
venv_dir="$runtime_dir/venv"
user_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
user_bin_dir="$HOME/.local/bin"
application_dir="$HOME/.local/share/applications"
icon_dir="$HOME/.local/share/icons/hicolor/scalable/apps"

use_lucille=false
if systemctl --user is-enabled --quiet lucille-zc360-renderer.service 2>/dev/null; then
    use_lucille=true
fi

mkdir -p "$runtime_dir"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade "$project_dir[gui,surface]"

mkdir -p "$user_unit_dir" "$user_bin_dir" "$application_dir" "$icon_dir"
install -m 0644 \
    "$project_dir/systemd/jonsbo-fan-daemon.service" \
    "$project_dir/systemd/zc360-renderer.service" \
    "$project_dir/systemd/lucille-zc360-renderer.service" \
    "$user_unit_dir/"
ln -sfn "$venv_dir/bin/zc360ctl" "$user_bin_dir/zc360ctl"
ln -sfn "$venv_dir/bin/zc360-gui" "$user_bin_dir/zc360-gui"
ln -sfn "$venv_dir/bin/zc360-renderer" "$user_bin_dir/zc360-renderer"
install -m 0644 "$project_dir/desktop/io.github.jonsbo_zc360.Control.desktop" "$application_dir/"
install -m 0644 "$project_dir/desktop/io.github.jonsbo_zc360.Control.svg" "$icon_dir/"

systemctl --user daemon-reload
systemctl --user enable jonsbo-fan-daemon.service
if $use_lucille; then
    systemctl --user disable zc360-renderer.service >/dev/null 2>&1 || true
    systemctl --user enable lucille-zc360-renderer.service
    renderer_name="the existing Lucille renderer"
else
    systemctl --user enable zc360-renderer.service
    renderer_name="the generic display renderer"
fi

printf '%s\n' \
    "Installed the ZC-360 GUI and enabled $renderer_name without starting or restarting services." \
    'The existing USB owner, if any, was not contacted.' \
    'The service selection takes effect at the next normal login or boot.' \
    'Launch “ZC-360 Control” from the application menu, or run zc360-gui.' \
    'Use zc360ctl doctor to inspect the currently running daemon safely.'
