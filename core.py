from __future__ import print_function

import copy
import hashlib
import json
import math
import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP


def insist(condition, message):
    if not condition:
        raise ValueError("Jaw/MLC: " + message)


def checksum(data):
    running = total = 0
    for value in bytearray(data):
        running = (running + value) % 256
        total = (total + running) % 256
    return (((total - 2 * running) % 256) << 8) | ((running - total) % 256)


def native(body):
    if not isinstance(body, bytes):
        body = body.encode("latin1")
    data = bytearray(body + b"/* \0\0 */\n")
    running = total = 0
    for value in data:
        running = (running + value) % 256
        total = (total + running) % 256
    offset = len(body) + 3
    remaining = len(data) - offset - 1
    data[offset] = (remaining * running - total) % 256
    data[offset + 1] = (total - (remaining + 1) * running) % 256
    result = bytes(data)
    insist(checksum(result) == 0, "checksum generation failed")
    return result


NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
NAME = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|#(?:0|[1-9]\d*))(?:\."
                  r"(?:[A-Za-z_][A-Za-z0-9_]*|#(?:0|[1-9]\d*)))*(?:\[\])?$")


class Parser(object):
    def __init__(self, data):
        insist(isinstance(data, bytes) and 0 < len(data) <= 32 * 1024 * 1024,
               "expected native bytes of at most 32 MiB")
        insist(checksum(data) == 0, "native file checksum mismatch")
        insist(data[-9:-6] == b"/* " and data[-4:] == b" */\n", "unsupported native checksum trailer")
        self.text = data[:-9].decode("latin1")
        self.at = self.tokens = self.entries = 0
        self.look = self.token()

    def token(self):
        text = self.text
        while self.at < len(text):
            if text[self.at].isspace():
                self.at += 1
            elif text.startswith("//", self.at):
                end = text.find("\n", self.at + 2)
                self.at = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.at):
                end = text.find("*/", self.at + 2)
                insist(end >= 0, "unterminated comment")
                self.at = end + 2
            else:
                break
        self.tokens += 1
        insist(self.tokens <= 4000000, "token limit exceeded")
        start = self.at
        if start == len(text):
            return {"kind": "end", "start": start, "end": start}
        char = text[self.at]
        self.at += 1
        if char in "{}=;,":
            return {"kind": char, "start": start, "end": self.at}
        if char == '"':
            value = []
            while self.at < len(text):
                char = text[self.at]
                self.at += 1
                if char == '"':
                    return {"kind": "string", "value": "".join(value),
                            "start": start, "end": self.at}
                if char == "\\":
                    insist(self.at < len(text) and text[self.at] in '\\"', "unsupported string escape")
                    char = text[self.at]
                    self.at += 1
                insist(ord(char) >= 32 and ord(char) != 127, "control byte in string")
                value.append(char)
                insist(len(value) <= 65536, "string limit exceeded")
            insist(False, "unterminated string")
        if char == "\\":
            end = text.find("\\", self.at)
            insist(end >= 0, "unterminated binary reference")
            value = text[self.at:end]
            insist(re.match(r"^(XDR|XDR_COPY):(0|[1-9]\d{0,6})$", value), "invalid binary reference")
            self.at = end + 1
            return {"kind": "binary", "value": value, "start": start, "end": self.at}
        while (self.at < len(text) and not text[self.at].isspace()
               and text[self.at] not in '{}=;,"\\'
               and not text.startswith(("//", "/*"), self.at)):
            self.at += 1
        value = text[start:self.at]
        insist(len(value) <= 256, "token length exceeded")
        if NUMBER.match(value):
            number = float(value)
            insist(not math.isinf(number) and not math.isnan(number)
                   and (number != 0 or not re.search("[1-9]", re.split("[eE]", value)[0])),
                   "nonfinite or underflowing number")
            return {"kind": "number", "value": number, "start": start, "end": self.at}
        insist(NAME.match(value) or re.match(r"^\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", value),
               "unsupported token at byte " + str(start))
        return {"kind": "name", "value": value, "start": start, "end": self.at}

    def take(self, kind):
        insist(self.look["kind"] == kind, "expected " + kind + " at byte " + str(self.at))
        value = self.look
        self.look = self.token()
        return value

    def block(self, depth=0):
        insist(depth <= 32, "nesting limit exceeded")
        result, names = [], set()
        while self.look["kind"] not in ("end", "}"):
            key = self.take("name")["value"]
            while self.look["kind"] == "name" and self.look["value"].startswith("."):
                key += self.take("name")["value"]
            self.entries += 1
            insist(self.entries <= 1000000, "entry limit exceeded")
            self.take("=")
            insist(key not in names or self.look["kind"] == "{", "duplicate scalar " + key)
            names.add(key)
            item = {"key": key, "kind": self.look["kind"], "start": self.look["start"]}
            if self.look["kind"] == "{":
                self.take("{")
                if key.endswith("[]"):
                    values = []
                    while self.look["kind"] != "}":
                        values.append(self.take("number")["value"])
                        insist(len(values) <= 1000000, "numeric array limit exceeded")
                        if self.look["kind"] != "}":
                            self.take(",")
                    item.update(kind="array", values=values)
                else:
                    item.update(kind="block", children=self.block(depth + 1))
                item["end"] = self.take("}")["end"]
            else:
                insist(self.look["kind"] in ("string", "number", "binary"), "invalid scalar")
                item.update(value=self.look["value"], end=self.look["end"])
                self.look = self.token()
            self.take(";")
            result.append(item)
        insist(depth != 0 or self.look["kind"] == "end", "unexpected top-level terminator")
        return result


