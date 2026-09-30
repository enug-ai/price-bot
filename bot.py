#!/usr/bin/env python3
"""Bot d'alertes de preus: CoinGecko -> Telegram.

Dos modes (els tria el workflow de GitHub):
  python bot.py fast    cada ~15 min: objectius de tipus "toc"
  python bot.py daily   un cop al dia: descomptes des del maxim de l'onada,
                        objectius per tancament diari i setmanal,
                        i resum cada dilluns

Tot el que es configura es a config.json. L'estat es guarda a state.json.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT", "").strip()
CG_KEY = os.environ.get("CG_API_KEY", "").strip()

CG_BASE = "https://api.coingecko.com/api/v3"
DAY_MS = 86_400_000
HISTORY_DAYS = 365          # maxim que dona l'API gratuita
REARM_POINTS = 5            # un descompte es rearma quan la caiguda es 5 punts menor que el nivell
TOUCH_REARM = 0.03          # un "toc" es rearma quan el preu s'allunya un 3% del nivell
DEFAULT_LEVELS = [20, 40, 50, 60]

TIPUS = {"toc": "toc", "touch": "toc",
         "diari": "diari", "daily": "diari",
         "setmanal": "setmanal", "weekly": "setmanal"}


# --------------------------------------------------------------------------- utilitats

def now_utc():
    return datetime.now(timezone.utc)


def fmt_price(p):
    """Format europeu: punt per als milers, coma per als decimals."""
    if p is None:
        return "?"
    if p >= 1000:
        s = f"{p:,.0f}"
    elif p >= 1:
        s = f"{p:,.2f}"
    elif p >= 0.01:
        s = f"{p:.4f}"
    else:
        decimals, x = 3, p
        while x < 1 and decimals < 14:
            x *= 10
            decimals += 1
        s = f"{p:.{decimals}f}"
    return s.translate(str.maketrans(",.", ".,")) + " $"


def fmt_pct(x):
    return f"{x:.1f}".replace(".", ",")


def fmt_date(d):
    return d.strftime("%d/%m/%y")


def cg_link(cid):
    return f"https://www.coingecko.com/es/monedas/{cid}"


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[sense Telegram configurat]\n" + text + "\n")
        return
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": chunk, "disable_web_page_preview": "true"},
            timeout=20,
        )
        r.raise_for_status()


def cg_get(path, params):
    headers = {"accept": "application/json", "User-Agent": "price-alert-bot"}
    if CG_KEY:
        headers["x-cg-demo-api-key"] = CG_KEY
    last_error = None
    for attempt in range(4):
        try:
            r = requests.get(CG_BASE + path, params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            last_error = str(e)
        else:
            if r.status_code == 200:
                return r.json()
            last_error = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"CoinGecko {path}: {last_error}")
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"CoinGecko {path}: {last_error}")


# --------------------------------------------------------------------------- configuracio

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    default_levels = raw.get("descomptes_per_defecte", DEFAULT_LEVELS)
    tokens = {}
    for ticker, tc in raw.get("tokens", {}).items():
        if ticker.startswith("_"):
            continue
        if not isinstance(tc, dict) or not tc.get("id"):
            raise ValueError(f"{ticker}: falta l'\"id\" de CoinGecko")

        wave = tc.get("inici_onada") or None
        if wave:
            datetime.strptime(wave, "%Y-%m-%d")  # valida el format

        targets = []
        for side in ("compra", "venda"):
            for t in tc.get(side, []) or []:
                tipus = TIPUS.get(str(t.get("tipus", "toc")).lower())
                if tipus is None:
                    raise ValueError(f"{ticker}: tipus desconegut \"{t.get('tipus')}\" "
                                     "(ha de ser toc, diari o setmanal)")
                price = float(t["preu"])
                targets.append({"side": side, "preu": price, "tipus": tipus,
                                "key": f"{ticker}|{side}|{price}|{tipus}"})

        tokens[ticker.upper()] = {
            "id": tc["id"].strip(),
            "inici_onada": wave,
            "descomptes": sorted(float(x) for x in tc.get("descomptes", default_levels)),
            "targets": targets,
        }
    if not tokens:
        raise ValueError("no hi ha cap token a config.json")
    return tokens


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- dades

def fetch_markets(tokens):
    ids = sorted({tc["id"] for tc in tokens.values()})
    data = cg_get("/coins/markets", {"vs_currency": "usd", "ids": ",".join(ids),
                                     "per_page": 250, "page": 1})
    return {d["id"]: d for d in data}


def check_tickers(tokens, markets):
    """Avisos si un id no existeix o si el simbol no coincideix amb el ticker."""
    warnings = []
    for ticker, tc in tokens.items():
        d = markets.get(tc["id"])
        if d is None:
            warnings.append(f"{ticker}: CoinGecko no troba l'id \"{tc['id']}\"")
        elif str(d.get("symbol", "")).upper() != ticker:
            warnings.append(f"{ticker}: l'id \"{tc['id']}\" correspon a "
                            f"{d.get('name')} ({str(d.get('symbol')).upper()})")
    return warnings


def daily_closes(cid):
    """Llista [(data, preu_tancament)] dels dies ja tancats (UTC), del mes antic al mes recent."""
    data = cg_get(f"/coins/{cid}/market_chart", {"vs_currency": "usd", "days": HISTORY_DAYS})
    points = [(int(ts), p) for ts, p in data.get("prices", []) if p is not None]

    closes = {}
    # Punts diaris de CoinGecko: 00:00 UTC del dia D = tancament del dia D-1
    for ts, p in points:
        day_start = ts - ts % DAY_MS
        if ts - day_start <= 5 * 60 * 1000:
            d = datetime.fromtimestamp(day_start / 1000, tz=timezone.utc).date() - timedelta(days=1)
            closes.setdefault(d, p)

    if len(closes) < 2:
        # Alternativa (dades horaries): ultim punt de cada dia ja acabat
        today = now_utc().date()
        closes = {}
        for ts, p in points:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
            if d < today:
                closes[d] = p

    return sorted(closes.items())


# --------------------------------------------------------------------------- missatges

def target_hit(t, price):
    return price >= t["preu"] if t["side"] == "venda" else price <= t["preu"]


def target_msg(ticker, cid, t, price, ref_text):
    if t["side"] == "venda":
        head = f"🟠 VENDA · {ticker} per sobre de {fmt_price(t['preu'])}"
    else:
        head = f"🔵 COMPRA · {ticker} per sota de {fmt_price(t['preu'])}"
    return f"{head}\n{ref_text}: {fmt_price(price)}\n{cg_link(cid)}"


# --------------------------------------------------------------------------- mode fast

def run_fast(tokens, state):
    st = state.setdefault("fast", {})
    fired = st.setdefault("toc", {})
    warned = set(st.get("avisos_enviats", []))
    msgs = []

    markets = fetch_markets(tokens)

    new_warnings = [w for w in check_tickers(tokens, markets) if w not in warned]
    if new_warnings:
        msgs.append("⚠️ Revisa config.json:\n" + "\n".join(new_warnings))
        warned.update(new_warnings)
    st["avisos_enviats"] = sorted(warned)

    active = set()
    for ticker, tc in tokens.items():
        d = markets.get(tc["id"])
        price = d.get("current_price") if d else None
        if price is None:
            continue
        for t in tc["targets"]:
            if t["tipus"] != "toc":
                continue
            active.add(t["key"])
            if t["key"] in fired:
                if t["side"] == "venda":
                    away = price < t["preu"] * (1 - TOUCH_REARM)
                else:
                    away = price > t["preu"] * (1 + TOUCH_REARM)
                if away:
                    del fired[t["key"]]
            elif target_hit(t, price):
                fired[t["key"]] = now_utc().isoformat(timespec="minutes")
                msgs.append(target_msg(ticker, tc["id"], t, price, "Preu ara"))

    for key in list(fired):
        if key not in active:
            del fired[key]

    for m in msgs:
        send_telegram(m)


# --------------------------------------------------------------------------- mode daily

def run_daily(tokens, state):
    st = state.setdefault("daily", {})
    first_run = "tokens" not in st
    tstate = st.setdefault("tokens", {})
    cstate = st.setdefault("tancaments", {})
    today = now_utc().date()

    msgs, rows, errors = [], [], []
    active_tokens, active_targets = set(), set()

    try:
        warnings = check_tickers(tokens, fetch_markets(tokens))
    except Exception as e:
        warnings = [f"no s'han pogut comprovar els tickers ({e})"]

    pause = 2.5 if CG_KEY else 7
    for ticker, tc in tokens.items():
        active_tokens.add(ticker)
        try:
            closes = daily_closes(tc["id"])
        except Exception as e:
            errors.append(f"{ticker}: {e}")
            continue
        finally:
            time.sleep(pause)
        if not closes:
            errors.append(f"{ticker}: sense dades")
            continue

        last_d, last_p = closes[-1]

        # ---- Descomptes des del maxim de l'onada (tancaments diaris)
        wave = tc["inici_onada"]
        wave_d = datetime.strptime(wave, "%Y-%m-%d").date() if wave else None
        window = [(d, p) for d, p in closes if wave_d is None or d >= wave_d]
        limited = wave_d is not None and closes[0][0] > wave_d
        if window:
            peak_d, peak_p = max(window, key=lambda x: x[1])
            dd = (1 - last_p / peak_p) * 100
            levels = tc["descomptes"]
            ts = tstate.get(ticker)
            config_changed = ts is not None and (ts.get("onada") != wave or ts.get("nivells") != levels)

            if ts is None or config_changed:
                # Inici silencios: marca com a ja avisats els nivells que ja estan creuats
                ts = {"onada": wave, "nivells": levels, "fired": [L for L in levels if dd >= L]}
                if config_changed:
                    msgs.append(f"⚙️ {ticker}: configuració de descomptes actualitzada.\n"
                                f"Màxim de l'onada: {fmt_price(peak_p)} ({fmt_date(peak_d)}). "
                                f"Ara a -{fmt_pct(dd)}%.")
            else:
                if peak_p > ts.get("peak", 0) * 1.000001:
                    ts["fired"] = []  # maxim nou: es rearmen tots els nivells
                ts["fired"] = [L for L in ts["fired"] if dd >= L - REARM_POINTS]
                new = [L for L in levels if dd >= L and L not in ts["fired"]]
                if new:
                    ts["fired"] = sorted(set(ts["fired"]) | set(new))
                    msgs.append(
                        f"📉 DESCOMPTE · {ticker} -{max(new):g}%\n"
                        f"Tancament {fmt_date(last_d)}: {fmt_price(last_p)}\n"
                        f"Màxim de l'onada: {fmt_price(peak_p)} ({fmt_date(peak_d)})\n"
                        f"Caiguda: -{fmt_pct(dd)}%\n{cg_link(tc['id'])}")
            ts["peak"] = peak_p
            ts["peak_date"] = str(peak_d)
            tstate[ticker] = ts
            note = " ⚠️ onada > 1 any" if limited else ""
            rows.append((dd, f"{ticker}: {fmt_price(last_p)} · -{dd:.0f}% "
                             f"(màx {fmt_price(peak_p)}, {fmt_date(peak_d)}){note}"))

        # ---- Objectius per tancament diari o setmanal
        sundays = [(d, p) for d, p in closes if d.weekday() == 6]
        for t in tc["targets"]:
            if t["tipus"] == "toc":
                continue
            active_targets.add(t["key"])
            if t["tipus"] == "diari":
                ref = (last_d, last_p)
                label = f"Tancament diari {fmt_date(last_d)}"
            else:
                if not sundays:
                    continue
                ref = sundays[-1]
                label = f"Tancament setmanal {fmt_date(ref[0])}"
            entry = cstate.get(t["key"], {"fired": False, "eval": None})
            if entry["eval"] == str(ref[0]):
                continue  # aquest tancament ja s'ha avaluat
            hit = target_hit(t, ref[1])
            if hit and not entry["fired"]:
                msgs.append(target_msg(ticker, tc["id"], t, ref[1], label))
            cstate[t["key"]] = {"fired": hit, "eval": str(ref[0])}

    # Neteja de tokens i objectius que ja no son a config.json
    for k in list(tstate):
        if k not in active_tokens:
            del tstate[k]
    for k in list(cstate):
        if k not in active_targets:
            del cstate[k]

    if errors and len(errors) == len(tokens):
        raise RuntimeError("No s'ha pogut llegir cap token: " + "; ".join(errors[:3]))

    # Resum: primera execucio i cada dilluns (serveix de "segueixo viu")
    if first_run or (today.weekday() == 0 and st.get("ultim_resum") != str(today)):
        title = "🟢 Bot de preus actiu" if first_run else "🟢 Resum setmanal"
        lines = [title, "Caiguda des del màxim de l'onada (tancament diari):", ""]
        lines += [r for _, r in sorted(rows, key=lambda x: -x[0])]
        if warnings:
            lines += ["", "⚠️ Tickers a revisar:"] + warnings
        if errors:
            lines += ["", "⚠️ Sense dades avui:"] + errors
        msgs.insert(0, "\n".join(lines))
        st["ultim_resum"] = str(today)

    st["ultima_execucio"] = now_utc().isoformat(timespec="minutes")

    for m in msgs:
        send_telegram(m)
    for e in errors:
        print("ERROR", e)


# --------------------------------------------------------------------------- main

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "fast"
    try:
        tokens = load_config()
    except Exception as e:
        if mode == "daily":
            send_telegram(f"⚠️ config.json té un error i el bot no pot funcionar:\n{e}")
        raise

    state = load_state()
    if mode == "daily":
        run_daily(tokens, state)
    else:
        run_fast(tokens, state)
    save_state(state)


if __name__ == "__main__":
    main()
