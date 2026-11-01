#!/usr/bin/env python3
"""
signal_bot.py — трговски бот базиран на логиката од trading-signals апликацијата.

Режими:
  python signal_bot.py backtest --days 30          # тест врз историски податоци (прво ова!)
  python signal_bot.py backtest --synthetic        # тест на кодот со лажни податоци (без интернет)
  python signal_bot.py paper                       # жива пазарна слика, лажни пари
  python signal_bot.py live                        # Binance TESTNET (лажни пари, вистински налози)
  python signal_bot.py live --real                 # ВИСТИНСКИ ПАРИ (бара потврда)

Само стандардна библиотека на Python 3.9+ (без pip install).
Поставките се на врвот (CFG) или преку:  --set stop_pct=0.4 --set symbol=BTCUSDT

ВНИМАНИЕ: образовна алатка, не финансиски совет. Можеш да изгубиш пари.
"""
import os, sys, time, json, csv, hmac, hashlib, argparse, random, math
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

# ── Windows конзолата често не може да испише кирилица/симболи (грешка „device attached ... not functioning").
#    На Windows (или со BOT_ASCII=1) конзолниот излез се претвора во чист ASCII (кирилица -> латиница).
#    Датотеките (bot.log, backtest_trades.csv) остануваат во оригинален UTF-8.
import builtins
_CYR = dict(zip("абвгдѓежзѕијклљмнњопрстќуфхцчџш",
                ["a","b","v","g","d","gj","e","zh","z","dz","i","j","k","l","lj","m","n","nj","o","p","r","s","t","kj","u","f","h","c","ch","dj","sh"]))
_SYM = {"─": "-", "—": "-", "–": "-", "→": "->", "⚠": "!", "⚡": "*", "⬆": "^", "⬇": "v", "▶": ">",
        "⏪": "<<", "⏱": "", "🔴": "", "🔔": "", "🔕": "", "📅": "", "🕐": "", "·": "|", "≈": "~",
        "═": "=", "║": "|", "„": '"', "“": '"', "”": '"', "’": "'", "‘": "'", "…": "...", "≥": ">="}


