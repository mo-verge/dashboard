#!/usr/bin/env python3
"""Merge the per-category TVIP captures into one channel inventory and enrich it.

    python3 capture/enrich.py data/tvip/channels IPTV_ORG_DIR data/tvip

IPTV_ORG_DIR holds channels.json / guides.json / feeds.json from
https://iptv-org.github.io/api/ (open channel database + EPG source index).

Writes:
  data/tvip/channels.json      every channel: categories, capture, health, match, classification
  data/tvip/soccer.json        channels likely to carry soccer, best first
  data/tvip/spanish.json       Spanish-language channels ranked for language learning
  data/tvip/english.json       English-language channels ranked for language learning
"""
import glob
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

# ---------------------------------------------------------------- name parsing

PREFIX = re.compile(r"^\s*([A-Z]{2,4})\s*[:|]\s*")
QUALITY = re.compile(r"\b(4K|UHD|FHD|HD|SD|HEVC|H265|RAW|HQ|LQ|60FPS|50FPS|VIP|PLUS\+?|\+1|BACKUP|\*+)\b", re.I)
PREFIX_COUNTRY = {"ES": "ES", "SP": "ES", "MX": "MX", "AR": "AR", "CO": "CO", "CL": "CL", "PE": "PE", "VE": "VE",
                  "EC": "EC", "UY": "UY", "PY": "PY", "BO": "BO", "CR": "CR", "PA": "PA", "GT": "GT", "HN": "HN",
                  "SV": "SV", "DO": "DO", "PR": "PR", "US": "US", "USA": "US", "UK": "GB", "GB": "GB", "CA": "CA",
                  "AU": "AU", "IE": "IE", "TR": "TR", "AF": "AF", "IR": "IR", "IN": "IN", "PK": "PK", "AE": "AE",
                  "SA": "SA", "QA": "QA", "EG": "EG", "MA": "MA", "LB": "LB", "FR": "FR", "IT": "IT", "DE": "DE"}
CATEGORY_COUNTRY = {"SPAIN": "ES", "CHILE": "CL", "COLOMBIA": "CO", "BOLIVIA": "BO", "PUERTO RICO": "PR",
                    "ARGENTINA": "AR", "MEXICO": "MX", "COSTA RICA": "CR", "VENEZUELA": "VE", "DOMINICAN": "DO",
                    "PANAMA": "PA", "GUATEMALA": "GT", "HONDURAS": "HN", "EL SALVADOR": "SV", "PERU": "PE",
                    "URUGUAY": "UY", "ECUADOR": "EC", "PARAGUAY": "PY", "CANADA": "CA", "USA": "US", "UK": "GB",
                    "TURKEY": "TR", "INDIA": "IN", "BAHRAIN": "BH", "ALGERIA": "DZ", "MAURITANIA": "MR",
                    "LEBANON": "LB", "EGYPT": "EG", "LIBYA": "LY", "QATAR": "QA", "SYRIA": "SY", "KUWAIT": "KW",
                    "YEMEN": "YE", "SUDAN": "SD", "IRAQ": "IQ", "PALASTINA": "PS", "OMAN": "OM", "MOROCCO": "MA",
                    "JORDAN": "JO", "TUNISIA": "TN", "SAUDI ARABIA": "SA", "UAE": "AE"}
CATEGORY_LANG = [("SPANISH", "es"), ("LATINO", "es"), ("ENGLISH", "en"), ("LOCAL", "en"), ("KIDS", "en"),
                 ("ARABIC", "ar"), ("TURKISH", "tr"), ("PUNJABI", "pa"), ("AFGHAN", "fa")]
LANG3 = {"spa": "es", "eng": "en", "ara": "ar", "tur": "tr", "pan": "pa", "fas": "fa", "pus": "ps",
         "por": "pt", "fra": "fr", "hin": "hi", "urd": "ur", "ita": "it", "deu": "de", "cat": "ca"}


