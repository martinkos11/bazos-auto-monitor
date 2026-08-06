#!/usr/bin/env python3
"""
Monitor novych inzeratov na bazos.sk (sekcia Auto-Moto).

Periodicky prehladava vyhladavanie podla preferencii z config.json,
pamata si uz videne inzeraty v SQLite databaze a pri novom inzerate
posle notifikaciu na WhatsApp a/alebo email.

Pouzitie:
    python script.py --once            # jeden prechod a koniec
    python script.py                   # nekonecna slucka podla interval_minutes
    python script.py --seed            # oznaci vsetko aktualne za videne, nenotifikuje
    python script.py --test-notify     # posle testovaciu spravu do vsetkych kanalov
    python script.py --dry-run         # vypise co by poslal, ale neposiela
"""

import argparse
import html
import json
import logging
import os
import random
import re
import smtplib
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://auto.bazos.sk"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DB_PATH = HERE / "seen.sqlite3"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

log = logging.getLogger("bazos")


# --------------------------------------------------------------------------
# Pomocne funkcie
# --------------------------------------------------------------------------

def strip_diacritics(text):
    """'Rožňava' -> 'roznava' — aby hladanie fungovalo aj bez diakritiky."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def resolve_secret(value):
    """Hodnota 'env:NAZOV' sa nacita z premennej prostredia, inak sa vrati tak ako je."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


def parse_price(text):
    """'6 890 €' -> 6890. Vrati None ak je cena 'Dohodou' a pod."""
    digits = re.sub(r"[^\d]", "", (text or "").replace("\xa0", " "))
    return int(digits) if digits else None


def parse_date(text):
    """'[6.8. 2026]' -> date(2026, 8, 6). Vrati None ak sa neda rozparsovat."""
    m = re.search(r"\[\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*\]", text or "")
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def build_url(pref, offset=0):
    """Poskladá URL vyhladavania. Bazos strankuje cez cestu: /volkswagen/20/?..."""
    category = (pref.get("category") or "").strip("/")
    path = f"/{category}" if category else ""
    if offset:
        path += f"/{offset}"
    path += "/" if path else "/"

    params = {
        "hledat": pref.get("query", ""),
        "rubriky": "auto",
        "hlokalita": pref.get("location_zip", ""),
        "humkreis": pref.get("radius_km", ""),
        "cenaod": pref.get("price_min", ""),
        "cenado": pref.get("price_max", ""),
        "order": "",
    }
    params = {k: v for k, v in params.items() if v not in ("", None)}
    return f"{BASE_URL}{path}?{urlencode(params)}"


def fetch(session, url, timeout=30):
    resp = session.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_listings(page_html):
    """Vytiahne inzeraty zo stranky s vysledkami vyhladavania."""
    soup = BeautifulSoup(page_html, "lxml")
    listings = []

    for block in soup.select("div.inzeraty"):
        link = block.select_one("h2.nadpis a")
        if not link or not link.get("href"):
            continue

        href = link["href"]
        ad_id = re.search(r"/inzerat/(\d+)/", href)
        if not ad_id:
            continue

        meta = block.select_one("span.velikost10")
        meta_text = meta.get_text(" ", strip=True) if meta else ""
        location = block.select_one("div.inzeratylok")
        price_el = block.select_one("div.inzeratycena")
        desc = block.select_one("div.popis")

        listings.append({
            "id": ad_id.group(1),
            "title": link.get_text(strip=True),
            "url": BASE_URL + href if href.startswith("/") else href,
            "price": parse_price(price_el.get_text(strip=True) if price_el else ""),
            "price_text": (price_el.get_text(strip=True) if price_el else "").replace("\xa0", " "),
            "location": location.get_text(" ", strip=True) if location else "",
            "description": desc.get_text(" ", strip=True) if desc else "",
            "date": parse_date(meta_text),
            "is_top": bool(block.select_one("span.ztop")),
        })

    return listings


def scrape(session, pref):
    """Prejde az max_pages stranok vysledkov pre danu preferenciu."""
    max_pages = int(pref.get("max_pages", 2))
    collected, seen_ids = [], set()

    for page in range(max_pages):
        url = build_url(pref, offset=page * 20)
        log.debug("GET %s", url)
        try:
            page_html = fetch(session, url)
        except requests.RequestException as exc:
            log.warning("Nepodarilo sa nacitat %s: %s", url, exc)
            break

        batch = parse_listings(page_html)
        if not batch:
            break

        fresh = [ad for ad in batch if ad["id"] not in seen_ids]
        if not fresh:
            break  # bazos zopakoval poslednu stranku -> koniec strankovania

        seen_ids.update(ad["id"] for ad in fresh)
        collected.extend(fresh)

        if len(batch) < 20:
            break
        time.sleep(random.uniform(1.0, 2.5))  # slusne spravanie voci serveru

    return collected


