# E-Bike Doctor AI backend – előkészítő szolgáltatás

Ez valódi, szerveroldali AI-végpont mintája, de **még nincs telepítve nyilvános szerverre és az Android alkalmazás nincs hozzákapcsolva**. Ne adj ki úgy APK-t, mintha az online AI működne.

## Helyi próba
1. Python 3.11+ telepítése.
2. `python -m venv .venv`, aktiválás, majd `pip install -r requirements.txt`.
3. Környezeti változóként add meg az `OPENAI_API_KEY` értékét és az elérhető, engedélyezett `OPENAI_MODEL` nevét. Ne töltsd fel a titkot GitHubra.
4. `uvicorn main:app --host 127.0.0.1 --port 8000`
5. `http://127.0.0.1:8000/docs` – csak helyi fejlesztéshez.

## Élesítés előtt kötelező
- HTTPS és megfelelő hosting; a nyilvános API-t ne tedd korlátlanul elérhetővé.
- Hitelesítés, felhasználónkénti rate limit, napi/havi költségkeret, abuse-védelem, monitorozás.
- Titokkezelő, rotálható API-kulcs; nincs kulcs az APK-ban.
- Adatvédelmi tájékoztató, adatmegőrzési szabályok, törlési folyamat, adatfeldolgozói szerződések.
- Az Android kliens integrációja HTTPS-en, időtúllépés és hibakezelés, felhasználói hozzájárulás és költségkontroll mellett.
- A modellel elérhető név változhat; csak a szolgáltatónál ténylegesen elérhető modellnevet állítsd be.

A kód jelenleg nem naplózza a kérés törzsét adatbázisba, de a hosting/proxy naplózási beállításait külön ellenőrizni kell.
