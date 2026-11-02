from __future__ import print_function

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_jaw_mlc import fixture, multiple_segments, replace
import test_jaw_mlc
import core
import helper


def arc_fixture(pairs=None, aligned=1, replaces=0, negate=0, angles=None):
    f = fixture(aligned=aligned, replaces=replaces, negate=negate)
    if pairs is None:
        pairs = [[0.2, 0.2], [0.2, 0.2], [0.02, 0.03]]
    values = []
    for pair in pairs:
        row = f["values"][:]
        row[:2] = pair
        values.append(row)
    trial = multiple_segments(f, values)
    trial = replace(trial, 'SetBeamType="Step & Shoot MLC"', 'SetBeamType="Dynamic Arc"')
    parsed = core.read(trial)
    beam = core.block(parsed, "BeamList")[0]["children"]
    cps = core.block(core.manager(beam), "ControlPointList")
    text = trial[:-9].decode("latin1")
    angles = angles or [350 + 20.0 * i / (len(cps) - 1) for i in range(len(cps))]
    for i, cp in reversed(list(enumerate(cps))):
        body = text[cp["start"] + 1:cp["end"] - 1]
        body = body.replace("Gantry=90;", "Gantry=%.12g;" % (angles[i] % 360))
        body += ("DeliveryTime=%g;DoseRate=%g;PercentOfArc=%.12g;"
                 "ODM=\"synthetic untouched placeholder\";" %
                 (0 if i == len(cps) - 1 else 2, 0 if i == len(cps) - 1 else 600,
                  float(i) / (len(cps) - 1)))
        text = text[:cp["start"] + 1] + body + text[cp["end"] - 1:]
    f["trial"] = core.native(text)
    return f


def apply_curves(trial_bytes, curves):
    after = copy.deepcopy(core.read(trial_bytes))
    for curve in curves:
        beam = core.block(after, "BeamList")[curve["beamIndex"]]["children"]
        cp = core.block(core.manager(beam), "ControlPointList")[curve["segmentIndex"]]["children"]
        core.leaf_values(cp)["values"] = curve["values"][:]
    return after


def edited_bytes(trial_bytes, curves):
    parsed = core.read(trial_bytes)
    edits = []
    for curve in curves:
        beam = core.block(parsed, "BeamList")[curve["beamIndex"]]["children"]
        cp = core.block(core.manager(beam), "ControlPointList")[curve["segmentIndex"]]["children"]
        points = core.leaf_values(cp)
        edits.append((points["start"], points["end"],
                      ("{" + ",".join("%.12g" % v for v in curve["values"]) + "}").encode("ascii")))
    body = trial_bytes[:-9]
    for start, end, value in sorted(edits, reverse=True):
        body = body[:start] + value + body[end:]
    return core.native(body)