def to_ascii(text):
    out = []
    for ch in text:
        low = ch.lower()
        if low in _CYR:
            t = _CYR[low]
            out.append(t.upper() if ch != low else t)
        elif ch in _SYM:
            out.append(_SYM[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


if os.name == "nt" or os.environ.get("BOT_ASCII"):
    _orig_print = builtins.print

    def print(*args, **kwargs):  # noqa: A001
        _orig_print(*[to_ascii(str(a)) for a in args], **kwargs)
else:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ───────────────────────────── ПОСТАВКИ ─────────────────────────────
CFG = dict(
    symbol="PAXGUSDT",        # злато; пробај BTCUSDT / ETHUSDT
    window=500,               # колку 1-мин свеќи гледа сигналот
    start_equity=1000.0,      # почетен капитал (backtest/paper)
    fee_pct=0.10,             # провизија по страна (%)
    slippage_pct=0.02,        # проценето лизгање (%)
    stop_pct=0.50,            # stop-loss под влезот (%)
    take_profit_pct=1.00,     # take-profit над влезот (%)
    risk_per_trade_pct=0.5,   # колку % од капиталот смееш да ризикуваш по зделка
    max_position_pct=50.0,    # макс. % од капиталот во една позиција
    max_daily_loss_pct=2.0,   # дневен лимит на загуба -> нема нови зделки до утре (UTC)
    cooldown_bars=5,          # пауза (во свеќи) по излез
    exit_on_sell_signal=True, # излези кога сигналот ќе стане ПРОДАЈ
    block_on_conflict=True,   # не влегувај ако волуменот е спротивен на сигналот
    use_zone_filter=False,    # не купувај веднаш под/во силна 4ч зона на продавање
    zone_buffer_pct=0.30,
    poll_seconds=5,           # колку често се проверува цената (paper/live)
    # --- стратегија ---
    strategy="mtf",           # "mtf" = строго правило со 6 тајмфрејма + волумен; "classic" = стариот сигнал
    mtf_must=3,               # првите N тајмфрејма (5м, 15м, 30м) МОРА да се исти
    mtf_min_agree=3,          # минимум толку од 6 (5м,15м,30м,1ч,4ч,1д) да се исти
    vol_mult=1.5,             # „зголемен волумен" = над 1.5x од просекот
    vol_mode="either",        # spike1 (1м свеќа) | spike5 (последни 5м) | either (едното од двете)
    interval="1m",            # големина на свеќа: 1m | 5m | 15m (за "classic"; "mtf" бара 1m)
    classic_step_a=5,         # classic: втор глас = сигнал на секои 5 свеќи (на 5м свеќи = 25 мин)
    classic_step_b=15,        # classic: трет глас = сигнал на секои 15 свеќи (на 5м свеќи = 75 мин)
    htf_filter="both",        # филтер од 1ч+4ч+1д: off | sell_only | both
                              #   sell_only = ако 1ч, 4ч и 1д се СИТЕ ПРОДАЈ -> нема нови купувања
                              #   both      = и обратно: ако се СИТЕ КУПИ -> се игнорираат сигналите за продажба
    htf_force_exit=False,     # ако 1ч+4ч+1д се сите ПРОДАЈ и има отворена позиција -> затвори ја
    buy_rule="fast",          # mtf купување: fast = 1м+15м+30м сите КУПИ | same = 5м+15м+30м (и најмалку 3 од 6)
    buy_cont=True,            # купување бара и ПРОДОЛЖУВАЊЕ на растот (цената продолжува нагоре)
    sell_rule="fast",         # mtf продажба: fast = 1м+15м+30м сите ПРОДАЈ | same = 5м+15м+30м (и најмалку 3 од 6)
    sell_cont=True,           # продажба бара и ПРОДОЛЖУВАЊЕ на падот (цената продолжува надолу)
    cont_bars=5,              # продолжување = цената е пониска отколку пред 5 свеќи ...
    cont_min_pct=0.05,        # ... најмалку за 0.05% и последната свеќа е надолу
    # --- стратегија "trend" (4ч свеќи, ретки зделки) ---
    trend_sma_fast=50,        # 4ч SMA 50: цена мора да е над неа
    trend_sma_slow=200,       # 4ч SMA 200: SMA50 мора да е над неа (долгорочен раст)
    trend_pull_sma=20,        # повлекување = цената го допира 4ч SMA20
    trend_atr_mult=1.5,       # stop-loss = влез - 1.5 x ATR(14) на 4ч
    trend_rr=2.0,             # take-profit = 2 x ризикот
    trend_vol_mult=1.0,       # 4ч волумен над 1.0x од просекот (0 = исклучено)
    trend_risk_pct=1.0,       # ризик по зделка (% од капиталот)
    trend_max_pos_pct=100.0,  # макс. % од капиталот во позиција (spot, без левериџ)
    trend_daily_filter=True,  # 1д цена мора да е над 1д SMA20
    trend_exit_on_break=True, # излез ако 4ч затворање падне под SMA50
    # --- стратегија "zones" (одбивање од зони на јако купување/продавање) ---
    zone_vol_mult=1.8,        # зона настанува од свеќа со волумен над 1.8x (како во апликацијата)
    zone_min_move_pct=0.25,   # и цената да продолжи најмалку 0.25% во следните 10 свеќи
    zone_use_volume=True,     # при допир на зона бара зголемен волумен
    zone_touch_vol_mult=1.5,  # „зголемен волумен" при допир = над 1.5x од просекот
    zone_entry="confirm",     # confirm = влез по потврдена свеќа | limit = лимит налог на работ на зона
    zone_approach_pct=1.5,    # limit: налогот се поставува кога цената е до 1.5% над зоната
    zone_order_ttl=6,         # limit: налогот важи 6 свеќи
    zone_stop_atr_buf=1.0,    # stop = под зоната - 1.0 x ATR
    zone_rr=2.0,              # цел = 2 x ризикот
    zone_min_risk_pct=0.4,    # прескокни зделки со премал stop (трошоците ги јадат)
    zone_risk_pct=1.0,        # ризик по зделка (% од капиталот)
    zone_max_pos_pct=100.0,
    zone_trend_filter=False,  # само ако цената е над SMA200 на истиот тајмфрејм
    zone_eval_bars=12,        # анализа: колку свеќи се чека одбивање
    zone_depart_atr=1.0,      # зоната е „споготвена" штом цената се оддалечи најмалку 1 x ATR од неа
)

DATA_URLS = ["https://data-api.binance.vision", "https://api.binance.com"]
LIVE_URL, TESTNET_URL = "https://api.binance.com", "https://testnet.binance.vision"
LOG_FILE, STATE_FILE, STOP_FILE = "bot.log", "bot_state.json", "STOP"
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900}


_ASCII_MODE = os.name == "nt" or bool(os.environ.get("BOT_ASCII"))


def _exit(msg):
    sys.exit(to_ascii(msg) if _ASCII_MODE else msg)


def log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ───────────────────── ИНДИКАТОРИ (порт од апликацијата) ─────────────────────
def avg(a):
    return sum(a) / len(a)


def sma_at(arr, period, i):
    return None if i < period - 1 else avg(arr[i - period + 1:i + 1])


def rsi_last(arr, period):
    """Wilder RSI, последна вредност (иста формула како во апликацијата)."""
    n = len(arr)
    if n <= period:
        return None
    gains = losses = 0.0
    out = None
    for i in range(1, n):
        diff = arr[i] - arr[i - 1]
        gain, loss = max(diff, 0.0), max(-diff, 0.0)
        if i <= period:
            gains += gain
            losses += loss
            if i == period:
                rs = gains / period / ((losses / period) or 1e-9)
                out = 100 - 100 / (1 + rs)
        else:
            gains = (gains * (period - 1) + gain) / period
            losses = (losses * (period - 1) + loss) / period
            rs = gains / (losses or 1e-9)
            out = 100 - 100 / (1 + rs)
    return out


def resample_closes(prices, step):
    return [prices[min(i + step, len(prices)) - 1] for i in range(0, len(prices), step)]


def quick_signal(closes):
    """Враќа 1 (купи), -1 (продај) или 0 (неутрално)."""
    n = len(closes)
    if n < 4:
        return 0
    short_n = max(2, n // 4)
    long_n = max(short_n + 1, min(n, int(n * 0.6)))
    short_avg, long_avg = avg(closes[-short_n:]), avg(closes[-long_n:])
    r = rsi_last(closes, min(14, n - 1))
    r = 50 if r is None else r
    score = (1 if short_avg > long_avg else -1) + (1 if r > 55 else (-1 if r < 45 else 0))
    return 1 if score >= 1 else (-1 if score <= -1 else 0)


def compute_signal(prices, volumes, step_a=5, step_b=15):
    """Точна логика од render(): 4 гласа (1м, 5м, 15м, волумен) -> КУПИ/ПРОДАЈ/ЧЕКАЈ."""
    n = len(prices)
    if n < 60:
        return dict(word="WAIT", total=0, vote_vol=0, conflict=False)
    last, prev = prices[-1], prices[-2]
    rsi14 = rsi_last(prices, 14)

    # SMA20/SMA50 пресек во последните 3 свеќи
    recent_buy = recent_sell = False
    for i in range(n - 3, n):
        a0, b0 = sma_at(prices, 20, i - 1), sma_at(prices, 50, i - 1)
        a1, b1 = sma_at(prices, 20, i), sma_at(prices, 50, i)
        if None in (a0, b0, a1, b1):
            continue
        if a0 <= b0 and a1 > b1:
            recent_buy = True
        if a0 >= b0 and a1 < b1:
            recent_sell = True

    chg1 = (last - prev) / prev * 100
    vol1 = volumes[-1]
    prior = volumes[-21:-1]
    vol_avg20 = avg(prior) if prior else vol1
    spike1 = vol1 > vol_avg20 * 1.5
    chg5 = (last - prices[-5]) / prices[-5] * 100

    vote1 = 0
    if recent_buy and rsi14 < 70:
        vote1 = 1
    elif recent_sell and rsi14 > 30:
        vote1 = -1
    elif rsi14 > 70:
        vote1 = -1
    elif rsi14 < 30:
        vote1 = 1
    vote5 = quick_signal(resample_closes(prices, step_a))
    vote15 = quick_signal(resample_closes(prices, step_b))

    dir1 = 1 if chg1 >= 0 else -1
    sgn5 = (chg5 > 0) - (chg5 < 0)
    vote_vol = dir1 if (spike1 and sgn5 == dir1 and chg5 != 0) else 0

    total = vote1 + vote5 + vote15 + vote_vol
    word = "BUY" if total >= 2 else ("SELL" if total <= -2 else "WAIT")
    conflict = (word == "BUY" and vote_vol == -1) or (word == "SELL" and vote_vol == 1)
    return dict(word=word, total=total, vote_vol=vote_vol, conflict=conflict)


# ───────────── НОВА СТРАТЕГИЈА: 6 тајмфрејма + зголемен волумен ─────────────
HTF_KEEP = {"1h": 168, "4h": 120, "1d": 60}   # ист број свеќи како во апликацијата
TF_NAMES = ["5м", "15м", "30м", "1ч", "4ч", "1д"]


def volume_elevated(volumes, mult, mode):
    n = len(volumes)
    prior = volumes[-21:-1]
    spike1 = bool(prior) and volumes[-1] > avg(prior) * mult
    spike5 = False
    if n >= 30:
        v5 = sum(volumes[-5:])
        wins = [sum(volumes[i:i + 5]) for i in range(n - 25, n - 5, 5)]   # 4 претходни прозорци
        spike5 = v5 > avg(wins) * mult
    return spike1 if mode == "spike1" else (spike5 if mode == "spike5" else (spike1 or spike5))


def continuation(prices, direction, n, min_pct):
    """Продолжување: цената се движела во насоката (+1 нагоре / -1 надолу) во последните n свеќи
    најмалку min_pct %, и последната свеќа продолжува во истата насока."""
    if len(prices) < n + 2:
        return False
    move = (prices[-1] / prices[-1 - n] - 1) * 100 * direction
    return move >= min_pct and (prices[-1] - prices[-2]) * direction > 0


def mtf_signals(prices, htf):
    """6 сигнали: 5м, 15м, 30м (од 1м свеќите) + 1ч, 4ч, 1д (од свои свеќи). 1=купи, -1=продај, 0=неутрално."""
    out = [quick_signal(resample_closes(prices, st)) for st in (5, 15, 30)]
    for name in ("1h", "4h", "1d"):
        c = (htf or {}).get(name)
        out.append(quick_signal(c) if c and len(c) >= 4 else 0)
    return out


def htf_bias(htf):
    """1 = 1ч, 4ч и 1д сите КУПИ; -1 = сите ПРОДАЈ; 0 = не се сите исти."""
    sig = htf.get("_sig")
    if sig is None:
        sig = [quick_signal(htf[n]) if htf.get(n) and len(htf[n]) >= 4 else 0 for n in ("1h", "4h", "1d")]
    if all(x == 1 for x in sig):
        return 1
    if all(x == -1 for x in sig):
        return -1
    return 0


def decide(prices, volumes, htf, cfg):
    """Единствена функција за одлука (ја користат backtest, paper и live)."""
    r = _decide_base(prices, volumes, htf, cfg)
    if cfg["htf_filter"] == "off" or not htf:
        return r
    bias = htf_bias(htf)
    if bias == 0:
        return r
    r["info"] = (r.get("info") or "") + (" | 1ч/4ч/1д: ПРОДАЈ" if bias < 0 else " | 1ч/4ч/1д: КУПИ")
    if bias < 0:
        if r["word"] == "BUY":
            r["word"] = "WAIT"                     # сите повисоки тајмфрејмови продаваат -> без нови купувања
        elif r["word"] == "WAIT" and cfg["htf_force_exit"]:
            r["word"] = "SELL"                     # и затвори отворена позиција
    elif cfg["htf_filter"] == "both" and r["word"] == "SELL":
        r["word"] = "WAIT"                         # сите купуваат -> игнорирај продажба
    return r


def _decide_base(prices, volumes, htf, cfg):
    if cfg["strategy"] == "classic":
        r = compute_signal(prices, volumes, cfg["classic_step_a"], cfg["classic_step_b"])
        r["info"] = ""
        return r
    if len(prices) < 60:
        return dict(word="WAIT", total=0, conflict=False, info="нема доволно податоци")
    if not volume_elevated(volumes, cfg["vol_mult"], cfg["vol_mode"]):
        return dict(word="WAIT", total=0, conflict=False, info="волуменот е нормален")
    sigs = mtf_signals(prices, htf)
    info = " ".join(f"{n}{'+' if x > 0 else '-' if x < 0 else '0'}" for n, x in zip(TF_NAMES, sigs))
    hits = []
    s1 = quick_signal(prices[-60:])                              # 1м сигнал (последни 60 свеќи)
    info = "1м" + ("+" if s1 > 0 else "-" if s1 < 0 else "0") + " " + info
    for d, word, rule, cont in ((1, "BUY", cfg["buy_rule"], cfg["buy_cont"]), (-1, "SELL", cfg["sell_rule"], cfg["sell_cont"])):
        if rule == "fast":                                      # 1м + 15м + 30м
            ok = s1 == d and sigs[1] == d and sigs[2] == d
        else:                                                   # 5м + 15м + 30м (и најмалку mtf_min_agree од 6)
            ok = all(x == d for x in sigs[:cfg["mtf_must"]]) and sum(1 for x in sigs if x == d) >= cfg["mtf_min_agree"]
        if ok and cont:
            ok = continuation(prices, d, cfg["cont_bars"], cfg["cont_min_pct"])
            info += f" | {word} продолжување: " + ("да" if ok else "не")
        if ok:
            hits.append(word)
    return dict(word=hits[0] if len(hits) == 1 else "WAIT", total=sum(sigs), conflict=False, info=info)


class HTFSeries:
    """1ч/4ч/1д затворени свеќи до моментот t (без гледање во иднината).
    Резултатот се кешира додека не се затвори нова свеќа (брзо)."""
    def __init__(self, htf):
        self.b, self.k, self._out = htf, {n: 0 for n in htf}, None

    def at(self, t_ms):
        changed = self._out is None
        for n, b in self.b.items():
            k0 = k = self.k[n]
            while k < len(b) and b[k][6] <= t_ms:
                k += 1
            if k != k0:
                changed = True
            self.k[n] = k
        if changed:
            out = {n: [x[4] for x in b[max(0, self.k[n] - HTF_KEEP[n]):self.k[n]]] for n, b in self.b.items()}
            out["_sig"] = [quick_signal(out[n]) if n in out and len(out[n]) >= 4 else 0 for n in ("1h", "4h", "1d")]
            self._out = out
        return self._out


def aggregate_bars(bars, k):
    out = []
    for i in range(0, len(bars) - k + 1, k):
        ch = bars[i:i + k]
        out.append([ch[0][0], ch[0][1], max(x[2] for x in ch), min(x[3] for x in ch), ch[-1][4],
                    sum(x[5] for x in ch), ch[-1][6]])
    return out


def aggregate(bars, minutes):
    ms, out = minutes * 60_000, []
    for i in range(0, len(bars) - minutes + 1, minutes):
        ch = bars[i:i + minutes]
        out.append([ch[0][0], ch[0][1], max(x[2] for x in ch), min(x[3] for x in ch), ch[-1][4],
                    sum(x[5] for x in ch), ch[0][0] + ms - 1])
    return out


# ───────────────────────── ЗОНИ НА ВОЛУМЕН ─────────────────────────
def detect_volume_zones(prices, volumes, times, hi, lo, cap=30):
    n = len(prices)
    period = max(5, min(20, n // 4))
    lookahead = max(3, min(10, n // 6))
    raw = []
    for i in range(period, n - lookahead):
        av = avg(volumes[i - period:i])
        if av <= 0 or volumes[i] <= av * 1.8:
            continue
        d = "buy" if prices[i] >= prices[i - 1] else "sell"
        change = (prices[i + lookahead] - prices[i]) / prices[i] * 100
        if (d == "buy" and change > 0.25) or (d == "sell" and change < -0.25):
            raw.append(dict(index=i, price=prices[i], dir=d, mult=volumes[i] / av, change=change))
    if not raw:
        return []
    clusters, cur = [], [raw[0]]
    for z in raw[1:]:
        if z["dir"] == cur[-1]["dir"] and z["index"] - cur[-1]["index"] <= 3:
            cur.append(z)
        else:
            clusters.append(cur)
            cur = [z]
    clusters.append(cur)
    zones = []
    for c in clusters:
        ps = [z["price"] for z in c]
        top = max(c, key=lambda z: z["mult"])
        zones.append(dict(low=min(ps), high=max(ps), dir=c[0]["dir"],
                          end_idx=c[-1]["index"], end_time=times[c[-1]["index"]],
                          peak_mult=top["mult"], change=top["change"], n=len(c)))
    zones.sort(key=lambda z: -z["end_idx"])
    zones = zones[:cap]
    # затворена зона = цела подоцнежна свеќа е целосно надвор од неа
    active = []
    for z in zones:
        broken = False
        for i in range(z["end_idx"] + 1, n):
            if (hi[i] < z["low"]) if z["dir"] == "buy" else (lo[i] > z["high"]):
                broken = True
                break
        if not broken:
            active.append(z)
    return active


def blocked_by_zone(price, zones, buffer_pct):
    """Блокира КУПИ ако цената е во или веднаш под активна зона на продавање."""
    for z in zones:
        if z["dir"] == "sell" and z["low"] * (1 - buffer_pct / 100) <= price <= z["high"]:
            return True
    return False


# ───────────────────────────── ПОДАТОЦИ ─────────────────────────────
def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "signal-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def klines(symbol, interval, start_ms=None, end_ms=None, limit=1000):
    q = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms: q["startTime"] = int(start_ms)
    if end_ms: q["endTime"] = int(end_ms)
    last_err = None
    for base in DATA_URLS:
        try:
            return http_get(f"{base}/api/v3/klines?{urllib.parse.urlencode(q)}")
        except Exception as e:  # пробај следен сервер
            last_err = e
    raise RuntimeError(f"Не можам да симнам податоци: {last_err}")


def fetch_history(symbol, interval, days, cache=True):
    os.makedirs("data", exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = f"data/{symbol}_{interval}_{days}d_{day}.json"
    if cache and os.path.exists(path):
        return json.load(open(path))
    ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
    end = int(time.time() * 1000)
    t = end - int(days * 86_400_000)
    rows = []
    while t < end:
        chunk = klines(symbol, interval, start_ms=t, limit=1000)
        if not chunk:
            break
        rows += chunk
        t = chunk[-1][0] + ms
        print(f"\r  {symbol} {interval}: {len(rows)} свеќи", end="", flush=True)
        time.sleep(0.15)
    print()
    rows = [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), r[6]] for r in rows if r[6] <= end]
    json.dump(rows, open(path, "w"))
    return rows


def synthetic_bars(n=20000, start=2000.0, seed=7):
    """Лажни 1м свеќи (random walk + тренд-бранови) — само за тест на кодот."""
    rnd = random.Random(seed)
    t0 = int(time.time() * 1000) - n * 60_000
    p, bars = start, []
    for i in range(n):
        drift = math.sin(i / 400) * 0.02 + math.sin(i / 90) * 0.01
        o = p
        p = max(1.0, p * (1 + (drift + rnd.gauss(0, 0.05)) / 100))
        h, l = max(o, p) * (1 + abs(rnd.gauss(0, 0.01)) / 100), min(o, p) * (1 - abs(rnd.gauss(0, 0.01)) / 100)
        v = 100 * (1 + rnd.random()) * (3 if rnd.random() < 0.05 else 1)
        bars.append([t0 + i * 60_000, o, h, l, p, v, t0 + i * 60_000 + 59_999])
    return bars


# ───────────────────────────── BACKTEST ─────────────────────────────
class ZoneCache:
    """4ч зони, пресметани само од веќе затворени 4ч свеќи (без гледање во иднина)."""
    def __init__(self, bars4h):
        self.b = bars4h
        self.k = 0
        self.zones = []

    def at(self, t_ms):
        k = self.k
        while k < len(self.b) and self.b[k][6] <= t_ms:
            k += 1
        if k != self.k:
            self.k = k
            cl = self.b[:k]
            self.zones = detect_volume_zones([b[4] for b in cl], [b[5] for b in cl],
                                             [b[6] for b in cl], [b[2] for b in cl], [b[3] for b in cl]) if len(cl) >= 12 else []
        return self.zones


def backtest(bars, cfg, htf=None, quiet=False):
    W = cfg["window"]
    fee, slip = cfg["fee_pct"] / 100, cfg["slippage_pct"] / 100
    cash = cfg["start_equity"]
    pos = None                      # dict(qty, entry, stop, tp, t, fee_in)
    pending = None
    cooldown = 0
    day, day_pnl, day_start_eq = None, 0.0, cash
    trades, peak, max_dd = [], cash, 0.0
    zc = ZoneCache(htf["4h"]) if (cfg["use_zone_filter"] and htf and htf.get("4h")) else None
    hs = HTFSeries(htf) if (htf and (cfg["strategy"] == "mtf" or cfg["htf_filter"] != "off")) else None
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]

    def close_pos(price, t, reason):
        nonlocal cash, pos, cooldown, day_pnl
        gross = pos["qty"] * price
        fee_out = gross * fee
        pnl = gross - fee_out - pos["qty"] * pos["entry"] - pos["fee_in"]
        cash += gross - fee_out
        day_pnl += pnl
        trades.append(dict(entry_time=pos["t"], exit_time=t, entry=round(pos["entry"], 4), exit=round(price, 4),
                           qty=round(pos["qty"], 6), pnl=round(pnl, 4),
                           pnl_pct=round((price / pos["entry"] - 1) * 100, 3), reason=reason,
                           fees=round(pos["fee_in"] + fee_out, 4)))
        pos = None
        cooldown = cfg["cooldown_bars"]

    for i in range(W, len(bars)):
        t, o, h, l, c = bars[i][0], bars[i][1], bars[i][2], bars[i][3], bars[i][4]
        d = datetime.fromtimestamp(t / 1000, timezone.utc).date()
        if d != day:
            day, day_pnl, day_start_eq = d, 0.0, cash + (pos["qty"] * o if pos else 0)

        # 1) изврши налог од претходната свеќа на отворање
        if pending == "buy" and pos is None:
            entry = o * (1 + slip)
            equity = cash
            size_quote = min(equity * (cfg["risk_per_trade_pct"] / 100) / (cfg["stop_pct"] / 100),
                             equity * cfg["max_position_pct"] / 100)
            size_quote = min(size_quote, cash / (1 + fee))
            if size_quote > 10:
                qty = size_quote / entry
                fee_in = size_quote * fee
                cash -= size_quote + fee_in
                pos = dict(qty=qty, entry=entry, stop=entry * (1 - cfg["stop_pct"] / 100),
                           tp=entry * (1 + cfg["take_profit_pct"] / 100), t=t, fee_in=fee_in)
        elif pending == "sell" and pos is not None:
            close_pos(o * (1 - slip), t, "signal")
        pending = None

        # 2) stop / take-profit во рамките на свеќата (stop има приоритет)
        if pos is not None:
            if l <= pos["stop"]:
                close_pos(min(o, pos["stop"]) * (1 - slip), t, "stop")
            elif h >= pos["tp"]:
                close_pos(max(o, pos["tp"]), t, "take_profit")

        # 3) mark-to-market drawdown
        eq = cash + (pos["qty"] * c if pos else 0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)

        # 4) сигнал на затворање на свеќата
        if cooldown > 0:
            cooldown -= 1
        sig = decide(closes[i - W + 1:i + 1], vols[i - W + 1:i + 1], hs.at(t) if hs else None, cfg)
        if pos is None and sig["word"] == "BUY" and cooldown == 0:
            if cfg["block_on_conflict"] and sig["conflict"]:
                continue
            floating_pnl = (pos["qty"] * c - pos["qty"] * pos["entry"]) if pos else 0.0
            if day_pnl + floating_pnl <= -day_start_eq * cfg["max_daily_loss_pct"] / 100:
                continue
            if zc and blocked_by_zone(c, zc.at(t), cfg["zone_buffer_pct"]):
                continue
            pending = "buy"
        elif pos is not None and sig["word"] == "SELL" and cfg["exit_on_sell_signal"]:
            pending = "sell"

    if pos is not None:                       # затвори на крајот
        close_pos(bars[-1][4], bars[-1][0], "end")
    return summarize(trades, cfg, bars, cash, max_dd, quiet)


def summarize(trades, cfg, bars, final_cash, max_dd, quiet):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    start = cfg["start_equity"]
    first, last = bars[cfg["window"]][4], bars[-1][4]
    days = (bars[-1][0] - bars[cfg["window"]][0]) / 86_400_000
    res = dict(trades=n, days=round(days, 1), win_rate=round(len(wins) / n * 100, 1) if n else 0,
               profit_factor=round(gp / gl, 2) if gl > 0 else (float("inf") if gp > 0 else 0),
               avg_win=round(gp / len(wins), 2) if wins else 0, avg_loss=round(-gl / len(losses), 2) if losses else 0,
               fees=round(sum(t["fees"] for t in trades), 2),
               return_pct=round((final_cash / start - 1) * 100, 2), max_drawdown_pct=round(max_dd, 2),
               buy_hold_pct=round((last / first - 1) * 100, 2), final_equity=round(final_cash, 2),
               pre_fee_pct=round((sum(t["pnl"] for t in trades) + sum(t["fees"] for t in trades)) / start * 100, 2),
               avg_hold_min=round(sum((t["exit_time"] - t["entry_time"]) / 60000 for t in trades) / n, 1) if n else 0)
    if not quiet:
        print("\n──────── РЕЗУЛТАТ ОД BACKTEST ────────")
        print(f" Период:            {res['days']} дена, {cfg['symbol']}")
        print(f" Зделки:            {n}  (добитни {res['win_rate']}%)")
        print(f" Профит фактор:     {res['profit_factor']}   (>1 = профитабилно)")
        print(f" Просечна добивка:  ${res['avg_win']}   просечна загуба: ${res['avg_loss']}")
        print(f" Провизии вкупно:   ${res['fees']}")
        print(f" Принос на ботот:   {res['return_pct']}%   (капитал ${start} → ${res['final_equity']})")
        print(f" Принос ПРЕД провизии: {res['pre_fee_pct']}%  (покажува дали сигналот има вистинска предност)")
        print(f" Просечно држење:   {res['avg_hold_min']} мин")
        print(f" Макс. пад (DD):    {res['max_drawdown_pct']}%")
        print(f" Само држење (B&H): {res['buy_hold_pct']}%")
        reasons = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        print(f" Излези по причина: {reasons}")
        if trades:
            with open("backtest_trades.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                w.writeheader()
                w.writerows(trades)
            print(" Сите зделки: backtest_trades.csv")
    return res


SWEEP_PRESETS = [
    ("тековни поставки",                      {}),
    ("БЕЗ трошоци (само предност на сигнал)", dict(fee_pct=0.0, slippage_pct=0.0)),
    ("без излез на сигнал, stop1 / tp2",      dict(exit_on_sell_signal=False, stop_pct=1.0, take_profit_pct=2.0)),
    ("без излез на сигнал, stop1.5 / tp3",    dict(exit_on_sell_signal=False, stop_pct=1.5, take_profit_pct=3.0)),
    ("посилен волумен (2.5x)",                dict(vol_mult=2.5)),
    ("4 од 6 се исти",                        dict(mtf_min_agree=4)),
    ("5 од 6 се исти",                        dict(mtf_min_agree=5)),
    ("5 од 6 + stop1/tp2, без излез на сигнал", dict(mtf_min_agree=5, exit_on_sell_signal=False, stop_pct=1.0, take_profit_pct=2.0)),
    ("купување како пред (5м+15м+30м)",         dict(buy_rule="same", buy_cont=False)),
    ("купување 1м+15м+30м БЕЗ продолжување",   dict(buy_cont=False)),
    ("продажба како купување (5м+15м+30м)",    dict(sell_rule="same")),
    ("продажба 1м+15м+30м БЕЗ продолжување",   dict(sell_cont=False)),
    ("продажба 1м+15м+30м, продолжување 0.15%", dict(cont_min_pct=0.15)),
]


CLASSIC_PRESETS = [
    ("тековни поставки",                          {}),
    ("БЕЗ трошоци (само предност на сигнал)",     dict(fee_pct=0.0, slippage_pct=0.0)),
    ("без излез на сигнал, stop1 / tp2",          dict(exit_on_sell_signal=False, stop_pct=1.0, take_profit_pct=2.0)),
    ("без излез на сигнал, stop1.5 / tp3",        dict(exit_on_sell_signal=False, stop_pct=1.5, take_profit_pct=3.0)),
    ("дозволи и при спротивен волумен",           dict(block_on_conflict=False)),
    ("пократки чекори (3 и 6 свеќи)",             dict(classic_step_a=3, classic_step_b=6)),
    ("подолги чекори (6 и 12 свеќи)",             dict(classic_step_a=6, classic_step_b=12)),
    ("подолги чекори + stop1/tp2, без излез",     dict(classic_step_a=6, classic_step_b=12, exit_on_sell_signal=False, stop_pct=1.0, take_profit_pct=2.0)),
    ("БЕЗ филтер од 1ч/4ч/1д",                    dict(htf_filter="off")),
    ("филтер + принуден излез при ПРОДАЈ 1ч/4ч/1д", dict(htf_force_exit=True)),
    ("двонасочен филтер (both)",                  dict(htf_filter="both")),
]


def sweep(bars, cfg, htf):
    """Пробува повеќе поставки и ги тестира на две половини од периодот (за да не се лаже самите себе)."""
    W = cfg["window"]
    mid = len(bars) // 2
    parts = [("1ва половина", bars[:mid]), ("2ра половина", bars[mid - W:])]
    bh1, bh2 = (bars[mid - 1][4] / bars[W][4] - 1) * 100, (bars[-1][4] / bars[mid][4] - 1) * 100
    print(f"\nСвеќи: {cfg['interval']}, стратегија: {cfg['strategy']}.  Само држење: 1ва пол. {bh1:+.1f}%   2ра пол. {bh2:+.1f}%")
    print(f"{'поставки':44s} {'зделки':>6s} {'добитни':>8s} {'PF':>5s} {'1ва пол.':>9s} {'2ра пол.':>9s} {'пред пров.':>10s}")
    for name, ov in (CLASSIC_PRESETS if cfg["strategy"] == "classic" else SWEEP_PRESETS):
        c = dict(cfg)
        c.update(ov)
        r = [backtest(b, c, htf, quiet=True) for _, b in parts]
        tr = r[0]["trades"] + r[1]["trades"]
        wr = (r[0]["win_rate"] * r[0]["trades"] + r[1]["win_rate"] * r[1]["trades"]) / tr if tr else 0
        pf = min(r[0]["profit_factor"], 99), min(r[1]["profit_factor"], 99)
        print(f"{name:44s} {tr:6d} {wr:7.1f}% {sum(pf) / 2:5.2f} {r[0]['return_pct']:+8.2f}% {r[1]['return_pct']:+8.2f}% "
              f"{(r[0]['pre_fee_pct'] + r[1]['pre_fee_pct']):+9.2f}%")
    print("\nПрочитај: поставка е вредна внимание САМО ако и двете половини се позитивни. "
          "Ако е добра само во едната, најверојатно е случајност.")



# ───────────── СТРАТЕГИЈА "trend": 4ч свеќи, влез на повлекување во растечки тренд ─────────────
def sma_arr(a, n):
    out, run = [None] * len(a), 0.0
    for i, x in enumerate(a):
        run += x
        if i >= n:
            run -= a[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def atr_arr(h, l, c, n=14):
    out = [None] * len(c)
    tr = [0.0] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    if len(c) > n:
        out[n] = avg(tr[1:n + 1])
        for i in range(n + 1, len(c)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def trend_prepare(b4, b1d, cfg):
    O, H, L, C = [[x[k] for x in b4] for k in (1, 2, 3, 4)]
    V, T = [x[5] for x in b4], [x[6] for x in b4]
    ind = dict(O=O, H=H, L=L, C=C, V=V, T=T,
               fast=sma_arr(C, cfg["trend_sma_fast"]), slow=sma_arr(C, cfg["trend_sma_slow"]),
               pull=sma_arr(C, cfg["trend_pull_sma"]), atr=atr_arr(H, L, C))
    dC = [x[4] for x in b1d]
    dS = sma_arr(dC, 20)
    ok, k = [], 0                       # 1д филтер за секоја 4ч свеќа (само затворени 1д свеќи)
    for t in T:
        while k < len(b1d) and b1d[k][6] <= t:
            k += 1
        ok.append(k > 0 and dS[k - 1] is not None and dC[k - 1] > dS[k - 1])
    ind["daily_ok"] = ok
    return ind


def trend_run(ind, cfg, lo, hi):
    O, H, L, C, V = ind["O"], ind["H"], ind["L"], ind["C"], ind["V"]
    fee, slip = cfg["fee_pct"] / 100, cfg["slippage_pct"] / 100
    start = cash = cfg["start_equity"]
    pos, pending, trades = None, None, []
    peak, max_dd, in_mkt = cash, 0.0, 0

    def close(price, reason):
        nonlocal cash, pos
        gross = pos["qty"] * price
        pnl = gross * (1 - fee) - pos["qty"] * pos["entry"] - pos["fee_in"]
        cash += gross * (1 - fee)
        risk_amt = pos["qty"] * (pos["entry"] - pos["stop"])
        trades.append(dict(pnl=pnl, r=pnl / risk_amt if risk_amt > 0 else 0.0, reason=reason))
        pos = None

    for i in range(lo, hi):
        o, h, l, c = O[i], H[i], L[i], C[i]
        if pending and pending[0] == "buy" and pos is None:
            entry = o * (1 + slip)
            stop = entry - cfg["trend_atr_mult"] * pending[1]
            if 0 < stop < entry:
                dist = (entry - stop) / entry
                quote = min(cash * cfg["trend_risk_pct"] / 100 / dist, cash * cfg["trend_max_pos_pct"] / 100, cash / (1 + fee))
                if quote > 10:
                    cash -= quote * (1 + fee)
                    pos = dict(qty=quote / entry, entry=entry, stop=stop, fee_in=quote * fee,
                               tp=entry + cfg["trend_rr"] * (entry - stop))
        elif pending and pending[0] == "sell" and pos is not None:
            close(o * (1 - slip), "trend")
        pending = None
        if pos is not None:
            in_mkt += 1
            if l <= pos["stop"]:
                close(min(o, pos["stop"]) * (1 - slip), "stop")
            elif h >= pos["tp"]:
                close(max(o, pos["tp"]), "take_profit")
        eq = cash + (pos["qty"] * c if pos else 0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
        if i >= hi - 1 or ind["slow"][i] is None or ind["atr"][i] is None:
            continue
        if pos is None:
            up = c > ind["fast"][i] and ind["fast"][i] > ind["slow"][i]
            if cfg["trend_daily_filter"]:
                up = up and ind["daily_ok"][i]
            pulled = min(L[i - 3:i + 1]) <= ind["pull"][i] * 1.002
            resume = c > ind["pull"][i] and c > O[i]
            volok = cfg["trend_vol_mult"] <= 0 or V[i] > cfg["trend_vol_mult"] * avg(V[i - 20:i])
            if up and pulled and resume and volok:
                pending = ("buy", ind["atr"][i])
        elif cfg["trend_exit_on_break"] and c < ind["fast"][i]:
            pending = ("sell",)
    if pos is not None:
        close(C[hi - 1], "end")
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return dict(trades=n, win=len(wins) / n * 100 if n else 0.0,
                pf=min(gp / gl, 99.0) if gl > 0 else (99.0 if gp > 0 else 0.0),
                avg_r=sum(t["r"] for t in trades) / n if n else 0.0,
                ret=(cash / start - 1) * 100, dd=max_dd, bh=(C[hi - 1] / O[lo] - 1) * 100,
                in_mkt=in_mkt / max(1, hi - lo) * 100)


TREND_PRESETS = [
    ("основно (ATR x1.5, RR 2)",  {}),
    ("поширок stop (ATR x2.5)",   dict(trend_atr_mult=2.5)),
    ("поголема цел (RR 3)",       dict(trend_rr=3.0)),
    ("без филтер за волумен",     dict(trend_vol_mult=0.0)),
    ("без дневен филтер",         dict(trend_daily_filter=False)),
    ("без излез на пробив SMA50", dict(trend_exit_on_break=False)),
]


def trend_test(b4, b1d, cfg):
    warm = cfg["trend_sma_slow"] + 5
    n = len(b4)
    if n < warm + 200:
        _exit("Премалку податоци за trend тест (треба повеќе денови, пробај --days 730).")
    mid = (warm + n) // 2
    days = (b4[-1][0] - b4[warm][0]) / 86_400_000
    print(f"\nSimbol {cfg['symbol']}, 4h sveki, period za test: {days:.0f} dena (prvite {cfg['trend_sma_slow']} sveki se samo za zagrevanje)")
    ind = trend_prepare(b4, b1d, cfg)
    base = trend_run(ind, cfg, warm, n)
    h1, h2 = trend_run(ind, cfg, warm, mid), trend_run(ind, cfg, mid, n)
    print(f"Samo drzenje (buy&hold): cel period {base['bh']:+.1f}%   1va polovina {h1['bh']:+.1f}%   2ra polovina {h2['bh']:+.1f}%")
    print(f"\n{'postavki':30s} {'zdelki':>6s} {'dobitni':>8s} {'PF':>5s} {'avg R':>6s} {'1va pol.':>9s} {'2ra pol.':>9s} {'cel period':>10s} {'max pad':>8s} {'vo pazar':>8s}")
    for name, ov in TREND_PRESETS:
        c = dict(cfg)
        c.update(ov)
        ind = trend_prepare(b4, b1d, c)
        a, b, f = trend_run(ind, c, warm, mid), trend_run(ind, c, mid, n), trend_run(ind, c, warm, n)
        print(f"{name:30s} {f['trades']:6d} {f['win']:7.1f}% {f['pf']:5.2f} {f['avg_r']:+6.2f} {a['ret']:+8.1f}% {b['ret']:+8.1f}% "
              f"{f['ret']:+9.1f}% {f['dd']:7.1f}% {f['in_mkt']:7.0f}%")
    print("\nKako da procitash:")
    print(" - avg R > 0 znaci deka zdelkite vo prosek zarabotuvaat povekje otkolku se riskuva (posle provizii).")
    print(" - Postavka e vredna vnimanie samo ako i dvete polovini se pozitivni.")
    print(" - Sporedi so buy&hold: ako botot ne go nadmine drzenjeto (ili nema pomal pad), ne vredi da trguva.")
    print(" - Malku zdelki (pod ~30) = premal primerok, ne zaklucuvaj od toa.")


def synthetic_4h(n=5000, start=2000.0, seed=5, ms=14_400_000):
    """Лажни 4ч свеќи со наизменични бранови (само за тест на кодот)."""
    rnd = random.Random(seed)
    t0 = int(time.time() * 1000) - n * ms
    p, bars = start, []
    for i in range(n):
        drift = math.sin(i / 120) * 0.12 + math.sin(i / 25) * 0.05
        o = p
        p = max(5.0, p * (1 + (drift + rnd.gauss(0, 0.6)) / 100))
        h, l = max(o, p) * (1 + abs(rnd.gauss(0, 0.2)) / 100), min(o, p) * (1 - abs(rnd.gauss(0, 0.2)) / 100)
        v = 1000 * (1 + rnd.random()) * (2.5 if rnd.random() < 0.1 else 1)
        bars.append([t0 + i * ms, o, h, l, p, v, t0 + (i + 1) * ms - 1])
    return bars



# ───────────── СТРАТЕГИЈА "zones": дали зоните на јако купување/продавање одбиваат ─────────────
class ZoneTracker:
    """Зони без гледање во иднината: зона е позната дури 10 свеќи по настанувањето
    (затоа што тогаш се потврдува продолжувањето), а се брише кога цела свеќа ќе ја пробие."""
    LA, PERIOD = 10, 20

    def __init__(self, ind, cfg):
        self.H, self.L, self.atr = ind["H"], ind["L"], ind["atr"]
        self.dep = cfg["zone_depart_atr"]
        C, V, vavg = ind["C"], ind["V"], ind["vavg"]
        n, la = len(C), self.LA
        mult, mm = cfg["zone_vol_mult"], cfg["zone_min_move_pct"]
        self.ev = {}
        for i in range(self.PERIOD, n - la):
            av = vavg[i]
            if av and av > 0 and V[i] > av * mult:
                d = "buy" if C[i] >= C[i - 1] else "sell"
                chg = (C[i + la] - C[i]) / C[i] * 100
                if (d == "buy" and chg > mm) or (d == "sell" and chg < -mm):
                    self.ev[i] = (d, C[i])
        self.zones, self.prev, self.steps = [], None, 0

    def _advance(self, z, k):
        """Една свеќа за една зона: 'broken' | 'touch' | None. Допир се брои дури откако цената
        прво ќе се оддалечи од зоната (armed), па ќе се врати."""
        a = self.atr[k] or 0.0
        if z["dir"] == "buy":
            if self.H[k] < z["low"]:
                return "broken"
            if not z["armed"]:
                if self.L[k] > z["high"] + self.dep * a:
                    z["armed"] = True
            elif not z["touched"] and self.L[k] <= z["high"]:
                z["touched"] = True
                return "touch"
        else:
            if self.L[k] > z["high"]:
                return "broken"
            if not z["armed"]:
                if self.H[k] < z["low"] - self.dep * a:
                    z["armed"] = True
            elif not z["touched"] and self.H[k] >= z["low"]:
                z["touched"] = True
                return "touch"
        return None

    def step(self, t):
        """Враќа (активни зони, зони допрени за прв пат на свеќата t)."""
        e = self.ev.get(t - self.LA)
        if e:
            idx, (d, p) = t - self.LA, e
            if self.prev and self.prev[1] == d and idx - self.prev[0] <= 3:
                z = self.prev[2]
                z["low"], z["high"], z["end"] = min(z["low"], p), max(z["high"], p), idx
            else:
                z = dict(dir=d, low=p, high=p, end=idx, alive=True, touched=False, armed=False)
                self.zones.append(z)
            self.prev = (idx, d, z)
            z["alive"], z["touched"], z["armed"] = True, False, False
            for k in range(z["end"] + 1, t):                 # тивко „препуштање" на свеќите до сега
                if self._advance(z, k) == "broken":
                    z["alive"] = False
                    break
        new = []
        for z in self.zones:
            if not z["alive"] or z["end"] >= t:
                continue
            r = self._advance(z, t)
            if r == "broken":
                z["alive"] = False
            elif r == "touch":
                new.append(z)
        self.steps += 1
        if self.steps % 300 == 0:
            self.zones = [z for z in self.zones if z["alive"]]
        return [z for z in self.zones if z["alive"]], new


def zones_prepare(bars):
    O, H, L, C = [[x[k] for x in bars] for k in (1, 2, 3, 4)]
    V, T = [x[5] for x in bars], [x[6] for x in bars]
    run, vavg = 0.0, [None] * len(bars)
    for i in range(len(bars)):
        if i >= 20:
            vavg[i] = sum(V[i - 20:i]) / 20
    return dict(O=O, H=H, L=L, C=C, V=V, T=T, atr=atr_arr(H, L, C), slow=sma_arr(C, 200), vavg=vavg)


def _sim_outcome(H, L, C, i, d, dist, N, rr):
    entry = C[i]
    if d == "buy":
        stop, tp = entry * (1 - dist), entry * (1 + rr * dist)
        for j in range(i + 1, i + N + 1):
            if L[j] <= stop:
                return -1
            if H[j] >= tp:
                return 1
    else:
        stop, tp = entry * (1 + dist), entry * (1 - rr * dist)
        for j in range(i + 1, i + N + 1):
            if H[j] >= stop:
                return -1
            if L[j] <= tp:
                return 1
    return 0


def zone_study(ind, cfg, warm):
    """Дали цената вистински одбива од зоните — споредено со ИСТИ зделки на случајни места."""
    H, L, C, O, V, atr, vavg = ind["H"], ind["L"], ind["C"], ind["O"], ind["V"], ind["atr"], ind["vavg"]
    n, N, rr, buf = len(C), cfg["zone_eval_bars"], cfg["zone_rr"], cfg["zone_stop_atr_buf"]
    minrisk = cfg["zone_min_risk_pct"] / 100
    tr = ZoneTracker(ind, cfg)
    samples = {(d, k): [] for d in ("buy", "sell") for k in (0, 1, 2)}
    for i in range(n):
        _, new = tr.step(i)
        if i < warm or i >= n - N - 1 or atr[i] is None or vavg[i] is None:
            continue
        for z in new:
            d, c = z["dir"], C[i]
            if d == "buy" and c > z["low"]:
                stop = z["low"] - buf * atr[i]
            elif d == "sell" and c < z["high"]:
                stop = z["high"] + buf * atr[i]
            else:
                continue
            dist = abs(c - stop) / c
            if dist < minrisk or stop <= 0:
                continue
            volok = V[i] > cfg["zone_touch_vol_mult"] * vavg[i]
            rev = (c > O[i]) if d == "buy" else (c < O[i])
            for k, cond in ((0, True), (1, volok), (2, volok and rev)):
                if cond:
                    samples[(d, k)].append((i, dist))
    rng = random.Random(1)
    names = {0: "сите допири", 1: "допир + зголемен волумен", 2: "допир + волумен + свеќа на одбивање"}
    cost = 2 * cfg["fee_pct"] + 2 * cfg["slippage_pct"]
    print(f"{'зона / случај':58s} {'примероци':>9s} {'добитни':>8s} {'E(R)':>6s} {'случајно':>9s} {'E(R)сл.':>8s} {'z':>6s} {'трошок(R)':>9s}")
    for d, dn in (("buy", "КУПУВАЊЕ (нагоре)"), ("sell", "ПРОДАВАЊЕ (надолу)")):
        for k in (0, 1, 2):
            sm = samples[(d, k)]
            if len(sm) < 5:
                print(f"{dn + ' / ' + names[k]:58s} {len(sm):9d}   (премалку примероци)")
                continue
            res = [_sim_outcome(H, L, C, i, d, dist, N, rr) for i, dist in sm]
            w, lo_ = res.count(1), res.count(-1)
            p = w / len(sm)
            ex = (rr * w - lo_) / len(sm)
            bl = []
            for i, dist in sm:
                for _ in range(6):
                    kk = rng.randint(warm, n - N - 2)
                    bl.append(_sim_outcome(H, L, C, kk, d, dist, N, rr))
            q = bl.count(1) / len(bl)
            exb = (rr * bl.count(1) - bl.count(-1)) / len(bl)
            z = (p - q) / math.sqrt(max(q * (1 - q), 1e-9) / len(sm))
            avg_d = sum(x[1] for x in sm) / len(sm) * 100
            flag = "*" if len(sm) < 30 else " "
            print(f"{dn + ' / ' + names[k]:58s} {len(sm):9d} {p * 100:7.1f}% {ex:+6.2f} {q * 100:8.1f}% {exb:+8.2f} {z:+6.1f}{flag} {cost / avg_d:8.2f}")


def zones_run(ind, cfg, lo, hi):
    O, H, L, C, V = ind["O"], ind["H"], ind["L"], ind["C"], ind["V"]
    atr, slow, vavg = ind["atr"], ind["slow"], ind["vavg"]
    fee, slip = cfg["fee_pct"] / 100, cfg["slippage_pct"] / 100
    buf, rr, minrisk = cfg["zone_stop_atr_buf"], cfg["zone_rr"], cfg["zone_min_risk_pct"] / 100
    start = cash = cfg["start_equity"]
    pos, pending, order, trades = None, None, None, []
    peak, max_dd, in_mkt = cash, 0.0, 0
    tr = ZoneTracker(ind, cfg)

    def close(price, reason):
        nonlocal cash, pos
        gross = pos["qty"] * price
        pnl = gross * (1 - fee) - pos["qty"] * pos["entry"] - pos["fee_in"]
        cash += gross * (1 - fee)
        risk_amt = pos["qty"] * (pos["entry"] - pos["stop"])
        trades.append(dict(pnl=pnl, r=pnl / risk_amt if risk_amt > 0 else 0.0, reason=reason))
        pos = None

    def open_pos(entry, stop):
        nonlocal cash, pos
        if not (0 < stop < entry):
            return False
        dist = (entry - stop) / entry
        if dist < minrisk * 0.5:
            return False
        quote = min(cash * cfg["zone_risk_pct"] / 100 / dist, cash * cfg["zone_max_pos_pct"] / 100, cash / (1 + fee))
        if quote <= 10:
            return False
        cash -= quote * (1 + fee)
        pos = dict(qty=quote / entry, entry=entry, stop=stop, fee_in=quote * fee, tp=entry + rr * (entry - stop))
        return True

    for i in range(0, hi):
        trading = i >= lo
        o, h, l, c = O[i], H[i], L[i], C[i]
        just_filled = False
        if trading:
            if pending and pos is None:
                open_pos(o * (1 + slip), pending)
            pending = None
            if order and pos is None:
                if (not order["zone"]["alive"]) or i > order["exp"]:
                    order = None
                elif l <= order["limit"]:
                    fillp = min(o, order["limit"])
                    just_filled = open_pos(fillp, order["stop"])
                    order = None
            if pos is not None:
                in_mkt += 1
                if l <= pos["stop"]:
                    close(min(o, pos["stop"]) * (1 - slip), "stop")
                elif h >= pos["tp"] and not just_filled:
                    close(max(o, pos["tp"]), "take_profit")
            eq = cash + (pos["qty"] * c if pos else 0)
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak * 100)
        _, new = tr.step(i)
        if not trading or i >= hi - 1 or atr[i] is None or vavg[i] is None or slow[i] is None:
            continue
        if pos is None and pending is None and order is None:
            vol_ok = (not cfg["zone_use_volume"]) or V[i] > cfg["zone_touch_vol_mult"] * vavg[i]
            if vol_ok and (not cfg["zone_trend_filter"] or c > slow[i]):
                if cfg["zone_entry"] == "confirm":
                    cand = [z for z in new if z["dir"] == "buy" and c > z["low"]]
                    if cand and c > o:
                        z = max(cand, key=lambda x: x["high"])
                        stop = z["low"] - buf * atr[i]
                        if stop > 0 and (c - stop) / c >= minrisk:
                            pending = stop
                else:
                    near = [z for z in tr.zones if z["alive"] and z["dir"] == "buy" and z["armed"] and not z["touched"]
                            and z["high"] < c <= z["high"] * (1 + cfg["zone_approach_pct"] / 100)]
                    if near:
                        z = max(near, key=lambda x: x["high"])
                        stop = z["low"] - buf * atr[i]
                        if stop > 0 and (z["high"] - stop) / z["high"] >= minrisk:
                            order = dict(zone=z, limit=z["high"], stop=stop, exp=i + cfg["zone_order_ttl"])
    if pos is not None:
        close(C[hi - 1], "end")
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return dict(trades=n, win=len(wins) / n * 100 if n else 0.0,
                pf=min(gp / gl, 99.0) if gl > 0 else (99.0 if gp > 0 else 0.0),
                avg_r=sum(t["r"] for t in trades) / n if n else 0.0,
                ret=(cash / start - 1) * 100, dd=max_dd, bh=(C[hi - 1] / O[lo] - 1) * 100,
                in_mkt=in_mkt / max(1, hi - lo) * 100)


ZONE_PRESETS = [
    ("потврда по допир + волумен",        {}),
    ("потврда по допир, БЕЗ волумен",     dict(zone_use_volume=False)),
    ("лимит налог (при волумен)",         dict(zone_entry="limit")),
    ("лимит налог, БЕЗ волумен",          dict(zone_entry="limit", zone_use_volume=False)),
    ("потврда + само над SMA200",         dict(zone_trend_filter=True)),
    ("само силни зони (движење >= 1%)",   dict(zone_min_move_pct=1.0)),
    ("цел RR 1.5",                        dict(zone_rr=1.5)),
    ("цел RR 3",                          dict(zone_rr=3.0)),
]


def zones_test(data, cfg):
    for tf, bars in data:
        warm, n = 205, len(bars)
        if n < warm + 300:
            print(f"\n[{tf}] премалку податоци ({n} свеќи, треба над {warm + 300}) — пробај поголем --days")
            continue
        ind = zones_prepare(bars)
        days = (bars[-1][0] - bars[warm][0]) / 86_400_000
        print(f"\n══════ {cfg['symbol']}  {tf} свеќи  ({days:.0f} дена) ══════")
        print("\nА) ДАЛИ ЗОНИТЕ ОДБИВААТ? (без трошоци; цел = {:.1f}x ризик во {} свеќи; „случајно“ = исти зделки на случајни места)".format(
            cfg["zone_rr"], cfg["zone_eval_bars"]))
        zone_study(ind, cfg, warm)
        print("   z над ~2 = зоната е статистички подобра од случајно; трошок(R) = колку од ризикот ќе го изедат провизиите.")
        print("   * = под 30 примероци: бројот е ШУМ, не носи заклучок. За сигурен заклучок треба над ~100 примероци.")
        mid = (warm + n) // 2
        print("\nБ) ТРГУВАЊЕ (само купување кај зони на купување, со провизии и лизгање)")
        print(f"   Само држење: цел период {(ind['C'][-1] / ind['O'][warm] - 1) * 100:+.1f}%")
        print(f"{'поставки':36s} {'зделки':>6s} {'добитни':>8s} {'PF':>5s} {'avg R':>6s} {'1ва пол.':>9s} {'2ра пол.':>9s} {'цел период':>10s} {'макс пад':>8s}")
        for name, ov in ZONE_PRESETS:
            c = dict(cfg)
            c.update(ov)
            a, b, f = zones_run(ind, c, warm, mid), zones_run(ind, c, mid, n), zones_run(ind, c, warm, n)
            print(f"{name:36s} {f['trades']:6d} {f['win']:7.1f}% {f['pf']:5.2f} {f['avg_r']:+6.2f} {a['ret']:+8.1f}% {b['ret']:+8.1f}% {f['ret']:+9.1f}% {f['dd']:7.1f}%")
    print("\nПрочитај: вреди внимание само ако (1) z во делот А е јасно над 2, (2) avg R е позитивен ПОСЛЕ трошоци и (3) двете половини се позитивни.")
    print("Ако зоните не се подобри од случајно, нема смисла да се тргува според нив, колку и да изгледаат убаво на графикот.")



# ───────────── НИВОА: точки за купување/продавање + ренџ за СЛ и ТП (логика од HTML апликацијата) ─────────────
LEVEL_TFS = [("15m", "15м"), ("1h", "1ч"), ("4h", "4ч"), ("1d", "1д")]
SL_TIGHT_ATR, SL_WIDE_ATR = 0.5, 1.0         # СЛ ренџ: 0.5 до 1.0 x ATR под/над зоната
WIDE_ZONE_ATR, TIGHT_CAP_ATR, WIDE_CAP_ATR = 3.0, 1.5, 3.0   # зона пошироока од 3 ATR = „широка"; СЛ не подалеку од 1.5 / 3 ATR
MAX_DIST_PCT = {"1h": 5.0, "4h": 10.0, "1d": 12.0}   # колку далеку смее да е зоната за да има практичен план (% од цената)
MIN_SL_TIGHT_PCT, MIN_SL_WIDE_PCT, MIN_RR = 0.5, 1.0, 1.5   # СЛ не помал од 0.5%/1.0% од влезот; цела со R:R најмалку 1.5
PAD_NEAR_ATR, PAD_FAR_ATR = 0.15, 0.25        # појас на влез: 0.15 x ATR кон цената, 0.25 x ATR длабоко во зоната


def fmt_price(x):
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.5f}"


def _closed(rows):
    now = time.time() * 1000
    return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), r[6]] for r in rows if r[6] < now]


def level_zones(bars, tf):
    c, v, t = [b[4] for b in bars], [b[5] for b in bars], [b[6] for b in bars]
    h, l = [b[2] for b in bars], [b[3] for b in bars]
    atr = atr_arr(h, l, c)[-1] if len(bars) > 20 else None
    zs = detect_volume_zones(c, v, t, h, l, cap=60) if len(bars) >= 60 else []
    for z in zs:
        z["tf"], z["atr"] = tf, atr
    return zs, atr, c


def make_plan(direction, z, zones, price, fallback_atr):
    """План од една зона. direction: 'buy' (долго) или 'sell' (кратко / излез)."""
    atr = z["atr"] or fallback_atr
    width_pct = (z["high"] - z["low"]) / price * 100
    wide = (z["high"] - z["low"]) > WIDE_ZONE_ATR * atr
    inside = z["low"] <= price <= z["high"]
    if direction == "buy":
        if wide and inside:                                    # цената е во широка зона -> купувај по падови во зоната
            entry_hi, entry_lo = price, max(z["low"], price - 1.0 * atr)
        else:
            entry_hi, entry_lo = z["high"] + PAD_NEAR_ATR * atr, z["low"] - PAD_FAR_ATR * atr   # појас на влез (зоните често се тенка линија)
        entry_hi = min(entry_hi, price)                         # лимит за купување не се става над пазарот
        if wide:
            entry_lo = max(entry_lo, entry_hi - 1.0 * atr)      # широка зона: појасот е само 1 ATR длабок, не цела зона
        entry = min(price, entry_hi)                           # ако цената е веќе во појасот = влез сега
        zone_t, zone_w = z["low"] - SL_TIGHT_ATR * atr, z["low"] - SL_WIDE_ATR * atr          # СЛ под зоната
        sl_t, sl_w = max(zone_t, entry - TIGHT_CAP_ATR * atr), max(zone_w, entry - WIDE_CAP_ATR * atr)   # но не подалеку од 1.5 / 3 ATR
        risk_t, risk_w = entry - sl_t, entry - sl_w
        tg = sorted([q for q in zones if q["dir"] == "sell" and q["low"] > entry], key=lambda q: q["low"])
    else:
        if wide and inside:
            entry_lo, entry_hi = price, min(z["high"], price + 1.0 * atr)
        else:
            entry_lo, entry_hi = z["low"] - PAD_NEAR_ATR * atr, z["high"] + PAD_FAR_ATR * atr
        entry_lo = max(entry_lo, price)                         # лимит за продавање не се става под пазарот
        if wide:
            entry_hi = min(entry_hi, entry_lo + 1.0 * atr)
        entry = max(price, entry_lo)
        zone_t, zone_w = z["high"] + SL_TIGHT_ATR * atr, z["high"] + SL_WIDE_ATR * atr
        sl_t, sl_w = min(zone_t, entry + TIGHT_CAP_ATR * atr), min(zone_w, entry + WIDE_CAP_ATR * atr)
        risk_t, risk_w = sl_t - entry, sl_w - entry
        tg = sorted([q for q in zones if q["dir"] == "buy" and q["high"] < entry], key=lambda q: -q["high"])
    capped = (direction == "buy" and sl_w > zone_w + 1e-9) or (direction == "sell" and sl_w < zone_w - 1e-9)
    sgn = 1 if direction == "buy" else -1
    widened = False
    # минимален СЛ (кога ATR е мал, на пр. викенд, СЛ би бил тесен и провизиите го јадат)
    if risk_t / entry * 100 < MIN_SL_TIGHT_PCT or risk_w / entry * 100 < MIN_SL_WIDE_PCT:
        widened = True
        risk_t, risk_w = max(risk_t, entry * MIN_SL_TIGHT_PCT / 100), max(risk_w, entry * MIN_SL_WIDE_PCT / 100)
        sl_t, sl_w = entry - sgn * risk_t, entry - sgn * risk_w
    # појасот на влез никогаш не смее да стигне до СЛ
    if direction == "buy" and entry_lo <= sl_t:
        entry_lo = sl_t + 0.25 * (entry - sl_t)
    if direction == "sell" and entry_hi >= sl_t:
        entry_hi = sl_t - 0.25 * (sl_t - entry)
    # цели: само зони на спротивната страна што даваат најмалку MIN_RR; поблиските се прескокнуваат
    edge = (lambda q: q["low"]) if direction == "buy" else (lambda q: q["high"])
    far = [q for q in tg if abs(edge(q) - entry) >= MIN_RR * risk_w]
    skipped = len(tg) - len(far)
    if far:
        tp1 = edge(far[0])
        tp2 = edge(far[1]) if len(far) > 1 else entry + sgn * 3 * risk_w
        src = "зони " + ",".join(f"{q['tf']}" for q in far[:2]) + (f"; прескокнати {skipped} блиски (R:R<{MIN_RR})" if skipped else "")
    else:
        tp1, tp2 = entry + sgn * MIN_RR * risk_w, entry + sgn * 3 * risk_w
        src = f"нема зона со R:R>={MIN_RR} -> 1.5R и 3R" + (f"; прескокнати {skipped} блиски" if skipped else "")
    if (tp2 - tp1) * sgn < 0:
        tp1, tp2 = tp2, tp1
    rr1, rr2 = abs(tp1 - entry) / risk_w if risk_w > 0 else 0, abs(tp2 - entry) / risk_w if risk_w > 0 else 0
    warn = []
    if wide:
        warn.append(f"зоната е ШИРОКА ({width_pct:.1f}% од цената): " + ("цената е внатре, планот е „по падови/пораст во зоната“" if inside else "влезот е од работ на зоната"))
    if capped:
        warn.append(f"СЛ е скратен на {TIGHT_CAP_ATR}-{WIDE_CAP_ATR} ATR; зоната е официјално пробиена дури " +
                    ("под " if direction == "buy" else "над ") + fmt_price(z["low"] if direction == "buy" else z["high"]))
    if widened:
        warn.append(f"СЛ е проширен до минимум {MIN_SL_TIGHT_PCT}-{MIN_SL_WIDE_PCT}% (ATR е мал, можно е тенко/викенд тргување)")
    if rr1 < MIN_RR - 1e-9:
        warn.append("прва цел под R:R 1.5 (нема доволно далечна зона)")
    safe15, safe2 = entry + sgn * 1.5 * risk_w, entry + sgn * 2.0 * risk_w
    return dict(dir=direction, zone=z, entry_hi=entry_hi, entry_lo=entry_lo, entry=entry, sl_t=sl_t, sl_w=sl_w, safe15=safe15, safe2=safe2,
                tp1=tp1, tp2=tp2, tp_src=src, risk_t=risk_t / entry * 100, risk_w=risk_w / entry * 100,
                gain1=abs(tp1 - entry) / entry * 100, gain2=abs(tp2 - entry) / entry * 100, rr1=rr1, rr2=rr2, warn=warn)


def levels_report(symbol, cfg, synthetic=False):
    if synthetic:
        mss = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        data = {tf: synthetic_4h(1000, ms=mss[tf], seed=3 + i) for i, (tf, _) in enumerate(LEVEL_TFS)}
        m1 = synthetic_bars(600)
        price = data["15m"][-1][4]
    else:
        data = {tf: _closed(klines(symbol, tf, limit=1000)) for tf, _ in LEVEL_TFS}
        m1 = _closed(klines(symbol, "1m", limit=600))
        price = float(http_get_first("/api/v3/ticker/price?symbol=" + symbol)["price"])
    zones, atrs = [], {}
    for tf, _ in LEVEL_TFS:
        zs, atr, _c = level_zones(data[tf], tf)
        zones += zs
        atrs[tf] = atr
    fb = atrs.get("1h") or price * 0.005
    nm = dict(LEVEL_TFS)
    print("\n" + "═" * 78)
    print(f" {symbol}   цена {fmt_price(price)}   ATR: 1ч {fmt_price(atrs['1h'] or 0)} | 4ч {fmt_price(atrs['4h'] or 0)} | 1д {fmt_price(atrs['1d'] or 0)}")
    print("═" * 78)

    # контекст: сигнал по логиката од HTML
    if len(m1) >= 100:
        cl, vo = [b[4] for b in m1][-500:], [b[5] for b in m1][-500:]
        sg = compute_signal(cl, vo)
        word = {"BUY": "КУПИ", "SELL": "ПРОДАЈ", "WAIT": "ЧЕКАЈ"}[sg["word"]]
        tfs = []
        for lbl, st in (("5м", 5), ("15м", 15), ("30м", 30)):
            x = quick_signal(resample_closes(cl, st))
            tfs.append(lbl + ("+" if x > 0 else "-" if x < 0 else "0"))
        for lbl, key in (("1ч", "1h"), ("4ч", "4h"), ("1д", "1d")):
            x = quick_signal([b[4] for b in data[key]][-HTF_KEEP[key]:])
            tfs.append(lbl + ("+" if x > 0 else "-" if x < 0 else "0"))
        print(f" Сигнал: {word} (гласови {sg['total']:+d}) | {' '.join(tfs)}   (+ купи, - продај)")
        hs_ = [quick_signal([b[4] for b in data[k]][-HTF_KEEP[k]:]) for k in ("1h", "4h", "1d")]
        bias = 1 if all(x == 1 for x in hs_) else (-1 if all(x == -1 for x in hs_) else 0)
        if bias:
            print(f" Повисоки тајмфрејмови (1ч+4ч+1д): СИТЕ {'КУПИ' if bias > 0 else 'ПРОДАЈ'} -> "
                  f"планот во спротивна насока е ПРОТИВ тренд")

    def table(direction, title):
        if direction == "buy":
            zs = sorted([z for z in zones if z["dir"] == "buy" and z["low"] <= price * 1.002], key=lambda z: max(0.0, price - z["high"]))
        else:
            zs = sorted([z for z in zones if z["dir"] == "sell" and z["high"] >= price * 0.998], key=lambda z: max(0.0, z["low"] - price))
        print(f"\n {title}")
        if not zs:
            print("   (нема активни зони)")
            return zs
        for z in zs[:5]:
            dist = (price - z["high"]) / price * 100 if direction == "buy" else (z["low"] - price) / price * 100
            inside = z["low"] <= price <= z["high"]
            if inside:
                where = "ВО ЗОНА"
            elif dist <= 0:                              # цената е веќе зад работ на зоната, но зоната сè уште не е пробиена
                where = f"НА ЗОНА (цената {abs(dist):.2f}% " + ("под" if direction == "buy" else "над") + " работ)"
            else:
                where = f"{dist:5.2f}% " + ("под" if direction == "buy" else "над") + " цената"
            print(f"   {nm[z['tf']]:>3s}  {fmt_price(z['low'])} - {fmt_price(z['high'])}   {where}   "
                  f"волумен {z['peak_mult']:.1f}x   продолжување {z['change']:+.2f}%")
        return zs

    bias = bias if len(m1) >= 100 else 0
    for direction, ttl, ptitle in (("buy", "ЗОНИ НА КУПУВАЊЕ (под цената)", "ПЛАН ЗА КУПУВАЊЕ (долго)"),
                                   ("sell", "ЗОНИ НА ПРОДАВАЊЕ (над цената)", "ПЛАН ЗА ПРОДАВАЊЕ (излез од долго / кратко)")):
        zs = table(direction, ttl)
        if not zs:
            continue
        # контекст: поголеми зони (4ч/1д) во кои е цената
        ctx = [q for q in zs if q["tf"] in ("4h", "1d") and q["low"] <= price <= q["high"]]
        if ctx:
            print("   Контекст - поголеми зони околу цената: " + "; ".join(
                f"{nm[q['tf']]} {fmt_price(q['low'])}-{fmt_price(q['high'])} ({q['peak_mult']:.1f}x)" for q in ctx))
            print("   Слабост на зоната (пробив " + ("под" if direction == "buy" else "над") + "): " + " / ".join(
                f"{fmt_price(q['low'] if direction == 'buy' else q['high'])} ({nm[q['tf']]})"
                for q in sorted(ctx, key=lambda q: -q["low"] if direction == "buy" else q["high"])))
        # по еден план за секој тајмфрејм (стабилно, не скока меѓу зони)
        print(f"\n {ptitle}  - по тајмфрејм (1ч = краток, 4ч = среден, 1д = долг рок)")
        for tf_key, tf_lbl in (("1h", "1ч"), ("4h", "4ч"), ("1d", "1д")):
            cands = [q for q in zs if q["tf"] == tf_key]
            if not cands:
                print(f"   [{tf_lbl}] нема активна зона")
                continue
            dist_of = (lambda q: max(0.0, price - q["high"])) if direction == "buy" else (lambda q: max(0.0, q["low"] - price))
            z = min(cands, key=lambda q: (dist_of(q), -q["peak_mult"]))
            dist = dist_of(z) / price * 100
            if dist > MAX_DIST_PCT[tf_key]:
                print(f"   [{tf_lbl}] најблиската зона е предалеку ({dist:.1f}% од цената, граница {MAX_DIST_PCT[tf_key]:g}%) - нема практичен план")
                continue
            p = make_plan(direction, z, zones, price, fb)
            where = "цената е во зоната" if z["low"] <= price <= z["high"] else f"{dist:.2f}% од цената"
            print(f"   [{tf_lbl}] зона {fmt_price(z['low'])}-{fmt_price(z['high'])}  ({where}, волумен {z['peak_mult']:.1f}x)")
            print(f"        Влез {fmt_price(p['entry_hi'] if direction == 'buy' else p['entry_lo'])} -> "
                  f"{fmt_price(p['entry_lo'] if direction == 'buy' else p['entry_hi'])} | "
                  f"СЛ {fmt_price(p['sl_t'])}-{fmt_price(p['sl_w'])} ({p['risk_t']:.1f}-{p['risk_w']:.1f}%) | "
                  f"ТП {fmt_price(p['tp1'])}-{fmt_price(p['tp2'])} | R:R 1:{p['rr1']:.1f}-{p['rr2']:.1f}")
            print(f"        Поконзервативно: 1.5R {fmt_price(p['safe15'])} | 2R {fmt_price(p['safe2'])}")
            print(f"        Цели од: {p['tp_src']}")
            if (direction == "buy" and bias < 0) or (direction == "sell" and bias > 0):
                p["warn"].append("ПРОТИВ насоката на 1ч+4ч+1д (сите " + ("ПРОДАЈ" if bias < 0 else "КУПИ") + ")")
            for w in p["warn"]:
                print(f"        ! {w}")
    print("\n Ова се НИВОА од зони со волумен, не предвидување. Во тестовите зоните не одбија подобро од случајно,")
    print(" па секогаш СЛ и мал ризик. Не е финансиски совет.")



# ─────────────────────────── БРОКЕРИ ───────────────────────────
class PaperBroker:
    def __init__(self, cfg):
        self.cfg, self.cash, self.qty = cfg, cfg["start_equity"], 0.0
        self.fee = cfg["fee_pct"] / 100
        self.sl = None

    def price(self):
        return float(http_get_first("/api/v3/ticker/price?symbol=" + self.cfg["symbol"])["price"])

    def equity(self):
        return self.cash + self.qty * self.price()

    def buy(self, quote):
        p = self.price() * (1 + self.cfg["slippage_pct"] / 100)
        quote = min(quote, self.cash / (1 + self.fee))
        self.qty += quote / p
        self.cash -= quote * (1 + self.fee)
        return self.qty, p

    def place_stop(self, qty, stop_price):
        self.sl = stop_price
        return "paper-stop"

    def stop_triggered(self):
        return self.sl is not None and self.qty > 0 and self.price() <= self.sl

    def cancel_stop(self):
        self.sl = None

    def sell(self, qty):
        p = self.price() * (1 - self.cfg["slippage_pct"] / 100)
        self.cash += self.qty * p * (1 - self.fee)
        self.qty = 0.0
        self.sl = None
        return p


def http_get_first(path):
    last = None
    for base in DATA_URLS:
        try:
            return http_get(base + path)
        except Exception as e:
            last = e
    raise RuntimeError(last)


class BinanceBroker:
    """Вистински налози преку Binance Spot API. Тестирај прво на testnet!"""
    def __init__(self, cfg, real):
        self.cfg, self.real = cfg, real
        self.base = LIVE_URL if real else TESTNET_URL
        self.key, self.secret = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
        if not self.key or not self.secret:
            _exit("Постави BINANCE_API_KEY и BINANCE_API_SECRET како environment променливи.")
        self.sym = cfg["symbol"]
        info = self._req("GET", "/api/v3/exchangeInfo", {"symbol": self.sym}, signed=False)["symbols"][0]
        self.base_asset, self.quote_asset = info["baseAsset"], info["quoteAsset"]
        f = {x["filterType"]: x for x in info["filters"]}
        self.step = float(f["LOT_SIZE"]["stepSize"])
        self.min_qty = float(f["LOT_SIZE"]["minQty"])
        self.tick = float(f["PRICE_FILTER"]["tickSize"])
        nf = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        self.min_notional = float(nf.get("minNotional", 10))
        self.stop_id = None

    # --- ниско ниво ---
    def _req(self, method, path, params=None, signed=True):
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.key, "User-Agent": "signal-bot/1.0"}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            qs = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        qs = urllib.parse.urlencode(params)
        url = self.base + path + ("?" + qs if method in ("GET", "DELETE") else "")
        data = qs.encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Binance {e.code}: {e.read().decode()}")

    def _round_qty(self, q):
        steps = math.floor(q / self.step + 1e-9)
        return float(f"{steps * self.step:.12f}")

    def _round_price(self, p):
        return float(f"{round(p / self.tick) * self.tick:.12f}")

    def _fmt(self, x):
        return f"{x:.12f}".rstrip("0").rstrip(".")

    # --- интерфејс ---
    def price(self):
        return float(self._req("GET", "/api/v3/ticker/price", {"symbol": self.sym}, signed=False)["price"])

    def equity(self):
        bal = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in self._req("GET", "/api/v3/account")["balances"]}
        return bal.get(self.quote_asset, 0.0) + bal.get(self.base_asset, 0.0) * self.price()

    def free_quote(self):
        bal = {b["asset"]: float(b["free"]) for b in self._req("GET", "/api/v3/account")["balances"]}
        return bal.get(self.quote_asset, 0.0)

    def buy(self, quote):
        quote = min(quote, self.free_quote() * 0.995)
        if quote < self.min_notional * 1.05:
            raise RuntimeError(f"Премал износ ({quote:.2f}) за минимумот {self.min_notional}")
        o = self._req("POST", "/api/v3/order", dict(symbol=self.sym, side="BUY", type="MARKET",
                      quoteOrderQty=self._fmt(round(quote, 2)), newOrderRespType="FULL"))
        qty = float(o["executedQty"])
        for fl in o.get("fills", []):                       # провизија во базна валута ја намалува количината
            if fl["commissionAsset"] == self.base_asset:
                qty -= float(fl["commission"])
        spent = float(o["cummulativeQuoteQty"])
        return self._round_qty(qty), spent / float(o["executedQty"])

    def place_stop(self, qty, stop_price):
        stop = self._round_price(stop_price)
        # STOP_LOSS is a market stop: avoids a stop-limit order remaining unfilled
        # during a fast move. Actual execution price can be worse due to slippage.
        o = self._req("POST", "/api/v3/order", dict(symbol=self.sym, side="SELL", type="STOP_LOSS",
                      quantity=self._fmt(self._round_qty(qty)), stopPrice=self._fmt(stop)))
        self.stop_id = o["orderId"]
        return self.stop_id

    def stop_triggered(self):
        if self.stop_id is None:
            return False
        o = self._req("GET", "/api/v3/order", {"symbol": self.sym, "orderId": self.stop_id})
        if o["status"] in ("CANCELED", "EXPIRED", "REJECTED"):
            log(f"⚠ Заштитниот stop налог е {o['status']} — позицијата нема заштита на берзата!")
            self.stop_id = None
            return False
        if o["status"] == "PARTIALLY_FILLED":
            log("⚠ Stop делумно исполнет; преостанатиот дел останува под активен stop и се следи.")
            return False
        return o["status"] == "FILLED"

    def cancel_stop(self):
        if self.stop_id is not None:
            try:
                self._req("DELETE", "/api/v3/order", {"symbol": self.sym, "orderId": self.stop_id})
            except RuntimeError as e:
                log(f"cancel stop: {e}")
            self.stop_id = None

    def sell(self, qty):
        self.cancel_stop()
        o = self._req("POST", "/api/v3/order", dict(symbol=self.sym, side="SELL", type="MARKET",
                      quantity=self._fmt(self._round_qty(qty)), newOrderRespType="FULL"))
        return float(o["cummulativeQuoteQty"]) / float(o["executedQty"])


# ─────────────────────────── ЖИВ ЦИКЛУС ───────────────────────────
def load_state():
    return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)


