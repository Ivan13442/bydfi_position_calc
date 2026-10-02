import json
import math
import os
import traceback

import ccxt
import pandas as pd
import streamlit as st

# ---------- биржи: основная и запасные ----------
# Если BYDFi не отвечает (например, 404), данные берутся с запасной биржи.
# Чтобы отключить запасные биржи: FALLBACK_EXCHANGES = []
PRIMARY_EXCHANGE = "bydfi"
FALLBACK_EXCHANGES = ["okx", "bitget"]
EXCHANGE_CHAIN = [PRIMARY_EXCHANGE] + FALLBACK_EXCHANGES


# ---------- кешируем тяжелые операции ----------

@st.cache_resource(ttl=600, show_spinner=False)
def connect_exchange(exchange_id: str):
    """Возвращает (биржа, текст_ошибки). Рынки грузятся один раз на всё приложение.
    Кэшируется и успех, и неудача на 10 минут: недоступная биржа не будет
    тормозить каждое нажатие повторными запросами."""
    try:
        ex = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 10000})
        ex.load_markets()
        return ex, None
    except Exception:
        return None, traceback.format_exc()


@st.cache_data(ttl=60, show_spinner=False)
def get_price_and_ohlcv(exchange_id: str, symbol: str):
    ex = connect_exchange(exchange_id)[0]
    ticker = ex.fetch_ticker(symbol)
    last = ticker.get("last") or ticker.get("close")
    ohlcv = ex.fetch_ohlcv(symbol, timeframe="4h", limit=30)
    return last, ohlcv


def fmt_num(x, min_dec=2, sig=5, max_dec=10):
    """Число с адаптивным количеством знаков: чем меньше цена, тем больше знаков после запятой.
    Показывает минимум sig значащих цифр (но не меньше min_dec знаков после запятой)."""
    if x is None or not math.isfinite(x) or x == 0:
        return f"{x}"
    dec = sig - 1 - math.floor(math.log10(abs(x)))
    dec = max(min_dec, min(max_dec, dec))
    return f"{x:.{dec}f}"


def find_symbol(markets: dict, user_raw: str):
    """BTCUSDT -> символ рынка. Фьючерсам (swap) отдаём приоритет перед спотом."""
    direct_map = {
        "BTCUSDT": "BTC/USDT:USDT",
        "ETHUSDT": "ETH/USDT:USDT",
    }
    mapped = direct_map.get(user_raw)
    if mapped in markets:
        return mapped

    swap_match, other_match = None, None
    for sym, m in markets.items():
        compact = f"{m.get('base', '')}{m.get('quote', '')}".upper()
        if compact != user_raw:
            continue
        if m.get("swap"):
            swap_match = swap_match or sym
        else:
            other_match = other_match or sym
    if swap_match:
        return swap_match
    if other_match:
        return other_match

    for sym in markets:
        if sym.replace("-", "").replace("/", "").replace(":", "").upper() == user_raw:
            return sym
    return None


# ---------- сохранение настроек ----------

SETTINGS_FILE = "settings.json"


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------- загрузка настроек один раз за сессию ----------

# Читаем настройки один раз при старте сессии, не при каждом ререндере.
# После save_settings() обновляем st.session_state вручную — файл не перечитываем.
if "settings" not in st.session_state:
    st.session_state["settings"] = load_settings()

settings = st.session_state["settings"]


# ---------- аналитика: ATR и стоп 10% ATR ----------