# --------------------------------------------------------------------------
# Filtrovanie podla preferencii
# --------------------------------------------------------------------------

def matches(ad, pref):
    """Overi ci inzerat splna preferencie. Vrati (True, None) alebo (False, dovod)."""
    title = strip_diacritics(ad["title"])
    full = strip_diacritics(f"{ad['title']} {ad['description']}")

    # "full" = nadpis + popis (siroke, nic neujde), "title" = len nadpis (presnejsie)
    include_haystack = title if pref.get("must_contain_scope", "full") == "title" else full
    for word in pref.get("must_contain", []):
        if strip_diacritics(word) not in include_haystack:
            return False, f"chyba klucove slovo '{word}'"

    # must_contain_any = OR (staci jedno slovo). Porovnava sa bez medzier, aby
    # '110kw' sadlo aj na '110 kW' — predajcovia vykon pisu oboma sposobmi.
    alternatives = pref.get("must_contain_any", [])
    if alternatives:
        compact = re.sub(r"\s+", "", include_haystack)
        if not any(re.sub(r"\s+", "", strip_diacritics(w)) in compact for w in alternatives):
            return False, f"chyba aspon jedno z {alternatives}"

    # Vylucene slova hladame len v nadpise. Inak by sme zahodili aj skutocne auta,
    # ktore maju v popise vybavy napr. "hlinikove disky" alebo "zimne pneumatiky".
    scope = pref.get("must_not_contain_scope", "title")
    exclude_haystack = full if scope == "full" else title
    for word in pref.get("must_not_contain", []):
        if strip_diacritics(word) in exclude_haystack:
            return False, f"nadpis obsahuje vylucene slovo '{word}'"

    price_max = pref.get("price_max")
    price_min = pref.get("price_min")

    if ad["price"] is None:
        # 'Dohodou' / cena v texte — preskocime len ak si to uzivatel zelal
        if not pref.get("allow_missing_price", True):
            return False, "neuvedena cena"
    else:
        if price_max not in (None, "") and ad["price"] > int(price_max):
            return False, f"cena {ad['price']} > {price_max}"
        if price_min not in (None, "") and ad["price"] < int(price_min):
            return False, f"cena {ad['price']} < {price_min}"

    if pref.get("skip_top_ads") and ad["is_top"]:
        return False, "TOP (platena) reklama"

    return True, None


# --------------------------------------------------------------------------
# Databaza videnych inzeratov
# --------------------------------------------------------------------------

class SeenStore:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                pref       TEXT NOT NULL,
                ad_id      TEXT NOT NULL,
                title      TEXT,
                price      INTEGER,
                url        TEXT,
                first_seen TEXT,
                PRIMARY KEY (pref, ad_id)
            )
        """)
        self.conn.commit()

    def is_seeded(self, pref_name):
        cur = self.conn.execute("SELECT 1 FROM seen WHERE pref = ? LIMIT 1", (pref_name,))
        return cur.fetchone() is not None

    def is_new(self, pref_name, ad_id):
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE pref = ? AND ad_id = ?", (pref_name, ad_id)
        )
        return cur.fetchone() is None

    def mark(self, pref_name, ad):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (pref, ad_id, title, price, url, first_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (pref_name, ad["id"], ad["title"], ad["price"], ad["url"],
             datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------
# Notifikacie
# --------------------------------------------------------------------------

def format_plain(ad, pref_name):
    price = f"{ad['price']} EUR" if ad["price"] is not None else (ad["price_text"] or "cena dohodou")
    date = ad["date"].strftime("%d.%m.%Y") if ad["date"] else "?"
    return (
        f"NOVY INZERAT ({pref_name})\n\n"
        f"{ad['title']}\n"
        f"Cena: {price}\n"
        f"Lokalita: {ad['location'] or '-'}\n"
        f"Pridane: {date}\n\n"
        f"{ad['url']}"
    )


def format_html(ad, pref_name):
    price = f"{ad['price']} EUR" if ad["price"] is not None else (ad["price_text"] or "cena dohodou")
    date = ad["date"].strftime("%d.%m.%Y") if ad["date"] else "?"
    esc = html.escape
    return f"""\
