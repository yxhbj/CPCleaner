#!/bin/sh
exec "${2:-/usr/bin/python}" -B "$(dirname "$0")/Install-CPCleaner.py" "$@" # CRLF-safe entry; run with sh.
