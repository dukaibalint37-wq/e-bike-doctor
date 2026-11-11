"""Backend unit tests (no network, no FastAPI install needed):  python3 -m unittest discover -s tests -v"""
import base64, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests_path import *  # noqa  (adds tests dir)
from fakes import FakeConn, FakeResp, ok_json, sse_resp, install_web_stubs, FakeRequest
import http.client
import gemini_client, quota, cache, metrics, store, ai_core


def env(**kw):
    for k, v in kw.items():
        os.environ[k] = v


class GeminiClientTest(unittest.TestCase):
    def setUp(self):
        env(GEMINI_API_KEY="k", GEMINI_MODEL="gemini-3.8-flash", GEMINI_FALLBACK_MODELS="gemini-3.5-flash-lite",
            GEMINI_MODEL_FAST="gemini-3.5-flash-lite", GEMINI_FALLBACK_MODELS_FAST="gemini-3.8-flash")
        for k in ("GEMINI_THINKING_LEVEL", "GEMINI_THINKING_LEVEL_FAST"):
            os.environ.pop(k, None)

    def make(self, script):
        log = []
        FakeConn.instances = 0
        c = gemini_client.GeminiClient(conn_factory=lambda: FakeConn(script, log))
        return c, log

    def test_connection_reused(self):
        c, log = self.make([ok_json("a"), ok_json("b")])
        c.generate([{"text": "x"}]); c.generate([{"text": "y"}])
        self.assertEqual(FakeConn.instances, 1, "keep-alive connection must be reused")

    def test_stale_socket_reconnects_once(self):
        c, log = self.make([http.client.RemoteDisconnected("closed"), ok_json("fine")])
        self.assertEqual(c.generate([{"text": "x"}])["text"], "fine")
        self.assertEqual(FakeConn.instances, 2)

    def test_404_falls_back(self):
        c, log = self.make([FakeResp(404, b'{"error":"not found"}'), ok_json("from lite")])
        r = c.generate([{"text": "x"}], tier="quality")
        self.assertEqual((r["text"], r["model"]), ("from lite", "gemini-3.5-flash-lite"))
        self.assertIn("gemini-3.8-flash:generateContent", log[0]["path"])

    def test_404_model_is_remembered(self):
        # 2.2: the second request must not waste a round trip on the retired model
        c, log = self.make([FakeResp(404, b'{"error":"not found"}'), ok_json("first"), ok_json("second")])
        c.generate([{"text": "x"}], tier="quality")
        r = c.generate([{"text": "y"}], tier="quality")
        self.assertEqual(r["model"], "gemini-3.5-flash-lite")
        self.assertEqual(len(log), 3, "only one 404 in total")
        self.assertIn("gemini-3.5-flash-lite", log[2]["path"])

    def test_last_candidate_always_tried(self):
        # test env: 2 quality models (gemini-3.8-flash, gemini-3.5-flash-lite)
        c, log = self.make([FakeResp(404, b'{}'), FakeResp(404, b'{}'), ok_json("back")])
        with self.assertRaises(gemini_client.ProviderError):
            c.generate([{"text": "x"}], tier="quality")      # every model 404 -> all marked dead
        r = c.generate([{"text": "y"}], tier="quality")      # still tries the last fallback instead of giving up
        self.assertEqual(r["text"], "back")
        self.assertEqual(len(log), 3)
        self.assertIn("gemini-3.5-flash-lite", log[2]["path"])

    def test_thinking_400_retry_without_config(self):
        c, log = self.make([FakeResp(400, b'Thinking level MINIMAL is not supported'), ok_json("ok")])
        c.generate([{"text": "x"}], tier="quality")
        self.assertIn("thinkingConfig", log[0]["body"]["generationConfig"])
        self.assertNotIn("thinkingConfig", log[1]["body"]["generationConfig"])

    def test_generation_config_by_family(self):
        g3 = gemini_client.generation_config("gemini-3.8-flash", 500)
        self.assertEqual(g3["thinkingConfig"], {"thinkingLevel": "low"}); self.assertNotIn("temperature", g3)
        self.assertEqual(gemini_client.generation_config("gemini-3.5-flash-lite", 500)["thinkingConfig"], {"thinkingLevel": "minimal"})
        g2 = gemini_client.generation_config("gemini-2.5-flash", 500)
        self.assertEqual(g2["thinkingConfig"], {"thinkingBudget": 0}); self.assertIn("temperature", g2)

    def test_stream_events(self):
        c, log = self.make([sse_resp(["Hel", "lo ", "world"])])
        evs = list(c.stream([{"text": "x"}], tier="fast"))
        self.assertEqual(evs[0][0], "start")
        self.assertEqual("".join(p for e, p in evs if e == "delta"), "Hello world")
        self.assertEqual(evs[-1][0], "done"); self.assertEqual(evs[-1][1]["usage"]["totalTokenCount"], 100)
        self.assertIn("streamGenerateContent?alt=sse", log[0]["path"])

    def test_stream_cancel_drops_socket(self):
        c, log = self.make([sse_resp(["a", "b", "c"]), ok_json("next")])
        g = c.stream([{"text": "x"}])
        next(g); next(g); g.close()  # client cancelled mid-stream
        c.generate([{"text": "y"}])
        self.assertEqual(FakeConn.instances, 2, "half-read socket must not be reused")

    def test_thought_parts_hidden(self):
        r = gemini_client._text_of({"candidates": [{"content": {"parts": [{"text": "secret", "thought": True}, {"text": "answer"}]}}]})
        self.assertEqual(r, "answer")

    def test_not_configured(self):
        env(GEMINI_API_KEY="")
        c, _ = self.make([])
        with self.assertRaises(gemini_client.ProviderError) as cm:
            c.generate([{"text": "x"}])
        self.assertEqual(cm.exception.status, 503)


