from __future__ import print_function

import json
import os
import re
import sys
import traceback
import time
import uuid

import core

ROOT = os.path.dirname(os.path.abspath(__file__))
_stage_data = None


class StageData(object):
    def __init__(self):
        self.parsed = {}
        self.fingerprints = {}

    def items(self, data):
        if data not in self.parsed:
            self.parsed[data] = core.read(data)
        return self.parsed[data]

    def fingerprint(self, data):
        if data not in self.fingerprints:
            self.fingerprints[data] = core.fingerprint(self.items(data))
        return self.fingerprints[data]


def read_items(file):
    data = read_bytes(file)
    return _stage_data.items(data) if _stage_data is not None else core.read(data)


def file_fingerprint(file):
    data = read_bytes(file)
    return _stage_data.fingerprint(data) if _stage_data is not None else core.fingerprint(core.read(data))


def analyze_snapshot(directory):
    data = read_bytes(directory + "/current.txt")
    trial = _stage_data.items(data) if _stage_data is not None else core.read(data)
    machines = [read_items(directory + "/machine-%d.txt" % i)
                for i in range(len(core.block(trial, "BeamList")))]
    return core.analyze_parsed(data, trial, machines)


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_bytes(file):
    with open(file, "rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    core.insist(len(data) <= 32 * 1024 * 1024, "file exceeds limit")
    return data


def text(file):
    return read_bytes(file).decode("latin1").strip()


def load(file):
    return json.loads(read_bytes(file).decode("ascii"))


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii")


def write_exclusive(file, data):
    fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save(file, value):
    write_exclusive(file, encoded(value))


def atomic(file, data):
    if not isinstance(data, bytes):
        data = data.encode("latin1")
    if file.endswith(".Script"):
        data = core.native(data)
    tmp = file + "." + uuid.uuid4().hex + ".tmp"
    write_exclusive(tmp, data)
    getattr(os, "replace", os.rename)(tmp, file)


def quote(value):
    value = str(value) if not isinstance(value, type(u"")) else value
    core.insist(all(ord(c) < 256 for c in value), "unsupported native message encoding")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ") + '"'


def index(value):
    return '#"#%d"' % value


def notice(message):
    return "InfoMessage = " + quote(message) + ";\n"


def execute(file, condition=""):
    return ("IF." + condition + ".THEN." if condition else "") + "Load = " + quote(file) + ";\n"


def command(stage, argument):
    config = load(os.path.join(ROOT, "config.json"))
    python = config["python"]
    core.insist(re.match(r"^/[A-Za-z0-9_/-]+$", python), "invalid interpreter path")
    return "SpawnCommand = " + quote(python + " -B " + ROOT + "/helper.py " + stage + " " + argument) + ";\n"


def display_dir():
    display = os.environ.get("DISPLAY", "")
    core.insist(re.match(r"^[A-Za-z0-9_.-]*:\d+(?:\.\d+)?$", display), "invalid DISPLAY")
    base = os.path.join(ROOT, "runs", display.replace(".", "_").replace(":", "_"))
    core.insist(os.path.realpath(base) == os.path.abspath(base), "symlink in display path")
    return base


def validate_dir(directory):
    core.insist(os.path.dirname(directory) == display_dir()
                and re.match(r"^[a-f0-9]{32}$", os.path.basename(directory))
                and os.path.isdir(directory) and not os.path.islink(directory), "invalid request directory")


def pointer(file, directory):
    validate_dir(directory)
    atomic(file, encoded({"dir": directory}))


def previous(file):
    if not os.path.exists(file):
        return None
    value = load(file)
    validate_dir(value["dir"])
    return value["dir"]


def native_snapshot(directory, prefix=""):
    script = "FileToWrite = " + quote(directory + "/" + prefix + "plan.txt") + ";\nWriteToFile = PlanInfo.PlanPath;\n"
    for obj, name in (("TrialList", "all"), ("TrialList.Current", "current")):
        script += obj + ".Save = " + quote(directory + "/" + prefix + name + ".txt") + ";\n"
    for name, query in (
            ("dose", "TrialList.Current.DoseIsValid"),
            ("optimizing", "PluginManager.InversePlanningManager.Optimizing"),
            ("resetting", "PluginManager.InversePlanningManager.Resetting"),
            ("computing", "PluginManager.InversePlanningManager.ComputingFinalDose")):
        script += "FileToWrite = " + quote(directory + "/" + prefix + name + ".txt") + ';\nWriteToFile = "UNKNOWN";\n'
        script += "IF." + query + '.EQUALTO.#"0".THEN.WriteToFile = "0";\n'
        if name == "dose":
            script += "IF." + query + '.EQUALTO.#"1".THEN.WriteToFile = "1";\n'
    return script


def idle(directory, prefix=""):
    for name in ("optimizing", "resetting", "computing"):
        core.insist(re.match(r"^0(?:\.0+)?$", text(directory + "/" + prefix + name + ".txt")),
                    "TPS is not idle or getter is unknown: " + name)


def identity(directory, prefix=""):
    trial = read_items(directory + "/" + prefix + "current.txt")
    plan = text(directory + "/" + prefix + "plan.txt")
    absolute = re.match(r"^/[A-Za-z0-9_./-]+$", plan)
    relative = re.match(r"^Institution_[0-9]+/Mount_[0-9]+/Patient_[0-9]+/Plan_[0-9]+$", plan)
    core.insist((absolute or relative) and not any(part in (".", "..") for part in plan.split("/")),
                "missing or unsupported plan path")
    name = core.string(trial, "Name")
    core.insist(load(ROOT + "/config.json")["researchOnly"] is True, "research-only configuration required")
    core.insist(name and not re.search(r'["\r\n\\]', name)
                and all(ord(c) < 256 for c in name), "unsupported native Trial name")
    return {"planPath": plan, "trialName": name,
            "trialFingerprint": file_fingerprint(directory + "/" + prefix + "current.txt")}


def beams(directory, prefix=""):
    return core.block(read_items(directory + "/" + prefix + "current.txt"), "BeamList")


def machine_bytes(directory, prefix=""):
    return [read_bytes(directory + "/" + prefix + "machine-%d.txt" % i)
            for i in range(len(beams(directory, prefix)))]


def machine_snapshot(directory, prefix=""):
    return "".join("TrialList.Current.BeamList." + index(i) + ".Machine.Save = "
                   + quote(directory + "/" + prefix + "machine-%d.txt" % i) + ";\n"
                   for i in range(len(beams(directory))))


def compare_snapshot(before, after, prefix=""):
    idle(after, prefix)
    core.insist(identity(before) == identity(after, prefix), "plan/Trial changed since preview")
    for name in ("current", "all"):
        core.insist(file_fingerprint(before + "/" + name + ".txt")
                    == file_fingerprint(after + "/" + prefix + name + ".txt"),
                    "protected fields or Trial inventory changed since preview")
    a, b = machine_bytes(before), machine_bytes(after, prefix)
    core.insist(len(a) == len(b) and all(
        (_stage_data.fingerprint(x) == _stage_data.fingerprint(y)) if _stage_data is not None
        else core.fingerprint(core.read(x)) == core.fingerprint(core.read(y))
        for x, y in zip(a, b)), "machine configuration changed")


def make_report(directory, analysis=None):
    analysis = analyze_snapshot(directory) if analysis is None else analysis
    result = dict(analysis)
    result["curves"] = [dict((key, value) for key, value in curve.items() if key != "bytes")
                        for curve in analysis["curves"]]
    result.update(identity=identity(directory), researchOnly=True, exportReady=False)
    return result


def summary(report):
    lines = ["Trial: " + report["trialName"], "Masked openings: " + str(len(report["changes"])),
             "Unchanged active/boundary openings: " + str(len(report["observations"])),
             "Configuration/closure blockers: " + str(len(report["blockers"]))]
    for finding in report["changes"][:12]:
        lines.append("Beam %d / Segment %d / Leaf pair %d: %s -> %s cm; %s; %s" % (
            finding["beamIndex"] + 1, finding["segmentIndex"] + 1, finding["leafIndex"] + 1,
            finding["before"], finding["after"], finding["classification"], finding["closureStrategy"]))
        if finding.get("alignmentFallback"):
            lines.append("Alignment fallback: " + finding["alignmentFallback"])
    if report["blockers"]:
        lines.extend(report["blockers"][:3])
    return "\n".join(lines)


def prepare(mode):
    core.insist(mode in ("run", "check", "clean", "verify"), "invalid entry")
    base = display_dir()
    if not os.path.exists(base):
        os.mkdir(base, 0o700)
    atomic(base + "/bridge.Script", notice("Jaw/MLC preparation failed. No cleanup was authorized."))
    preview = previous(base + "/preview.json")
    imported = previous(base + "/last-import.json")
    for name in os.listdir(base):
        if re.match(r"^[a-f0-9]{32}$", name):
            directory = base + "/" + name
            validate_dir(directory)
            if os.path.exists(directory + "/mutation.claim") and not os.path.exists(directory + "/completed.json"):
                core.insist(mode == "verify", "earlier import unresolved; Verify or reconcile, do not replay")
    source = imported if mode == "verify" else preview
    core.insist(mode in ("run", "check") or source, "run Check first; Verify requires an imported cleanup")
    directory = os.path.join(base, uuid.uuid4().hex)
    os.mkdir(directory, 0o700)
    if mode == "run":
        source = directory
    save(directory + "/request.json", {"mode": mode, "previous": source, "createdAt": timestamp()})
    for name in ("next", "result", "authorize", "guard", "import", "finished", "consent", "machine-stage"):
        atomic(directory + "/" + name + ".Script", notice("Jaw/MLC stage incomplete. No cleanup was authorized."))
    atomic(base + "/bridge.Script", native_snapshot(directory) + command("machines", directory)
           + execute(directory + "/next.Script"))


def machines(directory):
    idle(directory)
    plan = identity(directory)
    core.insist(1 <= len(beams(directory)) <= 64, "invalid beam count")
    prompt = ("CPCleaner: RESEARCH ONLY. Not clinically approved.\nTrial: " + plan["trialName"] +
              "\nThe entire current Trial will be inspected. Trial names are not restricted. "
              "Use only an authorized research copy with a backup. Geometry changes invalidate old dose/evaluation; "
              "dose recalculation, QA and reconfirmation are required. No automatic save or export. "
              "Continue research inspection? Cleanup requires a separate confirmation.")
    atomic(directory + "/consent.Script", native_snapshot(directory, "consent-")
           + command("acknowledge", directory) + execute(directory + "/machine-stage.Script"))
    atomic(directory + "/next.Script", "AskYesNoDefault = 0;\nAskYesNoPrompt = " + quote(prompt)
           + ";\n" + execute(directory + "/consent.Script", "AskYesNo"))


def acknowledge(directory):
    idle(directory, "consent-")
    core.insist(identity(directory) == identity(directory, "consent-"),
                "plan/Trial changed during research warning")
    for name in ("current", "all"):
        core.insist(file_fingerprint(directory + "/" + name + ".txt")
                    == file_fingerprint(directory + "/consent-" + name + ".txt"),
                    "protected fields or Trial inventory changed during research warning")
    save(directory + "/research-consent.json", {"identity": identity(directory), "at": timestamp()})
    atomic(directory + "/machine-stage.Script", machine_snapshot(directory) + command("analyze", directory)
           + execute(directory + "/result.Script"))


def require_consent(directory):
    core.insist(load(directory + "/research-consent.json")["identity"] == identity(directory),
                "research acknowledgement missing or changed")


def inspected(directory):
    require_consent(directory)
    request = load(directory + "/request.json")
    analysis = analyze_snapshot(directory)
    report = make_report(directory, analysis)
    save(directory + "/report.json", report)
    write_exclusive(directory + "/report.txt", (summary(report) + "\n").encode("latin1"))
    if request["mode"] == "verify":
        return verify_previous(directory, request["previous"])
    if request["mode"] == "check":
        pointer(display_dir() + "/preview.json", directory)
        message = summary(report) + "\nFull report: " + directory + "/report.json; then run Clean."
    elif not report["changes"] and not report["blockers"]:
        message = "Jaw/MLC: no masked openings found. No edit or import performed. This is not export approval."
    else:
        source = request["previous"]
        validate_dir(source)
        core.insist(not os.path.exists(source + "/mutation.claim"), "preview already claimed; do not replay")
        if source != directory:
            compare_snapshot(source, directory)
        core.insist(load(source + "/report.json") == report, "preview report differs")
        core.insist(not report["blockers"], "closure constraints block cleanup; see report.json")
        for i, curve in enumerate(analysis["curves"]):
            write_exclusive(directory + "/curve-%d.txt" % i, curve["bytes"])
        atomic(directory + "/authorize.Script", native_snapshot(directory, "live-")
               + machine_snapshot(directory, "live-") + command("claim", directory)
               + execute(directory + "/guard.Script"))
        prompt = summary(report)
        if len(report["changes"]) > 12:
            prompt += "\nFull report: " + directory + "/report.json"
        prompt += (
            "\nRESEARCH ONLY. Old dose/evaluation must not be used. Recompute final dose, review and reconfirm "
            "before any treatment-system export. Import these changes?")
        atomic(directory + "/result.Script", "AskYesNoDefault = 0;\nAskYesNoPrompt = " + quote(prompt)
               + ";\n" + execute(directory + "/authorize.Script", "AskYesNo"))
        return
    if not report["changes"] and not report["blockers"]:
        message = "Jaw/MLC: no masked openings found. No edit or import performed. This is not export approval."
    atomic(directory + "/result.Script", notice(message))


def guard(directory):
    trial = read_items(directory + "/current.txt")
    plan = identity(directory)
    conditions = []

    def numeric(key, value):
        tolerance = max(0.000001, abs(value) * 0.000001)
        conditions.extend((key + ".GREATERTHANOREQUALTO.#" + quote("%.12g" % (value - tolerance)),
                           key + ".LESSTHANOREQUALTO.#" + quote("%.12g" % (value + tolerance))))

    def string(key, value):
        core.insist(not re.search(r'["\r\n\\]', value), "unsupported native identity")
        conditions.append(key + ".STRINGEQUALTO.#" + quote("#" + value))

    string("PlanInfo.PlanPath", plan["planPath"])
    string("TrialList.Current.Name", plan["trialName"])
    numeric("TrialList.Count", len(read_items(directory + "/all.txt")))
    entries = core.block(trial, "BeamList")
    numeric("TrialList.Current.BeamList.Count", len(entries))
    for field in ("Optimizing", "Resetting", "ComputingFinalDose"):
        numeric("PluginManager.InversePlanningManager." + field, 0)
    for bi, entry in enumerate(entries):
        beam = entry["children"]
        bp = "TrialList.Current.BeamList." + index(bi) + "."
        for field in ("Name", "MachineNameAndVersion", "MachineEnergyName", "Modality"):
            string(bp + field, core.string(beam, field))
        string(bp + "BeamType", "Step & Shoot MLC")
        for field, value in (("UseMLC", 1), ("BlocksAreLocked", 0), ("CPManager.ComputeDoseFromODM", 0)):
            numeric(bp + field, value)
        cps = core.block(core.manager(beam), "ControlPointList")
        numeric(bp + "CPManager.ControlPointList.Count", len(cps))
        for ci, segment in enumerate(cps):
            cp = segment["children"]
            prefix = bp + "CPManager.ControlPointList." + index(ci) + "."
            for key in ("Gantry", "Couch", "Collimator", "LeftJawPosition", "RightJawPosition",
                        "TopJawPosition", "BottomJawPosition", "Weight", "WeightLocked"):
                numeric(prefix + key, core.number(cp, key))
            values = core.leaf_values(cp)["values"]
            numeric(prefix + "MLCLeafPositions.NumberOfPoints", len(values) // 2)
            for i, value in enumerate(values):
                numeric(prefix + "MLCLeafPositions.Value." + index(i % 2) + "." + index(i // 2), value)
    script = "Store.FloatAt.CPCleanerMatches = 0;\n"
    script += "".join("IF." + condition + ".THEN.Store.At.CPCleanerMatches.Add = 1;\n" for condition in conditions)
    script += execute(directory + "/import.Script", "Store.FloatAt.CPCleanerMatches.EQUALTO.#" + quote(len(conditions)))
    script += "IF.Store.FloatAt.CPCleanerMatches.NOTEQUALTO.#" + quote(len(conditions)) + ".THEN.InfoMessage = " + quote(
        "Jaw/MLC input changed or required getter failed. Import blocked; reconcile this request.") + ";\n"
    return script


def claim(directory):
    require_consent(directory)
    request = load(directory + "/request.json")
    core.insist(request["mode"] in ("run", "clean"), "not a cleanup request")
    source = request["previous"]
    validate_dir(source)
    core.insist(not os.path.exists(directory + "/mutation.claim")
                and not os.path.exists(source + "/mutation.claim"), "already claimed; do not replay")
    if source != directory:
        compare_snapshot(source, directory)
    compare_snapshot(directory, directory, "live-")
    report = load(directory + "/report.json")
    core.insist(report == make_report(directory) == load(source + "/report.json"), "report changed")
    core.insist(report["changes"] and not report["blockers"], "no authorized changes")
    for i, curve in enumerate(report["curves"]):
        core.insist(core.digest(read_bytes(directory + "/curve-%d.txt" % i)) == curve["sha256"], "cleaned curve changed")
    live_guard = guard(directory)
    save(source + "/mutation.claim", {"cleanupRequest": directory, "at": timestamp()})
    if source != directory:
        save(directory + "/mutation.claim", {"preview": source, "at": timestamp()})
    pointer(display_dir() + "/last-import.json", directory)
    script = ""
    for i, curve in enumerate(report["curves"]):
        script += ("TrialList.Current.BeamList." + index(curve["beamIndex"]) + ".CPManager.ControlPointList."
                   + index(curve["segmentIndex"]) + ".MLCLeafPositions.Load = "
                   + quote(directory + "/curve-%d.txt" % i) + ";\n")
    for bi in sorted(set(curve["beamIndex"] for curve in report["curves"])):
        script += "TrialList.Current.BeamList." + index(bi) + '.InvalidateDose = "";\n'
    script += native_snapshot(directory, "after-") + command("finish", directory) + execute(directory + "/finished.Script")
    atomic(directory + "/import.Script", script)
    atomic(directory + "/guard.Script", live_guard)


def verify_snapshot(source, directory, prefix):
    report = load(source + "/report.json")
    before_id, after_id = identity(source), identity(directory, prefix)
    core.insist(before_id["planPath"] == after_id["planPath"]
                and before_id["trialName"] == after_id["trialName"], "plan/Trial identity changed")
    idle(directory, prefix)
    core.verify_items(read_items(source + "/current.txt"),
                      read_items(directory + "/" + prefix + "current.txt"), report["curves"])
    core.verify_all(read_items(source + "/all.txt"),
                    read_items(directory + "/" + prefix + "all.txt"), report["trialName"], report["curves"])
    core.insist(text(directory + "/" + prefix + "dose.txt") == "0", "old dose is valid or state unknown")
    return report


def finish(directory):
    core.insist(os.path.exists(directory + "/mutation.claim"), "missing import claim")
    report = verify_snapshot(directory, directory, "after-")
    result = {"status": "geometry-verified-dose-required", "geometryVerified": True,
              "exportReady": False, "changes": len(report["changes"]), "completedAt": timestamp()}
    save(directory + "/completed.json", result)
    request = load(directory + "/request.json")
    if request["previous"] != directory:
        save(request["previous"] + "/completed.json", result)
    atomic(directory + "/finished.Script", notice(
        "Jaw/MLC geometry verified. Recompute final dose, regenerate evaluation and reconfirm before export. "
        "No plan was saved or exported."))


def verify_previous(directory, source):
    validate_dir(source)
    core.insist(os.path.exists(source + "/mutation.claim"), "no imported cleanup to verify")
    verify_snapshot(source, directory, "")
    before_machines, after_machines = machine_bytes(source), machine_bytes(directory)
    core.insist(len(before_machines) == len(after_machines), "machine inventory changed")
    for a, b in zip(before_machines, after_machines):
        core.insist(core.fingerprint(core.read(a)) == core.fingerprint(core.read(b)), "machine configuration changed")
    result = {"geometryVerified": True, "doseIsValid": "0", "exportReady": False,
              "note": "Geometry verification is not dose equivalence or clinical approval."}
    save(directory + "/verification.json", result)
    atomic(directory + "/result.Script", notice(
        "Jaw/MLC geometry matches the imported cleanup. DoseIsValid=0. "
        "Recompute final dose, evaluate and reconfirm. No export approval."))


def main(args):
    global _stage_data
    core.insist(_stage_data is None, "nested helper stage")
    _stage_data = StageData()
    try:
        return run_stage(args)
    finally:
        _stage_data = None


def run_stage(args):
    core.insist(len(args) == 2, "expected stage and argument")
    stage, argument = args
    if stage == "prepare":
        return prepare(argument)
    validate_dir(argument)
    handler = {"machines": machines, "acknowledge": acknowledge,
               "analyze": inspected, "claim": claim, "finish": finish}.get(stage)
    core.insist(handler is not None, "unknown stage")
    handler(argument)


def failed(args, error):
    message = str(error) + " No automatic retry or rollback."
    if len(args) == 2 and args[0] != "prepare":
        directory = args[1]
        validate_dir(directory)
        atomic(directory + "/failure.json", encoded({
            "error": str(error), "at": timestamp(),
            "reconciliationRequired": os.path.exists(directory + "/mutation.claim")}))
        for name in ("next", "result", "authorize", "guard", "import", "finished", "consent", "machine-stage"):
            atomic(directory + "/" + name + ".Script", notice(message))
    elif os.path.isdir(display_dir()):
        atomic(display_dir() + "/bridge.Script", notice(message))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as error:
        traceback.print_exc()
        try:
            failed(sys.argv[1:], error)
        except Exception:
            traceback.print_exc()
        sys.exit(1)
