#!/usr/bin/env python3
"""Bot d'alertes de preus: CoinGecko -> Telegram.

Dos modes (els tria el workflow de GitHub):
  python bot.py fast    cada ~15 min: objectius "toc", caigudes i retrocessos (preu en directe)
  python bot.py daily   un cop al dia: recalcula maxims i bases (amb metxes),
                        objectius per tancament diari i setmanal,
                        i resum cada dilluns

Dues mesures de "descompte":
  - CAIGUDA (macro): per token. % que ha caigut el preu des del maxim
    (maxim dels ultims 12 mesos, o el "maxim" posat a ma si es mes alt).
  - RETROCES (micro): per entrada amb "inici_onada". % de la pujada
    (del minim del dia d'inici fins al maxim posterior) que s'ha desfet.

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
INTRADAY_DAYS = 90          # dades horaries (per a metxes)
REARM_POINTS = 5            # un nivell es rearma quan el % baixa 5 punts per sota del nivell
TOUCH_REARM = 0.03          # un "toc" es rearma quan el preu s'allunya un 3% del nivell
DEFAULT_LEVELS = [20, 40, 50, 60]
METODE = "v3"               # caiguda macro per token + retroces per onada

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
    if isinstance(d, str):
        d = parse_date(d)
    return d.strftime("%d/%m/%y") if d else "?"


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


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

def _levels(value, default, where):
    try:
        return sorted(float(x) for x in value) if value is not None else list(default)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: els nivells han de ser una llista de números, per exemple [20, 40, 50, 60]")


def load_config():
    """Retorna (entrades, monedes).

    entrades: una per linia de config.json (objectius i retroces d'onada).
    monedes:  una per id de CoinGecko (caiguda macro des del maxim).
    """
    with open(CONFIG_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    fallback = raw.get("descomptes_per_defecte", DEFAULT_LEVELS)
    def_caigudes = _levels(raw.get("caigudes_per_defecte", fallback), DEFAULT_LEVELS, "caigudes_per_defecte")
    def_retro = _levels(raw.get("retrocessos_per_defecte", fallback), DEFAULT_LEVELS, "retrocessos_per_defecte")

    entries, coins = {}, {}
    for ticker, tc in raw.get("tokens", {}).items():
        if ticker.startswith("_"):
            continue
        if not isinstance(tc, dict) or not tc.get("id"):
            raise ValueError(f"{ticker}: falta l'\"id\" de CoinGecko")
        name = ticker.upper()
        cid = tc["id"].strip()

        wave = tc.get("inici_onada") or None
        if wave:
            parse_date(wave)  # valida el format

        targets = []
        for side in ("compra", "venda"):
            for t in tc.get(side, []) or []:
                tipus = TIPUS.get(str(t.get("tipus", "toc")).lower())
                if tipus is None:
                    raise ValueError(f"{ticker}: tipus desconegut \"{t.get('tipus')}\" "
                                     "(ha de ser toc, diari o setmanal)")
                price = float(t["preu"])
                targets.append({"side": side, "preu": price, "tipus": tipus,
                                "key": f"{name}|{side}|{price}|{tipus}"})

        entries[name] = {
            "id": cid,
            "inici_onada": wave,
            "retrocessos": _levels(tc.get("retrocessos"), def_retro, ticker),
            "targets": targets,
        }

        # ---- dades de la moneda (caiguda macro): la primera entrada li dona nom
        coin = coins.setdefault(cid, {"nom": name.split()[0], "maxim": None,
                                      "data_maxim": None, "caigudes": None})
        manual = tc.get("maxim")
        if manual not in (None, ""):
            manual = float(manual)
            manual_d = tc.get("data_maxim") or None
            if manual_d:
                parse_date(manual_d)
            if coin["maxim"] is None or manual > coin["maxim"]:
                coin["maxim"], coin["data_maxim"] = manual, manual_d
        if "caigudes" in tc and coin["caigudes"] is None:
            coin["caigudes"] = _levels(tc["caigudes"], def_caigudes, ticker)

    for coin in coins.values():
        if coin["caigudes"] is None:
            coin["caigudes"] = list(def_caigudes)

    if not entries:
        raise ValueError("no hi ha cap token a config.json")
    return entries, coins


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- dades

def fetch_markets(coins):
    data = cg_get("/coins/markets", {"vs_currency": "usd", "ids": ",".join(sorted(coins)),
                                     "per_page": 250, "page": 1})
    return {d["id"]: d for d in data}


def check_tickers(entries, markets):
    """Avisos si un id no existeix o si el simbol no coincideix amb el ticker."""
    warnings = []
    for name, e in entries.items():
        d = markets.get(e["id"])
        if d is None:
            warnings.append(f"{name}: CoinGecko no troba l'id \"{e['id']}\"")
        elif str(d.get("symbol", "")).upper() != name.split()[0]:
            warnings.append(f"{name}: l'id \"{e['id']}\" correspon a "
                            f"{d.get('name')} ({str(d.get('symbol')).upper()})")
    return warnings


def history(cid):
    """Retorna (tancaments, maxims, minims): llistes [(data, preu)] per dia (UTC).

    Tancaments: dies ja acabats. Maxims i minims: intradia (dades horaries) els
    ultims 90 dies; abans, aproximats amb l'obertura i el tancament del dia.
    """
    data = cg_get(f"/coins/{cid}/market_chart", {"vs_currency": "usd", "days": HISTORY_DAYS})
    points = [(int(ts), p) for ts, p in data.get("prices", []) if p is not None]
    today = now_utc().date()

    closes = {}
    # Punts diaris de CoinGecko: 00:00 UTC del dia D = tancament del dia D-1
    for ts, p in points:
        day_start = ts - ts % DAY_MS
        if ts - day_start <= 5 * 60 * 1000:
            d = datetime.fromtimestamp(day_start / 1000, tz=timezone.utc).date() - timedelta(days=1)
            closes.setdefault(d, p)
    if len(closes) < 2:
        closes = {}
        for ts, p in points:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
            if d < today:
                closes[d] = p

    highs, lows = {}, {}
    prev = None
    for d, p in sorted(closes.items()):
        o = prev if prev is not None else p
        highs[d], lows[d] = max(o, p), min(o, p)
        prev = p

    try:
        hourly = cg_get(f"/coins/{cid}/market_chart", {"vs_currency": "usd", "days": INTRADAY_DAYS})
        hourly = hourly.get("prices", [])
    except Exception as e:
        print(f"AVIS {cid}: sense dades horaries, s'usen tancaments ({e})")
        hourly = []
    for ts, p in hourly:
        if p is None:
            continue
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        highs[d] = max(highs.get(d, p), p)
        lows[d] = min(lows.get(d, p), p)

    return sorted(closes.items()), sorted(highs.items()), sorted(lows.items())


# --------------------------------------------------------------------------- nivells

def update_levels(st, pct, levels):
    """Rearma i retorna els nivells nous creuats (i els marca)."""
    st["fired"] = [L for L in st.get("fired", []) if pct >= L - REARM_POINTS]
    new = [L for L in levels if pct >= L and L not in st["fired"]]
    st["fired"] = sorted(set(st["fired"]) | set(new))
    return new


def drawdown(price, peak):
    return (1 - price / peak) * 100 if peak else 0.0


def retracement(price, base, peak):
    return (peak - price) / (peak - base) * 100 if peak > base else 0.0


def macro_ok(ms, coin):
    return (ms and ms.get("metode") == METODE and ms.get("peak")
            and ms.get("nivells") == coin["caigudes"] and ms.get("maxim") == coin["maxim"])


def wave_ok(ws, e):
    return (ws and ws.get("metode") == METODE and ws.get("peak") and ws.get("base") is not None
            and ws.get("onada") == e["inici_onada"] and ws.get("nivells") == e["retrocessos"])


def macro_line(coin, ms, price, new):
    dd = drawdown(price, ms["peak"])
    manual = " · manual" if ms.get("manual_actiu") else ""
    return (f"📉 Caiguda -{max(new):g}% des del màxim\n"
            f"   Màxim: {fmt_price(ms['peak'])} ({fmt_date(ms.get('peak_date'))}{manual}) · ara -{fmt_pct(dd)}%")


def wave_line(name, ws, price, new):
    r = retracement(price, ws["base"], ws["peak"])
    return (f"🔁 Retrocés {max(new):g}% · {name}\n"
            f"   Onada: {fmt_price(ws['base'])} ({fmt_date(ws['onada'])}) → "
            f"{fmt_price(ws['peak'])} ({fmt_date(ws.get('peak_date'))}) · desfet {fmt_pct(r)}%")


# --------------------------------------------------------------------------- objectius

def target_hit(t, price):
    return price >= t["preu"] if t["side"] == "venda" else price <= t["preu"]


def target_msg(name, cid, t, price, ref_text):
    if t["side"] == "venda":
        head = f"🟠 VENDA · {name} per sobre de {fmt_price(t['preu'])}"
    else:
        head = f"🔵 COMPRA · {name} per sota de {fmt_price(t['preu'])}"
    return f"{head}\n{ref_text}: {fmt_price(price)}\n{cg_link(cid)}"


# --------------------------------------------------------------------------- mode fast

def run_fast(cfg, state):
    entries, coins = cfg
    st = state.setdefault("fast", {})
    fired = st.setdefault("toc", {})
    warned = set(st.get("avisos_enviats", []))
    msgs = []

    markets = fetch_markets(coins)
    price_of = lambda cid: (markets.get(cid) or {}).get("current_price")

    new_warnings = [w for w in check_tickers(entries, markets) if w not in warned]
    if new_warnings:
        msgs.append("⚠️ Revisa config.json:\n" + "\n".join(new_warnings))
        warned.update(new_warnings)
    st["avisos_enviats"] = sorted(warned)

    # ---- Objectius "toc"
    active = set()
    for name, e in entries.items():
        price = price_of(e["id"])
        if price is None:
            continue
        for t in e["targets"]:
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
                msgs.append(target_msg(name, e["id"], t, price, "Preu ara"))
    for key in list(fired):
        if key not in active:
            del fired[key]

    # ---- Caigudes i retrocessos (el mode daily inicialitza l'estat)
    daily = state.get("daily", {})
    mstate, wstate = daily.get("macro", {}), daily.get("ones", {})
    today = str(now_utc().date())
    lines = {}  # cid -> [linies]

    for cid, coin in coins.items():
        ms, price = mstate.get(cid), price_of(cid)
        if price is None or not macro_ok(ms, coin):
            continue
        if price > ms["peak"] * 1.000001:   # maxim nou: es rearmen tots els nivells
            ms.update(peak=price, peak_date=today, fired=[], manual_actiu=False)
        new = update_levels(ms, drawdown(price, ms["peak"]), coin["caigudes"])
        if new:
            lines.setdefault(cid, []).append(macro_line(coin, ms, price, new))

    for name, e in entries.items():
        ws, price = wstate.get(name), price_of(e["id"])
        if price is None or not wave_ok(ws, e):
            continue
        if price > ws["peak"] * 1.000001:
            ws.update(peak=price, peak_date=today, fired=[])
        new = update_levels(ws, retracement(price, ws["base"], ws["peak"]), e["retrocessos"])
        if new:
            lines.setdefault(e["id"], []).append(wave_line(name, ws, price, new))

    for cid, ls in lines.items():
        coin = coins[cid]
        msgs.append(f"⚠️ {coin['nom']} · preu ara {fmt_price(price_of(cid))}\n\n"
                    + "\n\n".join(ls) + f"\n\n{cg_link(cid)}")

    for m in msgs:
        send_telegram(m)


# --------------------------------------------------------------------------- mode daily

def run_daily(cfg, state):
    entries, coins = cfg
    st = state.setdefault("daily", {})
    first_run = "ultim_resum" not in st
    upgraded = not first_run and "macro" not in st
    st.pop("tokens", None)  # estat de versions anteriors
    mstate = st.setdefault("macro", {})
    wstate = st.setdefault("ones", {})
    cstate = st.setdefault("tancaments", {})
    today = now_utc().date()

    msgs, macro_rows, wave_rows, errors = [], [], [], []

    try:
        markets = fetch_markets(coins)
        warnings = check_tickers(entries, markets)
    except Exception as e:
        markets = {}
        warnings = [f"no s'han pogut comprovar els tickers ({e})"]

    pause = 2.5 if CG_KEY else 7
    data = {}
    for cid in coins:
        try:
            data[cid] = history(cid)
        except Exception as e:
            data[cid] = e
        time.sleep(pause)

    # ---- Caiguda macro (per moneda)
    for cid, coin in coins.items():
        got = data[cid]
        if isinstance(got, Exception) or not got[0]:
            errors.append(f"{coin['nom']}: {got if isinstance(got, Exception) else 'sense dades'}")
            continue
        closes, highs, _ = got
        live = (markets.get(cid) or {}).get("current_price") or closes[-1][1]

        peak_d, peak_p = max(highs, key=lambda x: x[1])
        manual_on = coin["maxim"] is not None and coin["maxim"] >= peak_p
        if manual_on:
            peak_p, peak_d = coin["maxim"], parse_date(coin["data_maxim"])
        if live > peak_p:
            peak_p, peak_d, manual_on = live, today, False

        ms = mstate.get(cid)
        if macro_ok(ms, coin):
            if ms["peak"] > peak_p:   # el maxim no baixa amb el temps, nomes puja
                peak_p, peak_d, manual_on = ms["peak"], parse_date(ms.get("peak_date")), ms.get("manual_actiu", False)
        else:
            changed = ms is not None and ms.get("metode") == METODE
            dd = drawdown(live, peak_p)
            ms = {"nivells": coin["caigudes"], "maxim": coin["maxim"], "metode": METODE,
                  "fired": [L for L in coin["caigudes"] if dd >= L]}   # inici silencios
            if changed:
                msgs.append(f"⚙️ {coin['nom']}: caiguda macro actualitzada.\n"
                            f"Màxim: {fmt_price(peak_p)} ({fmt_date(peak_d)}). Ara a -{fmt_pct(dd)}%.")
        ms.update(peak=peak_p, peak_date=str(peak_d) if peak_d else None, manual_actiu=manual_on)
        mstate[cid] = ms
        dd = drawdown(live, peak_p)
        note = " · manual" if manual_on else ""
        macro_rows.append((dd, f"{coin['nom']}: {fmt_price(live)} · -{dd:.0f}% "
                               f"(màx {fmt_price(peak_p)}, {fmt_date(peak_d)}{note})"))

    # ---- Retroces de les onades (per entrada) i objectius per tancament
    for name, e in entries.items():
        cid = e["id"]
        got = data.get(cid)
        if isinstance(got, Exception) or not got or not got[0]:
            continue
        closes, highs, lows = got
        live = (markets.get(cid) or {}).get("current_price") or closes[-1][1]
        last_d, last_p = closes[-1]

        wave_d = parse_date(e["inici_onada"])
        if wave_d:
            low_map = dict(lows)
            if wave_d < closes[0][0]:
                wave_rows.append((-1, f"{name}: ⚠️ l'inici de l'onada ({fmt_date(wave_d)}) és de fa més "
                                      f"d'un any i no hi ha dades. Mou la data o treu-la."))
                wstate.pop(name, None)
            elif wave_d in low_map:
                base = low_map[wave_d]
                after = [(d, p) for d, p in highs if d >= wave_d]
                peak_d, peak_p = max(after, key=lambda x: x[1])
                if live > peak_p:
                    peak_d, peak_p = today, live
                ws = wstate.get(name)
                if wave_ok(ws, e):
                    if ws["peak"] > peak_p:
                        peak_p, peak_d = ws["peak"], parse_date(ws["peak_date"])
                else:
                    changed = ws is not None and ws.get("metode") == METODE
                    r = retracement(live, base, peak_p)
                    ws = {"onada": e["inici_onada"], "nivells": e["retrocessos"], "metode": METODE,
                          "fired": [L for L in e["retrocessos"] if r >= L]}   # inici silencios
                    if changed:
                        msgs.append(f"⚙️ {name}: onada actualitzada.\n"
                                    f"{fmt_price(base)} ({fmt_date(wave_d)}) → {fmt_price(peak_p)} "
                                    f"({fmt_date(peak_d)}). Desfet {fmt_pct(r)}%.")
                ws.update(base=base, peak=peak_p, peak_date=str(peak_d))
                wstate[name] = ws
                r = retracement(live, base, peak_p)
                wave_rows.append((r, f"{name}: desfet {r:.0f}% ({fmt_price(base)} {fmt_date(wave_d)} → "
                                     f"{fmt_price(peak_p)} {fmt_date(peak_d)})"))
            else:   # data d'avui o sense dades d'aquell dia encara
                wave_rows.append((-1, f"{name}: l'onada comença {fmt_date(wave_d)}, encara sense dades."))
        else:
            wstate.pop(name, None)

        sundays = [(d, p) for d, p in closes if d.weekday() == 6]
        for t in e["targets"]:
            if t["tipus"] == "toc":
                continue
            if t["tipus"] == "diari":
                ref, label = (last_d, last_p), f"Tancament diari {fmt_date(last_d)}"
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
                msgs.append(target_msg(name, cid, t, ref[1], label))
            cstate[t["key"]] = {"fired": hit, "eval": str(ref[0])}

    # Neteja del que ja no es a config.json
    active_targets = {t["key"] for e in entries.values() for t in e["targets"] if t["tipus"] != "toc"}
    for k in list(mstate):
        if k not in coins:
            del mstate[k]
    for k in list(wstate):
        if k not in entries or not entries[k]["inici_onada"]:
            del wstate[k]
    for k in list(cstate):
        if k not in active_targets:
            del cstate[k]

    if errors and len(errors) == len(coins):
        raise RuntimeError("No s'ha pogut llegir cap token: " + "; ".join(errors[:3]))

    # Resum: primera execucio, canvi de versio i cada dilluns (serveix de "segueixo viu")
    if first_run or upgraded or (today.weekday() == 0 and st.get("ultim_resum") != str(today)):
        title = ("🟢 Bot de preus actiu" if first_run else
                 "🟢 Bot actualitzat: caiguda macro + retrocés d'onades" if upgraded else
                 "🟢 Resum setmanal")
        lines = [title, "", "📉 Caiguda des del màxim (macro):"]
        lines += [r for _, r in sorted(macro_rows, key=lambda x: -x[0])]
        if wave_rows:
            lines += ["", "🔁 Retrocés de les onades (% de la pujada desfet):"]
            lines += [r for _, r in sorted(wave_rows, key=lambda x: -x[0])]
        if warnings:
            lines += ["", "⚠️ Tickers a revisar:"] + warnings
        if errors:
            lines += ["", "⚠️ Sense dades avui:"] + errors
        msgs.insert(0, "\n".join(lines))
        st["ultim_resum"] = str(today)

    st["ultima_execucio"] = now_utc().isoformat(timespec="minutes")

    for m in msgs:
        send_telegram(m)
    for err in errors:
        print("ERROR", err)


# --------------------------------------------------------------------------- main

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "fast"
    try:
        cfg = load_config()
    except Exception as e:
        if mode == "daily":
            send_telegram(f"⚠️ config.json té un error i el bot no pot funcionar:\n{e}")
        raise

    state = load_state()
    if mode == "daily":
        run_daily(cfg, state)
    else:
        run_fast(cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()
