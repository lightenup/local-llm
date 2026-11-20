import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))

import concurrency  # noqa: E402
import longctx_quality  # noqa: E402
import paired_sameprompt  # noqa: E402
import set_variant  # noqa: E402


def result(rows):
    return {"results": [{"benchmark": "s", "question_results": rows}]}


class PairedSamePromptTest(unittest.TestCase):
    def test_exact_p(self):
        self.assertEqual(paired_sameprompt.exact_p(0, 0), 1.0)
        self.assertAlmostEqual(paired_sameprompt.exact_p(0, 7), 2 / 2**7)
        self.assertEqual(paired_sameprompt.exact_p(5, 5), 1.0)

    def test_changed_prompts_are_dropped(self):
        a = [
            {"id": 1, "question": "q1", "correct": True},
            {"id": 2, "question": "old", "correct": False},
            {"id": 3, "question": "q3", "correct": False},
        ]
        b = [
            {"id": 1, "question": "q1", "correct": False},
            {"id": 2, "question": "new", "correct": True},
            {"id": 3, "question": "q3", "correct": True},
        ]
        with tempfile.TemporaryDirectory() as d:
            old = paired_sameprompt.RESULTS
            paired_sameprompt.RESULTS = Path(d)
            try:
                (Path(d) / "a.json").write_text(json.dumps(result(a)))
                (Path(d) / "b.json").write_text(json.dumps(result(b)))
                r = paired_sameprompt.compare("a", "b")
            finally:
                paired_sameprompt.RESULTS = old
        s = r["suites"]["s"]
        self.assertEqual((s["n"], s["of"], s["a_only"], s["b_only"]), (2, 3, 1, 1))
        self.assertEqual(r["pooled"]["n"], 2)


class LongctxThinkingTest(unittest.TestCase):
    def test_bare_id_turns_thinking_off(self):
        self.assertEqual(
            longctx_quality.chat_extra("Some-Model-8bit"),
            {"chat_template_kwargs": {"enable_thinking": False}},
        )

    def test_profile_id_is_left_alone(self):
        self.assertEqual(longctx_quality.chat_extra("Qwen3.8-27B-8bit:qwen-long"), {})


class ConcurrencySaltTest(unittest.TestCase):
    def test_salt_changes_the_leading_block_only(self):
        a = concurrency.build_prompt(3, 100)
        b = concurrency.build_prompt(3, 100, "x-")
        self.assertNotEqual(a.split("\n")[0], b.split("\n")[0])
        self.assertEqual(a.split("\n", 1)[1], b.split("\n", 1)[1])
        self.assertEqual(b, concurrency.build_prompt(3, 100, "x-"))


class FakeOmlx:
    def __init__(self):
        self.patches = {}

    def global_settings(self):
        return {"integrations": dict(set_variant.GLOBAL_RESET)}

    def update_global_settings(self, patch):
        pass

    def update_settings(self, patch, model_id):
        self.patches[model_id] = patch

    def settings(self, model_id):
        return self.patches.get(model_id, {})


class SetVariantContextTest(unittest.TestCase):
    OTHER = "Qwen3.6-35B-A3B-MLX-8bit"

    def test_shipped_model_resets_to_shipped_context(self):
        o = FakeOmlx()
        set_variant.apply(o, "splash", set_variant.MODEL, reload=False)
        self.assertEqual(o.patches[set_variant.MODEL]["max_context_window"],
                         set_variant.CONTEXT_RESET)

    def test_other_model_falls_back_to_its_own_context(self):
        o = FakeOmlx()
        set_variant.apply(o, "plain", self.OTHER, reload=False)
        self.assertIsNone(o.patches[self.OTHER]["max_context_window"])

    def test_context_variant_applies_to_other_model(self):
        o = FakeOmlx()
        set_variant.apply(o, "splash_262k", self.OTHER, reload=False)
        p = o.patches[self.OTHER]
        self.assertEqual(p["max_context_window"], 262144)
        self.assertEqual(p["dflash_draft_model"], set_variant.SPLASH_DRAFTS[self.OTHER])


class Qwen36ProfilesTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "profiles"))
        import qwen36
        self.mod = qwen36

    def test_valid(self):
        self.assertEqual(self.mod.validate(), [])

    def test_names_match_engine_checks(self):
        names = {p["name"] for p in self.mod.PROFILES}
        self.assertEqual(names, {"code", "agent", "reason", "longctx"})

    def test_thinking_stated_twice(self):
        for p in self.mod.PROFILES:
            s = p["settings"]
            self.assertEqual(s["chat_template_kwargs"]["enable_thinking"], s["enable_thinking"])

    def test_catches_contradiction(self):
        import copy
        saved = self.mod.PROFILES
        self.mod.PROFILES = copy.deepcopy(saved)
        try:
            self.mod.PROFILES[0]["settings"]["chat_template_kwargs"]["enable_thinking"] = True
            self.assertTrue(self.mod.validate())
        finally:
            self.mod.PROFILES = saved


if __name__ == "__main__":
    unittest.main()


class ShippedVariantTest(unittest.TestCase):
    def test_shipped_splash_reaches_qwen_long(self):
        import doctor
        sys.path.insert(0, str(ROOT / "profiles"))
        import definitions
        v = set_variant.VARIANTS[doctor.SHIPPED_VARIANT]
        if v.get("external_engine") != "splash":
            return
        self.assertIn(set_variant.MODEL, set_variant.SPLASH_DRAFTS)
        need = max(p["settings"].get("max_context_window", 0) for p in definitions.PROFILES)
        self.assertGreaterEqual(set_variant.CONTEXT_VARIANTS.get(doctor.SHIPPED_VARIANT, 0), need)

    def test_no_profile_sizes_splash_below_the_shipped_context(self):
        # oMLX sizes Splash from the settings of the request that loads it,
        # profile overlay included, so one low cap plans 128K for all (§48).
        import doctor
        sys.path.insert(0, str(ROOT / "profiles"))
        import definitions
        v = set_variant.VARIANTS[doctor.SHIPPED_VARIANT]
        if v.get("external_engine") != "splash":
            return
        shipped = set_variant.CONTEXT_VARIANTS.get(doctor.SHIPPED_VARIANT,
                                                   set_variant.CONTEXT_RESET)
        for p in definitions.PROFILES:
            cap = p["settings"].get("max_context_window")
            if cap is not None:
                self.assertGreaterEqual(cap, shipped, p["name"])
