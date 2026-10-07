"""
Ranní agent: stáhne čerstvé zprávy z RSS kanálů, pošle je Claudovi
a uloží i vypíše české shrnutí.

Příprava (jednorázově):
    pip install anthropic feedparser
    nastav proměnnou prostředí ANTHROPIC_API_KEY (viz návod v chatu)

Spuštění:
    python ranni_zpravy.py
"""

import datetime
import os
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

import anthropic
import feedparser

# --- Nastavení -------------------------------------------------------------

# Zdroje zpráv podle témat. Kdyby některý odkaz přestal fungovat,
# program ho přeskočí a napíše to do výpisu.
FEEDS = {
    "Česká politika": [
        "https://www.irozhlas.cz/rss/irozhlas",
        "https://ct24.ceskatelevize.cz/rss/hlavni-zpravy",
        "https://www.seznamzpravy.cz/rss",
    ],
    "Evropa a svět": [
        "https://www.politico.eu/feed/",
        "http://feeds.bbci.co.uk/news/world/europe/rss.xml",
    ],
    "USA": [
        "https://feeds.npr.org/1014/rss.xml",
    ],
    "Ukrajina a obrana": [
        "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml",
        "http://feeds.bbci.co.uk/news/world/europe/rss.xml",
    ],
    "Finance": [
        "http://feeds.bbci.co.uk/news/business/rss.xml",
    ],
}

HODIN_ZPETNE = 36        # jak staré zprávy ještě bereme
MAX_NA_KANAL = 15        # kolik nejnovějších článků z jednoho kanálu
MODEL = "claude-sonnet-5-5"

PROMPT = """Jsi můj ranní zpravodajský asistent. Dnes je {datum}.
Níže jsou čerstvé titulky a perexy z různých zdrojů, rozdělené podle témat.

Napiš česky přehledné ranní shrnutí v těchto částech:
1. Česká politika (nejvíc prostoru, tohle mě zajímá nejvíc)
2. Evropská a americká politika (jen to nejdůležitější)
3. Finance a ekonomika
4. Ukrajina a evropský obranný průmysl
5. Co nás čeká dnes (jen události, které jsou ve zprávách zmíněny, nic si nevymýšlej)

Pravidla: buď stručný a věcný, u každé položky 1-2 věty, slučuj duplicity
z různých zdrojů, nevymýšlej fakta, která ve zprávách nejsou. Pokud k tématu
nejsou žádné relevantní zprávy, napiš to jednou větou. Za položku dej
v hranatých závorkách název zdroje.

ZPRÁVY:
{zpravy}
"""

# --- Stahování zpráv -------------------------------------------------------


def je_cerstve(entry):
    """True, pokud je článek mladší než HODIN_ZPETNE (nebo nemá datum)."""
    publikovano = entry.get("published_parsed") or entry.get("updated_parsed")
    if not publikovano:
        return True
    stari = time.time() - time.mktime(publikovano)
    return stari < HODIN_ZPETNE * 3600


def stahni_zpravy():
    bloky = []
    for tema, urls in FEEDS.items():
        radky = []
        videno = set()
        for url in urls:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f"  ! Nepodařilo se načíst: {url}")
                continue
            zdroj = feed.feed.get("title", url)
            for entry in feed.entries[:MAX_NA_KANAL]:
                titulek = entry.get("title", "").strip()
                if not titulek or titulek in videno or not je_cerstve(entry):
                    continue
                videno.add(titulek)
                perex = entry.get("summary", "").strip()[:300]
                radky.append(f"- [{zdroj}] {titulek}. {perex}")
        bloky.append(f"## {tema}\n" + ("\n".join(radky) or "(nic nového)"))
    return "\n\n".join(bloky)


# --- Shrnutí přes Claude ---------------------------------------------------


def udelej_shrnuti(zpravy):
    client = anthropic.Anthropic()  # klíč si vezme z ANTHROPIC_API_KEY
    datum = datetime.date.today().strftime("%d. %m. %Y")
    odpoved = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[
            {"role": "user", "content": PROMPT.format(datum=datum, zpravy=zpravy)}
        ],
    )
    # Odpověď může obsahovat i blok s "myšlením", proto bereme jen textové bloky
    return "".join(blok.text for blok in odpoved.content if blok.type == "text")