<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;color:#222">
  <p style="color:#777;margin:0 0 4px">Nov&yacute; inzer&aacute;t &mdash; {esc(pref_name)}</p>
  <h2 style="margin:0 0 12px"><a href="{esc(ad['url'])}">{esc(ad['title'])}</a></h2>
  <table cellpadding="4" style="border-collapse:collapse">
    <tr><td><b>Cena</b></td><td>{esc(price)}</td></tr>
    <tr><td><b>Lokalita</b></td><td>{esc(ad['location'] or '-')}</td></tr>
    <tr><td><b>Pridan&eacute;</b></td><td>{esc(date)}</td></tr>
  </table>
  <p style="color:#444">{esc(ad['description'][:600])}</p>
  <p><a href="{esc(ad['url'])}"
        style="background:#0b7;color:#fff;padding:10px 18px;text-decoration:none;border-radius:5px">
     Otvori&#357; inzer&aacute;t</a></p>
</body></html>"""


def send_email(cfg, subject, text_body, html_body=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = resolve_secret(cfg.get("from")) or resolve_secret(cfg.get("username"))
    recipients = cfg.get("to") if isinstance(cfg.get("to"), list) else [cfg.get("to")]
    recipients = [r for r in (resolve_secret(x) for x in recipients) if r]
    if not recipients:
        raise ValueError("nie je vyplneny ziadny prijemca (email.to)")
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 465))
    username = resolve_secret(cfg.get("username"))
    password = resolve_secret(cfg.get("password"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as srv:
            srv.login(username, password)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as srv:
            srv.starttls()
            srv.login(username, password)
            srv.send_message(msg)


def send_whatsapp_callmebot(cfg, text):
    """Zadarmo, bez uctu. Aktivacia: posli 'I allow callmebot to send me messages'
    na +34 644 51 95 23 cez WhatsApp, dostanes apikey."""
    phone = resolve_secret(cfg["phone"])
    apikey = resolve_secret(cfg["apikey"])

    if not phone or "X" in phone.upper():
        raise ValueError("v config.json nie je vyplnene callmebot.phone (format +421903123456)")
    if not apikey:
        raise ValueError("chyba CallMeBot apikey — nastav premennu BAZOS_CALLMEBOT_KEY a otvor nove okno")

    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={quote_plus(phone)}"
        f"&text={quote_plus(text)}"
        f"&apikey={quote_plus(apikey)}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    # CallMeBot vracia HTTP 200 aj pri chybe (zly apikey, neaktivovane cislo) —
    # skutocny vysledok je az v tele odpovede.
    body = re.sub(r"<[^>]+>", " ", resp.text)
    body = html.unescape(re.sub(r"\s+", " ", body)).strip()
    if re.search(r"error|invalid|not\s+(registered|allowed)|apikey", body, re.I):
        raise RuntimeError(f"CallMeBot odmietol spravu: {body[:300]}")


def send_whatsapp_twilio(cfg, text):
    sid = resolve_secret(cfg["account_sid"])
    token = resolve_secret(cfg["auth_token"])
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={
            "From": f"whatsapp:{resolve_secret(cfg['from_number'])}",
            "To": f"whatsapp:{resolve_secret(cfg['to_number'])}",
            "Body": text,
        },
        timeout=30,
    )
    resp.raise_for_status()


def notify(config, ad, pref_name, dry_run=False):
    """Posle inzerat do vsetkych zapnutych kanalov. Zlyhanie jedneho nezastavi ostatne."""
    text = format_plain(ad, pref_name)
    subject = f"Bazos: {ad['title']} — {ad['price']} EUR" if ad["price"] else f"Bazos: {ad['title']}"
    channels = config.get("notifications", {})
    delivered = []

    if dry_run:
        log.info("[DRY-RUN] Poslal by som:\n%s\n", text)
        return ["dry-run"]

    email_cfg = channels.get("email", {})
    if email_cfg.get("enabled"):
        try:
            send_email(email_cfg, subject, text, format_html(ad, pref_name))
            delivered.append("email")
        except Exception as exc:
            log.error("Email zlyhal: %s", exc)

    wa_cfg = channels.get("whatsapp", {})
    if wa_cfg.get("enabled"):
        provider = wa_cfg.get("provider", "callmebot")
        try:
            if provider == "callmebot":
                send_whatsapp_callmebot(wa_cfg["callmebot"], text)
            elif provider == "twilio":
                send_whatsapp_twilio(wa_cfg["twilio"], text)
            else:
                raise ValueError(f"neznamy WhatsApp provider: {provider}")
            delivered.append(f"whatsapp/{provider}")
        except Exception as exc:
            log.error("WhatsApp zlyhal: %s", exc)

    if not delivered:
        log.warning("Ziadny notifikacny kanal nie je zapnuty alebo vsetky zlyhali!")
    return delivered


# --------------------------------------------------------------------------
# Hlavna logika
# --------------------------------------------------------------------------

def run_once(config, store, session, seed=False, dry_run=False):
    total_new = 0

    for pref in config.get("preferences", []):
        if not pref.get("enabled", True):
            continue

        name = pref.get("name", pref.get("query", "bez nazvu"))
        ads = scrape(session, pref)
        log.info("[%s] nacitanych %d inzeratov", name, len(ads))

        # Prvy beh: len si zapamataj aktualny stav, nezaplav uzivatela stovkami sprav.
        seeding = seed or not store.is_seeded(name)
        if seeding:
            for ad in ads:
                store.mark(name, ad)
            log.info("[%s] prvy beh — %d inzeratov oznacenych za videne (bez notifikacii)",
                     name, len(ads))
            continue

        for ad in ads:
            if not store.is_new(name, ad["id"]):
                continue

            ok, reason = matches(ad, pref)
            if not ok:
                log.debug("[%s] preskocene %s — %s", name, ad["title"][:40], reason)
                store.mark(name, ad)  # nevyhovuje, ale uz ho nechceme znova riesit
                continue

            price = f"{ad['price']} EUR" if ad["price"] is not None else ad["price_text"]
            log.info("[%s] NOVE: %s | %s | %s", name, ad["title"], price, ad["url"])
            channels = notify(config, ad, name, dry_run=dry_run)
            if channels:
                log.info("        odoslane cez: %s", ", ".join(channels))

            if not dry_run:
                store.mark(name, ad)
            total_new += 1

    return total_new


def test_notify(config):
    fake = {
        "id": "0",
        "title": "TEST — VW Passat 2.0 TDI",
        "url": "https://auto.bazos.sk/",
        "price": 6500,
        "price_text": "6 500 €",
        "location": "Bratislava",
        "description": "Toto je testovacia sprava z monitoringu bazos.sk.",
        "date": datetime.now().date(),
        "is_top": False,
    }
    channels = notify(config, fake, "TEST")
    if channels:
        print("Testovacia sprava odoslana cez:", ", ".join(channels))
    else:
        print("Nepodarilo sa odoslat ziadnu testovaciu spravu — skontroluj config.json.")
    return 0 if channels else 1


def load_config(path=CONFIG_PATH):
    if not path.exists():
        sys.exit(f"Chyba config subor: {path}\nSkopiruj config.example.json na config.json a vypln ho.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    parser = argparse.ArgumentParser(description="Monitor novych inzeratov na bazos.sk Auto-Moto")
    parser.add_argument("--once", action="store_true", help="jeden prechod a koniec")
    parser.add_argument("--seed", action="store_true",
                        help="oznaci vsetky aktualne inzeraty za videne, neposiela notifikacie")
    parser.add_argument("--dry-run", action="store_true", help="vypise, ale neposiela notifikacie")
    parser.add_argument("--test-notify", action="store_true", help="posle testovaciu spravu")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Na pozadi (pythonw) neexistuje konzola — bez suboroveho logu by nebolo
    # ako zistit, ci monitor bezi a co robi. V logu je aj datum, nielen cas.
    file_handler = logging.FileHandler(HERE / "monitor.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(file_handler)

    config = load_config(args.config)

    if args.test_notify:
        return test_notify(config)

    store = SeenStore()
    session = requests.Session()
    interval = int(config.get("interval_minutes", 15))

    try:
        if args.once or args.seed:
            run_once(config, store, session, seed=args.seed, dry_run=args.dry_run)
            return 0

        log.info("Monitoring spusteny — kontrola kazdych %d minut. Ukoncenie: Ctrl+C", interval)
        while True:
            try:
                found = run_once(config, store, session, dry_run=args.dry_run)
                log.info("Prechod hotovy, novych inzeratov: %d", found)
            except Exception as exc:
                log.exception("Chyba pocas prechodu: %s", exc)

            # +-20 % rozptyl, aby to nevyzeralo ako bot presne na sekundu
            sleep_s = interval * 60 * random.uniform(0.8, 1.2)
            log.info("Dalsia kontrola o %.1f minut", sleep_s / 60)
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        log.info("Ukoncene pouzivatelom.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
