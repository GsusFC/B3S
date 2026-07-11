#!/bin/sh
set -eu

mkdir -p /data/reports /data/screenshots
chown -R b3s:b3s /data

exec gosu b3s "$@"
