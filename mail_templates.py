"""Customer e-mails (HU/DE/EN): plain text + simple HTML. Only facts that are true for the shipped apps."""
import html as _h
import os

LANGS = ("hu", "de", "en")
NAMES = {"EBD-WIN": {"hu": "E-Bike Doctor PRO – Windows", "de": "E-Bike Doctor PRO – Windows", "en": "E-Bike Doctor PRO – Windows"},
         "EBD-AND": {"hu": "E-Bike Doctor PRO – Android (webshop)", "de": "E-Bike Doctor PRO – Android (Webshop)",
                     "en": "E-Bike Doctor PRO – Android (web shop)"}}
PRICE = {"EBD-WIN": "19,99 EUR", "EBD-AND": "5 990 Ft"}


def lang_of(v):
    v = (v or "").strip().lower()[:2]
    return v if v in LANGS else "en"


def support():
    return os.getenv("SUPPORT_EMAIL", "").strip()


T = {
    "hu": {
        "subj_purchase": "{name} – licenckulcs és letöltés",
        "subj_resend": "E-Bike Doctor PRO – licenceid és letöltési linkjeid",
        "subj_confirm": "Erősítsd meg a feliratkozásod – E-Bike Doctor Smart Hardware",
        "test": "[TESZTVÁSÁRLÁS – nem valódi fizetés, a licenc tesztlicenc]",
        "hello": "Köszönjük a vásárlást{n}!",
        "key": "Licenckulcs",
        "dl": "Letöltés (a link 48 óráig érvényes)",
        "dl_btn": "Letöltés",
        "dl_again": "Lejárt link helyett újat kérhetsz a weboldal Letöltések oldalán a licenckulccsal.",
        "steps_title": "Aktiválás",
        "steps_EBD-WIN": ["Töltsd le és futtasd a telepítőt. Ha a Windows SmartScreen „ismeretlen közzétevő” figyelmeztetést mutat: További információ → Futtatás mindenképp.",
                          "Indítsd el az E-Bike Doctor PRO programot, majd: Licenc és vásárlás → Aktiválás.",
                          "Másold be a licenckulcsot. Az aktiváláshoz internet kell, utána a PRO funkciók offline is működnek."],
        "steps_EBD-AND": ["Töltsd le az APK-fájlt a telefonodra, és engedélyezd a telepítést ebből a forrásból.",
                          "Nyisd meg az E-Bike Doctor PRO alkalmazást, majd: Licenc és vásárlás → Aktiválás.",
                          "Másold be a licenckulcsot. Az aktiváláshoz internet kell."],
        "device": "Egy licenc egyszerre egy eszközön aktív. Eszközcsere az alkalmazásból indítható (30 napon belül legfeljebb 2 alkalommal).",
        "ai": "A PRO tartalmaz AI-hibaelemzést: licencenként napi 50 kérdés, a szolgáltatás teljes napi/havi költségkeretén belül.",
        "keep": "Őrizd meg ezt a levelet – a licenckulcsra újratelepítéskor is szükséged lesz.",
        "price": "Ár: {price}, egyszeri vásárlás. A számlát és a visszatérítést a Lemon Squeezy (viszonteladó) kezeli.",
        "support": "Segítség: {s}",
        "resend_intro": "Kérésedre elküldjük az ehhez az e-mail-címhez tartozó aktív licenceket.",
        "resend_nokey": "Ehhez a licenchez a kulcs nem küldhető újra automatikusan – írj az ügyfélszolgálatnak, és új kulcsot állítunk ki.",
        "resend_none": "Ehhez az e-mail-címhez nem találtunk aktív licencet. Ha más címmel vásároltál, azt add meg.",
        "ignore": "Ha nem te kérted, ezt a levelet nyugodtan figyelmen kívül hagyhatod.",
        "confirm_intro": "Köszönjük az érdeklődést az E-Bike Doctor Smart Hardware iránt! A feliratkozás véglegesítéséhez kattints:",
        "confirm_btn": "Feliratkozás megerősítése",
        "confirm_what": "Csak a Smart Hardware fejlesztéséről és indulásáról írunk – ritkán, reklám-hírlevél nélkül. Ez nem előrendelés, fizetési kötelezettség nincs.",
        "unsub": "Leiratkozás bármikor",
        "subj_refund": "{name} – visszatérítés és licencvisszavonás",
        "refund_intro": "A rendelésed ({order}) visszatérítése megtörtént.",
        "refund_licence": "A rendeléshez tartozó licenc (…{hint}) ezzel érvényét vesztette: az alkalmazás a következő online ellenőrzéskor visszavált ingyenes módra, a letöltési linkek pedig nem működnek tovább.",
        "refund_money": "A visszatérítés összegét a Lemon Squeezy (viszonteladó) utalja vissza az eredeti fizetési módra; a jóváírás a banktól függően néhány napig tarthat.",
        "refund_mistake": "Ha nem kérted a visszatérítést, válaszolj erre a levélre vagy írj az ügyfélszolgálatnak.",
    },
    "de": {
        "subj_purchase": "{name} – Lizenzschlüssel und Download",
        "subj_resend": "E-Bike Doctor PRO – Ihre Lizenzen und Download-Links",
        "subj_confirm": "Bitte bestätigen Sie Ihre Anmeldung – E-Bike Doctor Smart Hardware",
        "test": "[TESTKAUF – keine echte Zahlung, Testlizenz]",
        "hello": "Vielen Dank für Ihren Kauf{n}!",
        "key": "Lizenzschlüssel",
        "dl": "Download (Link 48 Stunden gültig)",
        "dl_btn": "Herunterladen",
        "dl_again": "Einen neuen Link erhalten Sie jederzeit auf der Download-Seite der Website mit Ihrem Lizenzschlüssel.",
        "steps_title": "Aktivierung",
        "steps_EBD-WIN": ["Installationsprogramm herunterladen und starten. Zeigt Windows SmartScreen „Unbekannter Herausgeber“: Weitere Informationen → Trotzdem ausführen.",
                          "E-Bike Doctor PRO starten, dann: Lizenz & Kauf → Aktivieren.",
                          "Lizenzschlüssel einfügen. Für die Aktivierung ist Internet nötig, danach funktionieren die PRO-Funktionen auch offline."],
        "steps_EBD-AND": ["APK-Datei auf das Smartphone laden und die Installation aus dieser Quelle erlauben.",
                          "E-Bike Doctor PRO öffnen, dann: Lizenz & Kauf → Aktivieren.",
                          "Lizenzschlüssel einfügen. Für die Aktivierung ist Internet nötig."],
        "device": "Eine Lizenz ist gleichzeitig auf einem Gerät aktiv. Gerätewechsel in der App (höchstens 2-mal in 30 Tagen).",
        "ai": "PRO enthält KI-Fehleranalyse: 50 Fragen pro Tag und Lizenz, im Rahmen des täglichen/monatlichen Gesamtbudgets des Dienstes.",
        "keep": "Bitte bewahren Sie diese E-Mail auf – den Schlüssel brauchen Sie auch bei einer Neuinstallation.",
        "price": "Preis: {price}, Einmalkauf. Rechnung und Erstattung über Lemon Squeezy (Wiederverkäufer).",
        "support": "Hilfe: {s}",
        "resend_intro": "Wie angefordert senden wir Ihnen die aktiven Lizenzen zu dieser E-Mail-Adresse.",
        "resend_nokey": "Für diese Lizenz kann der Schlüssel nicht automatisch erneut gesendet werden – bitte kontaktieren Sie den Support, wir stellen einen neuen Schlüssel aus.",
        "resend_none": "Zu dieser E-Mail-Adresse wurde keine aktive Lizenz gefunden. Bitte verwenden Sie die Adresse, mit der Sie gekauft haben.",
        "ignore": "Falls Sie das nicht angefordert haben, können Sie diese E-Mail ignorieren.",
        "confirm_intro": "Danke für Ihr Interesse an E-Bike Doctor Smart Hardware! Bitte bestätigen Sie Ihre Anmeldung:",
        "confirm_btn": "Anmeldung bestätigen",
        "confirm_what": "Wir schreiben nur über Entwicklung und Start der Smart Hardware – selten, ohne Werbe-Newsletter. Keine Vorbestellung, keine Zahlungspflicht.",
        "unsub": "Jederzeit abmelden",
        "subj_refund": "{name} – Erstattung und Lizenzwiderruf",
        "refund_intro": "Ihre Bestellung ({order}) wurde erstattet.",
        "refund_licence": "Die zugehörige Lizenz (…{hint}) ist damit ungültig: Die App wechselt bei der nächsten Online-Prüfung in den kostenlosen Modus, und die Download-Links funktionieren nicht mehr.",
        "refund_money": "Den Betrag erstattet Lemon Squeezy (Wiederverkäufer) auf die ursprüngliche Zahlungsart; die Gutschrift kann je nach Bank einige Tage dauern.",
        "refund_mistake": "Falls Sie keine Erstattung angefordert haben, antworten Sie bitte auf diese E-Mail oder kontaktieren Sie den Support.",
    },
    "en": {
        "subj_purchase": "{name} – licence key and download",
        "subj_resend": "E-Bike Doctor PRO – your licences and download links",
        "subj_confirm": "Please confirm your sign-up – E-Bike Doctor Smart Hardware",
        "test": "[TEST PURCHASE – no real payment, test licence]",
        "hello": "Thank you for your purchase{n}!",
        "key": "Licence key",
        "dl": "Download (link valid for 48 hours)",
        "dl_btn": "Download",
        "dl_again": "If the link has expired, get a new one on the website's Downloads page with your licence key.",
        "steps_title": "Activation",
        "steps_EBD-WIN": ["Download and run the installer. If Windows SmartScreen shows “unknown publisher”: More info → Run anyway.",
                          "Start E-Bike Doctor PRO, then: Licence & purchase → Activate.",
                          "Paste the licence key. Activation needs internet; afterwards the PRO features also work offline."],
        "steps_EBD-AND": ["Download the APK to your phone and allow installs from this source.",
                          "Open E-Bike Doctor PRO, then: Licence & purchase → Activate.",
                          "Paste the licence key. Activation needs internet."],
        "device": "One licence is active on one device at a time. Device change from the app (at most 2 times in 30 days).",
        "ai": "PRO includes AI fault analysis: 50 questions per day per licence, within the service's overall daily/monthly budget.",
        "keep": "Please keep this e-mail – you will need the key again if you reinstall.",
        "price": "Price: {price}, one-time purchase. Invoice and refunds are handled by Lemon Squeezy (reseller).",
        "support": "Help: {s}",
        "resend_intro": "As requested, here are the active licences for this e-mail address.",
        "resend_nokey": "The key of this licence cannot be re-sent automatically – please contact support and we will issue a new key.",
        "resend_none": "We found no active licence for this e-mail address. Please use the address you purchased with.",
        "ignore": "If you did not request this, you can ignore this e-mail.",
        "confirm_intro": "Thank you for your interest in E-Bike Doctor Smart Hardware! Please confirm your sign-up:",
        "confirm_btn": "Confirm sign-up",
        "confirm_what": "We only write about the development and launch of the Smart Hardware – rarely, no marketing newsletter. Not a pre-order, no payment obligation.",
        "unsub": "Unsubscribe at any time",
        "subj_refund": "{name} – refund and licence revocation",
        "refund_intro": "Your order ({order}) has been refunded.",
        "refund_licence": "The licence of this order (…{hint}) is therefore no longer valid: the app switches back to the free mode at its next online check, and the download links stop working.",
        "refund_money": "Lemon Squeezy (the reseller) returns the amount to the original payment method; depending on the bank this can take a few days.",
        "refund_mistake": "If you did not request a refund, please reply to this e-mail or contact support.",
    },
}