def read(data):
    return Parser(data).block()


def one(items, key, kind):
    found = [item for item in items if item["key"] == key]
    insist(len(found) == 1 and found[0]["kind"] == kind, "missing or ambiguous " + key)
    return found[0]


def block(items, key):
    return one(items, key, "block")["children"]


def number(items, key):
    return one(items, key, "number")["value"]


def string(items, key):
    return one(items, key, "string")["value"]


def flag(items, key):
    value = number(items, key)
    insist(value in (0, 1), "invalid boolean " + key)
    return bool(value)


def manager(beam):
    result = block(beam, "CPManager")
    return block(result, "CPManagerObject") if any(i["key"] == "CPManagerObject" for i in result) else result


def canonical(items, parent=""):
    result = []
    for item in items:
        key, kind = item["key"], item["kind"]
        if ((parent == "ObjectVersion" and key == "WriteTimeStamp")
                or (parent == "BrachyImplantDateAndTime" and key == "UTC_ISODateAndTimeString")):
            continue
        value = canonical(item["children"], key) if kind == "block" else (
            item["values"] if kind == "array" else item["value"])
        result.append([key, kind, value])
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(items):
    return digest(json.dumps(canonical(items), ensure_ascii=True, separators=(",", ":")).encode("ascii"))


def geometry(items):
    insist(not flag(items, "HasFixedJaw") and not flag(items, "HasmMLC"),
           "fixed-jaw and secondary-MLC configurations are not yet supported")
    mlc = block(items, "MultiLeaf")
    decimals = number(mlc, "DecimalPlaces")
    insist(decimals == int(decimals) and 0 <= decimals <= 4, "unsupported position precision")
    profile = {
        "name": string(items, "Name"), "version": string(items, "VersionTimestamp"),
        "alignedX": flag(mlc, "AlignedWithLeftRightJaw"), "replacesJaw": flag(mlc, "ReplacesJaw"),
        "negateDisplay": flag(mlc, "NegateLeafCoordinates"), "precision": 10.0 ** -decimals,
        "minGap": number(mlc, "MinStaticLeafGap"),
        "minDynamicGap": number(mlc, "MinDynamicLeafGap"),
        "overlapAllowed": flag(mlc, "OpposingAdjacentLeavesCanOverlap"),
        "maxDifference": number(mlc, "MaxOverallLeafDifference"),
        "tongue": number(mlc, "TongueAndGrooveLeakageWidth"),
        "hasCarriage": flag(mlc, "HasCarriage"), "leaves": []
    }
    insist(0 <= profile["minGap"] <= 1 and 0 <= profile["minDynamicGap"] <= 1
           and 0 <= profile["tongue"] <= 1
           and 0 <= profile["maxDifference"] <= 100, "unsupported closure constraints")
    if profile["hasCarriage"]:
        profile["maxFromJaw"] = number(mlc, "MaxTipPositionFromJaw")
        insist(0 < profile["maxFromJaw"] <= 100, "invalid carriage limit")
    # RawData uses internal coordinates; display-negation is not a second transform.
    for entry in block(mlc, "LeafPairList"):
        insist(entry["key"] == "MLCLeafPair" and entry["kind"] == "block", "invalid leaf list")
        leaf = entry["children"]
        insist(flag(leaf, "NegateLeafCoordinate") == profile["negateDisplay"], "inconsistent display coordinates")
        row = dict((key, number(leaf, field)) for key, field in (
            ("center", "YCenterPosition"), ("width", "Width"),
            ("min", "MinTipPosition"), ("max", "MaxTipPosition"), ("tip", "TipLeakageWidth")))
        insist(0 < row["width"] <= 10 and -100 <= row["min"] < row["max"] <= 100
               and 0 <= row["tip"] <= 2 and abs(row["center"]) <= 100, "invalid leaf dimensions")
        profile["leaves"].append(row)
    leaves = profile["leaves"]
    insist(1 <= len(leaves) <= 500, "unsupported leaf count")
    for a, b in zip(leaves, leaves[1:]):
        insist(abs(a["center"] + a["width"] / 2 - b["center"] + b["width"] / 2) < 0.00001,
               "leaf strips must be ordered and contiguous")
    return profile


