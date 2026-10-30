#!/bin/sh
set -eu
umask 077

if [ "$#" -gt 2 ]; then
    echo "Usage: sh Install-CPCleaner.sh [INSTALL_DIRECTORY [PYTHON_PATH]]" >&2
    exit 1
fi

SOURCE=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)
PYTHON=${2:-/usr/bin/python}
if [ ! -x "$PYTHON" ]; then
    echo "System Python is not executable: $PYTHON. Ask your authorized administrator." >&2
    exit 1
fi
if [ -z "${HOME:-}" ] && [ "$#" -eq 0 ]; then
    echo "HOME is missing. Specify an absolute installation directory." >&2
    exit 1
fi

"$PYTHON" -B -c '
from __future__ import print_function
import os
import sys
import hashlib
import json

source, requested, python = sys.argv[1:]
sys.path.insert(0, source)
import build
import core

try:
    core.insist(os.getuid() != 0, "run as the authorized Pinnacle runtime account, not root")
    core.insist(os.path.isabs(requested), "installation directory must be absolute")
    core.insist(not os.path.lexists(requested), "installation path already exists; choose a new directory")
    parent = os.path.realpath(os.path.dirname(requested))
    core.insist(os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK),
                "installation parent must exist and be writable by the current account")
    output = os.path.join(parent, os.path.basename(requested))
    core.insist(os.path.isdir(source) and output != source, "install outside the extracted source directory")
    manifest = build.build(output, output, python)
    for name, digest in manifest["files"].items():
        with open(os.path.join(output, name), "rb") as stream:
            data = stream.read()
        core.insist(hashlib.sha256(data).hexdigest() == digest, "installed hash mismatch: " + name)
        if name.endswith(".Script"):
            core.insist(core.checksum(data) == 0, "native checksum mismatch: " + name)
        os.chmod(os.path.join(output, name), 0o600)
    os.chmod(os.path.join(output, "manifest.json"), 0o600)
    os.chmod(output, 0o700)
    core.insist(not os.listdir(os.path.join(output, "runs")), "installation evidence directory is not empty")
    print("CPCleaner research installation verified.")
    print("Scripting entry: " + output + "/Start-CPCleaner.Script")
    print("No plan was accessed or changed. No global HotScriptList was modified.")
except Exception as error:
    print("Installation failed: " + str(error), file=sys.stderr)
    print("No automatic overwrite or cleanup. Inspect any partial output before retrying.", file=sys.stderr)
    sys.exit(1)
' "$SOURCE" "${1:-$HOME/CPCleaner}" "$PYTHON"
