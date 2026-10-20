"""Tests for bench/model_scout.py — the rules, against the real scout.toml.

Run: python3 -m unittest discover -s tests -v
No network: every Hub request goes through a fake fetch keyed by URL. The
policy is the committed scout/scout.toml, so a policy edit that breaks a
measured case (gemma fits, Flash-Next is closed) fails here.
"""

import json
import re
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))

import model_scout as ms  # noqa: E402

POLICY = ms.load_policy()
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)

# The lineup at export time, plus one architecture only mlx_vlm has.
SETUP = {
    "generated": "2026-10-16T09:00:00+00:00",
    "mlx_lm": "0.31.4", "mlx_vlm": "0.7.7",
    "architectures": ["gemma4", "qwen3_5", "qwen3_5_moe", "qwen4_exp", "lfm2_moe"],
    "local_model_types": ["gemma4", "qwen3_5", "qwen3_5_moe", "qwen4_exp"],
    "source_repos": [],
    "models": [
        {"id": "Qwen3.8-27B-8bit", "gib": 28.8},
        {"id": "Qwen3.8-27B-oQ4e-mtp", "gib": 16.6},
        {"id": "gemma-4-26B-A4B-it-oQ6e-mtp", "gib": 22.4},
        {"id": "gemma-4-31B-it-qat-OptiQ-4bit", "gib": 21.9},
        {"id": "Qwen3-Embedding-0.6B-8bit", "gib": 0.6},
        {"id": "Qwen3-Reranker-0.6B-mxfp8", "gib": 0.6},
    ],
}


def config(model_type="qwen3_5", bits=4, mtp_layers=0):
    c = {"model_type": model_type, "mtp_num_hidden_layers": mtp_layers}
    if bits:
        c["quantization"] = {"bits": bits, "group_size": 64}
    return c


def facts(size, **kw):
    mtp = kw.pop("mtp", None)
    return {"config": config(**kw), "size": size, "mtp": mtp}


def verdict(repo, size, **kw):
    return ms.evaluate(repo, facts(size, **kw), POLICY, SETUP)


class BudgetTests(unittest.TestCase):
    def test_headroom_is_ceiling_minus_core_minus_margin(self):
        free, tiers = ms.headroom(POLICY, SETUP)
        # 91.29 - (28.8 + 22.4 + 0.6 + 0.6) - 20 ; the fast tier is the
        # gemma-4-26B-A4B MoE since §41, gemma-31B is not core
        self.assertAlmostEqual(free, 18.9, places=1)
        self.assertEqual(tiers["fast"], 22.4)
        self.assertEqual(tiers["additive"], 21.9)

    def test_small_model_fits_alongside(self):
        self.assertEqual(ms.fit(18.5, POLICY, SETUP), "alongside")

    def test_gemma_sized_model_must_replace_a_tier(self):
        # Beside the §41 lineup a 21.9 GiB build no longer clears the 20 GiB
        # serve margin; fit() names the first core tier it would fit in place of.
        self.assertEqual(ms.fit(21.9, POLICY, SETUP), "replaces quality")

    def test_larger_model_must_replace_a_tier(self):
        self.assertEqual(ms.fit(32.3, POLICY, SETUP), "replaces quality")

    def test_flash_next_size_fits_nowhere(self):
        self.assertIsNone(ms.fit(103.9, POLICY, SETUP))