class VmatTests(unittest.TestCase):
    def test_full_arc_constant_closure_preserves_closed_cps_and_all_other_fields(self):
        for aligned in (0, 1):
            for replaces in (0, 1):
                for negate in (0, 1):
                    f = arc_fixture(aligned=aligned, replaces=replaces, negate=negate)
                    result = core.analyze(f["trial"], [f["machine"]])
                    self.assertEqual(result["blockers"], [])
                    self.assertEqual(len(result["changes"]), 2)
                    self.assertEqual([x["segmentIndex"] for x in result["curves"]], [0, 1])
                    self.assertTrue(all(x["after"] == [0.02, 0.03] for x in result["changes"]))
                    self.assertTrue(all(x["closureStrategy"] == "arc-closed-cps-consensus"
                                        for x in result["changes"]))
                    after = apply_curves(f["trial"], result["curves"])
                    core.verify_items(core.read(f["trial"]), after, result["curves"])
                    cps = core.block(core.manager(core.block(after, "BeamList")[0]["children"]), "ControlPointList")
                    self.assertTrue(all(core.leaf_values(cp["children"])["values"][:2] == [0.02, 0.03]
                                        for cp in cps))
                    self.assertTrue(all(curve["values"][2:] == f["values"][2:] for curve in result["curves"]))

    def test_no_closed_reference_uses_common_aperture_and_dynamic_minimum(self):
        f = arc_fixture(pairs=[[0.2, 0.3], [0.3, 0.2], [0.4, 0.3]])
        result = core.analyze(f["trial"], [f["machine"]])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(len(result["changes"]), 3)
        target = result["changes"][0]["after"]
        self.assertAlmostEqual(sum(target), 0.05)
        self.assertTrue(all(x["after"] == target for x in result["changes"]))
        self.assertTrue(all(x["closureStrategy"] == "arc-common-aperture" for x in result["changes"]))
        self.assertTrue(core.closed_opening(target, core.geometry(core.read(f["machine"]))))

    def test_nonrepresentable_dynamic_closure_is_refused(self):
        f = arc_fixture(pairs=[[0.2, 0.2]] * 3)
        machine = replace(f["machine"], "MinDynamicLeafGap=0.05;", "MinDynamicLeafGap=0.051;")
        result = core.analyze(f["trial"], [machine])
        self.assertTrue(result["blockers"])
        self.assertEqual(result["curves"], [])
        self.assertEqual(result["changes"], [])

    def test_conflicting_closed_positions_are_blockers_not_relocated(self):
        f = arc_fixture(pairs=[[0.2, 0.2], [0.02, 0.03], [2, -1.95]])
        result = core.analyze(f["trial"], [f["machine"]])
        self.assertEqual(result["changes"], [])
        self.assertIn("different positions", result["blockers"][0])

    def test_subdynamic_reference_and_unquantized_reference_are_refused(self):
        for target in ([0, 0], [0.026, 0.024]):
            f = arc_fixture(pairs=[[0.2, 0.2], [0.2, 0.2], target])
            result = core.analyze(f["trial"], [f["machine"]])
            self.assertTrue(result["blockers"])
            self.assertFalse(result["curves"])

    def test_active_boundary_and_closed_only_plan_are_no_ops(self):
        f = arc_fixture(pairs=[[0.02, 0.03]] * 3)
        result = core.analyze(f["trial"], [f["machine"]])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["curves"], [])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(len(result["observations"]), 3)
        after = copy.deepcopy(core.read(f["trial"]))
        core.verify_items(core.read(f["trial"]), after, [])

    def test_moving_jaws_couch_collimator_and_missing_delivery_data_are_refused(self):
        f = arc_fixture()
        text = f["trial"][:-9].decode("latin1")
        for before, after in [
                ("TopJawPosition=1;", "TopJawPosition=1.1;"),
                ("Couch=0;", "Couch=1;"),
                ("Collimator=270;", "Collimator=271;"),
                ("DeliveryTime=2;", "DeliveryTime=0;"),
                ("DoseRate=600;", "DoseRate=-1;"),
                ("Weight=1;", "Weight=-1;"),
                ("PercentOfArc=0.5;", "PercentOfArc=0;"),
                ("PercentOfArc=1;", "PercentOfArc=0.9;"),
                ("DeliveryTime=2;", "")]:
            changed = core.native(text.replace(before, after, 1))
            with self.assertRaises(ValueError):
                core.analyze(changed, [f["machine"]])

    def test_wrap_and_direction_do_not_change_jaw_local_geometry(self):
        for angles in ([350, 0, 10], [10, 0, 350], [90, 180, 270]):
            f = arc_fixture(angles=angles)
            result = core.analyze(f["trial"], [f["machine"]])
            self.assertEqual(result["blockers"], [])
            self.assertEqual(len(result["changes"]), 2)

    def test_travel_jaw_hull_cannot_cross_the_active_region(self):
        f = arc_fixture()
        # Move the candidate to a strip inside the jaws, behind opposite travel jaws.
        original = core.read(f["trial"])
        cps = core.block(core.manager(core.block(original, "BeamList")[0]["children"]), "ControlPointList")
        text = f["trial"][:-9].decode("latin1")
        pairs = [[-3, 3.2], [3.2, -3], [-3, 3.05]]
        for i, item in reversed(list(enumerate(cps))):
            cp = item["children"]
            points = core.leaf_values(cp)
            vals = points["values"][:]
            vals[:2] = [0.02, 0.03]
            vals[6:8] = pairs[i]
            text = text[:points["start"]] + "{" + ",".join("%.12g" % v for v in vals) + "}" + text[points["end"]:]
        result = core.analyze(core.native(text), [f["machine"]])
        self.assertFalse(result["curves"])
        self.assertTrue(any("swept aperture" in x for x in result["blockers"]))

    def test_global_closure_must_not_change_unmasked_cp(self):
        f = arc_fixture(pairs=[[0.02, 0.03]] * 3)
        trial = f["trial"]
        parsed = core.read(trial)
        cps = core.block(core.manager(core.block(parsed, "BeamList")[0]["children"]), "ControlPointList")
        text = trial[:-9].decode("latin1")
        for i, item in reversed(list(enumerate(cps))):
            points = core.leaf_values(item["children"])
            vals = points["values"][:]
            vals[6:8] = [-3, 3.2] if i != 1 else [1, 1]
            text = text[:points["start"]] + "{" + ",".join("%.12g" % v for v in vals) + "}" + text[points["end"]:]
        result = core.analyze(core.native(text), [f["machine"]])
        self.assertFalse(result["curves"])
        self.assertIn("active/boundary", result["blockers"][0])

    def test_already_closed_but_visible_reference_blocks_unsafe_motion(self):
        f = arc_fixture(pairs=[[0.02, 0.03]] * 3)
        parsed = core.read(f["trial"])
        cps = core.block(core.manager(core.block(parsed, "BeamList")[0]["children"]), "ControlPointList")
        text = f["trial"][:-9].decode("latin1")
        for i, item in reversed(list(enumerate(cps))):
            points = core.leaf_values(item["children"])
            vals = points["values"][:]
            vals[6:8] = [0.02, 0.03] if i == 2 else [-3, 3.2]
            text = text[:points["start"]] + "{" + ",".join("%.12g" % v for v in vals) + "}" + text[points["end"]:]
        result = core.analyze(core.native(text), [f["machine"]])
        self.assertFalse(result["curves"])
        self.assertTrue(result["blockers"])

    def test_curve_import_readback_protects_delivery_parameters_and_odm(self):
        f = arc_fixture()
        result = core.analyze(f["trial"], [f["machine"]])
        for field in ("DeliveryTime", "DoseRate", "PercentOfArc", "Weight", "Gantry"):
            after = apply_curves(f["trial"], result["curves"])
            cp = core.block(core.manager(core.block(after, "BeamList")[0]["children"]),
                            "ControlPointList")[0]["children"]
            core.one(cp, field, "number")["value"] += 0.1
            with self.assertRaises(ValueError):
                core.verify_items(core.read(f["trial"]), after, result["curves"])

        after = apply_curves(f["trial"], result["curves"])
        cp = core.block(core.manager(core.block(after, "BeamList")[0]["children"]),
                        "ControlPointList")[0]["children"]
        core.one(cp, "ODM", "string")["value"] = "changed"
        with self.assertRaises(ValueError):
            core.verify_items(core.read(f["trial"]), after, result["curves"])

    def test_cleanup_is_idempotent_for_both_arc_closure_strategies(self):
        for pairs in (None, [[0.2, 0.3], [0.3, 0.2], [0.4, 0.3]]):
            f = arc_fixture(pairs=pairs)
            result = core.analyze(f["trial"], [f["machine"]])
            self.assertFalse(result["blockers"])
            self.assertTrue(result["changes"])
            cleaned = edited_bytes(f["trial"], result["curves"])
            again = core.analyze(cleaned, [f["machine"]])
            self.assertFalse(again["blockers"])
            self.assertEqual(again["changes"], [])
            self.assertEqual(again["curves"], [])