CSS_BTN = "display:inline-block;background:#06b6d4;color:#04131a;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:8px"


def _wrap(lang, inner):
    return (f'<!doctype html><html lang="{lang}"><body style="margin:0;background:#f3f6f9;font-family:Segoe UI,Arial,sans-serif;color:#0b1a24">'
            f'<div style="max-width:560px;margin:0 auto;padding:24px"><div style="background:#0b1622;color:#e6f6fb;padding:18px 22px;border-radius:12px 12px 0 0;'
            f'font-weight:700;font-size:18px">E-Bike Doctor <span style="color:#22d3ee">PRO</span></div>'
            f'<div style="background:#fff;padding:22px;border-radius:0 0 12px 12px;line-height:1.55;font-size:15px">{inner}</div></div></body></html>')


def _footer(t, lang):
    s = support()
    return [t["support"].format(s=s)] if s else []


def purchase(product, lang, name, key, link, test):
    lang = lang_of(lang)
    t, pname = T[lang], NAMES[product][lang]
    n = f", {name}" if name else ""
    steps = t["steps_" + product]
    text = ([t["test"], ""] if test else []) + [pname, "", t["hello"].format(n=n), "", f"{t['key']}: {key}", "",
                                               f"{t['dl']}:", link, t["dl_again"], "", t["steps_title"] + ":"]
    text += [f"{i}. {s}" for i, s in enumerate(steps, 1)] + ["", t["device"], t["ai"], "", t["keep"],
                                                              t["price"].format(price=PRICE[product])] + _footer(t, lang)
    e = _h.escape
    inner = (f'<p style="color:#b45309;font-weight:700">{e(t["test"])}</p>' if test else "") + \
        f'<h2 style="margin:0 0 6px">{e(t["hello"].format(n=n))}</h2><p style="margin:0 0 16px;color:#4b5b66">{e(pname)}</p>' \
        f'<p style="margin:0 0 4px;font-size:13px;color:#4b5b66">{e(t["key"])}</p>' \
        f'<p style="font-family:Consolas,monospace;font-size:18px;font-weight:700;background:#ecfeff;border:1px solid #a5f3fc;padding:12px;border-radius:8px;' \
        f'letter-spacing:.5px">{e(key)}</p>' \
        f'<p><a href="{e(link)}" style="{CSS_BTN}">{e(t["dl_btn"])}</a></p><p style="font-size:13px;color:#4b5b66">{e(t["dl"])}. {e(t["dl_again"])}</p>' \
        f'<h3 style="margin:18px 0 6px">{e(t["steps_title"])}</h3><ol style="padding-left:20px;margin:0">' + \
        "".join(f"<li>{e(s)}</li>" for s in steps) + "</ol>" + \
        f'<p style="font-size:13px;color:#4b5b66">{e(t["device"])}<br>{e(t["ai"])}</p><p><b>{e(t["keep"])}</b></p>' \
        f'<p style="font-size:12px;color:#6b7b86">{e(t["price"].format(price=PRICE[product]))}' + \
        "".join(f"<br>{e(x)}" for x in _footer(t, lang)) + "</p>"
    return t["subj_purchase"].format(name=pname), "\n".join(text), _wrap(lang, inner)