class VerdictTests(unittest.TestCase):
    def test_in_checkpoint_mtp_is_promising(self):
        r = verdict("Jundot/AREX-2-32B-oQ6e-fp16-mtp", 22.9, bits=6,
                    mtp_layers=1, mtp="in-checkpoint")
        self.assertEqual(r["verdict"], "promising")
        self.assertEqual(r["fit"], "replaces quality")  # 22.9 > 18.9 free (§41)
        self.assertTrue(any("candidate_gate.py AREX-2-32B-oQ6e-fp16-mtp" in s
                            for s in r["next_steps"]))

    def test_new_build_of_a_local_model_is_a_variant(self):
        # Reported as promising on 2026-10-17 while four 27B builds were local.
        r = verdict("Jundot/Qwen3.8-27B-oQ6e-fp16-mtp", 22.9, bits=6,
                    mtp_layers=1, mtp="in-checkpoint")
        self.assertEqual(r["verdict"], "consider")
        self.assertIn("Qwen3.8-27B-oQ4e-mtp", r["variant_of"])
        self.assertIn("A2b", r["why"])

    def test_variant_with_mtp_the_local_build_lacks_stays_promising(self):
        # The local gemma is a QAT OptiQ build without MTP.
        r = verdict("Jundot/gemma-4-31B-it-oQ8e-mtp", 21.0, model_type="gemma4",
                    bits=8, mtp_layers=1, mtp="in-checkpoint")
        self.assertEqual(r["verdict"], "promising")
        self.assertEqual(r["variant_of"], ["gemma-4-31B-it-qat-OptiQ-4bit"])
        self.assertIn("which lacks it", r["why"])

    def test_dwq_variant_stays_promising(self):
        r = verdict("mlx-community/Qwen3.8-27B-4bit-DWQ", 15.7)
        self.assertEqual(r["verdict"], "promising")

    def test_mtp_claimed_in_config_only_is_not_a_signal(self):
        # mlx-community conversions keep mtp_num_hidden_layers and drop the tensors.
        r = verdict("mlx-community/AREX-2-6bit", 21.2, bits=6, mtp_layers=1, mtp=None)
        self.assertEqual(r["verdict"], "consider")
        self.assertEqual(r["signals"], [])

    def test_sidecar_mtp_is_promising_with_a_warning(self):
        r = verdict("mlx-community/Hemmingway-1-OptiQ-4bit", 17.7, mtp="sidecar")
        self.assertEqual(r["verdict"], "promising")
        self.assertTrue(any("sidecar" in w for w in r["warnings"]))

    def test_dwq_name_is_a_signal(self):
        r = verdict("mlx-community/Foo-32B-4bit-DWQ", 18.0)
        self.assertEqual(r["verdict"], "promising")

    def test_new_architecture_is_a_signal(self):
        r = verdict("mlx-community/LFM2.5-24B-A2B-4bit", 12.6, model_type="lfm2_moe")
        self.assertEqual(r["verdict"], "promising")

    def test_text_variant_is_not_a_new_architecture(self):
        r = verdict("mlx-community/Bar-27B-6bit", 21.0, model_type="qwen3_5_text")
        self.assertEqual(r["verdict"], "consider")

    def test_unknown_architecture_is_blocked(self):
        r = verdict("mlx-community/MiniCPM-V-4.7-35B-A3B-4bit", 21.9,
                    model_type="minicpmv4_7")
        self.assertEqual(r["verdict"], "blocked")
        self.assertIn("minicpmv4_7", r["why"])

    def test_flagged_family_is_never_promising(self):
        r = verdict("NovaeonStudio/Qwen3.8-35B-A3B-Distill-oQ4-fp16-mtp", 21.0,
                    model_type="qwen3_5_moe", mtp="in-checkpoint")
        self.assertEqual(r["verdict"], "consider")
        self.assertIn("§34", r["why"])

    def test_closed_finding_skips(self):
        r = verdict("GBP-DE/Qwen3.8-Flash-Next-oQ5e-mtp", 20.0, model_type="qwen4_exp")
        self.assertEqual(r["verdict"], "skip")
        self.assertIn("§27", r["why"])

    def test_small_model_skips(self):
        r = verdict("mlx-community/K2-Horizon-7B-bf16", 16.8, bits=None)
        self.assertEqual(r["verdict"], "skip")
        self.assertIn("7.0B", r["why"])

    def test_drafter_named_after_its_base_skips(self):
        r = verdict("mlx-community/Qwen3.6-35B-A3B-DFlash2-4bit", 0.4)
        self.assertEqual(r["verdict"], "skip")
        self.assertIn("drafter", r["why"])

    def test_below_four_bits_skips(self):
        r = verdict("mlx-community/Big-70B-3bit", 24.0, bits=3)
        self.assertEqual(r["verdict"], "skip")

    def test_too_large_skips(self):
        r = verdict("mlx-community/Huge-120B-8bit", 110.0, bits=8)
        self.assertEqual(r["verdict"], "skip")
        self.assertIn("does not fit", r["why"])


