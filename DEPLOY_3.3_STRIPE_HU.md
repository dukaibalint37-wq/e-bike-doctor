# Éles Stripe-fizetés – backend 3.3.0 élesítése (előkészítve, NINCS bekapcsolva)

Eszköz: `tools/deploy_production_33.py` (Render `srv-dbfh3jss728c73ak0hc0`, `https://e-bike-doctor.onrender.com`).
Titkos értéket nem ír ki; minden lépés előtt új mentés készül a `C:\EBD_SIGNING\prod-backup-<idő>-<címke>` mappába (soha nem írja felül).

## Éles webhook (Stripe Dashboard → **Live mode** → Developers → Webhooks → Add endpoint)
- URL: `https://e-bike-doctor.onrender.com/v1/shop/stripe/webhook`
- Események (7):
  `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`,
  `charge.refunded`, `charge.refund.updated`, `charge.dispute.created`, `charge.dispute.closed`

## Környezeti változók (éles Render-szolgáltatás)
| Változó | Érték | Mikor |
|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_live_…` (vagy korlátozott `rk_live_…`) | 2. lépés |
| `STRIPE_PUBLISHABLE_KEY` | `pk_live_…` | 2. lépés |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` (az éles végpont aláíró titka) | 2. lépés |
| `SHOP_MODE` | `test` → `live` | 2. lépés |
| `OWNER_NOTIFY_EMAIL` | = `MAIL_REPLY_TO` (dukaibalint37@gmail.com) | 2. lépés |
| `STRIPE_PRICE_MAP` | üres (az árak: 19,99 / 14,99 EUR a kódból) | nem kell |
| `STRIPE_AUTOMATIC_TAX`, `STRIPE_INVOICE`, `STRIPE_REQUIRE_TERMS` | `0` / nincs (kézi számlázás) | nem kell |

Változatlan marad: Upstash, licencaláíró kulcs, pepper, R2-letöltés (2.0.0 / 3.0.1), Resend, admin-2FA, CORS, `SITE_URL`.

A három kulcsot a tulajdonos menti **új fájlba**: `C:\EBD_SIGNING\stripe-live\stripe-live.env`
```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

## Lépések
1. `python tools/deploy_production_33.py plan` – csak olvas: verzió, fast-forward, kulcsfájl, éles Stripe-fiók és webhook ellenőrzése.
2. **1. lépés – kód, értékesítés nélkül** (külön jóváhagyás): `deploy --confirm`
   mentés → visszaállítási címke `eles-3.2.0-3.3-elott` → `main` fast-forward a `feature/stripe-checkout` ágra → regresszió
   (3.3.0, aláírt frissítési manifestek változatlanok, tulajdonosi licencek frissítése, Stripe-pénztár **503 = kikapcsolva**).
3. **2. lépés – éles vásárlás** (CSAK külön tulajdonosi jóváhagyással): `activate --confirm`
   kulcsfájl + éles webhook ellenőrzése → mentés → a fenti változók → újraindítás → éles ellenőrzés (nyitott munkamenet, terhelés nincs).
4. Weboldal (külön jóváhagyás): `deploy/site.production.json` → `"checkout": {"windows": "stripe", "android": "stripe"}`,
   `deploy/launch_state.json` → `public_full_site` és a fizetési kapu tételei, jogi adatok; majd `main` → Cloudflare.
5. Első valódi vásárlás saját kártyával (kis összeg: Android 14,99 €) → licenclevél, értesítő, letöltés, aktiválás → teljes visszatérítés.

Vészleállítás: `deactivate --confirm` (SHOP_MODE=test → a pénztár azonnal 503). Visszaállítás: `rollback <mentésmappa> --confirm`.

## Megjegyzések
- **Render free csomag**: a szerver tétlenül elalszik; az első kérés 30–60 s. A Stripe webhook ilyenkor időtúllépéssel
  hibázhat, és a Stripe később újrapróbálja → a licenclevél késhet. Éles értékesítéshez a fizetős (Starter) csomag javasolt.
- **Adaptive Pricing be van kapcsolva** a Stripe-fiókban: magyar kártyával HUF-ban is lehet fizetni; a licenc, a bevétel és a
  visszatérítés EUR-ban számol (3.3: `currency_conversion`). A számla pénznemét a könyvelővel egyeztesd.
- Tesztlicencek az élesen: 7 db, mind visszavonva és `test` jelölésű (élő statisztikában nem szerepelnek); 2 aktív tulajdonosi licenc.
  Opcionális: admin → „Tesztadatok leválasztása” (purge) az élesítés előtt.