def resend(lang, items):
    """items: list of (product, key_or_None, link_or_None)."""
    lang = lang_of(lang)
    t, e = T[lang], _h.escape
    text, inner = [t["resend_intro"], ""], f"<p>{e(t['resend_intro'])}</p>"
    if not items:
        text += [t["resend_none"]]
        inner += f"<p>{e(t['resend_none'])}</p>"
    for product, key, link in items:
        pname = NAMES[product][lang]
        text += [pname]
        inner += f'<h3 style="margin:16px 0 6px">{e(pname)}</h3>'
        if key:
            text += [f"{t['key']}: {key}"]
            inner += f'<p style="font-family:Consolas,monospace;font-weight:700;background:#ecfeff;padding:10px;border-radius:8px">{e(key)}</p>'
        else:
            text += [t["resend_nokey"]]
            inner += f"<p>{e(t['resend_nokey'])}</p>"
        if link:
            text += [f"{t['dl']}: {link}"]
            inner += f'<p><a href="{e(link)}" style="{CSS_BTN}">{e(t["dl_btn"])}</a></p>'
        text += [""]
    text += ["", t["ignore"]] + _footer(t, lang)
    inner += f'<p style="font-size:12px;color:#6b7b86">{e(t["ignore"])}' + "".join(f"<br>{e(x)}" for x in _footer(t, lang)) + "</p>"
    return t["subj_resend"], "\n".join(text), _wrap(lang, inner)


