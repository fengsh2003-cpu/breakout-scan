#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出量突破掃描器（日終版）— 台股全市場。

定位
----
對照坊間「出量突破」盤中選股訊號服務，本工具是**日終（收盤後）版本**：
只用官方公開的每日收盤行情，不含盤中即時報價、不含逐筆成交明細，
因此**無法**產生「25張以上大單買超」「買賣成交比」「大單區間」這類
需要 tick 資料的欄位——那需要券商 API（如永豐 Shioaji）。
本工具不重製任何第三方服務的私有演算法，所有規則見下方常數，可重算。

資料源（皆為官方公開端點，非爬蟲頁面）
------------------------------------
- TWSE 上市：MI_INDEX?type=ALLBUT0999，**單次請求回傳全市場約 1,377 檔**
- TPEx 上櫃：afterTrading/otc，**單次請求回傳全市場約 1,014 檔**
兩者皆支援指定日期查詢歷史，故建立 N 日歷史只需 2N 次請求，
且**完全不消耗 FinMind 配額**（FinMind 的 TaiwanStockPrice 不支援
市場級查詢，實測無 data_id 會回 HTTP 400）。

相依性：**只用 Python 標準庫**，不 import stock_cache、不需 FinMind token、
不需 pandas。這是刻意的設計——本檔要能逐字複製到 GitHub Actions 的雲端 repo
執行，避免像 disposal 模組那樣得人工維護兩份會漂移的程式碼。

快取
----
data/market/<YYYY-MM-DD>.json，每個交易日一檔，已存在則不重抓。

指令
----
    python breakout_scan.py backfill [days=90]   建立/補齊全市場日K快取
    python breakout_scan.py scan [date]          掃描並列印當日訊號
    python breakout_scan.py chips [days=8]       補齊全市場籌碼（融資券＋法人）
    python breakout_scan.py export [date]        輸出行動網頁用 JSON
    python breakout_scan.py prune [keep=70]      刪除過舊的日快取（雲端用）
