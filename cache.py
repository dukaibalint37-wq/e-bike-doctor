"""Verified answer cache for repeated, generic technical questions.

Only generic single-turn questions are cached (no history, no measurements, no personal data).
Key = language + model tier + normalized brand/system/model/code + normalized question, so answers for
different bikes/systems never mix. Only complete successful AI answers are stored ("verified").
"""
import hashlib
import os
import re
import unicodedata

PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d \-/]{7,}\d|\b(?:utca|street|strasse|straße|tel\.?|phone)\b", re.I)
MEASUREMENT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:v|a|mv|ma|w|wh|ah|ohm|Ω|%|°c|km)\b", re.I)


def normalize(text):
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_cacheable_question(text):
    if not text or len(text) > int(os.getenv("CACHE_MAX_QUESTION_CHARS", "240")):
        return False
    if PERSONAL.search(text) or MEASUREMENT.search(text):
        return False
    return len(normalize(text)) >= 4


def make_key(kind, language, *fields):
    norm = "|".join(normalize(f) for f in fields)
    return f"cache:v1:{kind}:{language}:" + hashlib.sha256(norm.encode()).hexdigest()[:32]


class AnswerCache:
    def __init__(self, store):
        self.store = store
        self.ttl = int(os.getenv("CACHE_TTL_SECONDS", str(7 * 86_400)))
        self.enabled = os.getenv("CACHE_ENABLED", "1") != "0"

    def get(self, key):
        if not self.enabled:
            return None
        v = self.store.get(key)
        return v if isinstance(v, dict) and v.get("answer") else None

    def put(self, key, answer, model):
        if self.enabled and answer and len(answer) > 40:
            self.store.set(key, {"answer": answer, "model": model}, self.ttl)
