import streamlit as st
import pandas as pd
import ccxt
import json
import os
# ---------- кешируем тяжелые операции ----------

@st.cache_resource(show_spinner=False)
def get_exchange():
    # создаём клиент BYDFi один раз на процесс
    return ccxt.bydfi({"enableRateLimit": True})

@st.cache_data(ttl=300, show_spinner=False)  # кеш 5 минут
def get_markets():
    exchange = get_exchange()
    return exchange.load_markets()

@st.cache_data(ttl=60, show_spinner=False)  # кеш 60 секунд на тикер
def get_ticker_and_ohlcv(matched_symbol: str):
    exchange = get_exchange()
    ticker = exchange.fetch_ticker(matched_symbol)
    ohlcv = exchange.fetch_ohlcv(matched_symbol, timeframe="4h", limit=30)
    return ticker, ohlcv
    
# ---------- сохранение настроек ----------

SETTINGS_FILE = "settings.json"


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass


@st.cache_data(show_spinner=False)
def load_settings_cached() -> dict:
    # обёртка над load_settings, чтобы не читать файл при каждом ререндере
    return load_settings()


# используем кешированный вариант
settings = load_settings_cached()

# ---------- заголовок ----------

st.title("🧮 Калькулятор объема позиции для трейдинга")
st.markdown("Заполни параметры сделки, выбери риск и плечо — я посчитаю объем, количество монет и R:R.")

# ---------- 1. Аналитика фьючерса: ATR и стоп 10% ATR ----------

st.markdown("---")
st.subheader("📊 Аналитика фьючерса и рекомендуемый стоп 10% ATR (BYDFi USDT-M Perpetual)")

fut_symbol_input = st.text_input("Фьючерсный тикер (например BTCUSDT, ETHUSDT)", value="BTCUSDT")

if "rec_stop_distance" not in st.session_state:
    st.session_state["rec_stop_distance"] = None

show_analysis = st.checkbox("Показать аналитику фьючерса и стоп 10% ATR", value=False)
        