class RoleTests(unittest.TestCase):
    THINKING = "{% if tools %}{{ tools }}{% endif %}{% if enable_thinking %}<think>{% endif %}"

    def roles(self, ctx=262144, template=THINKING):
        c = config()
        c["text_config"] = {"max_position_embeddings": ctx}
        return {r["role"]: r for r in ms.role_fit(
            {"config": c, "template": template}, POLICY)}

    def test_the_four_roles(self):
        self.assertEqual(list(POLICY["roles"]), ["coder", "reasoner", "agent", "long"])

    def test_incumbents_are_served_by_litellm(self):
        # A role whose incumbent LiteLLM no longer serves is a stale policy.
        served = set(re.findall(r"model_name: (\S+)",
                                (ms.ROOT / "proxy" / "litellm-config.yaml").read_text()))
        for name, role in POLICY["roles"].items():
            for slot in ("incumbent", "fast"):
                if slot in role:
                    self.assertIn(role[slot], served, f"{name}.{slot}")

    def test_full_context_and_template_fit_every_role(self):
        self.assertTrue(all(r["eligible"] for r in self.roles().values()))

    def test_short_context_rules_out_long(self):
        r = self.roles(ctx=131072)
        self.assertIs(r["long"]["eligible"], False)
        self.assertIn("131,072 context < 262,144", r["long"]["why"])
        self.assertTrue(r["coder"]["eligible"])

    def test_template_without_thinking_rules_out_reasoner(self):
        r = self.roles(template="{% for tool in tools %}{{ tool }}{% endfor %}")
        self.assertIs(r["reasoner"]["eligible"], False)
        self.assertTrue(r["agent"]["eligible"])

    def test_missing_template_is_unknown_not_ineligible(self):
        r = self.roles(template=None)
        self.assertIsNone(r["reasoner"]["eligible"])
        self.assertTrue(r["long"]["eligible"])

    def test_role_table_carries_incumbent_and_bar(self):
        r = ms.role_table(POLICY)["coder"]
        self.assertEqual(r["incumbent"], "Qwen3.8-27B-8bit:qwen-code")
        self.assertIn("59.8", r["bar"])

    def test_notification_lists_roles(self):
        fam = {"roles": [{"role": "coder", "eligible": True, "why": ""},
                         {"role": "long", "eligible": False, "why": "131,072 context < 262,144"}]}
        self.assertEqual(ms.role_lines(fam), ["could compete for: coder",
                                              "cannot hold: long (131,072 context < 262,144)"])

    def test_evaluate_attaches_roles(self):
        r = verdict("mlx-community/AREX-2-6bit", 21.2, bits=6)
        self.assertEqual([x["role"] for x in r["roles"]],
                         ["coder", "reasoner", "agent", "long"])

    def test_template_read_from_tokenizer_config(self):
        hub = FakeHub({"https://huggingface.co/x/y/resolve/main/tokenizer_config.json":
                       {"chat_template": self.THINKING}})
        self.assertEqual(ms.chat_template("x/y", [{"rfilename": "tokenizer_config.json"}],
                                          hub), self.THINKING)


