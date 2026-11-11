"""In-process performance and cost metrics (exposed at /v1/metrics with ADMIN_TOKEN)."""
import os
import threading
import time
from collections import defaultdict, deque


def _price(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def estimate_cost_usd(usage):
    """Prices per 1M tokens are configurable (GEMINI_PRICE_IN_PER_M / GEMINI_PRICE_OUT_PER_M)."""
    if not usage:
        return 0.0
    tin = usage.get("promptTokenCount", 0) or 0
    tout = (usage.get("candidatesTokenCount", 0) or 0) + (usage.get("thoughtsTokenCount", 0) or 0)
    return tin / 1e6 * _price("GEMINI_PRICE_IN_PER_M", 0.75) + tout / 1e6 * _price("GEMINI_PRICE_OUT_PER_M", 3.75)


class Metrics:
    def __init__(self, window=500):
        self.started = time.time()
        self._lock = threading.Lock()
        self.count = defaultdict(int)
        self.errors = defaultdict(int)
        self.cache_hits = defaultdict(int)
        self.series = defaultdict(lambda: deque(maxlen=window))
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0

    def record(self, endpoint, ok, server_ms=None, gemini_ttfb_ms=None, total_ms=None, usage=None, cached=False, model=""):
        with self._lock:
            self.count[endpoint] += 1
            if not ok:
                self.errors[endpoint] += 1
            if cached:
                self.cache_hits[endpoint] += 1
            for name, v in (("server_ms", server_ms), ("gemini_ttfb_ms", gemini_ttfb_ms), ("total_ms", total_ms)):
                if v is not None:
                    self.series[f"{endpoint}:{name}"].append(float(v))
            if usage:
                self.tokens_in += usage.get("promptTokenCount", 0) or 0
                self.tokens_out += (usage.get("candidatesTokenCount", 0) or 0) + (usage.get("thoughtsTokenCount", 0) or 0)
                self.series[f"{endpoint}:tokens"].append(float(usage.get("totalTokenCount", 0) or 0))
                c = estimate_cost_usd(usage)
                self.cost_usd += c
            if model:
                self.count["model:" + model] += 1

    @staticmethod
    def _stats(values):
        if not values:
            return None
        v = sorted(values)
        pick = lambda q: v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]
        return {"n": len(v), "avg": round(sum(v) / len(v), 1), "p50": round(pick(0.5), 1), "p95": round(pick(0.95), 1)}

    def snapshot(self):
        with self._lock:
            endpoints = {}
            for ep in sorted(k for k in self.count if not k.startswith("model:")):
                n = self.count[ep]
                endpoints[ep] = {
                    "requests": n, "errors": self.errors[ep],
                    "error_rate": round(self.errors[ep] / n, 3) if n else 0,
                    "cache_hits": self.cache_hits[ep],
                    **{name: self._stats(list(self.series[f"{ep}:{name}"])) for name in ("server_ms", "gemini_ttfb_ms", "total_ms", "tokens")},
                }
            return {"uptime_s": int(time.time() - self.started), "endpoints": endpoints,
                    "models": {k[6:]: v for k, v in self.count.items() if k.startswith("model:")},
                    "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                    "estimated_cost_usd": round(self.cost_usd, 4)}
