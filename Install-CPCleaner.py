from __future__ import print_function

import hashlib
import os
import sys

import build
import core


def install(args):
    core.insist(len(args) <= 2,
                "Usage: sh Install-CPCleaner.sh [INSTALL_DIRECTORY [PYTHON_PATH]]")
    core.insist(hasattr(os, "getuid") and os.getuid() != 0,
                "run on Solaris as the authorized Pinnacle runtime account, not root")
    home = os.environ.get("HOME")
    core.insist(args or home, "HOME is missing; specify an absolute installation directory")
    requested = args[0] if args else os.path.join(home, "CPCleaner")
    python = args[1] if len(args) == 2 else "/usr/bin/python"
    core.insist(os.path.isabs(python) and os.access(python, os.X_OK),
                "system Python must be an executable absolute path")
    source = os.path.dirname(os.path.realpath(__file__))
    core.insist(os.path.isabs(requested), "installation directory must be absolute")
    core.insist(not os.path.lexists(requested),
                "installation path already exists; choose a new directory")
    parent = os.path.realpath(os.path.dirname(requested))
    core.insist(os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK),
                "installation parent must exist and be writable by the current account")
    output = os.path.join(parent, os.path.basename(requested))
    core.insist(output != source, "install outside the extracted source directory")
    os.umask(0o077)
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
    core.insist(not os.listdir(os.path.join(output, "runs")),
                "installation evidence directory is not empty")
    print("CPCleaner research installation verified.")
    print("Scripting entry: " + output + "/Start-CPCleaner.Script")
    print("No plan was accessed or changed. No global HotScriptList was modified.")


if __name__ == "__main__":
    try:
        install(sys.argv[1:])
    except Exception as error:
        print("Installation failed: " + str(error), file=sys.stderr)
        print("No automatic overwrite or cleanup. Inspect any partial output before retrying.", file=sys.stderr)
        sys.exit(1)