class NamingTests(unittest.TestCase):
    def test_base_key_ignores_instruct_and_qat(self):
        self.assertEqual(ms.base_key("gemma-4-31B-it-qat-OptiQ-4bit"),
                         ms.base_key("Jundot/gemma-4-31B-it-oQ8e-mtp"))
        self.assertNotEqual(ms.base_key("Qwen3.8-27B-8bit"),
                            ms.base_key("Qwen3.8-Flash-Next-oQ4e-mtp"))

    def test_family_strips_build_suffixes(self):
        self.assertEqual(ms.family("Jundot/Qwen3.8-27B-oQ6e-fp16-mtp"), "Qwen3.8-27B")
        self.assertEqual(ms.family("mlx-community/AREX-2-6bit"), "AREX-2")
        self.assertEqual(ms.family("mlx-community/LFM2.5-24B-A2B-Preview-mxfp4"),
                         "LFM2.5-24B-A2B-Preview")

    def test_params_from_name_then_from_size(self):
        self.assertEqual(ms.params_b("x/LFM2.5-24B-A2B-4bit", 12.6, 4), 24.0)
        self.assertEqual(ms.params_b("x/AREX-2-4bit", 15.0, 4), 28.6)
        self.assertEqual(ms.params_b("x/Tiny-bf16", 2.0, None), 1.1)


