import asyncio
import os
import time
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd

from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import ccxt.async_support as ccxt  # type: ignore

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command

import httpx
from contextlib import asynccontextmanager

# ---------- Config & utils ----------

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

EXCHANGES = [x.strip() for x in os.getenv("EXCHANGES", "binance,okx,bybit").split(",") if x.strip()]
WINDOW_MIN = int(os.getenv("WINDOW_MIN", "5"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
PCT_THRESHOLD = float(os.getenv("PCT_THRESHOLD", "200"))
ZSCORE_THRESHOLD = float(os.getenv("ZSCORE_THRESHOLD", "3.0"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "6"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
COINGECKO_BASE = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3")

DATA_DIR = "data"
LOGS_DIR = "logs"
STATE_DB = os.path.join(DATA_DIR, "state.sqlite")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

def now_ts() -> int:
    return int(time.time())

def minutes_ago(minutes: int) -> int:
    return now_ts() - minutes * 60

# ---------- State (SQLite) ----------

def init_db():
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            coin TEXT PRIMARY KEY,
            last_alert_ts INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS last_spikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            coin TEXT,
            pct REAL,
            zscore REAL,
            vol_now REAL,
            vol_mean REAL,
            vol_std REAL,
            exchanges TEXT
        )
    """)
    conn.commit()
    conn.close()

def can_alert(coin: str) -> bool:
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("SELECT last_alert_ts FROM alerts WHERE coin = ?", (coin,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return True
    last_ts = row[0]
    return (now_ts() - last_ts) >= COOLDOWN_MIN*60

def set_alerted(coin: str):
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO alerts(coin,last_alert_ts) VALUES(?,?) ON CONFLICT(coin) DO UPDATE SET last_alert_ts=excluded.last_alert_ts", (coin, now_ts()))
    conn.commit()
    conn.close()

def add_last_spike(coin, pct, zscore, vol_now, vol_mean, vol_std, exchanges):
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO last_spikes(ts,coin,pct,zscore,vol_now,vol_mean,vol_std,exchanges) VALUES(?,?,?,?,?,?,?,?)",
                (now_ts(), coin, pct, zscore, vol_now, vol_mean, vol_std, ",".join(exchanges)))
    conn.commit()
    conn.close()

def get_recent_spikes(limit: int = 10):
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("SELECT ts, coin, pct, zscore FROM last_spikes ORDER BY ts DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

# ---------- CoinGecko Top-50 ----------

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=30), reraise=True)
async def fetch_top50_symbols() -> List[Tuple[str, str]]:
    # returns list of (symbol_upper, name)
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 50, "page": 1}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    result = []
    for itm in data:
        sym = (itm.get("symbol") or "").upper()
        nm = itm.get("name") or sym
        if sym:
            result.append((sym, nm))
    return result

# ---------- Exchanges via ccxt ----------

EXCHANGE_FACTORIES = {
    "binance": ccxt.binance,
    "okx": ccxt.okx,
    "bybit": ccxt.bybit,
}

@asynccontextmanager
async def create_exchange(id_: str):
    if id_ not in EXCHANGE_FACTORIES:
        raise ValueError(f"Unsupported exchange {id_}")
    cls = EXCHANGE_FACTORIES[id_]
    ex = cls({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT*1000})
    try:
        await ex.load_markets()
        yield ex
    finally:
        await ex.close()

async def get_symbol_on_exchange(ex, symbol: str) -> Optional[str]:
    # Try exact COIN/USDT, fallback to ticker variations
    candidates = [
        f"{symbol}/USDT",
        f"{symbol}/USDC",
        f"{symbol}USDT",
    ]
    for c in candidates:
        if c in ex.markets:
            return c
    # try to find by base
    for mkt, info in ex.markets.items():
        if isinstance(info, dict):
            base = info.get("base")
        else:
            base = None
        if base == symbol and ("USDT" in mkt or "USDC" in mkt):
            return mkt
    return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=False)
async def fetch_ohlcv_safe(ex, market: str, timeframe: str, limit: int):
    try:
        return await ex.fetch_ohlcv(market, timeframe=timeframe, limit=limit)
    except Exception as e:
        return []

async def fetch_current_volume_for_coin(symbol: str, lookback_candles: int, timeframe: str = "5m") -> Tuple[float, List[float], List[str]]:
    volumes = []
    used_ex = []
    for ex_id in EXCHANGES:
        try:
            async with create_exchange(ex_id) as ex:
                mkt = await get_symbol_on_exchange(ex, symbol)
                if not mkt:
                    continue
                ohlcv = await fetch_ohlcv_safe(ex, mkt, timeframe=timeframe, limit=lookback_candles + 1)
                if not ohlcv or len(ohlcv) < lookback_candles + 1:
                    continue
                # ccxt OHLCV: [ts, open, high, low, close, volume]
                vols = [c[5] or 0 for c in ohlcv[-(lookback_candles+1):]]
                volumes.append(vols)
                used_ex.append(ex_id)
        except Exception:
            continue
    if not volumes:
        return 0.0, [], []
    # Aggregate by sum across exchanges
    # align length
    min_len = min(len(v) for v in volumes)
    volumes = [v[-min_len:] for v in volumes]
    agg = np.sum(np.array(volumes), axis=0).tolist()
    return agg[-1], agg[:-1], used_ex  # current vol, baseline vols, used exchanges

# ---------- Spike detection ----------

def detect_spike(vol_now: float, baseline_vols: List[float]) -> Tuple[bool, float, float, float, float]:
    if len(baseline_vols) < 5:
        return False, 0, 0, 0, 0
    arr = np.array(baseline_vols, dtype=float)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    if mean <= 0:
        return False, 0, 0, 0, 0
    pct = ((vol_now - mean) / mean) * 100.0
    z = (vol_now - mean) / std if std > 0 else 0.0
    triggered = (pct >= PCT_THRESHOLD) or (z >= ZSCORE_THRESHOLD)
    return triggered, pct, z, mean, std

# ---------- Telegram ----------

bot = Bot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
dp = Dispatcher()

async def tg_send(text: str):
    if not bot or TELEGRAM_CHAT_ID == 0:
        print("[DRY-RUN] Telegram disabled. Message would be:\n", text)
        return
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)

@dp.message(Command("status"))
async def cmd_status(message: Message):
    rows = get_recent_spikes(10)
    if not rows:
        await message.answer("Пока срабатываний нет. Мониторинг идёт ✅")
        return
    lines = ["Последние срабатывания:"]
    for ts, coin, pct, z in rows:
        lines.append(f"- {coin}: +{pct:.0f}% (z={z:.2f}) в {time.strftime('%H:%M:%S', time.localtime(ts))}")
    await message.answer("\n".join(lines))

@dp.message(Command("top"))
async def cmd_top(message: Message):
    coins = await fetch_top50_symbols()
    syms = [s for s,_ in coins]
    await message.answer("Топ‑50 по mcap (CoinGecko):\n" + ", ".join(syms))

# ---------- Main monitor ----------

async def monitor_loop():
    init_db()
    top_list: List[Tuple[str, str]] = await fetch_top50_symbols()
    last_cg_refresh = now_ts()

    lookback_candles = int(LOOKBACK_HOURS * 60 / WINDOW_MIN)
    timeframe = f"{WINDOW_MIN}m"

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def process_coin(sym: str, name: str):
        async with sem:
            vol_now, baseline, used_ex = await fetch_current_volume_for_coin(sym, lookback_candles, timeframe=timeframe)
            if vol_now <= 0 or len(baseline) < max(5, lookback_candles-1):
                return
            ok, pct, z, mean, std = detect_spike(vol_now, baseline)
            if not ok:
                return
            if not can_alert(sym):
                return
            set_alerted(sym)
            add_last_spike(sym, pct, z, vol_now, mean, std, used_ex)
            text = (
                f"⚡️ Аномальный рост объёма: <b>{name} ({sym})</b>\n"
                f"Окно: {WINDOW_MIN} мин | Биржи: {', '.join(used_ex) or '—'}\n"
                f"Текущий объём: {vol_now:,.0f}\n"
                f"База: mean={mean:,.0f}, σ={std:,.0f}\n"
                f"Δ к базе: <b>+{pct:.0f}%</b> | z={z:.2f}\n"
                f"Время: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ts()))}"
            )
            await tg_send(text)

    while True:
        try:
            # refresh top-50 each 6h
            if now_ts() - last_cg_refresh > 6*3600:
                top_list = await fetch_top50_symbols()
                last_cg_refresh = now_ts()

            tasks = [asyncio.create_task(process_coin(sym, name)) for sym, name in top_list]
            await asyncio.gather(*tasks)
        except Exception as e:
            await tg_send(f"⚠️ Ошибка цикла мониторинга: {e}")
        await asyncio.sleep(POLL_SECONDS)

async def main():
    if not TELEGRAM_TOKEN or TELEGRAM_CHAT_ID == 0:
        print("ВНИМАНИЕ: не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID — режим DRY‑RUN.")
        print("Бот будет писать в консоль. Заполните .env для отправки в Telegram.\n")

    # Запускаем и мониторинг, и Telegram-команды
    loop_task = asyncio.create_task(monitor_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановлено")