class QuotaTest(unittest.TestCase):
    def setUp(self):
        env(QUOTA_SECRET="s3cret", DAILY_AI_LIMIT="50", DAILY_IP_LIMIT="300", MAX_REGISTRATIONS_PER_IP="5", DAILY_GLOBAL_LIMIT="5000", DAILY_COST_LIMIT_USD="0")
        self.q = quota.QuotaManager(store.MemoryStore(), quota.TokenSigner("s3cret"))

    def test_fifty_per_day(self):
        tok = self.q.register("install-0123456789abcdef", "1.1.1.1")
        results = [self.q.check_and_consume(tok, "1.1.1.1") for _ in range(51)]
        self.assertTrue(all(r.allowed for r in results[:50]))
        self.assertEqual(results[49].remaining, 0)
        self.assertFalse(results[50].allowed); self.assertEqual(results[50].reason, "daily_quota")
        self.assertGreater(results[50].retry_after, 0)

    def test_refund(self):
        tok = self.q.register("install-0123456789abcdef", "1.1.1.1")
        d = self.q.check_and_consume(tok, "1.1.1.1"); self.q.refund(d, "1.1.1.1")
        self.assertEqual(self.q.remaining(tok, "1.1.1.1"), 50)

    def test_forged_token_is_anonymous(self):
        forged = "install-0123456789abcdef.1700000000.AAAAAAAAAAAAAAAAAAAAAAAA"
        self.assertIsNone(self.q.signer.verify(forged))
        d = self.q.check_and_consume(forged, "2.2.2.2")
        self.assertTrue(d.subject.startswith("anon:"))

    def test_reinstall_capped_per_ip(self):
        toks = [self.q.register(f"install-{i:016d}", "3.3.3.3") for i in range(6)]
        self.assertTrue(all(toks[:5])); self.assertIsNone(toks[5])

    def test_ip_limit(self):
        env(DAILY_IP_LIMIT="3")
        toks = [self.q.signer.issue(f"install-{i:016d}") for i in range(5)]
        res = [self.q.check_and_consume(t, "4.4.4.4") for t in toks]
        self.assertEqual([r.allowed for r in res], [True, True, True, False, False])
        self.assertEqual(res[3].reason, "ip_limit")

    def test_cost_cap(self):
        env(DAILY_COST_LIMIT_USD="0.01")
        self.q.add_cost(0.02)
        d = self.q.check_and_consume(None, "5.5.5.5")
        self.assertFalse(d.allowed); self.assertEqual(d.reason, "cost_limit")

    def test_bad_install_id(self):
        with self.assertRaises(ValueError):
            self.q.register("short", "1.1.1.1")