class FakeHub:
    """URL -> JSON, with a 404 for anything not registered."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url):
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return body
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


def hub_for(repo, cfg, files, index=None, created="2026-10-15T00:00:00Z",
            author="Jundot"):
    listing = [{"id": repo, "createdAt": created, "pipeline_tag": "text-generation",
                "config": {"model_type": cfg.get("model_type")}}]
    routes = {f"https://huggingface.co/api/models?author={author}&": listing,
              f"https://huggingface.co/{repo}/resolve/main/config.json": cfg,
              f"https://huggingface.co/api/models/{repo}?blobs=true": {"siblings": files}}
    if index is not None:
        routes[f"https://huggingface.co/{repo}/resolve/main/model.safetensors.index.json"] = index
    return FakeHub(routes)


SHARDS = [{"rfilename": "model-00001-of-00002.safetensors", "size": 12 * 2**30},
          {"rfilename": "model-00002-of-00002.safetensors", "size": 10 * 2**30}]


class InspectTests(unittest.TestCase):
    def test_mtp_from_index_not_config(self):
        repo = "Jundot/Q-27B-oQ6e-mtp"
        hub = hub_for(repo, config(bits=6, mtp_layers=1),
                      SHARDS + [{"rfilename": "model-mtp.safetensors", "size": 2**29}],
                      index={"weight_map": {"mtp.layers.0.w": "model-mtp.safetensors"}})
        f = ms.inspect(repo, hub)
        self.assertEqual(f["mtp"], "in-checkpoint")
        self.assertEqual(f["size"], 22.5)

    def test_config_claim_without_tensors(self):
        repo = "mlx-community/Q-27B-6bit"
        hub = hub_for(repo, config(bits=6, mtp_layers=1), SHARDS,
                      index={"weight_map": {"model.layers.0.w": "model-00001.safetensors"}})
        self.assertIsNone(ms.inspect(repo, hub)["mtp"])

    def test_mtp_file_outside_index_is_sidecar(self):
        repo = "mlx-community/H-1-OptiQ-4bit"
        hub = hub_for(repo, config(mtp_layers=1),
                      SHARDS + [{"rfilename": "optiq/mtp.safetensors", "size": 2**28}],
                      index={"weight_map": {"model.layers.0.w": "model-00001.safetensors"}})
        self.assertEqual(ms.inspect(repo, hub)["mtp"], "sidecar")

    def test_missing_config_raises_lookup(self):
        with self.assertRaises(LookupError):
            ms.inspect("x/gguf-only", FakeHub({}))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.policy = {**POLICY, "authors": ["Jundot"]}
        self.repo = "Jundot/Q-27B-oQ6e-mtp"
        self.hub = hub_for(self.repo, config(bits=6, mtp_layers=1),
                           SHARDS + [{"rfilename": "model-mtp.safetensors", "size": 2**29}],
                           index={"weight_map": {"mtp.fc": "model-mtp.safetensors"}})
        self.since = NOW - timedelta(days=7)

    def run_scan(self, state, **kw):
        return ms.scan(self.policy, SETUP, state, self.since, NOW, self.hub, **kw)

    def test_reports_once_then_remembers(self):
        state = {"last_scan": None, "seen": {}}
        first = self.run_scan(state)
        self.assertEqual(first["counts"]["promising"], 1)
        self.assertEqual(first["families"][0]["pick"], self.repo)
        self.assertEqual(state["seen"][self.repo], "promising")
        again = self.run_scan(state)
        self.assertEqual(again["results"], [])

    def test_blocked_is_rechecked_after_a_new_export(self):
        state = {"last_scan": None, "seen": {self.repo: "blocked"},
                 "setup_generated": "2026-09-01T00:00:00+00:00"}
        self.assertEqual(len(self.run_scan(state)["results"]), 1)

    def test_older_than_since_is_ignored(self):
        self.hub.routes["https://huggingface.co/api/models?author=Jundot&"][0][
            "createdAt"] = "2026-09-01T00:00:00Z"
        report = self.run_scan({"last_scan": None, "seen": {}})
        self.assertEqual(report["results"], [])

    def test_listing_error_is_reported_not_raised(self):
        report = ms.scan(self.policy, SETUP, {"last_scan": None, "seen": {}},
                         self.since, NOW, FakeHub({}))
        self.assertEqual(len(report["errors"]), 1)

    def test_stale_setup_warns(self):
        old = {**SETUP, "generated": "2026-08-01T00:00:00+00:00"}
        report = ms.scan(self.policy, old, {"last_scan": None, "seen": {}},
                         self.since, NOW, self.hub)
        self.assertTrue(any("export" in w for w in report["warnings"]))

    def test_builds_group_into_one_family(self):
        results = [
            {"repo": "m/AREX-2-4bit", "verdict": "consider", "fit": "alongside",
             "bits": 4, "signals": []},
            {"repo": "m/AREX-2-6bit", "verdict": "consider", "fit": "alongside",
             "bits": 6, "signals": []},
            {"repo": "m/AREX-2-8bit", "verdict": "consider", "fit": "replaces quality",
             "bits": 8, "signals": []},
        ]
        for r in results:
            r.update(why="", size_gib=1)
        (fam,) = ms.group(results)
        self.assertEqual(fam["family"], "AREX-2")
        self.assertEqual(fam["pick"], "m/AREX-2-6bit")  # most bits that fits alongside

    def test_report_is_json_serialisable(self):
        json.dumps(self.run_scan({"last_scan": None, "seen": {}}))


class NotifyTests(unittest.TestCase):
    def report(self, families=(), warnings=(), errors=()):
        return {"since": "2026-10-15T00:00:00+00:00", "families": list(families),
                "warnings": list(warnings), "errors": list(errors)}

    def fam(self, verdict="promising", name="Qwen3.8-27B"):
        pick = f"Jundot/{name}-oQ6e-fp16-mtp"
        return {"family": name, "verdict": verdict, "why": "ships MTP weights",
                "pick": pick, "fit": "alongside", "params_b": 27.0,
                "model_type": "qwen3_5", "warnings": [],
                "next_steps": ["leaderboard", "download", "gate"],
                "builds": [{"repo": pick, "size_gib": 22.9, "bits": 6,
                            "fit": "alongside", "verdict": verdict}]}

    def test_nothing_new_sends_nothing(self):
        self.assertIsNone(ms.notification(self.report()))

    def test_consider_is_not_sent_by_default(self):
        self.assertIsNone(ms.notification(self.report([self.fam("consider")])))
        self.assertIsNotNone(ms.notification(self.report([self.fam("consider")]),
                                             ms.NOTIFY_ON + ("consider",)))

    def test_promising_message_carries_pick_and_steps(self):
        note = ms.notification(self.report([self.fam()]))
        self.assertEqual(note["title"], "1 promising MLX model")
        self.assertIn("Jundot/Qwen3.8-27B-oQ6e-fp16-mtp", note["markdown"])
        self.assertIn("3. gate", note["markdown"])
        self.assertEqual(note["summary"], "Qwen3.8-27B")

    def test_stale_setup_is_sent_even_with_no_models(self):
        note = ms.notification(self.report(warnings=["setup.json is 40 days old"]))
        self.assertIn("40 days", note["markdown"])

    def test_send_posts_to_each_configured_sink(self):
        calls = []
        note = ms.notification(self.report([self.fam()]))
        env = {"SCOUT_OPENWEBUI_WEBHOOK": "https://owui/api/v1/channels/webhooks/a/b",
               "SCOUT_PUSHOVER_TOKEN": "t", "SCOUT_PUSHOVER_USER": "u",
               "SCOUT_LINK": "https://owui/channels/x"}
        sent = ms.send(note, env, lambda url, body: calls.append((url, body)))
        self.assertEqual(sent, ["openwebui", "pushover"])
        self.assertEqual(calls[0][1], {"content": note["markdown"]})
        self.assertEqual(calls[1][0], ms.PUSHOVER)
        self.assertEqual(calls[1][1]["url"], "https://owui/channels/x")

    def test_pushover_needs_both_keys(self):
        note = ms.notification(self.report([self.fam()]))
        sent = ms.send(note, {"SCOUT_PUSHOVER_TOKEN": "t"}, lambda *_: None)
        self.assertEqual(sent, [])

    def test_failed_delivery_raises(self):
        def boom(url, body):
            raise urllib.error.URLError("down")
        note = ms.notification(self.report([self.fam()]))
        with self.assertRaises(urllib.error.URLError):
            ms.send(note, {"SCOUT_OPENWEBUI_WEBHOOK": "https://x"}, boom)


class AssessGateTests(unittest.TestCase):
    """The Hermes assessment job's gate: silent unless something is pending and the Mac answers."""

    T0 = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)

    def report(self, *verdicts):
        return {"families": [{"family": f"F{i}", "verdict": v} for i, v in enumerate(verdicts)],
                "roles": {"coder": {"incumbent": "Qwen3.8-27B-8bit:qwen-code"}},
                "free_beside_lineup_gib": 24.7}

    def gate(self, state, report=None, job=None, up=True, at=None):
        calls = []

        def probe():
            calls.append(1)
            return up
        out = ms.assess_gate(state, report, job, probe, at or self.T0, "Assess this.")
        return out, calls

    def test_mac_away_is_silent_and_leaves_it_pending(self):
        state = {"families": {}}
        out, calls = self.gate(state, self.report("promising"), up=False)
        self.assertIsNone(out)
        self.assertEqual(calls, [1])
        self.assertEqual(state["families"]["F0"]["status"], "pending")

    def test_nothing_pending_never_probes(self):
        # The probe is a model call; it must not run on an idle tick.
        out, calls = self.gate({"families": {}}, self.report("consider", "blocked"))
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_pending_and_up_hands_over_one_family(self):
        state = {"families": {}}
        out, _ = self.gate(state, self.report("promising", "promising"))
        self.assertIn("Assess this.", out)
        payload = json.loads(out.split("```json\n")[1].split("```")[0])
        self.assertEqual(payload["family"]["family"], "F0")
        self.assertEqual(payload["roles"]["coder"]["incumbent"], "Qwen3.8-27B-8bit:qwen-code")
        self.assertEqual([e["status"] for e in state["families"].values()], ["handed", "pending"])

    def test_only_one_hand_over_at_a_time(self):
        state = {"families": {}}
        self.gate(state, self.report("promising", "promising"))
        out, calls = self.gate(state, at=self.T0 + timedelta(minutes=15))
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_ok_run_after_hand_over_marks_assessed(self):
        state = {"families": {}}
        self.gate(state, self.report("promising"))
        job = {"last_run_at": "2026-10-19T10:03:00+02:00", "last_status": "ok"}
        out, calls = self.gate(state, job=job, at=self.T0 + timedelta(minutes=15))
        self.assertEqual(state["families"]["F0"]["status"], "assessed")
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_failed_run_hands_it_over_again(self):
        state = {"families": {}}
        self.gate(state, self.report("promising"))
        job = {"last_run_at": "2026-10-19T08:03:00+00:00", "last_status": "error"}
        out, _ = self.gate(state, job=job, at=self.T0 + timedelta(minutes=15))
        self.assertIsNotNone(out)
        self.assertEqual(state["families"]["F0"]["attempts"], 2)

    def test_run_before_hand_over_does_not_settle_it(self):
        state = {"families": {}}
        self.gate(state, self.report("promising"))
        job = {"last_run_at": "2026-10-19T07:45:00+00:00", "last_status": "ok"}
        self.gate(state, job=job, at=self.T0 + timedelta(minutes=15))
        self.assertEqual(state["families"]["F0"]["status"], "handed")

    def test_gives_up_after_three_failed_attempts(self):
        state = {"families": {}}
        at = self.T0
        self.gate(state, self.report("promising"), at=at)
        for _ in range(3):
            at += timedelta(minutes=15)
            job = {"last_run_at": (at - timedelta(minutes=10)).isoformat(),
                   "last_status": "error"}
            self.gate(state, job=job, at=at)
        self.assertEqual(state["families"]["F0"]["status"], "gave_up")
        self.assertEqual(state["families"]["F0"]["attempts"], 3)

    def test_a_family_is_queued_once(self):
        state = {"families": {}}
        self.gate(state, self.report("promising"))
        state["families"]["F0"]["status"] = "assessed"
        out, _ = self.gate(state, self.report("promising"), at=self.T0 + timedelta(days=1))
        self.assertIsNone(out)

    def test_find_job_by_name(self):
        jobs = [{"name": "model-scout"}, {"name": "scout-assess", "last_status": "ok"}]
        self.assertEqual(ms.find_job(jobs, "scout-assess")["last_status"], "ok")
        self.assertEqual(ms.find_job({"jobs": jobs}, "scout-assess")["last_status"], "ok")
        self.assertIsNone(ms.find_job(None, "scout-assess"))


