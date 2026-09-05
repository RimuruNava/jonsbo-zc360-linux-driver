#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

sudo install -m 0644 \
    "$project_dir/udev/99-jonsbo-zc360.rules" \
    /etc/udev/rules.d/70-jonsbo-zc360.rules
sudo udevadm control --reload-rules

printf '%s\n' \
    'Installed the ZC-360 udev access rule.' \
    'No USB interface was claimed and no service was restarted.' \
    'The rule will apply automatically on the next physical reconnect or boot.'

