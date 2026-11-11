# E-Bike Doctor backend 2.1 – Render telepítési és frissítési útmutató

## 0. Sürgős: modellcsere
A Gemini API-n a `gemini-2.5-flash` leállítási dátuma 2026. október 16. volt, a `gemini-2.0-flash` 2026. június 1-jén
leállt. Ha a Renderen még ezek vannak beállítva, az AI 404/502 hibát ad. A 2.1 alapértelmezése:
`gemini-3.8-flash` (minőségi szint) és `gemini-3.5-flash-lite` (gyors szint), 404 esetén automatikus tartalékkal.
A Render „Environment” oldalon a régi `GEMINI_MODEL` értéket **töröld vagy írd át** – a környezeti változó felülírja a kódot.

## 1. Fájlok
`main.py`, `ai_core.py`, `gemini_client.py`, `quota.py`, `cache.py`, `metrics.py`, `store.py`, `requirements.txt`
(+ `tests/`, `tools/benchmark.py` – ezek nem kellenek a futáshoz). Új Python-csomag nem kell.

## 2. Telepítés lépésről lépésre
1. A `backend/` mappa tartalmát másold a GitHub-repód backend mappájába, commit + push.
2. Render → a szolgáltatás → **Settings**: Root Directory = `backend`;
   Build Command = `pip install -r requirements.txt`;
   Start Command = `uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 75`.
3. Render → **Environment**: a `.env.example` alapján add meg legalább: `GEMINI_API_KEY`, `QUOTA_SECRET`,
   `ADMIN_TOKEN`; javasolt: `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`.
4. **Manual Deploy → Deploy latest commit**.
5. Ellenőrzés böngészőben: `https://e-bike-doctor.onrender.com/health` →
   `"version": "2.1.0"`, `"streaming": true`, `"ai_configured": true`, `"daily_limit": 50`, `"store": "upstash+memory-fallback"`.
6. Mérés: `python3 tools/benchmark.py https://e-bike-doctor.onrender.com --runs 5 --admin-token <ADMIN_TOKEN>`.

## 3. Upstash (ingyenes, perzisztens számlálók) – miért kell?
Render ingyenes szinten a példány 15 perc inaktivitás után leáll, és újraindításkor a memória törlődik. Upstash nélkül a
napi 50-es számláló és a gyorsítótár minden újraindításkor nullázódik (a keret így megkerülhetővé válik).
Beállítás: upstash.com → Create Database (Redis, régió: Frankfurt/eu-central-1) → REST API fül → a
`UPSTASH_REDIS_REST_URL` és `UPSTASH_REDIS_REST_TOKEN` értékeket másold a Render Environment-be.
Ha Upstash átmenetileg nem érhető el, a szerver memóriára vált (nem áll le).

## 4. Hidegindítás (cold start)
- Ingyenes Render: alvás után az első kérés kb. 30–60 s (Render-ébredés) + a Gemini válaszideje.
- Az app 2.1 induláskor és a Chat megnyitásakor háttérben „felébreszti” a szervert (`/health`), így mire a kérdés
  elkészül, a szerver gyakran már fut.
- Garantált gyors első válaszhoz: Render **Starter** (fizetős) csomag – a példány nem alszik el.

## 5. Végpontok
| Végpont | Leírás |
|---|---|
| `GET /health` | állapot, modellek, keret, tár |
| `POST /v1/register` | `{install_id}` → aláírt token (napi keret azonosítója) |
| `GET /v1/quota` | hátralévő napi kérdések (`X-EBD-Token` fejléc) |
| `POST /v1/chat`, `/v1/diagnose`, `/v1/smart-diagnose`, `/v1/vision-diagnose` | 2.0-val kompatibilis JSON-válasz |
| `POST /v1/chat/stream`, `/v1/diagnose/stream`, `/v1/smart-diagnose/stream` | SSE: `meta`, `delta`, `done`, `error` események |
| `GET /v1/metrics` | `X-Admin-Token` fejléccel: szerveridő, Gemini első bájt, teljes idő, token, hibaarány, becsült költség |

Válaszfejlécek: `X-Quota-Remaining`, `X-Quota-Limit`, `X-Cache` (HIT/MISS/BYPASS), `Server-Timing`.
Keret túllépésekor: HTTP 429 + `X-Quota-Reason: daily_quota` + `Retry-After` (másodperc éjfélig).

## 6. Átállás fizetős Gemini-szintre
Google AI Studio → a projekt → Billing bekapcsolása. Kódmódosítás nem kell. Ezután állítsd be:
`GEMINI_PRICE_IN_PER_M` / `GEMINI_PRICE_OUT_PER_M` (aktuális árlista), `DAILY_COST_LIMIT_USD` (napi költségplafon),
és szükség esetén `DAILY_GLOBAL_LIMIT`. A `/v1/metrics` mutatja a becsült napi költséget.

## 7. Tesztek
`python3 -m unittest discover -s tests -v` – 27 teszt, hálózat és FastAPI-telepítés nélkül (a FastAPI-t tesztcsonk helyettesíti).