class CacheTest(unittest.TestCase):
    def test_cacheable_rules(self):
        self.assertTrue(cache.is_cacheable_question("Mit jelent a Bosch 503 hibakód?"))
        self.assertFalse(cache.is_cacheable_question("Az akku 40,5 V nyugalomban, 32 V terhelve"))  # measurement -> case-specific
        self.assertFalse(cache.is_cacheable_question("Hívj a +36 30 123 4567 számon"))
        self.assertFalse(cache.is_cacheable_question("írj nekem: kovacs.bela@example.com"))

    def test_keys_separate_language_and_system(self):
        k1 = cache.make_key("chat", "hu", "Bosch 503 mit jelent")
        self.assertEqual(k1, cache.make_key("chat", "hu", "bosch 503 MIT jelent?"))
        self.assertNotEqual(k1, cache.make_key("chat", "de", "Bosch 503 mit jelent"))
        self.assertNotEqual(cache.make_key("diagnose", "hu", "bosch", "503"), cache.make_key("diagnose", "hu", "bafang", "503"))

    def test_store_roundtrip(self):
        c = cache.AnswerCache(store.MemoryStore())
        c.put("k", "x" * 50, "m"); self.assertEqual(c.get("k")["model"], "m")
        c.put("short", "tiny", "m"); self.assertIsNone(c.get("short"))


class MetricsTest(unittest.TestCase):
    def test_snapshot(self):
        m = metrics.Metrics()
        for i in range(10):
            m.record("chat_stream", i != 0, server_ms=100 + i, gemini_ttfb_ms=500 + i * 10, total_ms=2000, usage={"promptTokenCount": 100, "candidatesTokenCount": 50, "totalTokenCount": 150}, model="gemini-3.5-flash-lite")
        s = m.snapshot()["endpoints"]["chat_stream"]
        self.assertEqual(s["requests"], 10); self.assertEqual(s["error_rate"], 0.1)
        self.assertEqual(s["tokens"]["avg"], 150.0); self.assertEqual(s["gemini_ttfb_ms"]["p50"], 550.0)
        self.assertGreater(m.snapshot()["estimated_cost_usd"], 0)


class PromptTest(unittest.TestCase):
    def test_prompts_compact_and_safe(self):
        p = ai_core.chat_prompt("de", [{"role": "user", "content": "E10 Fischer"}])
        self.assertIn("German", p); self.assertIn("never invent", p); self.assertIn("detailed procedure", p)
        self.assertLess(len(ai_core.SAFETY_RULES), 600)
        self.assertIn("Never assert a pinout", ai_core.vision_prompt({"language": "hu", "mode": "connector"}))