class VmatRuntimeTests(unittest.TestCase):
    setUp = getattr(test_jaw_mlc.RuntimeTests.setUp, "__func__", test_jaw_mlc.RuntimeTests.setUp)
    tearDown = getattr(test_jaw_mlc.RuntimeTests.tearDown, "__func__", test_jaw_mlc.RuntimeTests.tearDown)
    run_stage = getattr(test_jaw_mlc.RuntimeTests.run_stage, "__func__", test_jaw_mlc.RuntimeTests.run_stage)
    prepare = getattr(test_jaw_mlc.RuntimeTests.prepare, "__func__", test_jaw_mlc.RuntimeTests.prepare)

    def snapshot(self, directory, f, prefix=""):
        all_trials = core.native(b"Trial={" + f["trial"][:-9] + b"};")
        for name, data in (("current", f["trial"]), ("all", all_trials),
                           ("machine-0", f["machine"]), ("plan", b"/var/tmp/research-plan\n")):
            with open(directory + "/" + prefix + name + ".txt", "wb") as stream:
                stream.write(data)
        for name in ("dose", "optimizing", "resetting", "computing"):
            with open(directory + "/" + prefix + name + ".txt", "wb") as stream:
                stream.write(b"0\n")

    def test_vmat_single_entry_guard_import_verify_and_noop(self):
        f = arc_fixture()
        directory = self.prepare("run", f)
        self.assertIn("AskYesNoDefault = 0", helper.text(directory + "/result.Script"))
        self.assertFalse(os.path.exists(directory + "/mutation.claim"))
        self.snapshot(directory, f, "live-")
        self.run_stage("claim", directory)
        guard = helper.text(directory + "/guard.Script")
        self.assertIn('BeamType.STRINGEQUALTO.#"#Dynamic Arc"', guard)
        for field in ("DeliveryTime", "DoseRate", "PercentOfArc", "Gantry", "Weight"):
            self.assertIn("." + field + ".GREATERTHANOREQUALTO.", guard)
        report = helper.load(directory + "/report.json")
        after = dict(f, trial=edited_bytes(f["trial"], report["curves"]))
        self.snapshot(directory, after, "after-")
        self.run_stage("finish", directory)
        self.assertTrue(helper.load(directory + "/completed.json")["geometryVerified"])
        self.assertFalse(helper.load(directory + "/completed.json")["exportReady"])
        verified = self.prepare("verify", after)
        self.assertTrue(helper.load(verified + "/verification.json")["geometryVerified"])
        noop = self.prepare("run", after)
        self.assertIn("no masked openings", helper.text(noop + "/result.Script"))
        self.assertFalse(os.path.exists(noop + "/mutation.claim"))

    def test_vmat_confirmation_change_and_motion_blockers_prevent_claim(self):
        f = arc_fixture()
        directory = self.prepare("run", f)
        changed = dict(f, trial=replace(f["trial"], "DeliveryTime=2;", "DeliveryTime=3;"))
        self.snapshot(directory, changed, "live-")
        self.run_stage("claim", directory, False)
        self.assertFalse(os.path.exists(directory + "/mutation.claim"))
        blocked = self.prepare("run", arc_fixture(pairs=[[0.2, 0.2], [0.02, 0.03], [2, -1.95]]), False)
        self.assertTrue(helper.load(blocked + "/report.json")["blockers"])
        self.assertFalse(os.path.exists(blocked + "/mutation.claim"))
        self.assertNotIn("MLCLeafPositions.Load", helper.text(blocked + "/result.Script"))


if __name__ == "__main__":
    unittest.main()