def validate_positions(values, machine, jaws):
    leaves = machine["leaves"]
    insist(len(values) == 2 * len(leaves), "leaf coordinate count mismatch")
    for i, leaf in enumerate(leaves):
        a, b = values[2 * i:2 * i + 2]
        insist(leaf["min"] - 1e-6 <= a <= leaf["max"] + 1e-6
               and leaf["min"] - 1e-6 <= b <= leaf["max"] + 1e-6, "leaf travel limit at pair " + str(i))
        insist(a + b >= machine["minGap"] - 1e-6, "minimum static gap at pair " + str(i))
        if machine["hasCarriage"]:
            insist(a >= jaws[0] - machine["maxFromJaw"] - 1e-6
                   and b >= jaws[1] - machine["maxFromJaw"] - 1e-6, "carriage limit at pair " + str(i))
        if i and not machine["overlapAllowed"]:
            insist(values[2 * i - 2] + b >= machine["minGap"] - 1e-6
                   and values[2 * i - 1] + a >= machine["minGap"] - 1e-6,
                   "opposing adjacent overlap at pair " + str(i))
    for bank in (values[0::2], values[1::2]):
        insist(machine["maxDifference"] == 0 or max(bank) - min(bank) <= machine["maxDifference"] + 1e-6,
               "maximum bank difference exceeded")


def minimum_opening(a, b, machine, leaf):
    step = Decimal(str(machine["precision"]))
    gap_units = (Decimal(str(machine["minGap"])) / step).to_integral_value(rounding=ROUND_CEILING)
    units = ((gap_units - (Decimal(str(b)) - Decimal(str(a))) / step) / 2).to_integral_value(rounding=ROUND_HALF_UP)
    low = max(Decimal(str(leaf["min"])) / step, gap_units - Decimal(str(b)) / step,
              gap_units - Decimal(str(leaf["max"])) / step).to_integral_value(rounding=ROUND_CEILING)
    high = min(Decimal(str(a)) / step, Decimal(str(leaf["max"])) / step,
               gap_units - Decimal(str(leaf["min"])) / step).to_integral_value(rounding=ROUND_FLOOR)
    insist(low <= high, "no quantized minimum opening inside the original aperture")
    units = max(low, min(high, units))
    return [float(units * step) or 0.0, float((gap_units - units) * step) or 0.0]


def leaf_values(cp):
    raw = block(block(cp, "MLCLeafPositions"), "RawData")
    insist(number(raw, "NumberOfDimensions") == 2, "unsupported leaf array dimensions")
    values = one(raw, "Points[]", "array")
    insist(number(raw, "NumberOfPoints") * 2 == len(values["values"]), "invalid point count")
    return values