def render_analysis(user_raw: str):
    # 1. подключаемся к бирже (основная, затем запасные)
    exchange_id, markets = None, None
    for ex_id in EXCHANGE_CHAIN:
        with st.spinner(f"Загружаем рынки {ex_id.upper()}..."):
            ex, _ = connect_exchange(ex_id)
        if ex is not None:
            markets = ex.markets
            exchange_id = ex_id
            break

    if exchange_id is None:
        st.error("Не удалось получить данные с биржи. Попробуй позже.")
        return

    ex_name = exchange_id.upper()
    st.caption(f"Источник данных: {ex_name}")

    # 2. ищем фьючерсный символ
    matched_symbol = find_symbol(markets, user_raw)
    if matched_symbol is None:
        st.error(f"Фьючерсный тикер не найден на {ex_name}: **{user_raw}**.")
        return

    # 3. цена и свечи
    try:
        with st.spinner(f"Получаем данные по {matched_symbol}..."):
            last_price, ohlcv = get_price_and_ohlcv(exchange_id, matched_symbol)
    except Exception as e:
        st.error(
            f"Не удалось получить данные по {matched_symbol} на {ex_name}.\n\n"
            f"Ошибка: {e}"
        )
        return

    if not ohlcv or len(ohlcv) < 30:
        st.error("Недостаточно 4h свечей для расчёта дневного ATR (нужно 30).")
        return

    if last_price is None:
        last_price = float(ohlcv[-1][4])

    df_4h = pd.DataFrame(
        ohlcv,
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    n = len(df_4h)
    start_idx = n - (n // 6) * 6
    chunked = df_4h.iloc[start_idx:]

    days = []
    for i in range(0, len(chunked), 6):
        block = chunked.iloc[i:i + 6]
        if len(block) < 6:
            continue
        days.append({
            "open": block["open"].iloc[0],
            "high": block["high"].max(),
            "low": block["low"].min(),
            "close": block["close"].iloc[-1],
        })

    days = days[-5:]
    df_days = pd.DataFrame(days)

    if len(df_days) < 5:
        st.error("Недостаточно дневных баров для расчёта ATR(5).")
        return

    df_days["prev_close"] = df_days["close"].shift(1)
    df_days["tr1"] = df_days["high"] - df_days["low"]
    df_days["tr2"] = (df_days["high"] - df_days["prev_close"]).abs()
    df_days["tr3"] = (df_days["low"] - df_days["prev_close"]).abs()
    df_days["tr"] = df_days[["tr1", "tr2", "tr3"]].max(axis=1)
    atr = df_days["tr"].rolling(window=5).mean().iloc[-1]

    if pd.isna(atr) or atr <= 0:
        st.error("Не удалось корректно посчитать ATR(5) по 5 дневным барам.")
        return

    atr_10 = atr * 0.10
    max_luft = atr_10 * 0.10

    range_pct = (df_days["high"] - df_days["low"]) / df_days["close"] * 100
    avg_range = range_pct.mean()

    if avg_range < 1:
        rec_leverage = 25
    elif avg_range < 2:
        rec_leverage = 20
    elif avg_range < 3:
        rec_leverage = 15
    elif avg_range < 5:
        rec_leverage = 10
    else:
        rec_leverage = 5

    st.write(f"Найденный фьючерсный символ на {ex_name}: **{matched_symbol}**")
    st.write(f"Текущая цена: **{fmt_num(last_price)} USDT**")
    st.write(f"ATR(5): **{fmt_num(atr)} USDT**")

    st.markdown(
        f"""
        <div style="
            border: 2px solid #3b82f6;
            background-color: #eff6ff;
            padding: 10px 14px;
            border-radius: 8px;
            margin: 8px 0;
            color: #1d4ed8;
            font-weight: 600;
        ">
            Максимальный люфт от уровня: {fmt_num(max_luft)} USDT (10% от рекомендуемого стопа)
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <div style="
            border: 2px solid #facc15;
            background-color: #fef9c3;
            padding: 10px 14px;
            border-radius: 8px;
            margin: 8px 0;
            color: #92400e;
            font-weight: 600;
        ">
            Рекомендуемый размер стопа: 10% ATR = {fmt_num(atr_10)} USDT
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.success(f"Условно рекомендуемое плечо по волатильности: **x{rec_leverage}**")

    st.session_state["rec_stop_distance"] = float(atr_10)

    # Entry по умолчанию = текущая цена актива. Фиксируем один раз на тикер,
    # чтобы введённое вручную значение не сбрасывалось при обновлении цены.
    if st.session_state.get("entry_default_symbol") != matched_symbol:
        st.session_state["entry_default"] = float(last_price)
        st.session_state["entry_default_symbol"] = matched_symbol
        st.session_state["entry_ver"] = st.session_state.get("entry_ver", 0) + 1
    if st.button("📥 Подставить текущую цену в Entry"):
        st.session_state["entry_default"] = float(last_price)
        st.session_state["entry_ver"] = st.session_state.get("entry_ver", 0) + 1
    st.caption("Расстояние стопа 10% ATR сохранено и используется как подсказка в поле SL.")


# ---------- заголовок ----------

st.title("🧮 Калькулятор объема позиции")
st.markdown("Заполни параметры сделки, выбери риск и плечо - я посчитаю объем, количество монет и R:R.")

# ---------- 1. Аналитика фьючерса: ATR и стоп 10% ATR ----------

st.markdown("---")
st.subheader("📊 Аналитика фьючерса и рекомендуемый стоп 10%ATR")

fut_symbol_input = st.text_input("Фьючерсный тикер (например BTCUSDT, ETHUSDT)", value="BTCUSDT")

if "rec_stop_distance" not in st.session_state:
    st.session_state["rec_stop_distance"] = None

show_analysis = st.checkbox("Показать аналитику фьючерса и стоп 10% ATR", value=False)

if show_analysis:
    user_raw = fut_symbol_input.upper().replace("PERP", "").strip()
    try:
        render_analysis(user_raw)
    except Exception as e:
        st.error(f"Ошибка при расчёте аналитики: {e}")

# ---------- 2. Риск и депозит ----------

st.subheader("1️⃣ Риск и депозит")

col_r1, col_r2 = st.columns([2, 1])

default_balance = float(settings.get("balance", 1000.0))
default_saved_risk = float(settings.get("risk_percent", 1.0))

with col_r1:
    balance = st.number_input("💰 Депозит, USDT", value=default_balance, min_value=0.0, step=100.0)

with col_r2:
    risk_percent = st.number_input(
        "⚠️ Риск на сделку, %",
        value=default_saved_risk,
        min_value=0.01,
        max_value=10.0,
        step=0.01
    )

st.write(f"Текущий риск: **{risk_percent:.2f}%** от депозита")

# ---------- 3. Параметры входа ----------

st.subheader("2️⃣ Параметры входа")

default_entry = 100.0

col_p1, col_p2, col_p3 = st.columns(3)
col_extra1, col_extra2 = st.columns(2)

default_side = settings.get("side", "Лонг")
default_leverage = int(settings.get("leverage", 10))

with col_extra1:
    side = st.radio("Направление сделки", ["Лонг", "Шорт"], index=0 if default_side == "Лонг" else 1)

with col_extra2:
    leverage = st.number_input(
        "🔧 Плечо (leverage)",
        value=default_leverage,
        min_value=1,
        max_value=200,
        step=1,
    )

with col_p1:
    entry_initial = st.session_state.get("entry_default", default_entry)
    entry_str = st.text_input(
        "📈 Цена входа (Entry)",
        value=fmt_num(entry_initial),
        key=f"entry_input_{st.session_state.get('entry_ver', 0)}",
        help="Можно вводить любое количество знаков после запятой.",
    )
    try:
        entry_price = float(entry_str.replace(",", "."))
    except ValueError:
        entry_price = 0.0

# Рекомендованные SL и TP подстраиваются под цену входа и направление сделки.
rec_stop_distance = st.session_state.get("rec_stop_distance", None)
sign = 1 if side == "Лонг" else -1
base_price = entry_price if entry_price > 0 else default_entry

use_atr = bool(rec_stop_distance) and entry_price > 0
price_mismatch = False
if use_atr:
    suggested_sl = entry_price - sign * rec_stop_distance
    suggested_tp = entry_price + sign * 2 * rec_stop_distance  # R:R = 2:1
    price_mismatch = suggested_sl <= 0 or suggested_tp <= 0
if not use_atr or price_mismatch:
    # без ATR (или если цена входа не подходит активу): SL на 5%, TP на 10% от входа
    suggested_sl = base_price * (1 - sign * 0.05)
    suggested_tp = base_price * (1 + sign * 0.10)

with col_p2:
    stop_str = st.text_input(
        "🛑 Стоп-лосс (SL)",
        value=fmt_num(suggested_sl),
        help="Если выше считали ATR, сюда подставлен стоп по 10% ATR от цены входа, можно скорректировать.",
    )
    try:
        stop_price = float(stop_str.replace(",", "."))
    except ValueError:
        stop_price = 0.0

with col_p3:
    tp_str = st.text_input(
        "🎯 Тейк-профит (TP)",
        value=fmt_num(suggested_tp),
        help="По умолчанию R:R = 2:1 от рекомендованного стопа, можно скорректировать.",
    )
    try:
        tp_price = float(tp_str.replace(",", "."))
    except ValueError:
        tp_price = 0.0

if price_mismatch:
    st.warning(
        "Цена входа сильно отличается от цены актива: стоп 10% ATR не помещается. "
        "Нажми «Подставить текущую цену в Entry» или введи актуальную цену."
    )

# ---------- 4. Комиссия биржи ----------

st.subheader("3️⃣ Комиссия биржи")

commission_percent = st.number_input(
    "Комиссия биржи, %",
    value=0.06,
    min_value=0.0,
    max_value=1.0,
    step=0.01,
    help="Комиссия за одну операцию (например, 0.05 = 0.05% за вход или выход).",
)

commission_rate = commission_percent / 100.0

# ---------- 5. Расчет ----------

st.subheader("4️⃣ Расчет")

if st.button("🚀 Рассчитать сделку"):
    errors = []
    if balance <= 0:
        errors.append("Депозит должен быть больше 0.")
    if entry_price <= 0 or stop_price <= 0 or tp_price <= 0:
        errors.append("Цены должны быть больше 0.")
    if entry_price == stop_price:
        errors.append("Entry и SL не должны быть равны.")
    if (side == "Лонг" and tp_price <= entry_price) or (side == "Шорт" and tp_price >= entry_price):
        errors.append("TP должен быть логичен направлению сделки (выше entry для лонга, ниже для шорта).")

    if errors:
        for e in errors:
            st.error(e)
    else:
        risk_amount = balance * (risk_percent / 100)

        if side == "Лонг":
            stop_distance = abs(entry_price - stop_price)
            tp_distance = abs(tp_price - entry_price)
        else:
            stop_distance = abs(stop_price - entry_price)
            tp_distance = abs(entry_price - tp_price)

        if stop_distance == 0:
            st.error("Расстояние до стопа равно 0, проверь цены.")
        else:
            qty = risk_amount / stop_distance
            position_usd_no_lev = qty * entry_price
            position_usd_with_lev = position_usd_no_lev / leverage
            rr = tp_distance / stop_distance

            fees = position_usd_no_lev * commission_rate * 2

            profit_gross = tp_distance * qty
            loss_gross = stop_distance * qty

            profit_net = profit_gross - fees
            loss_net = loss_gross + fees

            rr_good = 2.0
            rr_warning = 1.0

            if rr >= rr_good:
                verdict = "✅ Параметры сделки в норме"
                verdict_color = "#16a34a"
            elif rr >= rr_warning:
                verdict = "⚠️ R:R средний, подумай ещё раз перед входом"
                verdict_color = "#ea580c"
            else:
                verdict = "❌ R:R низкий, сделку лучше не брать"
                verdict_color = "#b91c1c"

            col_out1, col_out2 = st.columns(2)

            with col_out1:
                st.markdown(
                    f"""
                    <div style="
                        border: 2px solid #0ea5e9;
                        background-color: #e0f2fe;
                        padding: 12px 16px;
                        border-radius: 10px;
                        margin: 8px 0;
                        color: #0369a1;
                        font-weight: 500;
                        line-height: 1.5;
                    ">
                        <div style="font-size: 15px; font-weight: 700; margin-bottom: 4px;">
                            📌 Итог по сделке
                        </div>
                        <div style="font-size: 13px; margin-bottom: 6px; color: {verdict_color};">
                            {verdict}
                        </div>
                        <span style="font-size: 13px; color: #0f172a;">
                            Риск на сделку: <b>{risk_amount:.2f} USDT</b><br>
                            Кол-во монет: <b>{fmt_num(qty, sig=4)}</b><br>
                            Объём позиции без плеча(маржа): <b>{position_usd_with_lev:.2f} USDT</b><br>
                            Объём позиции с плечом x{leverage}: <b>{position_usd_no_lev:.2f} USDT</b><br>
                        </span><br>
                        <span style="font-size: 13px; color: #0f172a;">
                            R:R (TP:SL): <b>{rr:.2f} : 1</b>
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with col_out2:
                st.markdown(
                    f"""
                    <div style="
                        border: 1.5px solid #22c55e;
                        background-color: #f0fdf4;
                        padding: 12px 16px;
                        border-radius: 10px;
                        margin: 8px 0;
                        color: #166534;
                        font-weight: 500;
                        line-height: 1.5;
                    ">
                        <div style="font-size: 15px; font-weight: 700; margin-bottom: 4px;">
                            📊 PnL с учётом комиссий
                        </div>
                        <span style="font-size: 13px; color: #022c22;">
                            Комиссии (вход + выход): <b>{fees:.2f} USDT</b><br>
                            Профит по TP до комиссий: <b>{profit_gross:.2f} USDT</b><br>
                            Профит по TP после комиссий: <b>{profit_net:.2f} USDT</b><br>
                            Убыток по SL до комиссий: <b>{loss_gross:.2f} USDT</b><br>
                            Убыток по SL после комиссий: <b>{loss_net:.2f} USDT</b>
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            new_settings = {
                "balance": balance,
                "risk_percent": risk_percent,
                "leverage": leverage,
                "commission": commission_rate * 100,
                "side": side,
            }
            save_settings(new_settings)
            # обновляем сессию без перечитывания файла
            st.session_state["settings"] = new_settings
            st.caption("Настройки депозита, риска, плеча и комиссии сохранены.")

# ---------- футер ----------

st.markdown(
    """
    <div style="
        margin-top: 40px;
        padding: 12px 0;
        text-align: center;
        font-size: 12px;
        color: #9ca3af;
    ">
        Разработка: 
        <a href="https://t.me/averyanoviv" target="_blank" style="color: #60a5fa; text-decoration: none;">
            @averyanoviv
        </a>
    </div>
    """,
    unsafe_allow_html=True,
)
