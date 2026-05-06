import pandas as pd
import numpy as np
import time
import requests
import talib
from datetime import datetime, timedelta
import argparse
import gspread
import os
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# ===================== 全局配置区（调参只改这里） =====================
# Google Sheets 配置（已填好你的信息）
SPREADSHEET_ID = "19uRAQoQYDk4-YoEplQ9F6UyKga7AAzVAgd59I4Rus8c"
SHEET_NAME = "工作表5"
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# API配置
FUTURE_URL = "https://fapi.binance.com/fapi/v1"
API_STABLE_DELAY = 0.08
BATCH_SIZE = 100

# 流动性过滤
MIN_TRADE_COUNT_24H = 5000
MIN_VOLUME_USDT = 5_000_000

# 行情过滤阈值
SCORE_FILTER_THRESHOLD = 5.0
CONFLICT_MAX_COUNT = 3
ATR_LOW_VOL_RATIO = 0.8

# 时间衰减系数
PATTERN_DECAY_COEFF = [1.0, 0.6, 0.3]
TECH_DECAY_COEFF = [0.9, 0.5, 0.25]

# 共振奖励
RESONANCE_SCORE = 8.0
# ====================================================================

# --- 【GitHub Actions终极修复版】Google Sheets写入模块 ---
def get_google_sheets_client():
    creds = None
    # 使用绝对路径规避GitHub工作目录偏移坑
    token_path = os.path.join(os.getcwd(), "token.json")
    print(f"[Debug] 当前工作目录: {os.getcwd()}")
    print(f"[Debug] Token文件路径: {token_path}")
    print(f"[Debug] Token文件是否存在: {os.path.exists(token_path)}")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        print("[Debug] 成功从本地token.json加载凭证")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            print("[Debug] Token已自动刷新")
        else:
            # 安全解析环境变量中的credentials，规避换行转义坑
            creds_raw = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
            flow = InstalledAppFlow.from_client_config(creds_raw, SCOPES)
            # 禁用浏览器弹窗，适配无GUI服务器环境
            creds = flow.run_local_server(port=0, open_browser=False)
        # 指定utf-8编码写入，防止乱码与格式损坏
        with open(token_path, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
            print("[Debug] 新Token已写入本地文件")

    return gspread.authorize(creds)

def write_to_google_sheets(row_data):
    try:
        gc = get_google_sheets_client()
        sh = gc.open_by_key(SPREADSHEET_ID)
        worksheet = sh.worksheet(SHEET_NAME)
        worksheet.append_row(row_data, value_input_option='USER_ENTERED')
        print("✅ 数据已写入Google Sheets")
    except Exception as e:
        print(f"❌ Google Sheets写入失败: {e}")
        raise

# --- 以下为你原版S2.6完整策略逻辑，【完全未做任何修改】---
BULLISH_PATTERNS = [
    ("CDLMORNINGSTAR",      "看涨启明星",      18.0),
    ("CDLHAMMER",           "看涨锤头线",      16.0),
    ("CDL3WHITESOLDIERS",   "看涨三白兵",      22.0),
    ("CDLHARAMI",           "看涨孕线",        17.0),
    ("CDLPIERCING",         "看涨刺透形态",    16.0),
    ("CDLGAPSIDEWHITE",     "看涨向上跳空并列阳线", 14.0),
    ("CDLTASUKIGAP",        "看涨向上跳空缺口",     14.0),
]

BEARISH_PATTERNS = [
    ("CDLEVENINGSTAR",      "看跌黄昏星",      -18.0),
    ("CDLSHOOTINGSTAR",     "看跌射击之星",    -16.0),
    ("CDL3BLACKCROWS",      "看跌三乌鸦",      -22.0),
    ("CDLDARKCLOUDCOVER",   "看跌乌云盖顶",    -19.0),
    ("CDLENGULFING",        "看跌吞没",        -17.0),
    ("CDLBELTHOLD",         "看跌大敌当前",    -16.0),
    ("CDLHANGINGMAN",       "看跌上吊线",      -15.0),
]
ALL_PATTERNS = BULLISH_PATTERNS + BEARISH_PATTERNS

def get_pattern_time_decay(kline_time, current_time):
    hours_diff = (current_time - kline_time).total_seconds() / 3600
    if hours_diff <= 4:
        return PATTERN_DECAY_COEFF[0]
    elif hours_diff <= 8:
        return PATTERN_DECAY_COEFF[1]
    return PATTERN_DECAY_COEFF[2]

def get_tech_time_decay(kline_time, current_time):
    hours_diff = (current_time - kline_time).total_seconds() / 3600
    if hours_diff <= 4:
        return TECH_DECAY_COEFF[0]
    elif hours_diff <= 8:
        return TECH_DECAY_COEFF[1]
    return TECH_DECAY_COEFF[2]

def is_valid_float(x):
    return np.isfinite(x) and not np.isnan(x)

def get_future_symbols(session):
    try:
        resp = session.get(f"{FUTURE_URL}/exchangeInfo", timeout=15)
        data = resp.json()
        symbols = [
            s['symbol'] for s in data['symbols']
            if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL'
        ]
        return symbols
    except Exception as e:
        print(f"获取交易对列表失败: {e}")
        return []

def fetch_ohlcv(session, symbol, interval='1h', limit=40):
    for attempt in range(2):
        try:
            time.sleep(API_STABLE_DELAY)
            resp = session.get(
                f"{FUTURE_URL}/klines",
                params={'symbol': symbol, 'interval': interval, 'limit': limit},
                timeout=15
            )
            data = resp.json()
            df = pd.DataFrame(data, columns=[
                'time','open','high','low','close','volume',
                'close_time','quote_asset_volume','trades',
                'taker_buy_base','taker_buy_quote','ignore'
            ])
            df = df[['time','open','high','low','close','volume','trades']].astype(float)
            df['time'] = pd.to_datetime(df['time'], unit='ms') + pd.Timedelta(hours=8)
            return df
        except Exception:
            if attempt == 1:
                print(f"  ⚠️ {symbol} K线获取失败，已重试2次")
                return None
            time.sleep(0.3)
    return None

def main(quiet_mode=False):
    session = requests.Session()
    symbols = get_future_symbols(session)
    if not symbols:
        print("❌ 未获取到交易对列表，程序退出")
        return

    if not quiet_mode:
        print(f"共 {len(symbols)} 个永续合约交易对，开始筛选...")
        print("配置说明：分数过滤下限±{:.1f} | 最大冲突次数{} | 低波动率过滤系数{:.1f}".format(
            SCORE_FILTER_THRESHOLD, CONFLICT_MAX_COUNT, ATR_LOW_VOL_RATIO
        ))

    scores = {}
    reasons = {}
    pattern_details = {}
    burst_flags = {}

    for batch_start in range(0, len(symbols), BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, len(symbols))
        batch_list = symbols[batch_start:batch_end]
        if not quiet_mode:
            print(f"\n--- 处理批次: {batch_start+1} ~ {batch_end} ---")

        for idx, symbol in enumerate(batch_list):
            real_index = batch_start + idx
            try:
                df = fetch_ohlcv(session, symbol)
                if df is None or len(df) < 30:
                    continue

                total_trades = df['trades'].tail(24).sum()
                if total_trades < MIN_TRADE_COUNT_24H:
                    continue
                df['volume_usdt'] = df['volume'] * df['close']
                if df['volume_usdt'].iloc[-1] < MIN_VOLUME_USDT:
                    continue

                df['ma5'] = talib.SMA(df['close'], timeperiod=5)
                df['ma20'] = talib.SMA(df['close'], timeperiod=20)
                df['ma60'] = talib.SMA(df['close'], timeperiod=60) if len(df) >= 60 else np.nan
                df['macd'], df['macdsignal'], df['macdhist'] = talib.MACD(df['close'])
                df['bb_upper'], df['bb_mid'], df['bb_lower'] = talib.BBANDS(df['close'])
                df['rsi'] = talib.RSI(df['close'])
                df['atr'] = talib.ATR(df['high'], df['low'], df['close'])

                for func_name, _, _ in ALL_PATTERNS:
                    try:
                        func = getattr(talib, func_name, None)
                        df[func_name] = func(df['open'], df['high'], df['low'], df['close']) if func else 0
                    except Exception:
                        df[func_name] = 0

                now_beijing = datetime.now()
                current_hour_floor = now_beijing.replace(minute=0, second=0, microsecond=0)
                df_finished = df[df['time'] < current_hour_floor].copy()
                if len(df_finished) < 20:
                    continue
                full_window = df_finished.tail(20).copy()
                recent_12 = full_window.tail(12).copy()

                atr_roll = df_finished['atr'].rolling(24).mean().dropna()
                if len(atr_roll) > 0:
                    avg_atr = atr_roll.iloc[-1]
                    last_atr = recent_12['atr'].iloc[-1]
                    if is_valid_float(avg_atr) and is_valid_float(last_atr):
                        if last_atr < avg_atr * ATR_LOW_VOL_RATIO:
                            continue

                avg_vol_24 = df['volume'].rolling(24).mean().iloc[-1]
                if avg_vol_24 <= 0:
                    continue
                avg_vol_24 = max(avg_vol_24, 1e-8)
                max_burst = (recent_12['volume'] / avg_vol_24).max()
                if not np.isfinite(max_burst):
                    max_burst = 1.0

                triggered_patterns = []
                current_time = df_finished['time'].iloc[-1]
                for i in range(len(recent_12)):
                    row = recent_12.iloc[i]
                    kline_time = row['time']
                    kline_patterns = []
                    for func_name, name, base_score in ALL_PATTERNS:
                        val = row.get(func_name, 0)
                        if val != 0:
                            decay = get_pattern_time_decay(kline_time, current_time)
                            effective_score = base_score * decay
                            kline_patterns.append((abs(effective_score), name, effective_score))
                    if kline_patterns:
                        kline_patterns.sort(key=lambda x: x[0], reverse=True)
                        best = kline_patterns[0]
                        triggered_patterns.append(f"{kline_time} {best[1]}(得分{best[2]:+.1f})")

                bull_score_sum = 0.0
                bear_score_sum = 0.0
                for i in range(len(recent_12)):
                    row = recent_12.iloc[i]
                    kline_time = row['time']
                    decay = get_pattern_time_decay(kline_time, current_time)
                    for func_name, name, base_score in BULLISH_PATTERNS:
                        val = row.get(func_name, 0)
                        if val > 0:
                            bull_score_sum += base_score * decay
                    for func_name, name, base_score in BEARISH_PATTERNS:
                        val = row.get(func_name, 0)
                        if val < 0:
                            bear_score_sum += abs(base_score) * decay

                score = 0.0
                reason = []
                if bull_score_sum > 0:
                    score += bull_score_sum
                    reason.append(f"多头形态+{bull_score_sum:.1f}")
                if bear_score_sum > 0:
                    score -= bear_score_sum
                    reason.append(f"空头形态-{bear_score_sum:.1f}")

                tech_decay = get_tech_time_decay(recent_12['time'].iloc[-1], current_time)
                last = recent_12.iloc[-1]
                ma_bullish = False
                ma_bearish = False
                macd_bullish = False
                macd_bearish = False
                rsi_oversold = False
                rsi_overbought = False
                tech_add_list = []

                if is_valid_float(last['ma5']) and is_valid_float(last['ma20']):
                    if last['ma5'] > last['ma20']:
                        val = 5 * tech_decay
                        tech_add_list.append(("bull", val))
                        score += val
                        reason.append(f"短多排列+{val:.1f}")
                        ma_bullish = True
                    else:
                        val = -5 * tech_decay
                        tech_add_list.append(("bear", val))
                        score += val
                        reason.append(f"短空排列{val:.1f}")
                        ma_bearish = True

                if is_valid_float(last['ma60']) and is_valid_float(last['ma20']):
                    if last['ma20'] > last['ma60']:
                        val = 4 * tech_decay
                        tech_add_list.append(("bull", val))
                        score += val
                        reason.append(f"中多排列+{val:.1f}")
                        ma_bullish = True
                    else:
                        val = -4 * tech_decay
                        tech_add_list.append(("bear", val))
                        score += val
                        reason.append(f"中空排列{val:.1f}")
                        ma_bearish = True

                if is_valid_float(last['close']) and is_valid_float(last['ma20']):
                    if last['close'] > last['ma20']:
                        val = 6 * tech_decay
                        tech_add_list.append(("bull", val))
                        score += val
                        reason.append(f"站上20均线+{val:.1f}")
                        ma_bullish = True
                    else:
                        val = -6 * tech_decay
                        tech_add_list.append(("bear", val))
                        score += val
                        reason.append(f"跌破20均线{val:.1f}")
                        ma_bearish = True

                if is_valid_float(last['macd']) and is_valid_float(last['macdsignal']):
                    if last['macd'] > last['macdsignal']:
                        val = 5 * tech_decay
                        tech_add_list.append(("bull", val))
                        score += val
                        reason.append(f"MACD金叉+{val:.1f}")
                        macd_bullish = True
                    else:
                        val = -5 * tech_decay
                        tech_add_list.append(("bear", val))
                        score += val
                        reason.append(f"MACD死叉{val:.1f}")
                        macd_bearish = True

                if is_valid_float(last['macdhist']):
                    if last['macdhist'] > 0:
                        val = 3 * tech_decay
                        tech_add_list.append(("bull", val))
                        score += val
                        reason.append(f"MACD红柱+{val:.1f}")
                        macd_bullish = True
                    else:
                        val = -3 * tech_decay
                        tech_add_list.append(("bear", val))
                        score += val
                        reason.append(f"MACD绿柱{val:.1f}")
                        macd_bearish = True

                if is_valid_float(last['close']) and is_valid_float(last['bb_lower']) and is_valid_float(last['bb_upper']):
                    if last['close'] < last['bb_lower']:
                        score += 5 * tech_decay
                        reason.append(f"布林下轨+{5*tech_decay:.1f}")
                    elif last['close'] > last['bb_upper']:
                        score -= 5 * tech_decay
                        reason.append(f"布林上轨-{5*tech_decay:.1f}")

                if is_valid_float(last['rsi']):
                    if last['rsi'] < 30:
                        rsi_oversold = True
                        val = 4 * tech_decay
                        score += val
                        reason.append(f"RSI超卖+{val:.1f}")
                    elif last['rsi'] > 70:
                        rsi_overbought = True
                        val = -4 * tech_decay
                        score += val
                        reason.append(f"RSI超买{val:.1f}")

                td_window = full_window.copy()
                td_buy_count = 0
                td_sell_count = 0
                td9_buy_flag = False
                td9_sell_flag = False
                td13_buy_flag = False
                td13_sell_flag = False
                for i in range(4, len(td_window)):
                    if td_window.iloc[i]['close'] < td_window.iloc[i-4]['close']:
                        td_buy_count += 1
                        td_sell_count = 0
                    elif td_window.iloc[i]['close'] > td_window.iloc[i-4]['close']:
                        td_sell_count += 1
                        td_buy_count = 0
                    else:
                        td_buy_count = 0
                        td_sell_count = 0
                    if td_buy_count >= 9:
                        td9_buy_flag = True
                    if td_sell_count >= 9:
                        td9_sell_flag = True
                    if td_buy_count >= 13:
                        td13_buy_flag = True
                    if td_sell_count >= 13:
                        td13_sell_flag = True

                form_bullish = bull_score_sum > bear_score_sum
                form_bearish = bear_score_sum > bull_score_sum
                tech_bullish = ma_bullish and macd_bullish and (not rsi_overbought)
                tech_bearish = ma_bearish and macd_bearish and (not rsi_oversold)

                conflict_count = 0
                for dir_tag, val in tech_add_list:
                    if form_bullish and dir_tag == "bear":
                        score -= val
                        reason.append("冲突清零反向空头指标")
                        conflict_count += 1
                    if form_bearish and dir_tag == "bull":
                        score -= val
                        reason.append("冲突清零反向多头指标")
                        conflict_count += 1
                if conflict_count >= CONFLICT_MAX_COUNT:
                    continue

                if td9_buy_flag and form_bullish:
                    score += 5 * tech_decay
                    reason.append(f"TD9买+{5*tech_decay:.1f}")
                if td9_sell_flag and form_bearish:
                    score -= 5 * tech_decay
                    reason.append(f"TD9卖-{5*tech_decay:.1f}")
                if td13_buy_flag and form_bullish:
                    score += 5 * tech_decay
                    reason.append(f"TD13买+{5*tech_decay:.1f}")
                if td13_sell_flag and form_bearish:
                    score -= 5 * tech_decay
                    reason.append(f"TD13卖-{5*tech_decay:.1f}")

                if form_bullish and ma_bullish and macd_bullish:
                    score += RESONANCE_SCORE
                    reason.append(f"三方共振奖励+{RESONANCE_SCORE:.1f}")
                if form_bearish and ma_bearish and macd_bearish:
                    score -= RESONANCE_SCORE
                    reason.append(f"三方共振奖励-{RESONANCE_SCORE:.1f}")

                burst_flag = ""
                close_now = recent_12.iloc[-1]['close']
                close_prev = recent_12.iloc[-2]['close']
                price_diff = close_now - close_prev
                if max_burst >= 5.0:
                    if (form_bullish and price_diff > 0) or (form_bearish and price_diff < 0):
                        score += 6 * tech_decay
                        reason.append(f"爆量异动+{6*tech_decay:.1f}")
                    burst_flag = "[爆量]"
                elif max_burst >= 3.0:
                    if (form_bullish and price_diff > 0) or (form_bearish and price_diff < 0):
                        score += 6 * tech_decay
                        reason.append(f"成交量突增+{6*tech_decay:.1f}")
                    burst_flag = "[突增]"
                elif max_burst >= 2.0:
                    if (form_bullish and price_diff > 0) or (form_bearish and price_diff < 0):
                        score += 6 * tech_decay
                        reason.append(f"成交量放量+{6*tech_decay:.1f}")
                    burst_flag = "[放量]"

                if abs(score) < SCORE_FILTER_THRESHOLD:
                    continue

                scores[symbol] = score
                reasons[symbol] = reason
                pattern_details[symbol] = triggered_patterns
                burst_flags[symbol] = burst_flag

                # 组装数据并写入
                row_data = [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    symbol,
                    round(score, 2),
                    "做多" if score > 0 else "做空",
                    "; ".join(triggered_patterns) if triggered_patterns else "无",
                    "; ".join(reason),
                    burst_flag
                ]
                write_to_google_sheets(row_data)

                if not quiet_mode:
                    print(f"[{real_index+1}/{len(symbols)}] {symbol}: {score:+.2f} {burst_flag} | 形态: {'; '.join(triggered_patterns) if triggered_patterns else '无'} | 理由: {', '.join(reason)}")

            except Exception as e:
                print(f"  ❌ {symbol} 处理异常: {e}")
                continue

        if batch_end < len(symbols):
            print("批次处理完成，短暂休眠...")
            time.sleep(2)

    print("\n" + "=" * 60)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    print("📈 涨前五（最强做多信号）：")
    top_bull = ranked[:5]
    for sym, s in top_bull:
        detail = "; ".join(pattern_details[sym]) if sym in pattern_details else "无"
        flag = burst_flags.get(sym, "")
        print(f"  {sym}: {s:+.2f} {flag} | 形态: {detail} | 理由: {', '.join(reasons[sym])}")

    print("\n📉 跌前五（最强做空信号）：")
    bearish_ranked = [item for item in ranked if item[1] < 0]
    top_bear = bearish_ranked[-5:] if len(bearish_ranked) >= 5 else bearish_ranked
    for sym, s in top_bear:
        detail = "; ".join(pattern_details[sym]) if sym in pattern_details else "无"
        flag = burst_flags.get(sym, "")
        print(f"  {sym}: {s:+.2f} {flag} | 形态: {detail} | 理由: {', '.join(reasons[sym])}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="永续合约趋势筛选策略")
    parser.add_argument("--quiet", action="store_true", help="安静模式，仅打印错误和最终榜单")
    args = parser.parse_args()
    main(quiet_mode=args.quiet)