def run_live(cfg, broker, mode_name):
    st = load_state()
    # Restore exchange-side protective order tracking after a process restart.
    if hasattr(broker, "stop_id") and st.get("stop_id") is not None:
        broker.stop_id = st.get("stop_id")
    W = cfg["window"]
    zones, zones_ts = [], 0
    htf_cache, htf_ts = None, 0
    last_bar_t = None
    cooldown_until = 0.0
    day, day_pnl, day_start_eq = None, 0.0, broker.equity()
    log(f"▶ Стартува ({mode_name}) {cfg['symbol']}  капитал≈${broker.equity():.2f}  "
        f"{'позиција од претходно: ' + str(st.get('qty')) if st.get('qty') else 'без отворена позиција'}")

    def exit_position(reason):
        nonlocal cooldown_until, day_pnl
        if reason != "stop":
            p = broker.sell(st["qty"])
        else:
            p = broker.price()
        pnl = st["qty"] * (p - st["entry"])
        day_pnl += pnl
        log(f"⬇ ИЗЛЕЗ ({reason}) @ {p:.4f}  P/L≈${pnl:.2f} ({(p / st['entry'] - 1) * 100:+.2f}%)  капитал≈${broker.equity():.2f}")
        st.clear()
        save_state(st)
        cooldown_until = time.time() + cfg["cooldown_bars"] * INTERVAL_SEC[cfg["interval"]]

    while True:
        try:
            if os.path.exists(STOP_FILE):
                log("Пронајдена STOP датотека — затворам позиција и излегувам.")
                if st.get("qty"):
                    exit_position("kill-switch")
                return
            now = datetime.now(timezone.utc)
            if now.date() != day:
                day, day_pnl, day_start_eq = now.date(), 0.0, broker.equity()

            price = broker.price()
            if st.get("qty"):
                if broker.stop_triggered():
                    exit_position("stop")
                elif price >= st["tp"]:
                    exit_position("take_profit")

            # нова затворена 1м свеќа?
            rows = klines(cfg["symbol"], cfg["interval"], limit=W + 2)
            rows = [r for r in rows if r[6] < time.time() * 1000]
            if rows and rows[-1][0] != last_bar_t and len(rows) >= 60:
                last_bar_t = rows[-1][0]
                closes, vols = [float(r[4]) for r in rows[-W:]], [float(r[5]) for r in rows[-W:]]
                if (cfg["strategy"] == "mtf" or cfg["htf_filter"] != "off") and time.time() - htf_ts > 300:
                    htf_cache = {}
                    for name in ("1h", "4h", "1d"):
                        kl = [r for r in klines(cfg["symbol"], name, limit=HTF_KEEP[name] + 2) if r[6] < time.time() * 1000]
                        htf_cache[name] = [float(r[4]) for r in kl][-HTF_KEEP[name]:]
                    htf_ts = time.time()
                sig = decide(closes, vols, htf_cache, cfg)
                log(f"свеќа {datetime.fromtimestamp(last_bar_t / 1000, timezone.utc):%H:%M}  цена {closes[-1]:.4f}  "
                    f"сигнал {sig['word']} {sig['info']}{'  ⚠конфликт' if sig['conflict'] else ''}")

                if st.get("qty") and sig["word"] == "SELL" and cfg["exit_on_sell_signal"]:
                    exit_position("signal")
                elif not st.get("qty") and sig["word"] == "BUY" and time.time() >= cooldown_until:
                    ok = True
                    if cfg["block_on_conflict"] and sig["conflict"]:
                        ok = False
                    if day_pnl < -day_start_eq * cfg["max_daily_loss_pct"] / 100:
                        log("Дневен лимит на загуба достигнат — нема нови зделки до утре (UTC).")
                        ok = False
                    if ok and cfg["use_zone_filter"]:
                        if time.time() - zones_ts > 3600:
                            b4 = [r for r in klines(cfg["symbol"], "4h", limit=500) if r[6] < time.time() * 1000]
                            zones = detect_volume_zones([float(r[4]) for r in b4], [float(r[5]) for r in b4],
                                                        [r[6] for r in b4], [float(r[2]) for r in b4], [float(r[3]) for r in b4])
                            zones_ts = time.time()
                        if blocked_by_zone(price, zones, cfg["zone_buffer_pct"]):
                            log("Влезот е блокиран: цената е под/во силна 4ч зона на продавање.")
                            ok = False
                    if ok:
                        equity = broker.equity()
                        quote = min(equity * (cfg["risk_per_trade_pct"] / 100) / (cfg["stop_pct"] / 100),
                                    equity * cfg["max_position_pct"] / 100)
                        qty, entry = broker.buy(quote)
                        stop = entry * (1 - cfg["stop_pct"] / 100)
                        tp = entry * (1 + cfg["take_profit_pct"] / 100)
                        try:
                            sid = broker.place_stop(qty, stop)
                        except Exception as stop_err:
                            log(f"КРИТИЧНО: stop не е поставен: {stop_err}. Обид за итно затворање.")
                            try:
                                broker.sell(qty)
                                log("Итната продажба е испратена; проверете ја Binance сметката.")
                            except Exception as sell_err:
                                st.update(qty=qty, entry=entry, stop=stop, tp=tp, stop_id=None,
                                          time=datetime.now(timezone.utc).isoformat(), emergency=True)
                                save_state(st)
                                log(f"КРИТИЧНО: не успеа ни итната продажба: {sell_err}. Рачно проверете Binance веднаш.")
                            continue
                        st.update(qty=qty, entry=entry, stop=stop, tp=tp, stop_id=sid,
                                  time=datetime.now(timezone.utc).isoformat())
                        save_state(st)
                        log(f"⬆ ВЛЕЗ  qty={qty} @ {entry:.4f}  stop {stop:.4f}  tp {tp:.4f}  (износ≈${quote:.2f})")
        except Exception as e:
            log(f"ГРЕШКА: {e}")
            time.sleep(15)
        time.sleep(cfg["poll_seconds"])