if show_analysis:
    try:
        # рынки берём из кеша
        markets = get_markets()

        # приводим пользовательский ввод к формату BASE+QUOTE (BTCUSDT, ETHUSDT и т.п.)
        user_raw = fut_symbol_input.upper().replace("PERP", "").strip()

        # быстрый маппинг популярных тикеров на BYDFi-формат
        direct_map = {
            "BTCUSDT": "BTC/USDT:USDT",
            "ETHUSDT": "ETH/USDT:USDT",
        }

        if user_raw in direct_map:
            matched_symbol = direct_map[user_raw]
        else:
            matched_symbol = None

            # если не нашли в direct_map — пробуем искать по markets
            for m_symbol, m_info in markets.items():
                base = m_info.get("base", "")
                quote = m_info.get("quote", "")
                compact = f"{base}{quote}".upper()
                if compact == user_raw:
                    matched_symbol = m_symbol
                    break

            # если по compact не нашли, пробуем прямым совпадением по ключу
            if matched_symbol is None:
                for m_symbol in markets.keys():
                    if m_symbol.replace("-", "").replace("/", "").replace(":", "").upper() == user_raw:
                        matched_symbol = m_symbol
                        break

        if matched_symbol is None:
            st.error(f"Фьючерсный тикер не найден на BYDFi: **{user_raw}**.")
        else:
            # 1) тикер и 4h свечи берём из кешируемой функции
            try:
                ticker, ohlcv = get_ticker_and_ohlcv(matched_symbol)
            except Exception as e_4h:
                st.error(
                    f"Не удалось получить данные по {matched_symbol} на BYDFi.\n\n"
                    f"Ошибка: {e_4h}"
                )
                ohlcv = None
                ticker = None

            if ticker is None:
                st.stop()

            last_price = ticker["last"]

            if not ohlcv or len(ohlcv) < 30:
                st.error("Недостаточно 4h свечей для расчёта дневного ATR (нужно 30).")
                st.stop()

            # переводим в DataFrame
            df_4h = pd.DataFrame(
                ohlcv,
                columns=["time", "open", "high", "low", "close", "volume"]
            )

            st.caption(f"DEBUG: получено {len(df_4h)} 4h свечей (ожидаем 30)")

            # 3) группируем по 6 4h свечей в один "день"
            days = []
            n = len(df_4h)
            # берём последние 30 свечей, кратно 6
            start_idx = n - (n // 6) * 6
            chunked = df_4h.iloc[start_idx:]

            # идём по блокам 6 свечей от старых к новым
            for i in range(0, len(chunked), 6):
                block = chunked.iloc[i:i+6]
                if len(block) < 6:
                    continue
                day_open = block["open"].iloc[0]
                day_close = block["close"].iloc[-1]
                day_high = block["high"].max()
                day_low = block["low"].min()
                days.append({
                    "open": day_open,
                    "high": day_high,
                    "low": day_low,
                    "close": day_close,
                })

            # берём последние 5 "дней"
            days = days[-5:]
            df_days = pd.DataFrame(days)

            if len(df_days) < 5:
                st.error("Недостаточно дневных баров для расчёта ATR(5).")
                st.stop()

            # 4) считаем ATR(5) по этим 5 "дням"
            df_days["prev_close"] = df_days["close"].shift(1)
            df_days["tr1"] = df_days["high"] - df_days["low"]
            df_days["tr2"] = (df_days["high"] - df_days["prev_close"]).abs()
            df_days["tr3"] = (df_days["low"] - df_days["prev_close"]).abs()
            df_days["tr"] = df_days[["tr1", "tr2", "tr3"]].max(axis=1)
            atr = df_days["tr"].rolling(window=5).mean().iloc[-1]

            if pd.isna(atr) or atr <= 0:
                st.error("Не удалось корректно посчитать ATR(5) по 5 дневным барам.")
            else:
                atr_10 = atr * 0.10           # 10% ATR
                max_luft = atr_10 * 0.10      # 10% от рекомендуемого стопа = 1% ATR

                # дневной диапазон в % для рекомендации плеча
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

                st.write(f"Найденный фьючерсный символ на BYDFi: **{matched_symbol}**")
                st.write(f"Текущая цена: **{last_price:.4f} USDT**")
                st.write(f"ATR(5): **{atr:.4f} USDT**")

                # твои рамки
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
                        Максимальный люфт от уровня: {max_luft:.4f} USDT (10% от рекомендуемого стопа)
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
                        Рекомендуемый размер стопа: 10% ATR = {atr_10:.4f} USDT
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                st.success(f"Условно рекомендуемое плечо по волатильности: **x{rec_leverage}**")

                st.session_state["rec_stop_distance"] = float(atr_10)
                st.caption("Расстояние стопа 10% ATR сохранено и используется как подсказка в поле SL.")
    except Exception as e:
        st.error(f"Ошибка при запросе к BYDFi: {e}")

# ---------- 2. Риск и депозит ----------

st.subheader("1️⃣ Риск и депозит")

col_r1, col_r2 = st.columns([2, 1])

default_balance = float(settings.get("balance", 1000.0))
default_saved_risk = float(settings.get("risk_percent", 1.0))

with col_r1:
    balance = st.number_input("💰 Депозит, USDT", value=default_balance, min_value=0.0, step=100.0)

with col_r2:
    # убрали режим кнопок, оставляем только ручной ввод риска
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
default_sl = 95.0
default_tp = 110.0

col_p1, col_p2, col_p3 = st.columns(3)
col_extra1, col_extra2 = st.columns(2)

default_side = settings.get("side", "Лонг")
default_leverage = int(settings.get("leverage", 10))

with col_extra1:
    side = st.radio("Направление сделки", ["Лонг", "Шорт"], index=0 if default_side == "Лонг" else 1)
with col_extra2:
    leverage = st.number_input("🔧 Плечо (leverage)", value=default_leverage, min_value=1, max_value=200, step=1)

with col_p1:
    entry_price = st.number_input("📈 Цена входа (Entry)", value=default_entry, min_value=0.0001)

with col_p2:
    rec_stop_distance = st.session_state.get("rec_stop_distance", None)

    if rec_stop_distance and entry_price > 0:
        if side == "Лонг":
            suggested_sl = entry_price - rec_stop_distance
        else:
            suggested_sl = entry_price + rec_stop_distance
    else:
        suggested_sl = default_sl

    if suggested_sl <= 0:
        suggested_sl = 0.0001

    stop_price = st.number_input(
        "🛑 Стоп-лосс (SL)",
        value=float(suggested_sl),
        min_value=0.0001,
        help="Если ниже считали ATR, сюда подставлен стоп по 10% ATR, можно скорректировать."
    )

with col_p3:
    tp_price = st.number_input("🎯 Тейк-профит (TP)", value=default_tp, min_value=0.0001)

# ---------- 4. Комиссии ----------

st.subheader("3️⃣ Комиссии (можно оставить по умолчанию)")
default_fee = float(settings.get("taker_fee", 0.06))
taker_fee = st.number_input("Комиссия (taker), % за сделку", value=default_fee, min_value=0.0, max_value=1.0, step=0.01)
taker_fee /= 100.0

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
            position_usd_with_lev = position_usd_no_lev * leverage
            rr = tp_distance / stop_distance

            fees = position_usd_with_lev * taker_fee * 2

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
                            Кол-во монет: <b>{qty:.4f}</b><br>
                            Объём позиции без плеча: <b>{position_usd_no_lev:.2f} USDT</b><br>
                            <span style="text-decoration: underline; text-decoration-color: red; text-decoration-thickness: 2px;">
                                Объём позиции c плечом x{leverage}: <b>{position_usd_with_lev:.2f} USDT</b>
                            </span><br>
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
                "taker_fee": taker_fee * 100,
                "side": side,
            }
            save_settings(new_settings)
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
        Разработка: Ivan Averyanov
    </div>
    """,
    unsafe_allow_html=True,
)
