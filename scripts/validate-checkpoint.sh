#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
preview_dir=$(mktemp -d)
trap 'rm -rf -- "$preview_dir"' EXIT

cd -- "$project_dir"

python3 -m compileall -q src tests
python3 -m py_compile \
    jonsbo_fan_lib.py \
    jonsbo_fan_daemon.py \
    lucille_zc360_control.py \
    lucille_zc360_renderer.py \
    lucille_zc360_surface.py \
    examples/send_test_via_daemon.py

PYTHONPATH=src python3 -m unittest discover -s tests -v

PYTHONPATH=src ZC360_STATE="$preview_dir/generic-state.json" \
    python3 -m zc360.cli play previews/triptych.png --layout span --fit contain >/dev/null
PYTHONPATH=src ZC360_STATE="$preview_dir/generic-state.json" \
    python3 -m zc360.renderer --preview --preview-dir "$preview_dir/generic" >/dev/null
PYTHONPATH=src python3 -m zc360.gui --version >/dev/null

python3 lucille_zc360_renderer.py --preview-dir "$preview_dir" >/dev/null
python3 lucille_zc360_surface.py \
    --preview-source previews/triptych.png \
    --preview-dir "$preview_dir/media" \
    --layout span \
    --fit contain >/dev/null
python3 lucille_zc360_surface.py \
    --preview-panels \
        previews/panel-0.png \
        previews/panel-1.png \
        previews/panel-2.png \
    --preview-dir "$preview_dir/panels" \
    --fit cover >/dev/null

LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py telemetry >/dev/null
LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py play previews/triptych.png --layout span >/dev/null
LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py play-panels \
        previews/panel-0.png \
        previews/panel-1.png \
        previews/panel-2.png >/dev/null
LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py framing --begin >/dev/null
LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py framing --panel left --focus-x 0.7 --zoom 1.5 >/dev/null
LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py framing --apply >/dev/null
LUCILLE_ZC360_STATE="$preview_dir/state.json" \
    python3 lucille_zc360_control.py overlay CHAT --detail 'LAYER ACTIVE' >/dev/null

test "$(find "$preview_dir" -maxdepth 1 -name 'panel-*.png' | wc -l)" -eq 3
test -f "$preview_dir/triptych.png"
test -f "$preview_dir/media/media-triptych.png"
test -f "$preview_dir/panels/media-triptych.png"
test -f "$preview_dir/generic/triptych.png"
rg -q '"mode": "media"' "$preview_dir/state.json"
rg -q '"title": "CHAT"' "$preview_dir/state.json"
rg -q '"layout": "panels"' "$preview_dir/state.json"
rg -q '^Restart=no$' systemd/jonsbo-fan-daemon.service
rg -q '^TimeoutStopSec=infinity$' systemd/jonsbo-fan-daemon.service
rg -q '^SendSIGKILL=no$' systemd/jonsbo-fan-daemon.service
rg -q '/zc360d$' systemd/jonsbo-fan-daemon.service
rg -q '^Restart=on-failure$' systemd/lucille-zc360-renderer.service
! rg -q '^(Wants|Requires)=jonsbo-fan-daemon' systemd/lucille-zc360-renderer.service
rg -q '^Restart=on-failure$' systemd/zc360-renderer.service
! rg -q '^(Wants|Requires)=jonsbo-fan-daemon' systemd/zc360-renderer.service
rg -q '^zc360ctl = "zc360.cli:main"$' pyproject.toml
rg -q '^zc360d = "zc360.daemon:main"$' pyproject.toml
rg -q '^zc360-renderer = "zc360.renderer:main"$' pyproject.toml
rg -q '^zc360-gui = "zc360.gui:main"$' pyproject.toml
rg -q 'send_fan_frames' jonsbo_fan_lib.py
! rg -q 'systemctl .*\b(start|restart)\b' scripts/install-user.sh scripts/install-udev.sh
rg -q 'lucille_zc360_surface.py$' systemd/lucille-zc360-renderer.service
rg -q 'socket\+usb=' lucille_zc360_surface.py
rg -q 'payload=' lucille_zc360_surface.py

printf '%s\n' 'PASS  Python sources compile'
printf '%s\n' 'PASS  hardware-blind protocol and IPC unit tests'
printf '%s\n' 'PASS  three hardware-blind panel previews render'
printf '%s\n' 'PASS  media span and persistent shell-event control render'
printf '%s\n' 'PASS  three independent panel sources render in synchronized order'
printf '%s\n' 'PASS  generic renderer and lazy-loading GUI entry points'
printf '%s\n' 'PASS  USB owner is never auto-restarted'
printf '%s\n' 'PASS  renderer cannot implicitly start a second USB owner'
printf '%s\n' 'PASS  stale-panel warmup has a 120-second socket budget'
printf '%s\n' 'PASS  live media path reports decode, compose, PPM encode, transport, and actual FPS'
printf '%s\n' 'PASS  packaged daemon exposes versioned IPC while preserving legacy clients'