# ─────────────────────────────── CLI ───────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["levels", "backtest", "sweep", "trend", "zones", "paper", "live"])
    ap.add_argument("--days", type=int, default=None, help="колку дена историја (backtest/sweep=30, trend=730, zones=1800)")
    ap.add_argument("--interval", choices=["1m", "5m", "15m"], default="1m", help="големина на свеќа за backtest/sweep/paper/live")
    ap.add_argument("--symbols", default=None, help="levels: пазари одделени со запирка, на пр. PAXGUSDT,BTCUSDT,ETHUSDT")
    ap.add_argument("--every", type=float, default=0.0, help="levels: повторно пресметување на секои N минути (0 = еднаш)")
    ap.add_argument("--synthetic", action="store_true", help="лажни податоци (тест на кодот)")
    ap.add_argument("--real", action="store_true", help="live со ВИСТИНСКИ пари (инаку testnet)")
    ap.add_argument("--set", action="append", default=[], metavar="клуч=вредност")
    a = ap.parse_args()

    if a.days is None:
        a.days = 1800 if a.mode == "zones" else (730 if a.mode == "trend" else (30 if a.interval == "1m" else 365))
    CFG["interval"] = a.interval
    explicit_strategy = any(kv.startswith("strategy=") for kv in a.set)
    for kv in a.set:
        k, v = kv.split("=", 1)
        if k not in CFG:
            _exit(f"Непозната поставка: {k}. Достапни: {', '.join(CFG)}")
        old = CFG[k]
        CFG[k] = (v.lower() in ("1", "true", "yes")) if isinstance(old, bool) else type(old)(v)

    CFG["interval"] = a.interval
    if a.mode == "levels":
        syms = [x.strip().upper() for x in (a.symbols or CFG["symbol"]).split(",") if x.strip()]
        while True:
            for sym in syms:
                try:
                    levels_report(sym, CFG, a.synthetic)
                except Exception as e:
                    print(f"\n[{sym}] грешка: {e}")
            if a.every <= 0:
                break
            print(f"\n(следно освежување за {a.every:g} мин; прекини со Ctrl+C)")
            time.sleep(a.every * 60)
        return
    if a.interval != "1m" and a.mode in ("backtest", "sweep", "paper", "live"):
        if CFG["strategy"] == "mtf":
            if explicit_strategy:
                _exit("Стратегијата mtf работи само со 1m свеќи. Користи --set strategy=classic за 5m/15m.")
            CFG["strategy"] = "classic"
            print(f"Забелешка: за {a.interval} свеќи се користи логиката од HTML апликацијата (strategy=classic).")
    if a.mode == "zones":
        if a.synthetic:
            data = [("1h", synthetic_4h(15000, ms=3_600_000, seed=9)), ("4h", synthetic_4h(5000)),
                    ("1d", synthetic_4h(1500, ms=86_400_000, seed=11))]
            print("⚠ Лажни податоци: резултатот НЕ значи ништо за вистинскиот пазар — само го тестира кодот.")
        else:
            print(f"Симнувам {a.days} дена 1ч, 4ч и 1д податоци за {CFG['symbol']}...")
            data = [("1h", fetch_history(CFG["symbol"], "1h", a.days + 15)),
                    ("4h", fetch_history(CFG["symbol"], "4h", a.days + 40)),
                    ("1d", fetch_history(CFG["symbol"], "1d", a.days + 240))]
        zones_test(data, CFG)
    elif a.mode == "trend":
        if a.synthetic:
            b4 = synthetic_4h()
            b1d = aggregate_bars(b4, 6)
            print("⚠ Лажни податоци: резултатот НЕ значи ништо за вистинскиот пазар — само го тестира кодот.")
        else:
            print(f"Симнувам {a.days} дена 4ч и 1д податоци за {CFG['symbol']}...")
            b4 = fetch_history(CFG["symbol"], "4h", a.days + 60)
            b1d = fetch_history(CFG["symbol"], "1d", a.days + 80)
        trend_test(b4, b1d, CFG)
    elif a.mode in ("backtest", "sweep"):
        if a.synthetic:
            bars = synthetic_bars(20000 if a.interval == "1m" else 60000)
            htf = {"1h": aggregate(bars, 60), "4h": aggregate(bars, 240), "1d": aggregate(bars, 1440)}
            if a.interval != "1m":
                bars = aggregate_bars(bars, {"5m": 5, "15m": 15}[a.interval])
            print("⚠ Лажни податоци: резултатот НЕ значи ништо за вистинскиот пазар — само го тестира кодот.")
        else:
            print(f"Симнувам {a.days} дена {a.interval} податоци за {CFG['symbol']}...")
            bars = fetch_history(CFG["symbol"], a.interval, a.days)
            htf = None
            if CFG["strategy"] == "mtf" or CFG["use_zone_filter"] or CFG["htf_filter"] != "off" or a.mode == "sweep":
                htf = {"1h": fetch_history(CFG["symbol"], "1h", a.days + 10),
                       "4h": fetch_history(CFG["symbol"], "4h", a.days + 90),
                       "1d": fetch_history(CFG["symbol"], "1d", a.days + 80)}
        if a.mode == "sweep":
            sweep(bars, CFG, htf)
        else:
            backtest(bars, CFG, htf)
    elif a.mode == "paper":
        run_live(CFG, PaperBroker(CFG), "PAPER — лажни пари")
    else:
        if a.real:
            print("\n⚠⚠  ВИСТИНСКИ ПАРИ. Ботот ќе тргува на твојата Binance сметка.")
            print("    Користи API клуч САМО со дозвола за Spot trading (без withdrawals).")
            if input(to_ascii("    Напиши  I UNDERSTAND  за да продолжиш: ") if _ASCII_MODE else "    Напиши  I UNDERSTAND  за да продолжиш: ").strip() != "I UNDERSTAND":
                _exit("Откажано.")
        run_live(CFG, BinanceBroker(CFG, a.real), "LIVE REAL" if a.real else "LIVE TESTNET")


if __name__ == "__main__":
    main()