class MainLogicTest(unittest.TestCase):
    """Runs main.py endpoint functions with stubbed FastAPI and a scripted Gemini connection."""
    @classmethod
    def setUpClass(cls):
        install_web_stubs()
        env(GEMINI_API_KEY="k", QUOTA_SECRET="s3cret", DAILY_AI_LIMIT="2", DAILY_IP_LIMIT="300", DAILY_GLOBAL_LIMIT="5000",
            DAILY_COST_LIMIT_USD="0", MAX_REGISTRATIONS_PER_IP="5", ADMIN_TOKEN="adm")
        import importlib, main
        cls.main = importlib.reload(main)

    def setUp(self):
        self.log = []
        self.script = []
        m = self.main
        m.STORE.__init__(); m.QUOTA.store = m.STORE; m.QUOTA.signer = quota.TokenSigner("s3cret"); m.CACHE.store = m.STORE
        gemini_client._client = gemini_client.GeminiClient(conn_factory=lambda: FakeConn(self.script, self.log))

    def chat_req(self, text):
        m = self.main
        return m.ChatRequest(language="hu", messages=[m.ChatMessage(role="assistant", content="Szia"), m.ChatMessage(role="user", content=text)])

    def test_cache_hit_skips_gemini_and_quota(self):
        m = self.main
        self.script += [ok_json("A Bosch 503 a sebességérzékelő hibája – ellenőrizd a mágnest és a kábelt.")]
        r1 = m.chat(self.chat_req("Mit jelent a Bosch 503?"), FakeRequest(), None)
        r2 = m.chat(self.chat_req("mit jelent a bosch 503"), FakeRequest(), None)
        self.assertEqual(r1.headers["X-Cache"], "MISS"); self.assertEqual(r2.headers["X-Cache"], "HIT")
        self.assertTrue(r2.content["cached"]); self.assertEqual(len(self.log), 1)

    def test_quota_enforced_and_registered(self):
        m = self.main
        tok = m.register(m.RegisterRequest(install_id="install-0123456789abcdef"), FakeRequest("9.9.9.9"))["token"]
        self.script += [ok_json("első válasz " * 5), ok_json("második válasz " * 5)]
        m.chat(self.chat_req("40 V és 30 V mérve, mi a hiba?"), FakeRequest("9.9.9.9"), tok)
        r = m.chat(self.chat_req("40 V és 31 V mérve, mi a hiba?"), FakeRequest("9.9.9.9"), tok)
        self.assertEqual(r.headers["X-Quota-Remaining"], "0")
        with self.assertRaises(Exception) as cm:
            m.chat(self.chat_req("40 V és 32 V mérve, mi a hiba?"), FakeRequest("9.9.9.9"), tok)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(cm.exception.headers["X-Quota-Reason"], "daily_quota")

    def test_provider_error_refunds(self):
        m = self.main
        self.script += [FakeResp(500, b"boom")]
        with self.assertRaises(Exception) as cm:
            m.chat(self.chat_req("10 V mért, mi ez?"), FakeRequest("8.8.8.8"), None)
        self.assertEqual(cm.exception.status_code, 502)
        self.assertEqual(m.QUOTA.remaining(None, "8.8.8.8"), 2)

    def test_stream_endpoint(self):
        m = self.main
        self.script += [sse_resp(["Első ", "rész: a PAS a pedálforgást, a nyomatékszenzor a pedálerőt méri."])]
        resp = m.chat_stream(self.chat_req("Mi a különbség a PAS és a nyomatékszenzor között?"), FakeRequest("7.7.7.7"), None)
        text = "".join(resp.body)
        self.assertIn("event: meta", text); self.assertIn('"t": "Első "', text); self.assertIn("event: done", text)
        self.assertEqual(resp.media_type, "text/event-stream")
        resp2 = m.chat_stream(self.chat_req("mi a kulonbseg a PAS es a nyomatekszenzor kozott"), FakeRequest("7.7.7.7"), None)
        self.assertEqual(resp2.headers["X-Cache"], "HIT"); self.assertIn("Első rész", "".join(resp2.body))

    def test_stream_provider_error_is_http(self):
        m = self.main
        self.script += [FakeResp(429, b"quota")]
        with self.assertRaises(Exception) as cm:
            m.chat_stream(self.chat_req("12 V mért, mi ez?"), FakeRequest("6.6.6.6"), None)
        self.assertEqual(cm.exception.status_code, 429)

    # ---------------- 2.2: camera AI end-to-end (app payload -> backend -> Gemini multimodal request)
    JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 400).decode()

    def vision_req(self, **kw):
        m = self.main
        d = dict(language="de", image_base64=self.JPEG, mime_type="image/jpeg", mode="code", brand="Bafang", motor_model="DP C18", symptom="")
        d.update(kw)
        return m.VisionDiagnosisRequest(**d)

    def test_vision_image_reaches_gemini_in_selected_language(self):
        m = self.main
        self.script += [ok_json("Sichtbar: E30\nCODES: 30 | BRAND: Bafang")]
        r = m.vision_diagnose(self.vision_req(), FakeRequest("5.5.5.1"), None)
        self.assertIn("CODES: 30", r.content["answer"])
        parts = self.log[0]["body"]["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"], {"mime_type": "image/jpeg", "data": self.JPEG})
        self.assertIn("Reply in German", parts[0]["text"]); self.assertIn("CODES:", parts[0]["text"])
        self.assertIn("gemini-3.8-flash", self.log[0]["path"])          # quality tier for images
        self.assertEqual(r.headers.get("X-Cache"), "BYPASS")            # images are never cached
        self.assertEqual(r.headers["X-Quota-Remaining"], "1")           # counted against the daily limit

    def test_vision_counts_against_daily_limit(self):
        m = self.main
        tok = m.register(m.RegisterRequest(install_id="install-cam0123456789ab"), FakeRequest("5.5.5.2"))["token"]
        self.script += [ok_json("a\nCODES: NONE | BRAND: NONE"), ok_json("b\nCODES: NONE | BRAND: NONE")]
        m.vision_diagnose(self.vision_req(), FakeRequest("5.5.5.2"), tok)
        m.vision_diagnose(self.vision_req(), FakeRequest("5.5.5.2"), tok)
        with self.assertRaises(Exception) as cm:                         # DAILY_AI_LIMIT=2 in this test env
            m.vision_diagnose(self.vision_req(), FakeRequest("5.5.5.2"), tok)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(len(self.log), 2, "blocked request must not reach Gemini")

    def test_vision_rejects_bad_images_without_using_quota(self):
        m = self.main
        bad = base64.b64encode(b"<html>not an image</html>" + b"x" * 200).decode()
        with self.assertRaises(Exception) as cm:
            m.vision_diagnose(self.vision_req(image_base64=bad), FakeRequest("5.5.5.3"), None)
        self.assertEqual(cm.exception.status_code, 422)
        with self.assertRaises(Exception) as cm2:
            m.vision_diagnose(self.vision_req(image_base64="%%%" * 50), FakeRequest("5.5.5.3"), None)
        self.assertEqual(cm2.exception.status_code, 422)
        self.assertEqual(len(self.log), 0)
        self.assertEqual(m.QUOTA.remaining(None, "5.5.5.3"), 2)

    def test_vision_compare_needs_second_image_and_sends_both(self):
        m = self.main
        with self.assertRaises(Exception) as cm:
            m.vision_diagnose(self.vision_req(mode="compare"), FakeRequest("5.5.5.4"), None)
        self.assertEqual(cm.exception.status_code, 422)
        self.script += [ok_json("diff\nCODES: NONE | BRAND: NONE")]
        m.vision_diagnose(self.vision_req(mode="compare", image2_base64=self.JPEG), FakeRequest("5.5.5.4"), None)
        self.assertEqual(sum(1 for p in self.log[0]["body"]["contents"][0]["parts"] if "inline_data" in p), 2)

    def test_vision_provider_error_refunds(self):
        m = self.main
        self.script += [FakeResp(500, b"boom")]
        with self.assertRaises(Exception) as cm:
            m.vision_diagnose(self.vision_req(), FakeRequest("5.5.5.5"), None)
        self.assertEqual(cm.exception.status_code, 502)
        self.assertEqual(m.QUOTA.remaining(None, "5.5.5.5"), 2)

    # ---------------- 2.2.1: in-app AI content reports (Google Play AI-generated content policy)
    def test_report_stored_and_admin_only(self):
        m = self.main
        r = m.report_ai_content(m.ReportRequest(kind="vision", language="de", reason="incorrect", comment="falscher Code", excerpt="CODES: E21"), FakeRequest("4.4.4.1"))
        self.assertTrue(r["ok"])
        with self.assertRaises(Exception):
            m.reports_view(None, None)                       # hidden without admin token
        v = m.reports_view(None, "adm")
        self.assertEqual(v["count"], 1)
        self.assertEqual(v["reports"][0]["reason"], "incorrect"); self.assertEqual(v["reports"][0]["excerpt"], "CODES: E21")
        self.assertEqual(len(self.log), 0, "reports never call Gemini")
        self.assertEqual(m.QUOTA.remaining(None, "4.4.4.1"), 2, "reports do not use the AI quota")

    def test_report_rate_limited_per_ip(self):
        m = self.main
        for _ in range(m.REPORTS_PER_IP_PER_DAY):
            m.report_ai_content(m.ReportRequest(reason="other"), FakeRequest("4.4.4.2"))
        with self.assertRaises(Exception) as cm:
            m.report_ai_content(m.ReportRequest(reason="other"), FakeRequest("4.4.4.2"))
        self.assertEqual(cm.exception.status_code, 429)

    def test_metrics_protected(self):
        m = self.main
        with self.assertRaises(Exception):
            m.metrics_view(None)
        self.assertIn("endpoints", m.metrics_view("adm"))


if __name__ == "__main__":
    unittest.main()
