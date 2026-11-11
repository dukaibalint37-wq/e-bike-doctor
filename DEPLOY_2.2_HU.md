# Backend 2.2 telepítése Renderre – terv, előfeltételek, visszaállítás

> Állapot (2026-11-11): **előkészítve, NEM telepítve.** Az élő szerverhez senki nem nyúlt.
> Titkos értéket (API-kulcs, QUOTA_SECRET, ADMIN_TOKEN, Upstash token) soha ne írj chatbe – csak a Render
> „Environment” oldalára.

## 1. Jelenlegi élő állapot (mérve)
- `https://e-bike-doctor.onrender.com/openapi.json` → API **0.3.0**, végpontok: `/health`, `/v1/chat`, `/v1/diagnose`.
- Nincs: streaming, kamerás képelemzés (`/v1/vision-diagnose`), napi 50-es keret (`/v1/register`, `/v1/quota`), metrikák.
- Hidegindítás 32,8 s; meleg chat medián 2,85 s (teljes válasz egyben).

## 2. Kompatibilitás (ellenőrizve)
- A régi `ChatRequest` / `ChatMessage` / `DiagnosisRequest` mezői és korlátai **azonosak** az új backendben
  (az új csak egy opcionális `vehicle_type` mezőt ad) → a Playen lévő régebbi app-verziók is működnek.
- A 2.2 app a régi és az új backenddel is működik; az új képességeket ≤ 10 percen belül magától észleli.
- Ellenőrzések: backend unit-tesztek **34/34 OK**; valódi FastAPI 0.162 / pydantic 2.14 / uvicorn 0.54 indítás
  helyben OK; `/health`, `/v1/register`, `/v1/quota`, `/v1/metrics` (admin) működik; kulcs nélkül minden AI-végpont
  503-at ad és **nem** fogyaszt keretet.

## 3. Előfeltételek – ezeket NEKED kell beállítani (Render Dashboard → a szolgáltatás → Environment)
| Változó | Kötelező? | Miért |
|---|---|---|
| `GEMINI_API_KEY` | igen (már megvan) | Gemini hívások – csak a szerveren |
| `QUOTA_SECRET` | **igen** | a napi-keret tokenek aláírása; nélküle minden újraindításkor érvénytelenek |
| `ADMIN_TOKEN` | igen | `/v1/metrics` védelme |
| `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` | **erősen ajánlott** | ingyenes Renderen 15 perc tétlenség után a példány leáll → Upstash nélkül a napi 50-es számláló **nullázódik**, a keret megkerülhető |
| `DAILY_AI_LIMIT=50` | nem (alapértelmezés 50) | |
| `GEMINI_MODEL`, `GEMINI_MODEL_FAST` | nem | ha egy régi érték be van állítva, felülírja a kódot. A kód 404 esetén 1 órára kihagyja a nem létező modellt, a lánc végén `gemini-2.5-flash` |
Véletlen értékek generálása saját gépen (nem kell senkinek elküldeni):
`powershell -c "[guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N')"`

Render Settings (ellenőrizd, a 2.1 leírás szerint):
Root Directory `backend` · Build `pip install -r requirements.txt` ·
Start `uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 75`.

## 4. Hozzáférés – két lehetőség
**A) Te telepítesz (ajánlott, nem kell semmilyen hozzáférést adni):**
1. GitHub → a backend repója → `backend/` mappa → *Add file → Upload files* → a
   `Desktop\EBD_BACKEND_2.2_DEPLOY` mappa tartalma (vagy a ZIP kicsomagolva) → Commit.
2. Render → a szolgáltatás → *Manual Deploy → Deploy latest commit* (ha az auto-deploy nincs bekapcsolva).
3. Szólj, és lefuttatom az ellenőrzést (4. pont) – ehhez nem kell titok, a szerver nyilvános.

**B) Én telepítek, a te jóváhagyásod után:**
- telepítsd a Git for Windows-t és a GitHub CLI-t, majd ebben a munkamenetben futtasd: `! gh auth login`
  (a token a gh saját tárolójába kerül, nem a chatbe);
- add meg a repó nevét és ágát; a Render auto-deploy vagy a *Manual Deploy* indítja a telepítést;
- opcionális: Render API-kulcs csak felhasználói környezeti változóként (`RENDER_API_KEY`), soha nem chatben –
  ezzel a telepítés állapotát és a visszaállítást is tudom kezelni.
Az élő telepítést csak a külön, kifejezett jóváhagyásod után indítom.

## 5. Visszaállítási terv
1. **Telepítés előtt:** készíts képernyőképet a Render *Settings* és *Environment* oldalról (a változók NEVÉRŐL;
   az értékeket nem kell lefotózni), és jegyezd fel az aktuális deploy azonosítóját (*Events*).
2. **Hiba esetén (1 perc):** Render → *Events/Deploys* → az előző sikeres deploy → **Rollback**.
   Alternatíva: GitHubon a commit visszavonása (*Revert*) → automatikus újratelepítés.
3. **Az app nem romlik el visszaállításkor:** a 2.2 app a régi backenddel is működik (streaming és kamera nélkül).
4. Visszaállítási feltételek: `/health` nem 200; `ai_configured=false`; a chat 5 percen belül tartósan 5xx-et ad;
   vagy az ellenőrző szkript „chat (kompatibilis)” sora HIBA.

## 6. Telepítés utáni ellenőrzés
`powershell -ExecutionPolicy Bypass -File backend\tools\verify_deploy.ps1`
(opcionális: előtte `$env:EBD_ADMIN_TOKEN` beállítása a metrikákhoz – a szkript titkot nem ír ki).
Ellenőrzi: verzió 2.2.0, AI konfigurálva, streaming első szöveg ideje, kompatibilis chat, **kamerás elemzés egy
generált „E21” kijelzőképpel** (a `CODES:` sornak E21-et kell tartalmaznia), napi keret (50 → csökken), Upstash.
Költség: kb. 3 AI-kérés egy eldobható teszt-azonosítóval.

## 7. Hidegindítás (32,8 s mérve)
Render Starter csomag (nem alszik) vagy külső 10 perces ping a `/health`-re. A kód ezt nem tudja megszüntetni.