class QuietModeTests(unittest.TestCase):
    """--quiet is what a stdout-delivering scheduler runs: silence means no news."""

    def run_main(self, report, argv):
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(ms, "scan", return_value=report), \
             mock.patch.object(ms, "load_policy", return_value=POLICY), \
             mock.patch.object(ms, "load_setup", return_value=SETUP), \
             mock.patch.object(sys, "argv", ["model_scout.py", "scan", *argv]), \
             contextlib.redirect_stdout(out):
            code = ms.main()
        return code, out.getvalue()

    def report(self, families=()):
        return {"since": "2026-10-15T00:00:00+00:00", "families": list(families),
                "warnings": [], "errors": [], "results": [{"repo": "x", "verdict": "skip"}],
                "counts": {}}

    def test_nothing_new_prints_one_line(self):
        # A silent run could not be told apart from one that never ran (D7).
        code, out = self.run_main(self.report(), ["--quiet", "--dry-run"])
        self.assertEqual((code, out), (0, "Model scout: nothing new since "
                                          "2026-10-15 00:00 UTC (1 repos checked)\n"))

    def test_news_prints_markdown(self):
        fam = NotifyTests.fam(NotifyTests(), "promising")
        code, out = self.run_main(self.report([fam]), ["--quiet", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("**1 promising MLX model**", out)


if __name__ == "__main__":
    unittest.main()
