# E-Bike Doctor backend 3.0 – licencszerver, webshop, költségvédelem

**Állapot: helyben elkészült és tesztelt, NINCS élesítve.** A `backend-commercial-3.0` helyi git-ágon van, nincs pusholva.
Élesítés (push → Render deploy) csak a tulajdonos külön jóváhagyása után.

## Mi új a 2.2.1-hez képest

| Terület | Tartalom |
|---|---|
| Licencszerver | `/v1/license/activate`, `/refresh`, `/deactivate`; termékek: `EBD-WIN` (Windows), `EBD-AND` (Android webes kiadás). Egy licenc = egy aktív eszköz; eszközcsere `transfer` jelzővel, 30 napon belül max. 2. A kulcsot a szerver csak SHA-256 kivonatként (borssal) tárolja. |
| Offline lease | ECDSA P-256 aláírt, eszközhöz kötött igazolás (`EBDL1-…`), 14 napig offline érvényes. A privát kulcs csak a szerver környezeti változójában van (`LEASE_SIGNING_KEY_PEM`), az appokban csak a nyilvános kulcs. |
| AI-hozzáférés | `AI_ACCESS_MODE=open|token|license`. A licenchez kötött AI-token (`lic-…`) visszavonáskor / áthelyezéskor azonnal érvénytelen. A napi 50-es keret licencenként számít (újratelepítés nem nullázza). **Javítva:** a válasz-gyorsítótár már a licenckapu UTÁN jön (lásd tesztjelentés). |
| Költségvédelem | `DAILY_COST_LIMIT_USD`, `MONTHLY_COST_LIMIT_USD`, egyszeri figyelmeztetések 50/80/100%-nál (`COST_WARN_LEVELS`) e-mailben (`ADMIN_ALERT_EMAIL`). Gemini 402/403 → udvarias „átmenetileg nem elérhető” üzenet (`provider_billing`). |
| Lemon Squeezy | `/v1/shop/lemonsqueezy/webhook` – HMAC-SHA256 aláírás-ellenőrzés (`X-Signature`), `order_created` → licenc + e-mail, `order_refunded` → visszavonás, idempotens (ismételt kézbesítés nem ad 2. licencet), `SHOP_MODE=test` alatt csak teszt-rendelés, `live` alatt csak éles. |
| Letöltésvédelem | `/v1/shop/download?t=…` – 48 órás HMAC-aláírt link, csak fizetett/érvényes licenchez; visszavont licenchez nem. |
| Frissítések | `/v1/app/update?product=EBD-WIN|EBD-AND` – offline aláírt manifest (`updates/*.json`); az appok ellenőrzik az aláírást, a méretet és a SHA-256-ot. |
| Admin | `/admin` (böngészős felület) + `/v1/admin/licenses` (keresés, létrehozás, visszavonás, visszaállítás, eszköz-reset, új kulcs), `/v1/admin/stats` (eladások, visszatérítések, AI-használat, költség). Token nélkül 404. |

## Render környezeti változók (a meglévők mellé)

Minta: `.env.example`. **Ne commitold a valódi értékeket.**

| Változó | Érték / teendő |
|---|---|
| `LEASE_SIGNING_KEY_PEM` | A `C:\EBD_SIGNING\license-server-lease\private.pem` TELJES tartalma (BEGIN/END sorokkal). Nélküle a licencvégpontok 503 `not_configured`. |
| `LICENSE_KEY_PEPPER` | Hosszú véletlen szöveg (`python -c "import secrets;print(secrets.token_hex(32))"`). **Élesítés után ne változtasd** – különben a kiadott kulcsok nem találhatók. |
| `AI_ACCESS_MODE` | Első deploykor `open` (a Playen lévő 2.2-es app tovább működik). Lásd lent az átállási sorrendet. |
| `AI_PUBLIC` | `0` – csak a Gemini fizetős szint igazolása + jóváhagyás után `1`. A 2.3-as Android és 1.1-es Windows app csak `1` esetén használ AI-t. |
| `DAILY_COST_LIMIT_USD`, `MONTHLY_COST_LIMIT_USD` | Javaslat a 10 USD-os induló kerethez: napi `0.5`, havi `8`. |
| `COST_WARN_LEVELS`, `ADMIN_ALERT_EMAIL` | `50,80,100` és a saját e-mail-címed. |
| `SHOP_MODE` | `test` – amíg a Lemon Squeezy teszt-módban fut. |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | A Lemon Squeezy webhooknál megadott signing secret. |
| `LS_PRODUCT_MAP` | `{"<windows variant id>":"EBD-WIN","<android variant id>":"EBD-AND"}` |
| `PUBLIC_BASE_URL` | `https://e-bike-doctor.onrender.com` |
| `DOWNLOAD_SECRET` | Hosszú véletlen szöveg. |
| `DOWNLOAD_URL_EBD_WIN`, `DOWNLOAD_URL_EBD_AND` | A telepítő / web-APK nem nyilvános tárhely-URL-je (pl. privát tárhely előre aláírt linkje). |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | Vásárlási e-mailekhez (licenckulcs). Nélküle a levél csak a szerver kitakart postafiókjába kerül, a vevő nem kapja meg. |

## Javasolt élesítési sorrend (mindegyik lépés a te jóváhagyásodhoz kötött)

1. Render env változók beállítása (fenti tábla), `AI_ACCESS_MODE=open`, `AI_PUBLIC=0`, `SHOP_MODE=test`.
2. Jóváhagyás után: `git push origin backend-commercial-3.0:main` → Render deploy. Ellenőrzés: `tools\verify_deploy.ps1` (3.x, `license_server=true`).
3. Admin felületen tulajdonosi licencek létrehozása (Windows + Android, „owner”), aktiválás a saját gépeken.
4. Lemon Squeezy teszt-vásárlás (teszt-kártya) → licenc-e-mail, letöltés, aktiválás, visszatérítés → visszavonás.
5. Gemini fizetős szint igazolása (számlázási képernyő) → jóváhagyás → `AI_PUBLIC=1`.
6. Amikor a régi (2.2-es) Play-verziók kifutottak: `AI_ACCESS_MODE=token` (anonim hozzáférés megszűnik). A `license` mód a Play-kiadást kizárná, mert a Play-vásárlást a szerver jelenleg nem ellenőrzi (lásd korlátok).
7. Éles bolt: `SHOP_MODE=live`.

## Ismert korlátok (őszintén)

- A Play-kiadás AI-jogosultsága a telepítési azonosítón (ANDROID_ID-kivonat) alapul, a szerver **nem ellenőrzi** a Google Play-vásárlást (ehhez Play Integrity API / Play Developer API + szolgáltatásfiók kellene). A visszaélést a napi 50-es keret, a regisztrációs IP-korlát, a globális napi korlát és a napi/havi költségplafon korlátozza.
- A rendszer nem feltörhetetlen: egy módosított (újraaláírt) kliens a helyi korlátokat megkerülheti; a szerveroldali AI-hozzáférést és -költséget azonban a szerver kontrollálja.
- A Render free tier elalszik; az első kérés lassú lehet. Upstash nélkül a számlálók újraindításkor nullázódnak – éles használathoz Upstash kötelező.