"""
import csv
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
MARKET_DIR = DATA_DIR / "market"
CHIP_DIR = DATA_DIR / "chips"

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"


def export_path():
    """輸出位置：本專案在 mobile-app/breakout/www，雲端 repo 在 www。"""
    for rel in ("mobile-app/breakout/www", "www"):
        p = BASE / rel
        if p.is_dir():
            return p / "breakout_export.json"
    return BASE / "breakout_export.json"


def fetch_json_url(url, params=None, retries=3, label=None):
    """通用 JSON GET，網路錯誤指數退避重試。風格對齊本專案既有 fetch_json_url。"""
    full = url + ("?" + urlencode(params) if params else "")
    label = label or url
    for attempt in range(retries + 1):
        try:
            req = Request(full, headers={"User-Agent": "Mozilla/5.0 (stock-project)"})
            with urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"{label} 抓取失敗: {exc}") from exc
            wait = 2 * 2 ** attempt
            print(f"{label}: 網路錯誤（{exc}），{wait}秒後重試 {attempt + 1}/{retries}")
            time.sleep(wait)
    raise RuntimeError(f"{label} 抓取失敗（已重試 {retries} 次）")


def roc_to_iso(s_):
    """民國日期（'115/09/01'／'1150901'）轉西元 'YYYY-MM-DD'；格式不符原樣傳回。"""
    digits = "".join(ch for ch in str(s_) if ch.isdigit())
    if len(digits) != 7:
        return str(s_)
    return f"{int(digits[:3]) + 1911}-{digits[3:5]}-{digits[5:7]}"


def wilder_atr(highs, lows, closes, n=14):
    """Wilder ATR(n)，純 Python。回傳與輸入等長的 list，暖身期為 NaN。

    公式與本專案 stock_cache.wilder_atr 相同（TR 取三者最大、首值簡單平均、
    其後 Wilder 遞推），此處重寫是為了讓本檔不依賴 stock_cache。
    """
    nan = float("nan")
    size = len(closes)
    if size <= n:
        return [nan] * size
    trs = [nan]
    for i in range(1, size):
        pc = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    out = [nan] * size
    atr = sum(trs[1:n + 1]) / n
    out[n] = atr
    for i in range(n + 1, size):
        atr = (atr * (n - 1) + trs[i]) / n
        out[i] = atr
    return out


_CODE_MAP = None


def code_map():
    """代號→產業別對照，來源為 data/*_equities.csv（缺檔則降級為空 dict）。"""
    global _CODE_MAP
    if _CODE_MAP is not None:
        return _CODE_MAP
    m = {}
    for fn in ("twse_equities.csv", "tpex_equities.csv"):
        path = DATA_DIR / fn
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    code = (r.get("code") or "").strip()
                    if code and code not in m:
                        m[code] = {"name": (r.get("name") or "").strip(),
                                   "group": (r.get("group") or "").strip()}
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            print(f"代號對照表 {fn} 讀取失敗（僅影響產業別顯示）：{exc}")
    _CODE_MAP = m
    return m

# ---------------------------------------------------------------------------
# 篩選規則（全部可重算；調整時同步更新 RULES_VERSION 與行動網頁的說明段落）
# ---------------------------------------------------------------------------
RULES_VERSION = "v1 (2026-09-02)"  # v1：新增紅點（漲停鎖死）與全市場籌碼欄位
BREAKOUT_LOOKBACK = 20   # 突破基準：前 N 日最高「收盤」（不含當日）
VOL_MULT = 2.0           # 出量：當日量 >= 前 5 日均量 × 此倍數（均量不含當日）
VOL_BASE_DAYS = 5
MIN_CHANGE_PCT = 3.0     # 當日漲幅下限（%）
MIN_AVG_LOTS = 500       # 前 5 日均量下限（張），濾掉流動性不足者
MIN_CLOSE = 10.0         # 收盤價下限（元），濾掉雞蛋水餃股
ATR_N = 14
MIN_BARS = ATR_N + BREAKOUT_LOOKBACK + 2  # 計算所需最少根數

# --- 紅點（漲停鎖死 → 隔日衝候選）---
# 「尾盤鎖漲停」在日 K 的指紋：收盤 = 漲停價 且 收盤 = 當日最高。
# 「鎖得多死」用「當日均價 ÷ 漲停價」當代理——均價越貼近漲停，代表整天大部分
# 成交都在漲停價附近、盤中幾乎沒打開。這是日終資料能做到的最接近版本，
# **不等於**真的確認全日未打開（那需要盤中逐筆資料）。
RED_MIN_VWAP_RATIO = 0.985   # 均價 / 漲停價 下限
RED_MIN_AVG_LOTS = 200       # 隔日衝標的流動性門檻可低於波段
RED_MIN_CLOSE = 10.0
# 漲停但量縮多半是「惜售型」——沒人賣所以漲停，而不是主力用大量把賣單吃光
# 鎖上去。隔日衝要的是後者，故要求當日量至少不低於前 5 日均量。
# 與其他門檻同樣是初始設定值，未經回測校準。
RED_MIN_VOL_RATIO = 1.0
PRICE_EPS = 1e-6             # 價格比較容差（台股最小跳動 0.01，遠大於此）


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------
def _num(x):
    """把 '1,234.5' / '--' / '' / None 轉成 float；無法解析回傳 None。

    刻意不回傳 0——0 與「沒有成交」意義不同，混淆會讓量能倍數算錯。
    """
    if x is None:
        return None
    t = str(x).replace(",", "").replace("+", "").strip()
    if t in ("", "--", "---", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _is_common_share(code):
    """只保留普通股：4 碼純數字。

    自然排除 ETF（00xxx，5~6 碼）、特別股（如 2881A）、TDR（91xxxx）、
    權證（6 碼）與上櫃的次順位代號（如 31672）。
    """
    return len(code) == 4 and code.isdigit()


def tick_size(price):
    """台股股票最小升降單位（依價格級距）。"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def limit_up_price(prev_close):
    """由前一日收盤推算漲停價：前收 × 1.1，再向下取到該價位級距的合法檔位。

    級距由「取整後的價格」本身決定，故先以未取整價估級距、取整後再驗一次，
    避免落在 10/50/100/500/1000 邊界時用錯級距。
    """
    if not prev_close or prev_close <= 0:
        return None
    raw = prev_close * 1.1
    for _ in range(2):
        t = tick_size(raw)
        snapped = int(raw / t + PRICE_EPS) * t
        if abs(tick_size(snapped) - t) < PRICE_EPS:
            return round(snapped, 2)
        raw = snapped
    return round(snapped, 2)


def _pick_table(payload, must_have):
    """從 TWSE/TPEx 多表格回應中挑出欄位包含 must_have 全部字串的那張表。

    不用固定索引，避免官方調整表格順序就整個壞掉。
    """
    for t in payload.get("tables", []):
        fields = [str(f).strip() for f in (t.get("fields") or [])]
        joined = "|".join(fields)
        if all(k in joined for k in must_have):
            return t
    return None


def _field_index(fields, *keys):
    """依欄位名稱（去空白、去 HTML 換行）找索引，找不到回傳 None。"""
    norm = [str(f).replace("<br>", "").replace(" ", "").strip() for f in fields]
    for k in keys:
        for i, f in enumerate(norm):
            if f == k:
                return i
    for k in keys:  # 退而求其次用包含比對
        for i, f in enumerate(norm):
            if k in f:
                return i
    return None


# ---------------------------------------------------------------------------
# 全市場單日行情抓取
# ---------------------------------------------------------------------------
def _rows_from(data, idx, market):
    out = []
    for r in data:
        try:
            code = str(r[idx["code"]]).strip()
        except (IndexError, TypeError):
            continue
        if not _is_common_share(code):
            continue
        close = _num(r[idx["close"]])
        vol = _num(r[idx["vol"]])
        if close is None or not vol:  # 無成交（停牌／全日無量）不納入
            continue
        out.append({
            "id": code,
            "name": str(r[idx["name"]]).strip(),
            "market": market,
            "open": _num(r[idx["open"]]),
            "high": _num(r[idx["high"]]),
            "low": _num(r[idx["low"]]),
            "close": close,
            "vol": vol,                        # 股
            "amount": _num(r[idx["amount"]]),  # 元
        })
    return out


def fetch_twse_day(iso_date):
    """上市全市場單日行情。回傳 list[dict]；當日非交易日回傳空 list。"""
    payload = fetch_json_url(
        TWSE_URL,
        {"date": iso_date.replace("-", ""), "type": "ALLBUT0999", "response": "json"},
        label=f"TWSE/MI_INDEX {iso_date}",
    )
    if str(payload.get("stat", "")).upper() != "OK":
        return []
    table = _pick_table(payload, ["證券代號", "收盤價", "成交股數"])
    if not table:
        return []
    f = table.get("fields") or []
    idx = {
        "code": _field_index(f, "證券代號"),
        "name": _field_index(f, "證券名稱"),
        "vol": _field_index(f, "成交股數"),
        "amount": _field_index(f, "成交金額"),
        "open": _field_index(f, "開盤價"),
        "high": _field_index(f, "最高價"),
        "low": _field_index(f, "最低價"),
        "close": _field_index(f, "收盤價"),
    }
    if any(v is None for v in idx.values()):
        raise RuntimeError(f"TWSE 欄位對應失敗 {iso_date}: {f}")
    return _rows_from(table.get("data") or [], idx, "TWSE")


def fetch_tpex_day(iso_date):
    """上櫃全市場單日行情。回傳 list[dict]；當日非交易日回傳空 list。"""
    y, m, d = iso_date.split("-")
    roc = f"{int(y) - 1911}/{m}/{d}"
    payload = fetch_json_url(
        TPEX_URL, {"date": roc, "type": "EW", "id": "", "response": "json"},
        label=f"TPEx/otc {iso_date}",
    )
    table = _pick_table(payload, ["代號", "收盤", "成交股數"])
    if not table or not table.get("data"):
        return []
    # 官方對非交易日會退回鄰近交易日的資料，日期不符一律視為當日無資料
    got = roc_to_iso(table.get("date") or payload.get("date") or "")
    if got and got != iso_date:
        return []
    f = table.get("fields") or []
    idx = {
        "code": _field_index(f, "代號"),
        "name": _field_index(f, "名稱"),
        "vol": _field_index(f, "成交股數"),
        "amount": _field_index(f, "成交金額(元)", "成交金額"),
        "open": _field_index(f, "開盤"),
        "high": _field_index(f, "最高"),
        "low": _field_index(f, "最低"),
        "close": _field_index(f, "收盤"),
    }
    if any(v is None for v in idx.values()):
        raise RuntimeError(f"TPEx 欄位對應失敗 {iso_date}: {f}")
    return _rows_from(table.get("data") or [], idx, "TPEx")


# ---------------------------------------------------------------------------
# 全市場籌碼（融資融券、三大法人）— 同樣是「1 次請求拿整個市場」的官方端點
#
# 刻意不走 FinMind 的個股 dataset：那是每檔一次請求，數十檔命中就要數十次；
# 這裡兩個市場各 2 次請求就涵蓋全市場，且不需要 token。
# ---------------------------------------------------------------------------
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TWSE_INST_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
TPEX_INST_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"


def fetch_margin_day(iso_date):
    """全市場融資融券餘額。回傳 {id: {margin, limit, short}}，單位皆為「張」。"""
    out = {}
    # --- 上市 ---
    try:
        p = fetch_json_url(TWSE_MARGIN_URL,
                           {"date": iso_date.replace("-", ""), "selectType": "ALL",
                            "response": "json"}, label=f"TWSE/融資券 {iso_date}")
        if str(p.get("stat", "")).upper() == "OK":
            t = _pick_table(p, ["代號", "次一營業日限額"])
            for r in (t or {}).get("data", []):
                code = str(r[0]).strip()
                if not _is_common_share(code):
                    continue
                # 欄位順序：融資(買進,賣出,現金償還,前日餘額,今日餘額,限額)=2..7，
                # 融券同結構=8..13。官方以區段重複相同欄名，只能依位置取值。
                out[code] = {"margin": _num(r[6]), "limit": _num(r[7]),
                             "short": _num(r[12])}
    except Exception as exc:
        print(f"{iso_date} TWSE 融資券抓取失敗：{exc}")
    # --- 上櫃 ---
    try:
        y, m, d = iso_date.split("-")
        p = fetch_json_url(TPEX_MARGIN_URL,
                           {"date": f"{int(y) - 1911}/{m}/{d}", "response": "json"},
                           label=f"TPEx/融資券 {iso_date}")
        t = _pick_table(p, ["代號", "資餘額", "資限額"])
        f = (t or {}).get("fields") or []
        i_m = _field_index(f, "資餘額")
        i_l = _field_index(f, "資限額")
        i_s = _field_index(f, "券餘額")
        if t and None not in (i_m, i_l, i_s):
            for r in t.get("data", []):
                code = str(r[0]).strip()
                if _is_common_share(code):
                    out[code] = {"margin": _num(r[i_m]), "limit": _num(r[i_l]),
                                 "short": _num(r[i_s])}
    except Exception as exc:
        print(f"{iso_date} TPEx 融資券抓取失敗：{exc}")
    return out


def fetch_inst_day(iso_date):
    """全市場三大法人買賣超。回傳 {id: 淨買賣超股數}。

    直接取官方already-aggregated 的「三大法人買賣超股數」欄，不自行加總分項，
    避免重蹈本專案早期「外資是否含外資自營」那類定義分歧。
    """
    out = {}
    try:
        p = fetch_json_url(TWSE_INST_URL,
                           {"date": iso_date.replace("-", ""), "selectType": "ALL",
                            "response": "json"}, label=f"TWSE/法人 {iso_date}")
        if str(p.get("stat", "")).upper() == "OK":
            f = p.get("fields") or []
            i_n = _field_index(f, "三大法人買賣超股數")
            if i_n is not None:
                for r in p.get("data", []):
                    code = str(r[0]).strip()
                    if _is_common_share(code):
                        out[code] = _num(r[i_n])
    except Exception as exc:
        print(f"{iso_date} TWSE 法人抓取失敗：{exc}")
    try:
        y, m, d = iso_date.split("-")
        p = fetch_json_url(TPEX_INST_URL,
                           {"type": "Daily", "sect": "EW",
                            "date": f"{int(y) - 1911}/{m}/{d}", "response": "json"},
                           label=f"TPEx/法人 {iso_date}")
        t = _pick_table(p, ["代號", "三大法人買賣超股數合計"])
        f = (t or {}).get("fields") or []
        i_n = _field_index(f, "三大法人買賣超股數合計")
        if t and i_n is not None:
            for r in t.get("data", []):
                code = str(r[0]).strip()
                if _is_common_share(code):
                    out[code] = _num(r[i_n])
    except Exception as exc:
        print(f"{iso_date} TPEx 法人抓取失敗：{exc}")
    return out


def chip_cache_path(iso_date):
    return CHIP_DIR / f"{iso_date}.json"


def load_chip_day(iso_date, fetch=True):
    """讀取單日全市場籌碼；快取優先。回傳 {"margin": {...}, "inst": {...}}。"""
    path = chip_cache_path(iso_date)
    if path.exists():
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{iso_date} 籌碼快取毀損，重抓：{exc}")
    if not fetch:
        return {"margin": {}, "inst": {}}
    data = {"margin": fetch_margin_day(iso_date), "inst": fetch_inst_day(iso_date)}
    # 融資券的發布時間晚於收盤行情，當日盤後早一點抓會拿到空表。空表**不寫入
    # 快取**，否則會被永久記住、之後再也不會重抓（行情快取可以接受空值，因為
    # 那代表非交易日；籌碼的空值則多半只是還沒發布）。
    if not data["margin"] or not data["inst"]:
        print(f"{iso_date} 籌碼尚未齊全（融資券 {len(data['margin'])} 檔、"
              f"法人 {len(data['inst'])} 檔），不寫入快取，稍後可重抓")
        return data
    CHIP_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return data


def market_cache_path(iso_date):
    return MARKET_DIR / f"{iso_date}.json"


def load_market_day(iso_date, fetch=True):
    """讀取單日全市場行情；快取優先，必要時抓取後寫入快取。

    回傳 dict[stock_id] = row。該日無資料（假日）也會寫入空快取，
    避免每次重跑都對非交易日重複打兩次 API。
    """
    path = market_cache_path(iso_date)
    if path.exists():
        try:
            with path.open(encoding="utf-8") as fh:
                return {r["id"]: r for r in json.load(fh).get("rows", [])}
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            print(f"{iso_date} 快取毀損，重抓：{exc}")
    if not fetch:
        return {}
    rows = []
    for fn, label in ((fetch_twse_day, "TWSE"), (fetch_tpex_day, "TPEx")):
        try:
            rows.extend(fn(iso_date))
        except Exception as exc:  # 單一市場失敗不拖垮另一個，誠實記錄
            print(f"{iso_date} {label} 抓取失敗：{exc}")
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump({"date": iso_date, "rows": rows}, fh, ensure_ascii=False)
    return {r["id"]: r for r in rows}


def cached_dates():
    if not MARKET_DIR.exists():
        return []
    return sorted(p.stem for p in MARKET_DIR.glob("*.json"))


def cmd_backfill(days="90"):
    """補齊近 N 個日曆日的全市場日 K 快取。可中斷續跑，已快取者略過。"""
    n = int(days)
    today = date.today()
    todo = [(today - timedelta(days=i)).isoformat() for i in range(n)]
    todo = [d for d in reversed(todo) if not market_cache_path(d).exists()]
    # 只跳過週末（不查交易日曆，以維持零外部相依）。國定假日會抓到空資料，
    # 但空結果同樣寫入快取，因此每個假日最多只浪費一次請求。
    todo = [d for d in todo if date.fromisoformat(d).weekday() < 5]
    print(f"待抓取 {len(todo)} 天（週末與已快取者已略過）")
    for i, d in enumerate(todo, 1):
        rows = load_market_day(d)
        print(f"[{i}/{len(todo)}] {d} -> {len(rows)} 檔")
        time.sleep(0.4)
    cached = cached_dates()
    print(f"完成。快取共 {len(cached)} 日"
          + (f"（{cached[0]} ~ {cached[-1]}）" if cached else ""))


def build_series(dates):
    """把逐日快取轉成 dict[stock_id] = list[row]（依日期升冪，缺漏日自動跳過）。"""
    series = {}
    for d in dates:
        for sid, row in load_market_day(d, fetch=False).items():
            series.setdefault(sid, []).append(dict(row, date=d))
    return series


# ---------------------------------------------------------------------------
# 訊號判定與價位計算
# ---------------------------------------------------------------------------
def chip_fields(sid, margin, inst_days):
    """組出籌碼欄位。任一項資料缺失就標 None，**不以 0 頂替**。

    inst_days 為近 N 日的 {id: 淨買賣超股數} 由舊到新，缺該日資料者略過該日
    （而非當成 0），並回報實際採計天數，避免把「沒資料」讀成「法人沒動作」。
    """
    m = (margin or {}).get(sid) or {}
    mb, ml, sb = m.get("margin"), m.get("limit"), m.get("short")
    used = (mb / ml * 100) if (mb and ml) else None
    ratio = (sb / mb * 100) if (mb and sb is not None) else None
    vals = [d[sid] for d in inst_days if sid in d and d[sid] is not None]
    return {
        "margin_lots": round(mb) if mb is not None else None,
        "margin_used_pct": round(used, 2) if used is not None else None,
        "short_margin_pct": round(ratio, 2) if ratio is not None else None,
        "inst_net_lots": round(sum(vals) / 1000) if vals else None,
        "inst_days": len(vals),
    }


def evaluate(rows):
    """對單一個股的日 K 序列（升冪，最後一根為判定日）計算訊號與價位。

    回傳 dict（含 hit 布林與所有中間值，方便逐項追溯），資料不足回傳 None。
    """
    if len(rows) < MIN_BARS:
        return None
    cur, prev = rows[-1], rows[-2]
    hist = rows[:-1]  # 不含當日，避免把當日資訊混進基準
    closes = [r["close"] for r in hist[-BREAKOUT_LOOKBACK:]]
    lows = [r["low"] for r in hist[-BREAKOUT_LOOKBACK:] if r["low"] is not None]
    vols = [r["vol"] for r in hist[-VOL_BASE_DAYS:]]
    if len(closes) < BREAKOUT_LOOKBACK or len(vols) < VOL_BASE_DAYS or not lows:
        return None

    prior_high, prior_low = max(closes), min(lows)
    avg_vol = sum(vols) / len(vols)
    if not avg_vol or not prev["close"]:
        return None

    change_pct = (cur["close"] - prev["close"]) / prev["close"] * 100
    vol_ratio = cur["vol"] / avg_vol
    avg_lots = avg_vol / 1000
    vwap = (cur["amount"] / cur["vol"]) if (cur["amount"] and cur["vol"]) else None

    # --- 漲停鎖死判定（紅點）---
    lim = limit_up_price(prev["close"])
    at_limit = bool(lim and abs(cur["close"] - lim) < PRICE_EPS)
    close_is_high = (cur["high"] is not None
                     and abs(cur["close"] - cur["high"]) < PRICE_EPS)
    lock_ratio = (vwap / lim) if (vwap and lim) else None
    red = bool(
        at_limit and close_is_high
        and lock_ratio is not None and lock_ratio >= RED_MIN_VWAP_RATIO
        and vol_ratio >= RED_MIN_VOL_RATIO
        and avg_lots >= RED_MIN_AVG_LOTS
        and cur["close"] >= RED_MIN_CLOSE
    )

    # --- 出量突破（藍點）---
    # 收在漲停者排除：當天買不到，列進波段名單只會給出無法執行的進場價。
    blue = (
        cur["close"] > prior_high
        and vol_ratio >= VOL_MULT
        and change_pct >= MIN_CHANGE_PCT
        and avg_lots >= MIN_AVG_LOTS
        and cur["close"] >= MIN_CLOSE
        and not at_limit
    )
    hit = blue

    # wilder_atr 回傳與輸入等長的 list（暖身期為 NaN），取最後一根為當日 ATR。
    # 高低價缺漏者以收盤價補，避免整檔因單日缺值而算不出 ATR。
    tail = [r for r in rows[-(ATR_N * 3):]]
    atr_series = wilder_atr(
        [r["high"] if r["high"] is not None else r["close"] for r in tail],
        [r["low"] if r["low"] is not None else r["close"] for r in tail],
        [r["close"] for r in tail], ATR_N)
    atr = atr_series[-1] if atr_series else float("nan")
    if atr != atr:  # NaN
        atr = None

    box = prior_high - prior_low  # 箱體高度，用於量測滿足點
    chase = cur["close"]          # 追價進場：次日開盤附近，以當日收盤代表
    levels = None
    if atr and atr > 0:
        stop = round(prior_high - 0.5 * atr, 2)  # 型態失效：跌回箱體內
        t1 = round(prior_high + 0.5 * box, 2)    # 量測滿足點 1/2
        t2 = round(prior_high + 1.0 * box, 2)    # 量測滿足點 1/1
        pull_lo = round(prior_high, 2)
        pull_hi = round(prior_high + 0.3 * atr, 2)
        pull_mid = round((pull_lo + pull_hi) / 2, 2)
        # 漲停鎖死者收盤可能已越過 t1，此時第一個仍在上方的目標才是有效目標；
        # 若連 t2 都已達陣，代表量測空間用盡，不硬湊一個目標價出來。
        reached = [t for t in (t1, t2) if chase >= t]

        def _rr(entry_px):
            nxt = next((t for t in (t1, t2) if t > entry_px), None)
            risk = entry_px - stop
            if nxt is None or risk <= 0:
                return None, None
            return nxt, round((nxt - entry_px) / risk, 2)

        chase_target, rr_chase = _rr(chase)
        pull_target, rr_pull = _rr(pull_mid)
        levels = {
            "breakout_line": round(prior_high, 2),
            "chase_entry": chase,
            "pullback_lo": pull_lo, "pullback_hi": pull_hi, "pullback_mid": pull_mid,
            "stop": stop,
            "target1": t1, "target2": t2,
            "targets_reached": reached,
            "chase_target": chase_target, "rr_chase": rr_chase,
            "pullback_target": pull_target, "rr_pullback": rr_pull,
            "atr": round(atr, 2),
        }

    return {
        "id": cur["id"], "name": cur["name"], "market": cur["market"],
        "date": cur["date"], "hit": hit, "blue": blue, "red": red,
        "limit_up": lim, "at_limit": at_limit, "close_is_high": close_is_high,
        "lock_ratio": round(lock_ratio, 4) if lock_ratio else None,
        "close": cur["close"], "open": cur["open"],
        "high": cur["high"], "low": cur["low"],
        "prev_close": prev["close"], "change_pct": round(change_pct, 2),
        "vwap": round(vwap, 2) if vwap else None,
        "above_vwap": (vwap is not None and cur["close"] >= vwap),
        "lots": round(cur["vol"] / 1000),
        "avg_lots": round(avg_lots),
        "vol_ratio": round(vol_ratio, 2),
        "prior_high": round(prior_high, 2),
        "prior_low": round(prior_low, 2),
        "amount_yi": round((cur["amount"] or 0) / 1e8, 2),
        "levels": levels,
        "group": (code_map().get(cur["id"]) or {}).get("group", ""),
    }


def scan(target=None):
    """回傳 (判定日, 藍點清單, 紅點清單, 掃描檔數)。

    藍點依量能倍數排序、紅點依鎖死程度（均價/漲停價）排序。
    """
    dates = cached_dates()
    if not dates:
        raise SystemExit("尚無全市場快取，請先執行：python breakout_scan.py backfill 90")
    if target:
        if target not in dates:
            raise SystemExit(f"{target} 不在快取中（快取範圍 {dates[0]} ~ {dates[-1]}）")
        dates = [d for d in dates if d <= target]
    day = dates[-1]
    series = build_series(dates[-(MIN_BARS + 10):])

    chip_today = load_chip_day(day, fetch=False)
    margin = chip_today.get("margin") or {}
    inst_days = [load_chip_day(d, fetch=False).get("inst") or {}
                 for d in dates[-VOL_BASE_DAYS:]]

    blues, reds, scanned = [], [], 0
    for rows in series.values():
        if rows[-1]["date"] != day:  # 當日無成交者不判定
            continue
        scanned += 1
        r = evaluate(rows)
        if not r or not (r["blue"] or r["red"]):
            continue
        r["chips"] = chip_fields(r["id"], margin, inst_days)
        (blues if r["blue"] else reds).append(r)
    blues.sort(key=lambda x: x["vol_ratio"], reverse=True)
    reds.sort(key=lambda x: ((x["lock_ratio"] or 0), x["vol_ratio"]), reverse=True)
    return day, blues, reds, scanned


def _chip_line(c):
    if not c:
        return "   籌碼：資料不足"
    def f(v, unit=""):
        return "—" if v is None else f"{v}{unit}"
    return (f"   融資使用率 {f(c['margin_used_pct'], '%')}　"
            f"券資比 {f(c['short_margin_pct'], '%')}　"
            f"{c['inst_days']}日法人合計 {f(c['inst_net_lots'], ' 張')}")


def cmd_scan(target=None):
    day, blues, reds, scanned = scan(target)
    print(f"出量突破掃描（規則 {RULES_VERSION}）  判定日：{day}")
    print(f"掃描 {scanned} 檔普通股　→　🔴紅點 {len(reds)} 檔　🔵藍點 {len(blues)} 檔\n")

    print(f"🔴 紅點（漲停鎖死，隔日衝候選）：收盤=漲停價　且　收盤=最高　且　"
          f"均價/漲停 >= {RED_MIN_VWAP_RATIO}　且　前{VOL_BASE_DAYS}日均量 >= "
          f"{RED_MIN_AVG_LOTS}張\n")
    for r in reds:
        print(f"■ {r['id']} {r['name']}  {r['market']}  {r['group']}")
        print(f"   收盤 {r['close']}（漲停價 {r['limit_up']}）　漲幅 {r['change_pct']:+.2f}%　"
              f"均價 {r['vwap']}　鎖死度 {r['lock_ratio']:.3f}")
        print(f"   成交 {r['lots']:,} 張（{r['amount_yi']} 億）　量能 {r['vol_ratio']}x")
        print(_chip_line(r.get("chips")))
        print()
    if not reds:
        print("（今日無漲停鎖死標的）\n")

    print(f"🔵 藍點（出量突破，波段候選）：收盤 > 前{BREAKOUT_LOOKBACK}日最高收盤　且　"
          f"量 >= 前{VOL_BASE_DAYS}日均量×{VOL_MULT}　且　漲幅 >= {MIN_CHANGE_PCT}%　且　"
          f"前{VOL_BASE_DAYS}日均量 >= {MIN_AVG_LOTS}張　且　未收在漲停\n")
    if not blues:
        print("（今日無標的符合全部條件）")
        return
    for r in blues:
        lv = r["levels"] or {}
        print(f"■ {r['id']} {r['name']}  {r['market']}  {r['group']}")
        print(f"   收盤 {r['close']}　漲幅 {r['change_pct']:+.2f}%　均價 {r['vwap']}　"
              + ("收盤>均價(尾盤強)" if r["above_vwap"] else "收盤<均價(尾盤弱)"))
        print(f"   成交 {r['lots']:,} 張（{r['amount_yi']} 億）　量能 {r['vol_ratio']}x　"
              f"前{VOL_BASE_DAYS}日均量 {r['avg_lots']:,} 張")
        print(f"   突破確認線 {lv.get('breakout_line')}（前{BREAKOUT_LOOKBACK}日高）　"
              f"箱底 {r['prior_low']}（箱高 {round(r['prior_high'] - r['prior_low'], 2)}）　"
              f"ATR({ATR_N}) {lv.get('atr')}")
        print(f"   防守 {lv.get('stop')}（突破線 -0.5ATR）　"
              f"目標1 {lv.get('target1')}　目標2 {lv.get('target2')}"
              + (f"　※收盤已達 {lv.get('targets_reached')}" if lv.get("targets_reached") else ""))
        print(f"   追價進場 {lv.get('chase_entry')} → 目標 {lv.get('chase_target')}　"
              f"R/R {lv.get('rr_chase')}")
        print(f"   回測承接 {lv.get('pullback_lo')}~{lv.get('pullback_hi')} → "
              f"目標 {lv.get('pullback_target')}　R/R {lv.get('rr_pullback')}")
        print(_chip_line(r.get("chips")))
        print()


def cmd_export(target=None):
    day, blues, reds, scanned = scan(target)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trade_date": day,
        "rules_version": RULES_VERSION,
        "scanned": scanned,
        "rules": {
            "breakout_lookback": BREAKOUT_LOOKBACK, "vol_mult": VOL_MULT,
            "vol_base_days": VOL_BASE_DAYS, "min_change_pct": MIN_CHANGE_PCT,
            "min_avg_lots": MIN_AVG_LOTS, "min_close": MIN_CLOSE, "atr_n": ATR_N,
            "red_min_vwap_ratio": RED_MIN_VWAP_RATIO,
            "red_min_avg_lots": RED_MIN_AVG_LOTS, "red_min_close": RED_MIN_CLOSE,
            "red_min_vol_ratio": RED_MIN_VOL_RATIO,
        },
        "limitations": [
            "日終資料，非盤中即時；訊號於收盤後成立，最快次日開盤才能進場。",
            "無逐筆成交明細（tick），故無大單買超、買賣成交比、大單區間等欄位；"
            "隔日沖的盤中進出場訊號（大單翻綠、跌破大單均價）本工具無法提供。",
            "紅點的「鎖死度」是以當日均價÷漲停價推估，非確認全日未打開。",
            "規則門檻為初始設定值，尚未經回測校準，不等於已驗證的勝率。",
        ],
        "items": blues,
        "red_items": reds,
    }
    out = export_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"已輸出 紅點 {len(reds)} 檔 / 藍點 {len(blues)} 檔 -> {out}")


def cmd_chips(days="8"):
    """補齊近 N 個交易日的全市場籌碼快取（融資融券＋三大法人）。

    只需最近幾日：融資券取判定日當天，法人取近 5 日合計。
    """
    n = int(days)
    todo = [d for d in cached_dates()[-n:] if not chip_cache_path(d).exists()]
    print(f"待抓取 {len(todo)} 天籌碼（已快取者已略過）")
    for i, d in enumerate(todo, 1):
        c = load_chip_day(d)
        print(f"[{i}/{len(todo)}] {d} -> 融資券 {len(c['margin'])} 檔、"
              f"法人 {len(c['inst'])} 檔")
        time.sleep(0.4)


def cmd_prune(keep="70"):
    """只保留最近 N 個日快取檔，避免雲端 repo 無限膨脹。

    N 必須明顯大於 MIN_BARS，否則下次掃描會因歷史不足而全部算不出訊號。
    """
    k = int(keep)
    if k < MIN_BARS + 10:
        raise SystemExit(f"keep 至少要 {MIN_BARS + 10}（掃描需 {MIN_BARS} 根日K）")
    dates = cached_dates()
    for d in dates[:-k]:
        market_cache_path(d).unlink()
    # 籌碼只用到最近數日，保留期可短得多
    chip_keep = set(dates[-VOL_BASE_DAYS * 3:])
    n_chip = 0
    for p in sorted(CHIP_DIR.glob("*.json")) if CHIP_DIR.exists() else []:
        if p.stem not in chip_keep:
            p.unlink()
            n_chip += 1
    print(f"保留最近 {min(k, len(dates))} 日行情，刪除 {max(0, len(dates) - k)} 個過舊行情快取、"
          f"{n_chip} 個過舊籌碼快取")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "scan"
    args = argv[2:]
    if cmd == "backfill":
        cmd_backfill(*args)
    elif cmd == "scan":
        cmd_scan(*args)
    elif cmd == "export":
        cmd_export(*args)
    elif cmd == "chips":
        cmd_chips(*args)
    elif cmd == "prune":
        cmd_prune(*args)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
