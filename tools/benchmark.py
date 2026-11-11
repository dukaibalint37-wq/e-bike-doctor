"""E-Bike Doctor latency benchmark (run from any computer, not part of the server).

Usage:
  python3 tools/benchmark.py https://e-bike-doctor.onrender.com --runs 5 [--admin-token XXX]
Measures separately: server response (/health), non-streaming total, streaming time-to-first-text,
streaming total, and prints the server-side /v1/metrics snapshot (Gemini first byte, tokens, error rate).
Each run uses a slightly different question so the answer cache does not hide Gemini latency.
Note: one run consumes one AI question of the daily quota of this IP / token.
"""
import argparse, json, statistics, sys, time, uuid
import http.client
from urllib.parse import urlparse


def conn_for(url):
    u = urlparse(url)
    C = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return C(u.hostname, u.port, timeout=120)


def request(c, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    t0 = time.perf_counter()
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r, t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base"); ap.add_argument("--runs", type=int, default=5); ap.add_argument("--admin-token", default="")
    ap.add_argument("--lang", default="hu")
    a = ap.parse_args()
    c = conn_for(a.base)
    res = {"health_ms": [], "chat_total_ms": [], "stream_first_text_ms": [], "stream_total_ms": [], "errors": 0}
    # cold/warm server response
    for i in range(3):
        r, t0 = request(c, "GET", "/health"); r.read()
        res["health_ms"].append((time.perf_counter() - t0) * 1000)
    tok = None
    r, t0 = request(c, "POST", "/v1/register", {"install_id": "bench-" + uuid.uuid4().hex})
    body = r.read()
    if r.status == 200:
        tok = json.loads(body)["token"]
    hdr = {"X-EBD-Token": tok} if tok else {}
    for i in range(a.runs):
        q = f"Röviden: mire figyeljek egy 36 V-os agymotoros e-bike Hall-szenzor ellenőrzésekor? ({i}-{uuid.uuid4().hex[:4]})"
        msgs = {"language": a.lang, "messages": [{"role": "user", "content": q}]}
        r, t0 = request(c, "POST", "/v1/chat", msgs, hdr); r.read()
        if r.status == 200: res["chat_total_ms"].append((time.perf_counter() - t0) * 1000)
        else: res["errors"] += 1; print("chat HTTP", r.status, file=sys.stderr)
        r, t0 = request(c, "POST", "/v1/chat/stream", {**msgs, "messages": [{"role": "user", "content": q + " s"}]}, hdr)
        if r.status != 200:
            res["errors"] += 1; r.read(); print("stream HTTP", r.status, file=sys.stderr); continue
        first = None
        while True:
            line = r.readline()
            if not line: break
            if first is None and line.startswith(b"event: delta"):
                first = (time.perf_counter() - t0) * 1000
        res["stream_total_ms"].append((time.perf_counter() - t0) * 1000)
        if first: res["stream_first_text_ms"].append(first)
    def s(v): return f"n={len(v)} avg={statistics.mean(v):.0f} ms  median={statistics.median(v):.0f} ms  max={max(v):.0f} ms" if v else "no data"
    print("Server /health (1st call may include Render cold start):", [round(x) for x in res["health_ms"]])
    for k in ("chat_total_ms", "stream_first_text_ms", "stream_total_ms"):
        print(f"{k:24s} {s(res[k])}")
    print("errors:", res["errors"])
    if a.admin_token:
        r, _ = request(c, "GET", "/v1/metrics", headers={"X-Admin-Token": a.admin_token})
        print(json.dumps(json.loads(r.read()), indent=1))


if __name__ == "__main__":
    main()