def fold(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def base_name(name):
    """'ES: A3 SERIES 4K' -> 'A3 SERIES'."""
    n = PREFIX.sub("", name)
    n = re.sub(r"^24/7\s*\|\s*", "", n)
    n = QUALITY.sub("", n)
    return re.sub(r"[\s._\-|]+", " ", n).strip(" .-")


def key(s):
    """Loose matching key: folded, no quality tags, no filler words, no spaces."""
    s = fold(base_name(s))
    s = re.sub(r"\b(tv|channel|canal|television|televisión|the|hd|network)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


# ---------------------------------------------------------------- iptv-org

def load_iptv_org(d):
    chans = json.load(open(os.path.join(d, "channels.json")))
    guides = json.load(open(os.path.join(d, "guides.json")))
    feeds = json.load(open(os.path.join(d, "feeds.json")))
    by_key = defaultdict(list)
    for c in chans:
        if c.get("closed") or c.get("is_nsfw"):
            continue
        for n in [c["name"], *c.get("alt_names", [])]:
            k = key(n)
            if len(k) >= 2:
                by_key[k].append(c)
    langs = defaultdict(set)
    for f in feeds:
        for l in f.get("languages", []):
            langs[f["channel"]].add(LANG3.get(l, l))
    epg = defaultdict(list)
    for g in guides:
        epg[g["channel"]].append({"site": g["site"], "site_id": g["site_id"], "lang": g.get("lang")})
    return by_key, langs, epg


def match(name, country, by_key):
    cands = by_key.get(key(name), [])
    if not cands:
        return None
    if country:
        same = [c for c in cands if c["country"] == country]
        if same:
            return same[0]
        if len(cands) > 1:
            return None           # ambiguous name from another country: don't guess
    return cands[0] if len(cands) == 1 or not country else None


# ---------------------------------------------------------------- classification

SOCCER_STRONG = re.compile(  # soccer-specific channels / brands
    r"LALIGA|LA LIGA|\bLIGA\b|M\+ ?LIGA|DAZN|BEIN|BE IN|SSC|ALKASS|AL KASS|TUDN|\bGOL\b|GOLTV|GOL ?PERU|"
    r"FUTBOL|FÚTBOL|SOCCER|\bEPL\b|PREMIER LEAGUE|UEFA|CHAMPIONS|FIFA|WC REPLAY|WIN SPORTS|FUTV|\bCDF\b|"
    r"TYC|TIGO SPORTS|CABLEONDA SPO|AD SPORT|ABU DHABI SPORT|KORA|BOTOLA|ARRYADIA|MOROCCO REPLAY|SPFL|"
    r"\bMLS\b|TRT SPOR|S SPORT|TIVIBU|EXXEN|ESPN DEPORTES|DIRECTV SPORTS|DSPORTS", re.I)
SOCCER_MULTI = re.compile(  # multi-sport networks that carry a lot of soccer
    r"SKY SPORT|TNT SPORTS|ESPN|FOX SPORTS|FOX DEPORTES|EUROSPORT|SUPERSPORT|TELEDEPORTE|\bTDP\b|"
    r"DEPORTV|CLARO SPORTS|AFIZIONADOS|MOVISTAR DEPORTES|PARAMOUNT|PEACOCK|AMAZON|VICTORY|TSN|SPORTSNET", re.I)
OTHER_SPORT = re.compile(
    r"GOLF|TENNIS|\bNBA\b|\bNFL\b|\bNHL\b|\bMLB\b|NCAA|WNBA|\bF1\b|FORMULA|RACE|LE MANS|NASCAR|INDYCAR|"
    r"UFC|WWE|\bBOX|FIGHT|OHL|\bAHL\b|HOCKEY|CRICKET|RUGBY|BASEBALL|BASKET|NBATV|MOTOGP|CYCLING|DARTS", re.I)
SPORT = re.compile(r"SPORT|DEPORT|DEPORTE|SPOR\b|NBA|NFL|NHL|MLB|F1|FORMULA|TENNIS|GOLF|RACE|FIGHT|UFC|WWE|"
                   r"BOX|NCAA|ESPN|TSN|SPORTSNET|PPV|EVENT", re.I)
MATCH_TITLE = re.compile(r"\b(vs\.?|v\.)\b|\s-\s.*(FC|CF|United|City|Real|Atl|Club|Sporting|Deportivo)|"
                         r"f[uú]tbol|liga|copa|champions|premier league|serie a|bundesliga|ligue 1|"
                         r"mls|concacaf|conmebol|libertadores|sudamericana|mundial|eliminatorias|"
                         r"partido|jornada|matchday|kick.?off", re.I)

TYPES = [  # (type, regex on name/category/iptv categories), first match wins
    ("sports", SPORT),
    ("news", re.compile(r"NEWS|NOTICIAS|24 ?H|24 HORAS|CNN|BBC WORLD|SKY NEWS|FOX NEWS|MSNBC|CNBC|"
                        r"TELEDIARIO|AL JAZEERA|NTN|MILENIO|CRONICA|TN\b|C5N|A24|NOTICIERO|EURONEWS|DW", re.I)),
    ("kids", re.compile(r"KIDS|INFANTIL|CARTOON|DISNEY J|DISNEY JUNIOR|NICK|CLAN|BOING|BABY|JUNIOR|"
                        r"DISCOVERY KIDS|PBS KIDS|TOON|DREAMWORKS|PEPPA|PAW PATROL|BLUEY|LEARNING", re.I)),
    ("music", re.compile(r"MUSIC|MUSICA|MÚSICA|MTV|VH1|HITS|RADIO|CLUBBING|FOLK|RANCHER|BANDA", re.I)),
    ("religious", re.compile(r"RELIGIOUS|ISLAMIC|CHRISTIAN|EWTN|ENLACE|IGLESIA|CATOLIC|QURAN|GOD|TBN", re.I)),
    ("documentary", re.compile(r"DOCUMENT|DISCOVERY|NAT ?GEO|NATIONAL GEOGRAPHIC|HISTORY|HISTORIA|"
                               r"ANIMAL PLANET|A&E|ODISEA|NATURE|SCIENCE|CIENCIA|VIAJAR|TRAVEL|FOOD|COCINA|"
                               r"CHEF|HGTV|DECASA", re.I)),
    ("movies", re.compile(r"CINE|MOVIE|FILM|PELICUL|PELÍCUL|HBO|CINEMAX|STARZ|TCM|PARAMOUNT|AMC|"
                          r"GOLDEN|STUDIO|SONY CINE|DE PELICULA|BOX OFFICE|SHAHID CINEMA|NETFLIX", re.I)),
    ("series", re.compile(r"SERIE|SERIES|NOVELA|TELENOVELA|DRAMA|COMEDY|COMEDIA|SITCOM|24/7|24X7|"
                          r"ATRESERIES|A3S|FDF|NEOX|NOVA|DIVINITY|WARNER|UNIVERSAL|SONY|AXN|FX|TNT|"
                          r"TLNOVELAS|PASIONES|SHOOF DRAMA", re.I)),
    ("entertainment", re.compile(r".")),
]

# Where Spanish-language broadcasters' own streaming apps offer subtitles.
# Broadcast IPTV restreams normally carry no subtitle track; these are the
# legitimate places to get the same programmes with Spanish subtitles.
SUBTITLE_SOURCES = [
    (re.compile(r"\bLA ?1\b|\bLA ?2\b|^24 ?H|CANAL 24|TELEDEPORTE|\bTDP\b|CLAN|RTVE|TVE", re.I), {"ES"},
     {"platform": "RTVE Play", "url": "https://www.rtve.es/play/", "subtitles": "es (most programmes)"}),
    (re.compile(r"ANTENA ?3|LA ?SEXTA|NEOX|NOVA|MEGA|ATRESERIES|A3 ?SERIES|A3S|ATRES", re.I), {"ES"},
     {"platform": "atresplayer", "url": "https://www.atresplayer.com/", "subtitles": "es (many series)"}),
    (re.compile(r"TELECINCO|CUATRO|FDF|DIVINITY|ENERGY|BE ?MAD|BOING", re.I), {"ES"},
     {"platform": "Mitele", "url": "https://www.mitele.es/", "subtitles": "es (some programmes)"}),
    (re.compile(r"TV3|3CAT|SUPER3|33\b|ESPORT3", re.I), {"ES"},
     {"platform": "3Cat", "url": "https://www.3cat.cat/", "subtitles": "ca/es"}),
    (re.compile(r"CANAL ?SUR", re.I), {"ES"},
     {"platform": "Canal Sur Más", "url": "https://www.canalsurmas.es/", "subtitles": "es (some)"}),
    (re.compile(r"TELEMADRID", re.I), {"ES"},
     {"platform": "Telemadrid", "url": "https://www.telemadrid.es/", "subtitles": "es (some)"}),
    (re.compile(r"LAS ESTRELLAS|CANAL 5|TELEVISA|DISTRITO COMEDIA|TLNOVELAS|UNIVISION|UNIMAS|GALAVISION", re.I), {"MX", "US"},
     {"platform": "ViX", "url": "https://vix.com/", "subtitles": "es (many titles)"}),
    (re.compile(r"AZTECA", re.I), {"MX", "US"},
     {"platform": "TV Azteca en vivo / Azteca Uno app", "url": "https://www.tvazteca.com/", "subtitles": "unverified"}),
]


def content_type(text):
    if SOCCER_STRONG.search(text):   # TUDN, GolTV, Win Sports, ... don't say "sport" in the name
        return "sports"
    for t, rx in TYPES:
        if rx.search(text):
            return t
    return "entertainment"


def soccer_score(name, cats, epg):
    """0-5: soccer-specific brand 3, multi-sport network 2, other sport 0-1;
    +2 when the now/next panel lists something that looks like a match."""
    text = f"{name} {' '.join(cats)}"
    titles = " ".join(e.get("title", "") for e in epg or [])
    if OTHER_SPORT.search(name) and not SOCCER_STRONG.search(name):
        base = 0
    elif SOCCER_STRONG.search(text):
        base = 3
    elif SOCCER_MULTI.search(text):
        base = 2
    elif SPORT.search(text):
        base = 1
    else:
        base = 0
    if MATCH_TITLE.search(titles) and not OTHER_SPORT.search(titles):
        base += 2
    return min(base, 5)


LEARN = {"series": 5, "kids": 4, "movies": 4, "documentary": 4, "entertainment": 3, "news": 3,
         "religious": 1, "sports": 1, "music": 1}


def learning(ctype, lang, name, health):
    if lang not in ("es", "en"):
        return None
    s = LEARN.get(ctype, 2)
    notes = []
    if health and health.get("video_ok") is False:
        s = max(0, s - 2)
        notes.append("no video at capture time")
    if re.search(r"24/7|24X7", name, re.I):
        notes.append("24/7 single-show loop: repeated episodes are good for re-watching")
    if lang == "es":
        notes.append("Castilian (Spain)" if re.search(r"^ES\b|SPAIN", name) else "Latin American Spanish")
    return {"score": s, "notes": notes}


# ---------------------------------------------------------------- merge

def main(chan_dir, iptv_dir, out_dir):
    by_key, feed_langs, epg_src = load_iptv_org(iptv_dir)
    merged = {}
    for path in sorted(glob.glob(os.path.join(chan_dir, "*.json"))):
        cap = json.load(open(path))
        cat = cap["category"]
        for ch in cap["channels"]:
            n = ch["number"]
            m = merged.setdefault(n, {"number": n, "name": ch["name"], "categories": [], "captured": cap["captured"]})
            m["categories"].append(cat)
            for k in ("epg", "video_ok", "luma", "audio_ok", "audio_rms", "number_uncertain"):
                if k in ch and k not in m:
                    m[k] = ch[k]

    for m in merged.values():
        name, cats = m["name"], m["categories"]
        pre = PREFIX.match(name)
        country = PREFIX_COUNTRY.get(pre.group(1)) if pre else None
        if not country:
            for c in cats:
                for k, v in CATEGORY_COUNTRY.items():
                    if k in c:
                        country = v
        lang = None
        for c in cats:
            for k, v in CATEGORY_LANG:
                if c.startswith(k):
                    lang = lang or v
        hit = match(name, country, by_key)
        if hit:
            m["iptv_org"] = {"id": hit["id"], "name": hit["name"], "country": hit["country"],
                             "categories": hit.get("categories", []), "website": hit.get("website"),
                             "network": hit.get("network")}
            fl = sorted(feed_langs.get(hit["id"], []))
            if fl:
                m["iptv_org"]["languages"] = fl
                if lang is None or (cats and all(re.match(r"SPORTS|PPV|MLB|ESPN|TSN|BEIN|TRUE|FIFA", c) for c in cats)):
                    lang = fl[0]
            srcs = epg_src.get(hit["id"], [])
            if srcs:
                m["epg_sources"] = srcs[:8]
            country = country or hit["country"]
        if lang is None and country in ("ES", "MX", "AR", "CO", "CL", "PE", "VE", "EC", "UY", "PY", "BO", "CR",
                                        "PA", "GT", "HN", "SV", "DO", "PR"):
            lang = "es"
        if lang is None and re.match(r"(SPORTS|MLB|PPV|ESPN|TSN)", " ".join(cats)):
            lang = "en"
        m["country"], m["language"] = country, lang
        text = " ".join([name, *cats, *(m.get("iptv_org", {}).get("categories", []))])
        ctype = content_type(text)
        m["content_type"] = ctype
        m["soccer"] = soccer_score(name, cats, m.get("epg"))
        health = {k: m[k] for k in ("video_ok", "audio_ok") if k in m}
        m["language_learning"] = learning(ctype, lang, name, health)
        subs = [src for rx, countries, src in SUBTITLE_SOURCES
                if rx.search(base_name(name)) and country in countries] if lang == "es" else []
        if subs:
            m["subtitles_online"] = subs

    chans = sorted(merged.values(), key=lambda m: m["number"])
    os.makedirs(out_dir, exist_ok=True)
    meta = {"source": "TVIP S-Box v.705 portal, captured via HDMI + fake BLE remote (capture/inventory.py)",
            "enriched_with": "iptv-org database https://github.com/iptv-org/database + EPG index https://github.com/iptv-org/epg",
            "fields": {"soccer": "0-5 likelihood of soccer: soccer brand 3 / multi-sport 2 / other sport 0, +2 if now/next shows a match; soccer.json keeps >= 3",
                       "language_learning.score": "0-5 for es/en channels (series/kids highest; sports/music lowest)",
                       "video_ok/audio_ok": "stream health when tuned during capture",
                       "epg": "now/next panel as shown on the box when tuned",
                       "epg_sources": "public EPG grabber sites for this channel (iptv-org)"}}
    json.dump({**meta, "count": len(chans), "channels": chans},
              open(os.path.join(out_dir, "channels.json"), "w"), indent=1, ensure_ascii=False)

    def slim(m):
        return {k: m[k] for k in ("number", "name", "categories", "country", "language", "content_type", "soccer",
                                  "language_learning", "video_ok", "audio_ok", "epg", "subtitles_online",
                                  "epg_sources") if k in m}
    soccer = sorted((m for m in chans if m["soccer"] >= 3), key=lambda m: (-m["soccer"], m["number"]))
    json.dump({"count": len(soccer), "channels": [slim(m) for m in soccer]},
              open(os.path.join(out_dir, "soccer.json"), "w"), indent=1, ensure_ascii=False)
    for lang, fname in (("es", "spanish.json"), ("en", "english.json")):
        ls = [m for m in chans if m["language"] == lang and m.get("language_learning")]
        ls.sort(key=lambda m: (-m["language_learning"]["score"], not m.get("video_ok", True), m["number"]))
        json.dump({"count": len(ls), "channels": [slim(m) for m in ls]},
                  open(os.path.join(out_dir, fname), "w"), indent=1, ensure_ascii=False)
    matched = sum(1 for m in chans if "iptv_org" in m)
    print(f"{len(chans)} channels, {matched} matched to iptv-org, {sum(1 for m in chans if 'epg_sources' in m)} "
          f"with online EPG sources, {len(soccer)} soccer candidates")


if __name__ == "__main__":
    main(*sys.argv[1:4])
