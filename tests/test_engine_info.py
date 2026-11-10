import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))

import engine_info  # noqa: E402


class FakeOmlx:
    def __init__(self, settings, glob):
        self._settings = settings
        self._glob = glob

    def settings(self, model_id):
        assert ":" not in model_id
        return self._settings

    def global_settings(self):
        return self._glob


SPLASH_GLOBAL = {
    "integrations": {
        "splash_kv_format": "int8",
        "splash_ane_prefill": True,
        "splash_max_memory_gb": None,
        "splash_ssd_cache_gb": None,
    },
    "external_engines": {
        "splash": {
            "version": "1.6.0",
            "models": [
                {
                    "id": "M",
                    "memory_bytes": 30 * 2**30,
                    "memory_budget_bytes": 40 * 2**30,
                }
            ],
        }
    },
}


class EngineSnapshotTest(unittest.TestCase):
    def test_in_process(self):
        snap = engine_info.engine_snapshot(FakeOmlx({}, {}), "M:qwen-code")
        self.assertEqual(snap["engine"], "mlx")
        self.assertNotIn("splash", snap)

    def test_splash_serving(self):
        o = FakeOmlx(
            {"external_engine": "splash", "dflash_draft_model": "D"}, SPLASH_GLOBAL
        )
        snap = engine_info.engine_snapshot(o, "M:qwen-code")
        self.assertEqual(snap["engine"], "splash")
        sp = snap["splash"]
        self.assertEqual(sp["version"], "1.6.0")
        self.assertEqual(sp["draft_model"], "D")
        self.assertTrue(sp["serving"])
        self.assertEqual(sp["memory_budget_gb"], 40.0)
        self.assertEqual(engine_info.engine_tag({"engine": snap}), "splash 1.6.0 int8")

    def test_splash_selected_but_not_serving(self):
        o = FakeOmlx({"external_engine": "splash"}, SPLASH_GLOBAL)
        snap = engine_info.engine_snapshot(o, "Other")
        self.assertFalse(snap["splash"]["serving"])
        self.assertIn("NOT SERVING", engine_info.engine_tag({"engine": snap}))

    def test_settings_error_is_recorded(self):
        class Broken:
            def settings(self, model_id):
                raise OSError("down")

        snap = engine_info.engine_snapshot(Broken(), "M")
        self.assertIn("error", snap)


class EngineTagTest(unittest.TestCase):
    def test_legacy_file_is_mlx(self):
        self.assertEqual(engine_info.engine_tag({"variant": {"vlm_mtp_enabled": False}}), "mlx")
        self.assertEqual(engine_info.engine_tag({}), "mlx")

    def test_variant_only_splash(self):
        self.assertEqual(
            engine_info.engine_tag({"variant": {"external_engine": "splash"}}), "splash"
        )

    def test_no_ane(self):
        run = {"engine": {"engine": "splash", "splash": {"version": "1.6.0",
               "kv_format": "bf16", "ane_prefill": False, "serving": True}}}
        self.assertEqual(engine_info.engine_tag(run), "splash 1.6.0 bf16 no-ane")


if __name__ == "__main__":
    unittest.main()
