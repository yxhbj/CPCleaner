from __future__ import print_function

import json
import os
import re
import sys

import core


def build(output, install, python):
    for value in (install, python):
        core.insist(re.match(r"^/[A-Za-z0-9_/-]+$", value) and ".." not in value.split("/"),
                    "absolute Solaris paths without shell metacharacters required")
    core.insist(not os.path.exists(output), "output already exists")
    os.makedirs(output)
    os.mkdir(os.path.join(output, "runs"), 0o700)
    manifest = {"schemaVersion": 1, "researchOnly": True, "install": install, "python": python, "files": {}}

    def write(name, data):
        if not isinstance(data, bytes):
            data = data.encode("ascii")
        data = data.replace(b"\r\n", b"\n")
        if name.endswith(".Script"):
            data = core.native(data)
        with open(os.path.join(output, name), "wb") as stream:
            stream.write(data)
        manifest["files"][name] = core.digest(data)

    for name in ("core.py", "helper.py"):
        with open(os.path.join(os.path.dirname(__file__), name), "rb") as stream:
            write(name, stream.read())
    write("config.json", json.dumps({"schemaVersion": 1, "researchOnly": True, "python": python}, indent=2) + "\n")
    write("Failure.Script", 'InfoMessage = "Jaw/MLC helper failed. No cleanup was authorized.";\n')
    entries = ""
    for name, mode in (("Start-CPCleaner", "run"), ("01-Check", "check"), ("02-Clean", "clean"), ("03-Verify", "verify")):
        script = (
            'Store.StringAt.CPCleanerDisplay = GetEnv.DISPLAY;\n'
            'Store.At.CPCleanerDisplay.RemoveSpecialCharactersAndGetFileName = "";\n'
            'Store.StringAt.CPCleanerBase = "' + install + '/runs/";\n'
            'Store.At.CPCleanerBase.AppendString = Store.StringAt.CPCleanerDisplay;\n'
            'Store.StringAt.CPCleanerCommand = "/bin/mkdir -p ";\n'
            'Store.At.CPCleanerCommand.AppendString = Store.StringAt.CPCleanerBase;\n'
            'Store.At.CPCleanerCommand.AppendString = " && /bin/chmod 700 ";\n'
            'Store.At.CPCleanerCommand.AppendString = Store.StringAt.CPCleanerBase;\n'
            'Store.At.CPCleanerCommand.AppendString = " && /bin/cp ' + install + '/Failure.Script ";\n'
            'Store.At.CPCleanerCommand.AppendString = Store.StringAt.CPCleanerBase;\n'
            'Store.At.CPCleanerCommand.AppendString = "/bridge.Script && ' + python + ' -B ' + install
            + '/helper.py prepare ' + mode + '";\n'
            'SpawnCommand = Store.StringAt.CPCleanerCommand;\n'
            'Store.StringAt.CPCleanerBridge = Store.StringAt.CPCleanerBase;\n'
            'Store.At.CPCleanerBridge.AppendString = "/bridge.Script";\n'
            'Load = Store.StringAt.CPCleanerBridge;\n')
        write(name + ".Script", script)
        if mode == "run":
            entries += ('HotScript ={\n  ScriptName = "CPCleaner (Research)";\n'
                        '  ScriptFile = "' + install + "/" + name + '.Script";\n  Description = "";\n};\n')
    write("HotScriptList.entries", entries)
    with open(os.path.join(output, "manifest.json"), "wb") as stream:
        stream.write((json.dumps(manifest, indent=2) + "\n").encode("ascii"))
    return manifest


if __name__ == "__main__":
    try:
        core.insist(len(sys.argv) == 4, "Usage: python build.py OUTPUT SOLARIS_INSTALL SOLARIS_PYTHON")
        print(json.dumps(build(os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]), indent=2))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