STRANKA = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Ranní zprávy __DATUM__</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; line-height: 1.55;
         max-width: 42rem; margin: 0 auto; padding: 1rem; }
  h1 { font-size: 1.4rem; }
  h2, h3 { margin-top: 1.6rem; }
  a { color: inherit; }
  nav { margin-top: 2.5rem; font-size: .9rem; opacity: .8; line-height: 2; }
</style>
</head>
<body>
<h1>Ranní zprávy, __DATUM__</h1>
__OBSAH__
<nav>__ODKAZY__</nav>
</body>
</html>
"""


def markdown_na_html(text):
    """Převede text od Clauda na HTML (když chybí knihovna markdown, použije prostý text)."""
    try:
        import markdown

        return markdown.markdown(text)
    except ImportError:
        import html

        return "<pre style='white-space:pre-wrap'>" + html.escape(text) + "</pre>"


def sestav_stranku(datum_text, obsah, odkazy):
    return (
        STRANKA.replace("__DATUM__", datum_text)
        .replace("__OBSAH__", obsah)
        .replace("__ODKAZY__", odkazy)
    )


def uloz_web(shrnuti):
    """Vytvoří index.html (nejnovější) a archiv/DATUM.html (pro pozdější čtení)."""
    dnes = datetime.date.today()
    datum_text = dnes.strftime("%d. %m. %Y")
    obsah = markdown_na_html(shrnuti)

    slozka = Path("archiv")
    slozka.mkdir(exist_ok=True)

    # posledních 14 dnů (včetně dneška) pro navigaci
    dny = sorted(
        {p.stem for p in slozka.glob("*.html")} | {dnes.isoformat()}, reverse=True
    )[:14]

    odkazy_index = "Starší dny: " + " · ".join(
        f'<a href="archiv/{d}.html">{d}</a>' for d in dny
    )
    odkazy_archiv = '<a href="../index.html">Nejnovější</a> · Starší dny: ' + " · ".join(
        f'<a href="{d}.html">{d}</a>' for d in dny
    )

    (slozka / f"{dnes.isoformat()}.html").write_text(
        sestav_stranku(datum_text, obsah, odkazy_archiv), encoding="utf-8"
    )
    Path("index.html").write_text(
        sestav_stranku(datum_text, obsah, odkazy_index), encoding="utf-8"
    )
    print("Webová stránka uložena (index.html a složka archiv).")


def posli_email(shrnuti):
    """Pošle shrnutí e-mailem. Když není nastavená adresa, nic nedělá."""
    adresa = os.environ.get("EMAIL_ADRESA")
    heslo = os.environ.get("EMAIL_HESLO")
    server = os.environ.get("EMAIL_SMTP", "smtp.gmail.com")

    if not adresa or not heslo:
        print("E-mail není nastavený (chybí EMAIL_ADRESA nebo EMAIL_HESLO), přeskakuju.")
        return

    zprava = EmailMessage()
    zprava["Subject"] = f"Ranní zprávy {datetime.date.today().strftime('%d. %m. %Y')}"
    zprava["From"] = adresa
    zprava["To"] = adresa  # posíláme sami sobě
    zprava.set_content(shrnuti)

    with smtplib.SMTP_SSL(server, 465) as smtp:
        smtp.login(adresa, heslo)
        smtp.send_message(zprava)
    print(f"E-mail odeslán na {adresa}.")


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Chybí ANTHROPIC_API_KEY, viz návod.")

    print("Stahuju zprávy...")
    zpravy = stahni_zpravy()

    print("Nechávám Clauda udělat shrnutí...\n")
    shrnuti = udelej_shrnuti(zpravy)

    nazev = f"zpravy_{datetime.date.today().isoformat()}.md"
    with open(nazev, "w", encoding="utf-8") as f:
        f.write(shrnuti)

    print(shrnuti)
    print(f"\n(Uloženo do souboru {nazev})")

    uloz_web(shrnuti)
    posli_email(shrnuti)


if __name__ == "__main__":
    main()
