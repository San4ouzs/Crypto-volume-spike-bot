# Бот сигналов аномального роста объёмов (Top‑50 mcap)

Бот отслеживает **онлайн‑спайки объёмов** по криптовалютам из топ‑50 по капитализации и шлёт сигналы в Telegram.

## Как это работает
- Раз в `POLL_SECONDS` бот собирает 5‑минутные свечи (*OHLCV*) по парам `COIN/USDT` с бирж **Binance**, **OKX**, **Bybit** (регулируется переменной `EXCHANGES`).
- Для каждой монеты формируется базовая линия за последние `LOOKBACK_HOURS` (по 5‑минутным объёмам): среднее и σ.
- Сигнал, если текущий 5‑минутный объём:
  - выше `PCT_THRESHOLD`% от базового среднего **или**
  - Z‑score ≥ `ZSCORE_THRESHOLD`.
- Топ‑50 монет по капитализации берётся с CoinGecko и обновляется каждые 6 часов.
- Антидубликаты: повторный алерт по той же монете — не чаще, чем раз в `COOLDOWN_MIN` минут.

## Установка
1. Установите Python 3.10+.
2. Создайте и активируйте venv:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   # или: source .venv/bin/activate  # Linux/Mac
   ```
3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
4. Скопируйте `.env.example` в `.env` и заполните токен/чат:
   ```bash
   cp .env.example .env  # Windows: copy .env.example .env
   ```
5. Запуск:
   ```bash
   python bot.py
   ```

## Команды Telegram
- `/status` — краткий статус мониторинга и последние срабатывания.
- `/top` — показать текущий список топ‑50 тикеров (по данным CoinGecko).

## Переменные `.env`
```
TELEGRAM_BOT_TOKEN=  # токен бота @BotFather
TELEGRAM_CHAT_ID=    # id чата/канала (целое число)

# Мониторинг
EXCHANGES=binance,okx,bybit
WINDOW_MIN=5
LOOKBACK_HOURS=24
POLL_SECONDS=60
PCT_THRESHOLD=200
ZSCORE_THRESHOLD=3.0
COOLDOWN_MIN=30
MAX_CONCURRENCY=6
REQUEST_TIMEOUT=15

# Продвинутое
COINGECKO_BASE=https://api.coingecko.com/api/v3
```

> Примечание: Для чатов/каналов не забудьте добавить вашего бота в участники и выдать право писать.

## Пояснения по алгоритму
- **Базовая линия** рассчитывается по последним `LOOKBACK_HOURS * 60 / WINDOW_MIN` свечам: `mean`, `std`.
- **Сигнал**: если `vol_now > mean * (1 + PCT_THRESHOLD/100)` **или** `(vol_now - mean)/std >= ZSCORE_THRESHOLD`.
- Свечи берутся через `ccxt` с нескольких бирж и агрегируются **суммой** текущих объёмов по доступным биржам (устойчивее к сбоям одной биржи).
- Если монета отсутствует на части бирж, используем те, где есть пара `COIN/USDT`.

## Логи и состояние
- `data/state.sqlite` — база для антидубликатов и последних срабатываний.
- `logs/app.log` — логи работы.

## Частые вопросы
- **Можно ли добавить другие биржи?** Да, отредактируйте `EXCHANGES` и список в коде `EXCHANGE_FACTORIES`.
- **PostgreSQL вместо SQLite?** Да. В минимальной версии используется SQLite. Миграция на Postgres тривиальна — дайте знать, пришлю миграцию.
- **Docker?** По запросу добавлю `Dockerfile` и `docker-compose.yml`.