def analyze(trial_bytes, machine_bytes):
    trial = read(trial_bytes)
    beams = block(trial, "BeamList")
    insist(0 < len(beams) <= 64 and len(machine_bytes) == len(beams), "one machine snapshot per beam required")
    result = {"trialName": string(trial, "Name"), "beamCount": len(beams), "segmentCount": 0,
              "changes": [], "observations": [], "curves": [], "profiles": [], "blockers": []}
    for bi, entry in enumerate(beams):
        insist(entry["key"] == "Beam" and entry["kind"] == "block", "invalid beam list")
        beam = entry["children"]
        insist(string(beam, "SetBeamType") == "Step & Shoot MLC" and flag(beam, "UseMLC")
               and string(beam, "Modality") == "Photons", "only static photon Step & Shoot MLC supported")
        machine = geometry(read(machine_bytes[bi]))
        insist(string(beam, "MachineNameAndVersion") == machine["name"] + ": " + machine["version"],
               "machine name/version mismatch")
        result["profiles"].append(machine)
        cp_manager = manager(beam)
        segments = block(cp_manager, "ControlPointList")
        insist(1 <= len(segments) <= 999 and len(segments) == number(cp_manager, "NumberOfControlPoints"),
               "invalid segment count")
        result["segmentCount"] += len(segments)
        insist(result["segmentCount"] <= 10000, "segment limit exceeded")
        static = None
        for ci, segment in enumerate(segments):
            insist(segment["key"] == "#" + str(ci) and segment["kind"] == "block", "nonconsecutive segments")
            cp = segment["children"]
            angles_jaws = [number(cp, key) for key in ("Gantry", "Couch", "Collimator", "LeftJawPosition",
                                                      "RightJawPosition", "TopJawPosition", "BottomJawPosition")]
            if static is None:
                static = angles_jaws
            insist(static == angles_jaws, "moving jaws or angles are unsupported")
            left, right, top, bottom = angles_jaws[3:]
            insist(all(abs(v) <= 100 for v in (left, right, top, bottom))
                   and left + right > 0 and top + bottom > 0, "invalid jaw rectangle")
            travel, strip = ((left, right), (top, bottom)) if machine["alignedX"] else ((top, bottom), (left, right))
            points = leaf_values(cp)
            values = points["values"][:]
            validate_positions(values, machine, travel)
            changed = []
            for li, leaf in enumerate(machine["leaves"]):
                a, b = values[2 * li:2 * li + 2]
                step = machine["precision"]
                closed_gap = float((Decimal(str(machine["minGap"])) / Decimal(str(step)))
                                   .to_integral_value(rounding=ROUND_CEILING)) * step
                # Pinnacle treats a gap <= MinDynamicLeafGap + 0.0001 cm as closed.
                gap = Decimal(str(a)) + Decimal(str(b))
                closed_limit = max(Decimal(str(closed_gap)) + Decimal("0.000001"),
                                   Decimal(str(machine["minDynamicGap"])) + Decimal("0.0001"))
                if gap <= closed_limit:
                    continue
                v0, v1 = leaf["center"] - leaf["width"] / 2, leaf["center"] + leaf["width"] / 2
                behind_strip = (v1 + machine["tongue"] / 2 < -strip[0] - step
                                or v0 - machine["tongue"] / 2 > strip[1] + step)
                behind_travel = not machine["replacesJaw"] and (
                    b + leaf["tip"] / 2 < -travel[0] - step or -a - leaf["tip"] / 2 > travel[1] + step)
                finding = {"beamIndex": bi, "segmentIndex": ci, "leafIndex": li,
                           "beamName": string(beam, "Name"), "before": [a, b]}
                if not behind_strip and not behind_travel:
                    finding["classification"] = "active-or-boundary"
                    result["observations"].append(finding)
                    continue
                finding["classification"] = "behind-strip-jaw" if behind_strip else "behind-travel-jaw"
                try:
                    after = minimum_opening(a, b, machine, leaf)
                except ValueError as error:
                    result["blockers"].append(str(error))
                    continue
                values[2 * li:2 * li + 2] = after
                finding["after"] = after
                changed.append(finding)
            if changed:
                try:
                    validate_positions(values, machine, travel)
                except ValueError as error:
                    result["blockers"].append("Beam %d Segment %d: %s" % (bi + 1, ci + 1, error))
                curve = one(cp, "MLCLeafPositions", "block")
                body = trial_bytes[curve["start"] + 1:curve["end"] - 1]
                start = points["start"] - curve["start"] - 1
                end = points["end"] - curve["start"] - 1
                edited = native(body[:start] + ("{\n" + ",".join("%.12g" % v for v in values)
                                               + "\n}").encode("ascii") + body[end:])
                insist(one(block(read(edited), "RawData"), "Points[]", "array")["values"] == values,
                       "curve serialization mismatch")
                result["curves"].append({"beamIndex": bi, "segmentIndex": ci,
                                         "values": values, "bytes": edited, "sha256": digest(edited)})
                result["changes"].extend(changed)
    return result


def verify_items(before, after, curves):
    expected = copy.deepcopy(before)
    for curve in curves:
        beam = block(expected, "BeamList")[curve["beamIndex"]]["children"]
        cp = block(manager(beam), "ControlPointList")[curve["segmentIndex"]]["children"]
        leaf_values(cp)["values"] = curve["values"]
    insist(fingerprint(expected) == fingerprint(after), "readback differs from expected geometry or protected fields")


def verify_all(before, after, name, curves):
    insist(len(before) == len(after), "Trial count changed")
    targets = 0
    for a, b in zip(before, after):
        insist(a["key"] == b["key"] == "Trial" and a["kind"] == b["kind"] == "block", "invalid Trial inventory")
        if string(a["children"], "Name") == name:
            targets += 1
            verify_items(a["children"], b["children"], curves)
        else:
            insist(fingerprint(a["children"]) == fingerprint(b["children"]), "protected Trial changed")
    insist(targets == 1, "ambiguous target Trial")
