"""Counter/value store for quota, cache and metrics.

- MemoryStore: default; resets when the Render instance restarts or sleeps (free tier).
- UpstashStore: optional persistent store (Upstash Redis REST API, HTTPS, no extra dependency).
  Enabled when UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are set.
"""
import json
import os
import threading
import time
import urllib.request


class MemoryStore:
    kind = "memory"

    def __init__(self, max_keys=50_000):
        self._d = {}
        self._lock = threading.Lock()
        self._max = max_keys

    def _purge(self, now):
        if len(self._d) > self._max:
            for k in [k for k, (_, exp) in self._d.items() if exp and exp < now][: self._max // 4]:
                self._d.pop(k, None)
            while len(self._d) > self._max:
                self._d.pop(next(iter(self._d)))

    def incr(self, key, ttl, amount=1):
        now = time.time()
        with self._lock:
            val, exp = self._d.get(key, (0, 0))
            if exp and exp < now:
                val, exp = 0, 0
            val = int(val) + amount
            self._d[key] = (val, exp or now + ttl)
            self._purge(now)
            return val

    def get(self, key):
        now = time.time()
        with self._lock:
            v = self._d.get(key)
            if not v:
                return None
            val, exp = v
            if exp and exp < now:
                self._d.pop(key, None)
                return None
            return val

    def set(self, key, value, ttl):
        with self._lock:
            self._d[key] = (value, time.time() + ttl)
            self._purge(time.time())


class UpstashStore:
    kind = "upstash"

    def __init__(self, url, token, timeout=2.5, prefix=""):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # STORE_KEY_PREFIX: separate namespace (e.g. "stg:" for the staging service). Production uses no prefix.
        self.p = prefix

    def _pipeline(self, commands):
        req = urllib.request.Request(self.url + "/pipeline", data=json.dumps(commands).encode(),
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return [x.get("result") for x in json.loads(r.read().decode())]

    def incr(self, key, ttl, amount=1):
        key = self.p + key
        res = self._pipeline([["INCRBY", key, str(amount)], ["EXPIRE", key, str(int(ttl)), "NX"]])
        return int(res[0])

    def get(self, key):
        res = self._pipeline([["GET", self.p + key]])[0]
        if res is None:
            return None
        try:
            return json.loads(res)
        except (ValueError, TypeError):
            return res

    def set(self, key, value, ttl):
        self._pipeline([["SET", self.p + key, json.dumps(value), "EX", str(int(ttl))]])


class FailSafeStore:
    """Uses the remote store; on any network error falls back to memory so the API keeps working."""

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.kind = primary.kind + "+memory-fallback"
        self.errors = 0

    def _call(self, name, *a):
        try:
            return getattr(self.primary, name)(*a)
        except Exception:
            self.errors += 1
            return getattr(self.fallback, name)(*a)

    def incr(self, key, ttl, amount=1):
        return self._call("incr", key, ttl, amount)

    def get(self, key):
        return self._call("get", key)

    def set(self, key, value, ttl):
        return self._call("set", key, value, ttl)


def make_store():
    url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return FailSafeStore(UpstashStore(url, token, prefix=os.getenv("STORE_KEY_PREFIX", "").strip()), MemoryStore())
    return MemoryStore()