def refund(product, lang, order_id, key_hint, test):
    """Buyer notice after a refund: the licence is revoked (the money itself is returned by Lemon Squeezy)."""
    lang = lang_of(lang)
    t, e, pname = T[lang], _h.escape, NAMES[product][lang]
    paras = [t["refund_intro"].format(order=order_id), t["refund_licence"].format(hint=key_hint or "????"), t["refund_money"], t["refund_mistake"]]
    text = ([t["test"], ""] if test else []) + [pname, ""] + [p for x in paras for p in (x, "")] + _footer(t, lang)
    inner = (f'<p style="color:#b45309;font-weight:700">{e(t["test"])}</p>' if test else "") + \
        f'<h2 style="margin:0 0 6px">{e(pname)}</h2>' + "".join(f"<p>{e(p)}</p>" for p in paras) + \
        "".join(f'<p style="font-size:12px;color:#6b7b86">{e(x)}</p>' for x in _footer(t, lang))
    return t["subj_refund"].format(name=pname), "\n".join(text).rstrip(), _wrap(lang, inner)


def confirm(lang, confirm_url, unsub_url):
    lang = lang_of(lang)
    t, e = T[lang], _h.escape
    text = "\n".join([t["confirm_intro"], confirm_url, "", t["confirm_what"], "", f"{t['unsub']}: {unsub_url}", t["ignore"]] + _footer(t, lang))
    inner = f'<p>{e(t["confirm_intro"])}</p><p><a href="{e(confirm_url)}" style="{CSS_BTN}">{e(t["confirm_btn"])}</a></p>' \
            f'<p style="font-size:13px;color:#4b5b66">{e(t["confirm_what"])}</p>' \
            f'<p style="font-size:12px;color:#6b7b86">{e(t["ignore"])}<br><a href="{e(unsub_url)}">{e(t["unsub"])}</a>' + \
            "".join(f"<br>{e(x)}" for x in _footer(t, lang)) + "</p>"
    return t["subj_confirm"], text, _wrap(lang, inner)
