from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core
import build
import helper


def fixture(count=6, aligned=1, replaces=0, negate=0, gap=0, overlap=1, positions=None):
    leaves = "".join(
        "MLCLeafPair={YCenterPosition=%g;Width=1;NegateLeafCoordinate=%d;"
        "MinTipPosition=-20;MaxTipPosition=20;TipLeakageWidth=0.1;};" % (i - count / 2.0 + 0.5, negate)
        for i in range(count))
    machine = core.native(
        'Name="AnyVendorModel";VersionTimestamp="test-version";HasFixedJaw=0;HasmMLC=0;MultiLeaf={'
        'AlignedWithLeftRightJaw=%d;ReplacesJaw=%d;NegateLeafCoordinates=%d;DecimalPlaces=2;'
        'MinStaticLeafGap=%g;MinDynamicLeafGap=0.05;OpposingAdjacentLeavesCanOverlap=%d;MaxOverallLeafDifference=20;'
        'TongueAndGrooveLeakageWidth=0.04;HasCarriage=0;LeafPairList={%s};};'
        % (aligned, replaces, negate, gap, overlap, leaves))
    values = [gap / 2.0] * (count * 2)
    values[:2] = [0.2, 0.2]
    values[2 * (count // 2):2 * (count // 2) + 2] = [1, 1]
    if positions:
        for i, pair in positions.items():
            values[2 * i:2 * i + 2] = pair

    def trial_text(points):
        return ('Name="CPC_RESEARCH_PYTEST";BeamList={Beam={Name="Beam_1";'
                'MachineNameAndVersion="AnyVendorModel: test-version";MachineEnergyName="6MV";'
                'SetBeamType="Step & Shoot MLC";UseMLC=1;Modality="Photons";CPManager={CPManagerObject={'
                'NumberOfControlPoints=1;ControlPointList={#0={Gantry=90;Couch=0;Collimator=270;'
                'LeftJawPosition=1;RightJawPosition=1;TopJawPosition=1;BottomJawPosition=1;'
                'Weight=1;WeightLocked=0;MLCLeafPositions={RawData={NumberOfDimensions=2;'
                'NumberOfPoints=%d;Points[]={%s};};};};};};};};};'
                % (count, ",".join("%.12g" % v for v in points)))
    return {"machine": machine, "values": values, "trial": core.native(trial_text(values)), "trial_text": trial_text}


def replace(data, old, new):
    body = data[:data.rfind(b"/* ")].decode("latin1")
    if old not in body:
        raise AssertionError("Replacement target absent: " + old)
    return core.native(body.replace(old, new))


def multiple_segments(f, values):
    source = f["trial"].decode("latin1")
    beam = core.block(core.read(f["trial"]), "BeamList")[0]["children"]
    cp_list = core.one(core.manager(beam), "ControlPointList", "block")
    bodies = []
    for points in values:
        data = f["trial_text"](points)
        trial = core.read(core.native(data))
        cp = core.block(core.manager(core.block(trial, "BeamList")[0]["children"]),
                        "ControlPointList")[0]
        bodies.append(data[cp["start"]:cp["end"]])
    body = source[:cp_list["start"]] + "{" + "".join(
        "#%d=%s;" % (i, cp) for i, cp in enumerate(bodies)) + "}" + source[cp_list["end"]:]
    body = body[:body.rfind("/* ")]
    return core.native(body.replace("NumberOfControlPoints=1;", "NumberOfControlPoints=%d;" % len(values)))


class GeometryTests(unittest.TestCase):
    def test_consensus_alignment_outside_original_opening_for_masked_strips(self):
        for aligned in (0, 1):
            for replaces in (0, 1):
                f = fixture(aligned=aligned, replaces=replaces)
                reference = f["values"][:]
                reference[:2] = [6, -6]
                trial = multiple_segments(f, [f["values"], reference, reference])
                result = core.analyze(trial, [f["machine"]])
                self.assertEqual(len(result["changes"]), 1)
                finding = result["changes"][0]
                self.assertEqual(finding["after"], [6, -6])
                self.assertEqual(finding["closureStrategy"], "other-cps-consensus")
                self.assertEqual(result["blockers"], [])
                self.assertEqual(result["curves"][0]["values"][2:], f["values"][2:])

    def test_consensus_requires_all_other_cps_closed_identical_and_quantized(self):
        f = fixture()
        for pairs in (([6, -6], [7, -7]), ([6, -6], [0.2, 0.2]),
                      ([6.001, -6.001], [6.001, -6.001])):
            references = []
            for pair in pairs:
                values = f["values"][:]
                values[:2] = pair
                references.append(values)
            result = core.analyze(multiple_segments(f, [f["values"]] + references), [f["machine"]])
            finding = next(c for c in result["changes"] if c["segmentIndex"] == 0)
            self.assertEqual(finding["after"], [0, 0])
            self.assertEqual(finding["closureStrategy"], "original-midpoint")

    def test_consensus_preserves_positive_reference_gap_and_already_closed_cps(self):
        f = fixture(gap=0.02)
        reference = f["values"][:]
        reference[:2] = [6, -5.95]
        result = core.analyze(multiple_segments(f, [f["values"], reference]), [f["machine"]])
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["after"], [6, -5.95])
        self.assertEqual(core.analyze(multiple_segments(f, [reference, reference]),
                                     [f["machine"]])["changes"], [])

    def test_consensus_rejects_unmasked_sweep_between_travel_jaws(self):
        f = fixture(positions={0: [0, 0], 3: [-3, 3.2]})
        for reference_pair, expected in (([-4, 4], [-4, 4]), ([3, -3], [-3.1, 3.1]),
                                         ([0, 0], [-3.1, 3.1])):
            reference = f["values"][:]
            reference[6:8] = reference_pair
            result = core.analyze(multiple_segments(f, [f["values"], reference]), [f["machine"]])
            finding = next(c for c in result["changes"] if c["segmentIndex"] == 0)
            self.assertEqual(finding["after"], expected)

    def test_consensus_constraint_failure_falls_back_without_moving_neighbors(self):
        f = fixture()
        reference = [6, -6] * 6
        reference[6:8] = [1, 1]
        machine = replace(f["machine"], "MaxOverallLeafDifference=20;", "MaxOverallLeafDifference=7;")
        # The reference CP is legal, but mixing its leaf with this CP's other leaves is not.
        original = f["values"][:]
        original[2:4] = [-2, 2]
        result = core.analyze(multiple_segments(f, [original, reference]), [machine])
        finding = next(c for c in result["changes"] if c["segmentIndex"] == 0)
        self.assertEqual(finding["after"], [0, 0])
        self.assertEqual(finding["closureStrategy"], "original-midpoint")
        self.assertIn("maximum bank difference", finding["alignmentFallback"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["curves"][0]["values"][2:], original[2:])

    def test_generic_counts_directions_replacement_and_display_sign(self):
        for count in (6, 40, 60, 80):
            for aligned in (0, 1):
                for replaces in (0, 1):
                    for negate in (0, 1):
                        f = fixture(count, aligned, replaces, negate)
                        r = core.analyze(f["trial"], [f["machine"]])
                        self.assertEqual(len(r["changes"]), 1)
                        self.assertEqual(r["changes"][0]["leafIndex"], 0)
                        self.assertEqual(r["changes"][0]["after"], [0, 0])
                        self.assertEqual(r["blockers"], [])
                        self.assertEqual(r["curves"][0]["values"][count:count + 2], [1, 1])
                        after = core.native(f["trial_text"](r["curves"][0]["values"]))
                        core.verify_items(core.read(f["trial"]), core.read(after), r["curves"])
                        self.assertEqual(core.analyze(after, [f["machine"]])["changes"], [])

    def test_replaced_travel_jaw_is_never_used_as_a_physical_mask(self):
        for aligned in (0, 1):
            for replaces in (0, 1):
                f = fixture(aligned=aligned, replaces=replaces, positions={0: [0, 0], 3: [-3, 3.2]})
                r = core.analyze(f["trial"], [f["machine"]])
                self.assertEqual(len(r["changes"]), 0 if replaces else 1)
                if not replaces:
                    self.assertEqual(r["changes"][0]["after"], [-3.1, 3.1])

    def test_asymmetric_jaws_prove_axis_mapping_and_internal_coordinate_sign(self):
        for aligned in (0, 1):
            f = fixture(aligned=aligned)
            trial = replace(replace(f["trial"], "LeftJawPosition=1;", "LeftJawPosition=4;"),
                            "RightJawPosition=1;", "RightJawPosition=4;")
            r = core.analyze(trial, [f["machine"]])
            self.assertEqual(len(r["changes"]), 1 if aligned else 0)
            f = fixture(aligned=aligned, positions={0: [0, 0], 3: [-3, 3.2]})
            trial = replace(replace(f["trial"], "LeftJawPosition=1;", "LeftJawPosition=4;"),
                            "RightJawPosition=1;", "RightJawPosition=4;")
            r = core.analyze(trial, [f["machine"]])
            self.assertEqual(len(r["changes"]), 0 if aligned else 1)
        for negate in (0, 1):
            f = fixture(negate=negate)
            trial = replace(f["trial"], "BottomJawPosition=1;", "BottomJawPosition=4;")
            r = core.analyze(trial, [f["machine"]])
            self.assertEqual([c["leafIndex"] for c in r["changes"]], [0])

    def test_minimum_positive_gap_quantization_and_idempotence(self):
        for gap in (0.02, 0.035):
            f = fixture(gap=gap)
            r = core.analyze(f["trial"], [f["machine"]])
            self.assertEqual(r["blockers"], [])
            a, b = r["changes"][0]["after"]
            self.assertAlmostEqual(a + b, 0.02 if gap == 0.02 else 0.04)
            self.assertLessEqual(a, 0.2)
            self.assertLessEqual(b, 0.2)
            after = core.native(f["trial_text"](r["curves"][0]["values"]))
            self.assertEqual(core.analyze(after, [f["machine"]])["changes"], [])

    def test_native_dynamic_closed_gap_is_preserved_in_all_geometric_regions(self):
        for aligned in (0, 1):
            for gap in (0, 0.04, 0.05, 0.050099, 0.0501):
                f = fixture(aligned=aligned, positions={0: [gap / 2, gap / 2],
                                                      3: [-3, 3 + gap]})
                result = core.analyze(f["trial"], [f["machine"]])
                self.assertEqual(result["changes"], [], (aligned, gap))
                self.assertEqual(result["curves"], [])
        for gap in (0.0502, 0.06, 0.1):
            f = fixture(positions={0: [gap / 2, gap / 2], 3: [0.2, 0.2]})
            result = core.analyze(f["trial"], [f["machine"]])
            self.assertEqual([c["leafIndex"] for c in result["changes"]], [0])
            self.assertEqual(result["curves"][0]["values"][6:8], [0.2, 0.2])
        f = fixture()
        for machine in (replace(f["machine"], "MinDynamicLeafGap=0.05;", ""),
                        replace(f["machine"], "MinDynamicLeafGap=0.05;", "MinDynamicLeafGap=-1;")):
            with self.assertRaises(ValueError):
                core.analyze(f["trial"], [machine])

    def test_boundaries_and_active_small_openings_are_preserved(self):
        for jaw in (3, 2.5, 2.49, 2.48):
            f = fixture(positions={3: [0.01, 0.01]})
            field = "TopJawPosition"
            trial = replace(f["trial"], field + "=1;", field + "=%g;" % jaw)
            r = core.analyze(trial, [f["machine"]])
            self.assertEqual(r["changes"], [])

    def test_no_model_whitelist_but_unknown_config_is_rejected(self):
        f = fixture()
        for old, new in (("HasFixedJaw=0", "HasFixedJaw=1"), ("HasmMLC=0", "HasmMLC=1"),
                         ("AlignedWithLeftRightJaw=1", "AlignedWithLeftRightJaw=2"),
                         ("HasCarriage=0;", ""), ("NegateLeafCoordinate=0", "NegateLeafCoordinate=1"),
                         ("Width=1;", "Width=0;"), ("DecimalPlaces=2;", "DecimalPlaces=2.5;")):
            with self.assertRaises(ValueError):
                core.analyze(f["trial"], [replace(f["machine"], old, new)])
        with self.assertRaises(ValueError):
            core.analyze(replace(f["trial"], 'SetBeamType="Step & Shoot MLC"', 'SetBeamType="Arc"'), [f["machine"]])

    def test_constraints_cannot_silently_change_neighboring_leaves(self):
        f = fixture(overlap=0, positions={0: [0.2, 0.2], 1: [-0.1, 0.2], 2: [0.1, 0.1]})
        r = core.analyze(f["trial"], [f["machine"]])
        self.assertTrue(r["blockers"])
        self.assertIn("opposing adjacent", r["blockers"][0])
        f = fixture()
        machine = replace(f["machine"], "HasCarriage=0;", "HasCarriage=1;MaxTipPositionFromJaw=0.5;")
        with self.assertRaises(ValueError):
            core.analyze(f["trial"], [machine])
        machine = replace(f["machine"], "MaxOverallLeafDifference=20;", "MaxOverallLeafDifference=0.1;")
        with self.assertRaises(ValueError):
            core.analyze(f["trial"], [machine])

    def test_parser_checksum_limits_and_binary_preservation(self):
        for body in ('Name="x";Name="y";', 'A={B=1;', 'A=1e999;', 'A=1e-999;', 'A={};trailing',
                     'A="unterminated;', 'A=\\XDR:../x\\;', 'A=1;/* unterminated',
                     'A=1;};', 'A="bad\\n";'):
            with self.assertRaises(ValueError):
                core.read(core.native(body))
        data = core.native(b'Name="\xe9";A=\\XDR:0\\;Points[]={1,2};')
        self.assertEqual(core.checksum(data), 0)
        self.assertEqual(core.string(core.read(data), "Name"), u"\xe9")
        with self.assertRaises(ValueError):
            core.read(data + b"x")

    def test_full_field_verification_only_ignores_documented_save_effects(self):
        f = fixture()
        r = core.analyze(f["trial"], [f["machine"]])
        after = core.native(f["trial_text"](r["curves"][0]["values"]))
        for old, new in (("Weight=1;", "Weight=0.9;"), ("Gantry=90;", "Gantry=0;"),
                         ("LeftJawPosition=1;", "LeftJawPosition=2;")):
            with self.assertRaises(ValueError):
                core.verify_items(core.read(f["trial"]), core.read(replace(after, old, new)), r["curves"])

    def test_multiple_beams_segments_and_moving_geometry_rejection(self):
        f = fixture()
        trial = core.read(f["trial"])
        beam_list = core.one(trial, "BeamList", "block")
        beam = beam_list["children"][0]
        cp_list = core.one(core.manager(beam["children"]), "ControlPointList", "block")
        cp = cp_list["children"][0]
        source = f["trial"][:-9].decode("latin1")
        cp_body = source[cp["start"]:cp["end"]]
        two_cp = (source[:cp_list["start"]] + "{#0=" + cp_body + ";#1=" + cp_body + ";}"
                  + source[cp_list["end"]:])
        two_cp = two_cp.replace("NumberOfControlPoints=1;", "NumberOfControlPoints=2;")
        parsed = core.one(core.read(core.native(two_cp)), "BeamList", "block")
        entry = parsed["children"][0]
        beam_body = two_cp[entry["start"]:entry["end"]]
        multiple = core.native(two_cp[:parsed["start"]] + "{Beam=" + beam_body + ";Beam="
                               + beam_body.replace('Name="Beam_1"', 'Name="Beam_2"') + ";}"
                               + two_cp[parsed["end"]:])
        r = core.analyze(multiple, [f["machine"], f["machine"]])
        self.assertEqual([(c["beamIndex"], c["segmentIndex"]) for c in r["curves"]],
                         [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.assertEqual(r["blockers"], [])
        for field in ("Gantry", "Collimator", "LeftJawPosition"):
            original = core.number(cp["children"], field)
            altered_cp = cp_body.replace(field + "=%g;" % original, field + "=5;", 1)
            # Replace only the second segment, leaving the first geometry unchanged.
            bad = two_cp.replace("#1=" + cp_body, "#1=" + altered_cp, 1)
            with self.assertRaises(ValueError) as caught:
                core.analyze(core.native(bad), [f["machine"]])
            self.assertIn("moving jaws or angles", str(caught.exception))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = os.path.realpath(tempfile.mkdtemp(prefix="jaw-python-"))
        self.output = os.path.join(self.temp, "bundle")
        self.manifest = build.build(self.output, "/var/tmp/jaw-python-test", "/usr/bin/python")
        self.old_root = helper.ROOT
        self.old_display = os.environ.get("DISPLAY")
        helper.ROOT = self.output
        os.environ["DISPLAY"] = ":9.0"
        self.base = helper.display_dir()

    def tearDown(self):
        helper.ROOT = self.old_root
        if self.old_display is None:
            del os.environ["DISPLAY"]
        else:
            os.environ["DISPLAY"] = self.old_display
        shutil.rmtree(self.temp)

    def run_stage(self, stage, arg, succeeds=True):
        try:
            helper.main([stage, arg])
        except Exception as error:
            helper.failed([stage, arg], error)
            if succeeds:
                raise
        else:
            self.assertTrue(succeeds, "expected stage failure")

    def snapshot(self, directory, f, prefix=""):
        for name, data in (
                ("current", f["trial"]), ("all", core.native("Trial={" + f["trial_text"](f["values"]) + "};")),
                ("machine-0", f["machine"]), ("plan", b"/var/tmp/research-plan\n")):
            with open(directory + "/" + prefix + name + ".txt", "wb") as stream:
                stream.write(data)
        for name in ("dose", "optimizing", "resetting", "computing"):
            with open(directory + "/" + prefix + name + ".txt", "wb") as stream:
                stream.write(b"0\n")

    def prepare(self, mode, f, analyze_ok=True):
        existing = set(os.listdir(self.base)) if os.path.isdir(self.base) else set()
        self.run_stage("prepare", mode)
        directory = next(os.path.join(self.base, n) for n in os.listdir(self.base)
                         if n not in existing and len(n) == 32)
        self.snapshot(directory, f)
        self.run_stage("machines", directory)
        self.snapshot(directory, f, "consent-")
        self.run_stage("acknowledge", directory)
        self.run_stage("analyze", directory, analyze_ok)
        return directory

    def test_package_has_no_javascript_and_all_native_checksums_match(self):
        self.assertEqual(sorted(self.manifest["files"]),
                         sorted(["core.py", "helper.py", "config.json", "Failure.Script", "Start-CPCleaner.Script",
                                 "01-Check.Script", "02-Clean.Script", "03-Verify.Script", "HotScriptList.entries"]))
        for name, digest in self.manifest["files"].items():
            data = helper.read_bytes(os.path.join(self.output, name))
            self.assertEqual(core.digest(data), digest)
            if name.endswith(".Script"):
                self.assertEqual(core.checksum(data), 0)
                body = data[:data.rfind(b"/* ")]
                for forbidden in (b"node", b"helper.js", b"TimeOut", b"ExecuteNow", b"\r"):
                    self.assertNotIn(forbidden, body)
        start = helper.text(os.path.join(self.output, "Start-CPCleaner.Script"))
        self.assertIn("/helper.py prepare run", start)
        self.assertIn("Failure.Script", start)
        entries = helper.text(os.path.join(self.output, "HotScriptList.entries"))
        self.assertEqual(entries.count("ScriptFile ="), 1)
        self.assertIn("/Start-CPCleaner.Script", entries)
        with self.assertRaises(ValueError):
            build.build(self.output, "/var/tmp/foo", "/usr/bin/python")
        with self.assertRaises(ValueError):
            build.build(self.output + "-bad", "/var/tmp/a;bad", "/usr/bin/python")

    def test_full_no_yes_guard_finish_verify_and_noop_flow(self):
        f = fixture()
        check = self.prepare("check", f)
        no = self.prepare("clean", f)
        self.assertFalse(os.path.exists(no + "/mutation.claim"))
        self.assertFalse(os.path.exists(check + "/mutation.claim"))
        self.assertIn("AskYesNoDefault = 0", helper.text(no + "/result.Script"))
        clean = self.prepare("clean", f)
        self.snapshot(clean, f, "live-")
        self.run_stage("claim", clean)
        self.assertTrue(os.path.exists(clean + "/mutation.claim"))
        self.assertIn('STRINGEQUALTO.#"#CPC_RESEARCH_PYTEST"', helper.text(clean + "/guard.Script"))
        self.assertIn("MLCLeafPositions.Load", helper.text(clean + "/import.Script"))
        self.run_stage("prepare", "check", False)
        report = helper.load(clean + "/report.json")
        values = report["curves"][0]["values"]
        after = dict(f, values=values, trial=core.native(f["trial_text"](values)))
        self.snapshot(clean, after, "after-")
        self.run_stage("finish", clean)
        self.assertFalse(helper.load(clean + "/completed.json")["exportReady"])
        self.prepare("check", after)
        verified = self.prepare("verify", after)
        self.assertTrue(helper.load(verified + "/verification.json")["geometryVerified"])
        noop = self.prepare("clean", after)
        self.assertFalse(os.path.exists(noop + "/mutation.claim"))
        self.assertFalse(os.path.exists(noop + "/curve-0.txt"))
        for directory in (check, no, clean, verified, noop):
            for name in os.listdir(directory):
                if name.endswith(".Script"):
                    self.assertEqual(core.checksum(helper.read_bytes(directory + "/" + name)), 0)

    def test_single_start_checks_prompts_and_automatically_verifies_import(self):
        f = fixture()
        no = self.prepare("run", f)
        self.assertIn("AskYesNoDefault = 0", helper.text(no + "/result.Script"))
        self.assertFalse(os.path.exists(no + "/mutation.claim"))
        clean = self.prepare("run", f)
        self.assertEqual(helper.load(clean + "/request.json")["previous"], clean)
        self.assertIn('WriteToFile = "UNKNOWN"', helper.text(clean + "/authorize.Script"))
        self.snapshot(clean, f, "live-")
        self.run_stage("claim", clean)
        self.assertIn("helper.py finish", helper.text(clean + "/import.Script"))
        self.run_stage("prepare", "run", False)
        report = helper.load(clean + "/report.json")
        values = report["curves"][0]["values"]
        after = dict(f, values=values, trial=core.native(f["trial_text"](values)))
        self.snapshot(clean, after, "after-")
        self.run_stage("finish", clean)
        self.assertTrue(helper.load(clean + "/completed.json")["geometryVerified"])
        self.assertFalse(helper.load(clean + "/completed.json")["exportReady"])
        noop = self.prepare("run", after)
        self.assertIn("no masked openings", helper.text(noop + "/result.Script"))
        self.assertNotIn("AskYesNo", helper.text(noop + "/result.Script"))
        self.assertFalse(os.path.exists(noop + "/curve-0.txt"))
        self.assertFalse(os.path.exists(noop + "/mutation.claim"))
        verified = self.prepare("verify", after)
        self.assertTrue(helper.load(verified + "/verification.json")["geometryVerified"])

    def test_single_start_rejects_changes_during_confirmation_and_closure_blockers(self):
        f = fixture()
        run = self.prepare("run", f)
        changed = dict(f, trial=replace(f["trial"], "Weight=1;", "Weight=0.5;"))
        self.snapshot(run, changed, "live-")
        self.run_stage("claim", run, False)
        self.assertFalse(os.path.exists(run + "/mutation.claim"))
        self.assertNotIn("MLCLeafPositions.Load", helper.text(run + "/import.Script"))
        blocked = fixture(overlap=0, positions={0: [0.2, 0.2], 1: [-0.1, 0.2], 2: [0.1, 0.1]})
        directory = self.prepare("run", blocked, False)
        self.assertNotIn("AskYesNo", helper.text(directory + "/result.Script"))
        self.assertFalse(os.path.exists(directory + "/curve-0.txt"))

    def test_fresh_native_snapshot_is_required_after_confirmation(self):
        f = fixture()
        self.prepare("check", f)
        clean = self.prepare("clean", f)
        changed = dict(f, trial=replace(f["trial"], "Weight=1;", "Weight=0.5;"))
        self.snapshot(clean, changed, "live-")
        self.run_stage("claim", clean, False)
        self.assertFalse(os.path.exists(clean + "/mutation.claim"))
        self.assertNotIn("MLCLeafPositions.Load", helper.text(clean + "/import.Script"))

    def test_native_relative_plan_identity_is_preserved_and_guarded(self):
        f = fixture()
        directory = self.prepare("check", f)
        relative = "Institution_1/Mount_0/Patient_1/Plan_0"
        with open(directory + "/plan.txt", "wb") as stream:
            stream.write((relative + "\n").encode("ascii"))
        self.assertEqual(helper.identity(directory)["planPath"], relative)
        self.assertIn('STRINGEQUALTO.#"#' + relative + '"', helper.guard(directory))
        for invalid in ("", "Institution_1/../Patient_1/Plan_0", "../Plan_0",
                        "Institution_1/Mount_0/Patient_1/Plan_0;bad", "/tmp/../plan", "/tmp/./plan"):
            with open(directory + "/plan.txt", "wb") as stream:
                stream.write(invalid.encode("ascii"))
            with self.assertRaises(ValueError):
                helper.identity(directory)

    def test_tampered_curve_and_report_and_replay_are_rejected(self):
        f = fixture()
        self.prepare("check", f)
        for tamper in ("curve", "report"):
            clean = self.prepare("clean", f)
            self.snapshot(clean, f, "live-")
            if tamper == "curve":
                with open(clean + "/curve-0.txt", "ab") as stream:
                    stream.write(b"x")
            else:
                report = helper.load(clean + "/report.json")
                report["curves"][0]["values"][0] = 4
                helper.atomic(clean + "/report.json", helper.encoded(report))
            self.run_stage("claim", clean, False)
            self.assertFalse(os.path.exists(clean + "/mutation.claim"))
        clean = self.prepare("clean", f)
        self.snapshot(clean, f, "live-")
        self.run_stage("claim", clean)
        self.run_stage("claim", clean, False)
        self.assertTrue(helper.load(clean + "/failure.json")["reconciliationRequired"])

    def test_busy_and_unknown_still_fail_closed(self):
        f = fixture()
        check = self.prepare("check", f)
        with open(check + "/optimizing.txt", "wb") as stream:
            stream.write(b"UNKNOWN\n")
        self.run_stage("machines", check, False)
        self.assertIn("unknown", helper.text(check + "/next.Script"))
    def test_arbitrary_trial_name_requires_default_no_research_consent(self):
        f = fixture()
        bad = dict(f, trial=replace(f["trial"], 'Name="CPC_RESEARCH_PYTEST"', 'Name="Experiment 1"'))
        self.run_stage("prepare", "check")
        directory = next(os.path.join(self.base, n) for n in os.listdir(self.base)
                         if len(n) == 32 and not os.path.exists(os.path.join(self.base, n, "current.txt")))
        self.snapshot(directory, bad)
        self.run_stage("machines", directory)
        warning = helper.text(directory + "/next.Script")
        self.assertIn("AskYesNoDefault = 0", warning)
        self.assertIn("RESEARCH ONLY", warning)
        self.assertIn("Experiment 1", warning)
        self.assertIn("IF.AskYesNo.THEN.Load", warning)
        self.assertFalse(os.path.exists(directory + "/research-consent.json"))
        self.assertFalse(os.path.exists(directory + "/report.json"))
        self.assertNotIn("Machine.Save", warning)
        self.run_stage("analyze", directory, False)
        self.assertFalse(os.path.exists(directory + "/curve-0.txt"))

    def test_research_consent_accepts_arbitrary_name_and_rejects_changes(self):
        f = fixture()
        named = dict(f, trial=replace(f["trial"], 'Name="CPC_RESEARCH_PYTEST"', 'Name="Experiment 1"'))
        directory = self.prepare("run", named)
        self.assertTrue(os.path.exists(directory + "/research-consent.json"))
        self.assertIn("AskYesNoDefault = 0", helper.text(directory + "/result.Script"))
        self.assertIn('STRINGEQUALTO.#"#Experiment 1"', helper.guard(directory))
        for changed in (f, dict(named, trial=replace(named["trial"], "Weight=1;", "Weight=0.5;"))):
            self.run_stage("prepare", "run")
            directory = next(os.path.join(self.base, n) for n in os.listdir(self.base)
                             if len(n) == 32 and not os.path.exists(os.path.join(self.base, n, "current.txt")))
            self.snapshot(directory, named)
            self.run_stage("machines", directory)
            self.snapshot(directory, changed, "consent-")
            self.run_stage("acknowledge", directory, False)
            self.assertFalse(os.path.exists(directory + "/research-consent.json"))
            self.assertNotIn("Machine.Save", helper.text(directory + "/machine-stage.Script"))

    def test_closure_blockers_allow_check_but_never_publish_import(self):
        f = fixture(overlap=0, positions={0: [0.2, 0.2], 1: [-0.1, 0.2], 2: [0.1, 0.1]})
        check = self.prepare("check", f)
        self.assertTrue(helper.load(check + "/report.json")["blockers"])
        clean = self.prepare("clean", f, False)
        self.assertNotIn("AskYesNoPrompt", helper.text(clean + "/result.Script"))
        self.assertFalse(os.path.exists(clean + "/curve-0.txt"))

    def test_cli_failure_replaces_continuations_with_checksummed_notices(self):
        f = fixture()
        check = self.prepare("check", f)
        environment = dict(os.environ)
        result = subprocess.call([sys.executable, "-B", os.path.join(self.output, "helper.py"), "claim", check],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result, 0)
        self.assertTrue(os.path.exists(check + "/failure.json"))
        for name in ("guard", "import", "authorize"):
            self.assertNotIn("MLCLeafPositions.Load", helper.text(check + "/" + name + ".Script"))


if __name__ == "__main__":
    unittest.main()
