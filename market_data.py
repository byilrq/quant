#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quant market data adapter layer.

Only stable internal source keys are used between programs:
- A/ETF realtime: live_a1/live_a2/live_a3
- HK realtime: live_hk1/live_hk2/live_hk3
- A/ETF historical: historical_a1/historical_a2/historical_a3
- HK historical: historical_hk1/historical_hk2/historical_hk3

Customer-facing names, source options, normalization and concrete API logic all live here.
"""
from __future__ import annotations

import json
import logging
import random
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent
MARKET_DATA_PATCH_VERSION = "2026-05-23-history-fallback-daily-cache-v3-retention-1d"
SYSTEM_CONFIG_FILE = BASE_DIR / "system_config.json"
CACHE_DIR = BASE_DIR / "data" / "bars"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Source-level historical K cache. This is deliberately separate from
# strategy_history because strategy_history only stores final per-symbol
# strategy results (SYMBOL_YYYY-MM-DD.json). Source snapshots are cached under
# history_cache for reuse/diagnostics and must not pollute strategy_history.
SOURCE_HISTORY_CACHE_DIR = BASE_DIR / "data" / "history_cache"
SOURCE_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SOURCE_HISTORY_CACHE_RETENTION_DAYS = 1

SYSTEM_DEFAULTS = {
    "A_QUOTE_SOURCE": "live_a1",
    "HK_MARKET_SOURCE": "live_hk1",
    "A_BACKTEST_SOURCE": "historical_a1",
    "HK_BACKTEST_SOURCE": "historical_hk1",
    "XUEQIU_TOKEN": "",
}

SOURCE_OPTIONS: Dict[str, List[Tuple[str, str]]] = {
    "A_QUOTE_SOURCE": [
        ("live_a1", "腾讯实时"),
        ("live_a2", "雪球实时"),
        ("live_a3", "EasyQuotation(新浪源)"),   # 改为 EasyQuotation 新浪源
    ],
    "HK_MARKET_SOURCE": [
        ("live_hk1", "腾讯港股"),
        ("live_hk2", "雪球港股"),
        ("live_hk3", "东方财富港股"),           # 保留东方财富港股接口
    ],
    "A_BACKTEST_SOURCE": [
        ("historical_a1", "腾讯A股/ETF日K"),
        ("historical_a2", "新浪A股/ETF日K"),
        ("historical_a3", "BaoStock A股含权息"),
    ],
    "HK_BACKTEST_SOURCE": [
        ("historical_hk1", "腾讯港股日K"),
        ("historical_hk2", "Yahoo港股日K"),
        ("historical_hk3", "Yahoo港股含权息"),
    ],
}

SOURCE_DISPLAY = {key: label for items in SOURCE_OPTIONS.values() for key, label in items}
DISPLAY_TO_KEY = {label: key for key, label in SOURCE_DISPLAY.items()}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

@dataclass
class MarketSnapshot:
    symbol: str
    source: str
    closes: List[float]
    price_scale: float = 1.0
    last_bar_date: Optional[str] = None
    error: str = ""
    trade_allowed: bool = True
    dates: Optional[List[str]] = None
    strategy_source: str = ""
    strategy_status: str = "OK"
    # Optional total-return columns for historical backtests.
    # closes remains the strategy/signal close series, usually adjusted close.
    raw_closes: Optional[List[float]] = None
    adj_closes: Optional[List[float]] = None
    dividends: Optional[List[float]] = None
    split_ratios: Optional[List[float]] = None

    @property
    def ok(self) -> bool:
        return bool(self.closes)

    @property
    def current_price(self) -> float:
        return round(float(self.closes[-1]), 4) if self.closes else 0.0


def get_source_options(field: str) -> List[Tuple[str, str]]:
    return list(SOURCE_OPTIONS.get(str(field or ""), []))


def get_all_source_options() -> Dict[str, List[Tuple[str, str]]]:
    return {k: list(v) for k, v in SOURCE_OPTIONS.items()}


def get_source_display_name(source: str) -> str:
    s = str(source or "").strip()
    if not s:
        return "未知"
    if s.startswith("cache_"):
        return "本地缓存"
    return SOURCE_DISPLAY.get(s, SOURCE_DISPLAY.get(DISPLAY_TO_KEY.get(s, ""), s))


def get_source_canonical_key(source: str) -> str:
    s = str(source or "").strip()
    if not s:
        return ""
    if s.startswith("cache_"):
        return "cache"
    return DISPLAY_TO_KEY.get(s, s)


def normalize_system_source_value(field: str, value: str) -> str:
    raw = str(value or "").strip()
    allowed = {key for key, _ in SOURCE_OPTIONS.get(field, [])}
    if raw in allowed:
        return raw
    # Accept current customer-facing labels only. No legacy source-name compatibility.
    mapped = DISPLAY_TO_KEY.get(raw, raw)
    if mapped in allowed:
        return mapped
    return SYSTEM_DEFAULTS.get(field, raw)


def _load_system_config() -> dict:
    cfg = dict(SYSTEM_DEFAULTS)
    try:
        if SYSTEM_CONFIG_FILE.exists():
            raw = json.loads(SYSTEM_CONFIG_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                cfg.update({k: "" if v is None else str(v).strip() for k, v in raw.items()})
    except Exception as e:
        logging.debug(f"读取系统行情配置失败: {e}")
    for field in SOURCE_OPTIONS:
        cfg[field] = normalize_system_source_value(field, cfg.get(field, SYSTEM_DEFAULTS.get(field, "")))
    return cfg


def _headers(referer: str = "https://quote.eastmoney.com/") -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }


def _preferred_order(items, preferred_key: str):
    preferred_key = str(preferred_key or "").strip()
    return [x for x in items if x[0] == preferred_key] + [x for x in items if x[0] != preferred_key]


def _cache_path(symbol: str) -> Path:
    safe = str(symbol or "").upper().replace("/", "_")
    return CACHE_DIR / f"{safe}.json"


def _write_cache(snapshot: MarketSnapshot) -> None:
    if not snapshot.closes or str(snapshot.source).startswith("cache_"):
        return
    payload = {
        "symbol": snapshot.symbol,
        "source": snapshot.source,
        "strategy_source": snapshot.strategy_source,
        "last_bar_date": snapshot.last_bar_date,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "dates": (snapshot.dates or [])[-800:],
        "closes": snapshot.closes[-800:],
        "raw_closes": (snapshot.raw_closes or [])[-800:] if snapshot.raw_closes else [],
        "adj_closes": (snapshot.adj_closes or [])[-800:] if snapshot.adj_closes else [],
        "dividends": (snapshot.dividends or [])[-800:] if snapshot.dividends else [],
        "split_ratios": (snapshot.split_ratios or [])[-800:] if snapshot.split_ratios else [],
    }
    try:
        _cache_path(snapshot.symbol).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.debug(f"写入行情缓存失败 {snapshot.symbol}: {e}")



def _snapshot_to_payload(snapshot: MarketSnapshot) -> dict:
    return {
        "symbol": snapshot.symbol,
        "source": snapshot.source,
        "closes": list(snapshot.closes or []),
        "price_scale": snapshot.price_scale,
        "last_bar_date": snapshot.last_bar_date,
        "error": snapshot.error,
        "trade_allowed": bool(snapshot.trade_allowed),
        "dates": list(snapshot.dates or []),
        "strategy_source": snapshot.strategy_source,
        "strategy_status": snapshot.strategy_status,
        "raw_closes": list(snapshot.raw_closes or []),
        "adj_closes": list(snapshot.adj_closes or []),
        "dividends": list(snapshot.dividends or []),
        "split_ratios": list(snapshot.split_ratios or []),
    }


def _snapshot_from_payload(data: dict) -> MarketSnapshot:
    closes = [float(x) for x in (data.get("closes") or []) if x is not None]
    return MarketSnapshot(
        symbol=str(data.get("symbol") or "").upper(),
        source=str(data.get("source") or ""),
        closes=closes,
        price_scale=float(data.get("price_scale") or 1.0),
        last_bar_date=data.get("last_bar_date"),
        error=str(data.get("error") or ""),
        trade_allowed=bool(data.get("trade_allowed", True)),
        dates=list(data.get("dates") or []),
        strategy_source=str(data.get("strategy_source") or ""),
        strategy_status=str(data.get("strategy_status") or "OK"),
        raw_closes=[float(x) for x in (data.get("raw_closes") or []) if x is not None] or None,
        adj_closes=[float(x) for x in (data.get("adj_closes") or []) if x is not None] or None,
        dividends=[float(x) for x in (data.get("dividends") or []) if x is not None] or None,
        split_ratios=[float(x) for x in (data.get("split_ratios") or []) if x is not None] or None,
    )


def _strategy_history_cache_path(symbol: str, source_key: str, days: int, price_scale: float, cache_day: str = "") -> Path:
    raw = str(symbol or "").upper().replace("/", "_")
    src = str(source_key or "").strip() or "default"
    day = cache_day or date.today().isoformat()
    scale = str(float(price_scale)).replace(".", "p")
    return SOURCE_HISTORY_CACHE_DIR / f"{raw}_{src}_{int(days)}_{scale}_{day}.json"


def _prune_strategy_history_daily_cache(retention_days: int = SOURCE_HISTORY_CACHE_RETENTION_DAYS) -> None:
    """Delete expired source-history daily cache files.

    With retention_days=1, only today's cache files are kept. This prevents
    /data/history_cache from accumulating one file per symbol per day forever.
    """
    try:
        keep_days = max(1, int(retention_days or 1))
        today = date.today()
        cutoff = today - timedelta(days=keep_days - 1)
        for path in SOURCE_HISTORY_CACHE_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw_day = str(data.get("cache_day") or "").strip()
                if not raw_day:
                    # Fall back to filename suffix: *_YYYY-MM-DD.json
                    raw_day = path.stem.rsplit("_", 1)[-1]
                cache_day = date.fromisoformat(raw_day[:10])
            except Exception:
                # Remove unreadable cache files; they cannot be trusted.
                try:
                    path.unlink()
                except Exception:
                    pass
                continue
            if cache_day < cutoff:
                try:
                    path.unlink()
                except Exception:
                    pass
    except Exception as e:
        logging.debug(f"清理策略历史日缓存失败: {e}")


def _read_strategy_history_daily_cache(symbol: str, source_key: str, days: int, price_scale: float) -> Optional[MarketSnapshot]:
    path = _strategy_history_cache_path(symbol, source_key, days, price_scale)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cache_day") != date.today().isoformat():
            return None
        snap = _snapshot_from_payload(data.get("snapshot") or {})
        if not snap.closes or len(snap.closes) < 2:
            return None
        snap.source = snap.source or str(data.get("resolved_source") or source_key)
        snap.strategy_source = snap.strategy_source or get_source_display_name(source_key)
        return snap
    except Exception as e:
        logging.debug(f"读取策略历史日缓存失败 {symbol}/{source_key}: {e}")
        return None


def _write_strategy_history_daily_cache(symbol: str, source_key: str, days: int, price_scale: float, snapshot: MarketSnapshot) -> None:
    if not snapshot.closes:
        return
    _prune_strategy_history_daily_cache()
    path = _strategy_history_cache_path(symbol, source_key, days, price_scale)
    payload = {
        "cache_day": date.today().isoformat(),
        "symbol": str(symbol or "").upper(),
        "requested_source": source_key,
        "resolved_source": snapshot.source,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": _snapshot_to_payload(snapshot),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.debug(f"写入策略历史日缓存失败 {symbol}/{source_key}: {e}")

def _read_cache(symbol: str, days: int, price_scale: float, reason: str) -> MarketSnapshot:
    path = _cache_path(symbol)
    if not path.exists():
        raise RuntimeError(reason + "；无本地缓存")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        closes = []
        for x in data.get("closes") or []:
            try:
                v = float(x)
                if v > 0:
                    closes.append(round(v, 4))
            except Exception:
                continue
        if len(closes) < 2:
            raise RuntimeError("缓存K线不足")
        n = max(int(days), 2)
        dates = data.get("dates") or []
        return MarketSnapshot(
            symbol=str(data.get("symbol") or symbol).upper(),
            source="cache_" + str(data.get("source") or "unknown"),
            closes=closes[-n:],
            price_scale=price_scale,
            last_bar_date=data.get("last_bar_date"),
            error=reason + "；已使用本地缓存，仅监控不交易",
            trade_allowed=False,
            dates=dates[-n:] if isinstance(dates, list) else [],
            strategy_source=data.get("strategy_source") or "",
            strategy_status="WARN",
            raw_closes=(data.get("raw_closes") or [])[-n:] or None,
            adj_closes=(data.get("adj_closes") or [])[-n:] or None,
            dividends=(data.get("dividends") or [])[-n:] or None,
            split_ratios=(data.get("split_ratios") or [])[-n:] or None,
        )
    except Exception as e:
        raise RuntimeError(reason + f"；读取本地缓存失败: {e}")


def _is_hk(symbol: str) -> bool:
    return str(symbol or "").upper().strip().startswith("HK")


def _hk_code(symbol_or_code: str) -> str:
    raw = str(symbol_or_code or "").upper().strip()
    if raw.startswith("HK"):
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.zfill(5)


def _tencent_a_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "sh" + raw[2:]
    if raw.startswith("SZ"):
        return "sz" + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("sh" if raw.startswith("6") else "sz") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _sina_a_symbol(symbol: str) -> str:
    return _tencent_a_symbol(symbol)


def _eastmoney_a_secid(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "1." + raw[2:]
    if raw.startswith("SZ"):
        return "0." + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("1." if raw.startswith("6") else "0.") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _eastmoney_hk_secids(symbol_or_code: str) -> List[str]:
    code = _hk_code(symbol_or_code)
    nozero = code.lstrip("0") or code
    return list(dict.fromkeys(["116." + code, "116." + nozero]))


def _parse_tencent_json(text: str) -> dict:
    text = str(text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"腾讯返回无法解析: {text[:160]}")
    return json.loads(text[start:end + 1])


def _parse_volume(value, default=1.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except Exception:
        return default


def _fetch_tencent_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    t_symbol = _tencent_a_symbol(symbol)
    lmt = max(int(days), 2)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    params = [
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},day",
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},",
    ]
    rows, last_err = [], None
    for param in params:
        try:
            resp = requests.get(url, params={"param": param, "r": str(random.random())}, headers=_headers("https://gu.qq.com/"), timeout=12)
            resp.raise_for_status()
            data = _parse_tencent_json(resp.text)
            node = (((data or {}).get("data") or {}).get(t_symbol) or {})
            rows = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
            if rows:
                break
            last_err = f"empty response={str(data)[:220]}"
        except Exception as e:
            last_err = e
    dedup = {}
    for row in rows:
        try:
            date = str(row[0])
            close_price = float(row[2]) * float(price_scale)
            volume = _parse_volume(row[5] if len(row) > 5 else 1)
            if date and close_price > 0 and volume > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) < 2:
        raise RuntimeError(f"腾讯A股/ETF日K不足: {symbol}, count={len(closes)}, last_error={last_err}")
    n = min(lmt, len(closes))
    snap = MarketSnapshot(symbol.upper(), get_source_display_name("historical_a1"), closes[-n:], price_scale, dates[-1], dates=dates[-n:], strategy_source=get_source_display_name("historical_a1"))
    _write_cache(snap)
    return snap


def _fetch_sina_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    raw = str(symbol or "").upper().strip()
    s_symbol = _sina_a_symbol(raw)
    n = max(int(days), 2)
    url = "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
    resp = requests.get(url, params={"symbol": s_symbol, "scale": "240", "ma": "no", "datalen": str(n)}, headers=_headers("https://finance.sina.com.cn/"), timeout=12)
    resp.raise_for_status()
    data = resp.json()
    status = (((data or {}).get("result") or {}).get("status") or {})
    if status.get("code") not in (0, "0", None):
        raise RuntimeError(f"新浪A股/ETF日K接口错误: {status}")
    part = (((data or {}).get("result") or {}).get("data"))
    rows = part.get("data") if isinstance(part, dict) else part
    rows = rows or []
    pairs = []
    for row in rows:
        try:
            date = str(row.get("day") or row.get("date") or "")
            close_price = float(row.get("close")) * float(price_scale)
            volume = _parse_volume(row.get("volume", 1))
            if date and close_price > 0 and volume > 0:
                pairs.append((date, round(close_price, 4)))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[0])
    if len(pairs) < 2:
        raise RuntimeError(f"新浪A股/ETF日K为空: {symbol}, count={len(pairs)}")
    dates = [d for d, _ in pairs[-n:]]
    closes = [c for _, c in pairs[-n:]]
    snap = MarketSnapshot(raw, get_source_display_name("historical_a2"), closes, price_scale, dates[-1] if dates else None, dates=dates, strategy_source=get_source_display_name("historical_a2"))
    _write_cache(snap)
    return snap


def _ensure_baostock_module():
    """Import BaoStock.

    BaoStock is an optional dependency for historical_a3. Install it in the
    same Python environment that runs quant before selecting this source:
    python -m pip install baostock
    """
    try:
        import baostock as bs
        return bs
    except ImportError as e:
        raise RuntimeError("BaoStock 数据源需要先安装: python -m pip install baostock") from e


def _baostock_a_code(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH"):
        return "sh." + raw[2:]
    if raw.startswith("SZ"):
        return "sz." + raw[2:]
    if raw.isdigit() and len(raw) == 6:
        return ("sh." if raw.startswith("6") else "sz.") + raw
    raise ValueError(f"不支持的A股代码格式: {symbol}")


def _collect_baostock_result(rs, label: str) -> List[dict]:
    if getattr(rs, "error_code", "0") != "0":
        raise RuntimeError(f"BaoStock {label} 查询失败: {getattr(rs, 'error_msg', '')}")
    rows = []
    fields = list(getattr(rs, "fields", []) or [])
    while getattr(rs, "error_code", "0") == "0" and rs.next():
        rows.append(dict(zip(fields, rs.get_row_data())))
    return rows


def _safe_float_or_zero(value) -> float:
    try:
        if value in (None, "", "-"):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _first_nonempty(row: dict, keys: List[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _fetch_baostock_a_snapshot(symbol: str, days: int, price_scale: float) -> MarketSnapshot:
    """Fetch A-share total-return columns from BaoStock.

    raw_close: unadjusted daily close (adjustflag=3)
    adj_close: forward-adjusted daily close for continuous MA/signal calculations (adjustflag=2)
    dividend: cash dividend before tax per share on ex-dividend date when available
    split_ratio: 1 + bonus-share-per-share + reserve-to-stock-per-share on ex-rights date
    """
    bs = _ensure_baostock_module()

    raw = str(symbol or "").upper().strip()
    bs_code = _baostock_a_code(raw)
    n = max(int(days), 2)
    start_dt = datetime.now() - timedelta(days=max(n * 3, 1200))
    end_dt = datetime.now() + timedelta(days=2)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")
    label = get_source_display_name("historical_a3")

    login_ok = False
    try:
        lg = bs.login()
        login_ok = True
        if getattr(lg, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock 登录失败: {getattr(lg, 'error_msg', '')}")

        fields = "date,code,close,volume,amount,adjustflag,tradestatus"
        raw_rs = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="3",
        )
        adj_rs = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2",
        )
        raw_rows = _collect_baostock_result(raw_rs, "不复权日K")
        adj_rows = _collect_baostock_result(adj_rs, "前复权日K")

        raw_by_date = {}
        for row in raw_rows:
            date = str(row.get("date") or "")
            close_price = _safe_float_or_zero(row.get("close")) * float(price_scale)
            volume = _parse_volume(row.get("volume"), default=0.0)
            tradestatus = str(row.get("tradestatus", "1") or "1")
            if date and close_price > 0 and volume > 0 and tradestatus != "0":
                raw_by_date[date] = round(close_price, 4)

        adj_by_date = {}
        for row in adj_rows:
            date = str(row.get("date") or "")
            close_price = _safe_float_or_zero(row.get("close")) * float(price_scale)
            volume = _parse_volume(row.get("volume"), default=0.0)
            tradestatus = str(row.get("tradestatus", "1") or "1")
            if date and close_price > 0 and volume > 0 and tradestatus != "0":
                adj_by_date[date] = round(close_price, 4)

        dates_all = sorted(set(raw_by_date) & set(adj_by_date))
        if len(dates_all) < 2:
            raise RuntimeError(f"BaoStock A股含权息日K不足: {raw}, count={len(dates_all)}")

        dividend_by_date: Dict[str, float] = {}
        split_by_date: Dict[str, float] = {}
        first_year = int(dates_all[0][:4])
        last_year = int(dates_all[-1][:4])
        valid_dates = dates_all

        for year in range(first_year - 1, last_year + 1):
            try:
                div_rs = bs.query_dividend_data(code=bs_code, year=str(year), yearType="operate")
                div_rows = _collect_baostock_result(div_rs, f"除权除息 {year}")
            except Exception as e:
                logging.debug(f"BaoStock 除权除息查询失败 {raw} {year}: {e}")
                continue
            for row in div_rows:
                ex_date = _first_nonempty(row, [
                    "dividOperateDate", "dividOperate_date", "operateDate",
                    "dividDate", "dividStockMarketDate", "dividRegistDate",
                ])
                if not ex_date:
                    continue
                # Align to the first available trading day on or after the ex-rights/ex-dividend date.
                aligned = None
                for d in valid_dates:
                    if d >= ex_date:
                        aligned = d
                        break
                if aligned is None:
                    continue
                cash = _safe_float_or_zero(_first_nonempty(row, [
                    "dividCashPsBeforeTax", "diviCashPsBeforeTax", "cashBeforeTax",
                    "dividCashPsAfterTax", "diviCashPsAfterTax",
                ])) * float(price_scale)
                bonus = _safe_float_or_zero(_first_nonempty(row, [
                    "dividStocksPs", "diviStocksPs", "bonusShareRatio", "stockBonusRatio",
                ]))
                transfer = _safe_float_or_zero(_first_nonempty(row, [
                    "dividReserveToStockPs", "diviReserveToStockPs", "transferShareRatio",
                ]))
                ratio = 1.0 + max(0.0, bonus) + max(0.0, transfer)
                if cash > 0:
                    dividend_by_date[aligned] = dividend_by_date.get(aligned, 0.0) + round(cash, 6)
                if ratio > 1.0:
                    split_by_date[aligned] = split_by_date.get(aligned, 1.0) * ratio

        dates = dates_all[-n:]
        raw_closes = [raw_by_date[d] for d in dates]
        adj_closes = [adj_by_date[d] for d in dates]
        dividends = [round(dividend_by_date.get(d, 0.0), 6) for d in dates]
        split_ratios = [round(split_by_date.get(d, 1.0), 6) for d in dates]
        snap = MarketSnapshot(
            raw,
            label,
            adj_closes,
            price_scale,
            dates[-1] if dates else None,
            dates=dates,
            strategy_source=label,
            raw_closes=raw_closes,
            adj_closes=adj_closes,
            dividends=dividends,
            split_ratios=split_ratios,
        )
        _write_cache(snap)
        return snap
    finally:
        if login_ok:
            try:
                bs.logout()
            except Exception:
                pass


def _fetch_tencent_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    t_symbol = "hk" + code
    lmt = max(int(days), 2)
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    params = [
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},day",
        f"{t_symbol},day,,,{min(max(lmt, 2), 800)},",
    ]
    rows, last_err = [], None
    for param in params:
        try:
            resp = requests.get(url, params={"param": param, "r": str(random.random())}, headers=_headers("https://gu.qq.com/"), timeout=12)
            resp.raise_for_status()
            data = _parse_tencent_json(resp.text)
            node = (((data or {}).get("data") or {}).get(t_symbol) or {})
            rows = node.get("day") or node.get("qfqday") or node.get("hfqday") or []
            if rows:
                break
            last_err = f"empty response={str(data)[:220]}"
        except Exception as e:
            last_err = e
    dedup = {}
    for row in rows:
        try:
            date = str(row[0])
            close_price = float(row[2]) * float(price_scale)
            volume = _parse_volume(row[5] if len(row) > 5 else 1)
            if date and close_price > 0 and volume > 0:
                dedup[date] = round(close_price, 4)
        except Exception:
            continue
    dates = sorted(dedup.keys())
    closes = [dedup[d] for d in dates]
    if len(closes) < 2:
        raise RuntimeError(f"腾讯港股日K不足: HK{code}, count={len(closes)}, last_error={last_err}")
    n = min(lmt, len(closes))
    snap = MarketSnapshot("HK" + code, get_source_display_name("historical_hk1"), closes[-n:], price_scale, dates[-1], dates=dates[-n:], strategy_source=get_source_display_name("historical_hk1"))
    _write_cache(snap)
    return snap


def _yahoo_hk_symbol(symbol_or_code: str) -> str:
    code = _hk_code(symbol_or_code)
    return f"{code[-4:].zfill(4)}.HK"


def _safe_event_date(ts) -> str:
    try:
        if isinstance(ts, str):
            s = ts.strip()
            if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                return s[:10]
            ts = float(s)
        return datetime.fromtimestamp(int(float(ts))).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _parse_yahoo_hk_dividend_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    yf_symbol = _yahoo_hk_symbol(code)
    n = max(int(days), 2)
    years = max(2, int(n / 220) + 2)
    period1 = int((datetime.now() - timedelta(days=years * 370)).timestamp())
    period2 = int((datetime.now() + timedelta(days=2)).timestamp())
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "events": "history,div,splits",
        "includeAdjustedClose": "true",
    }
    last_err = None
    data = None
    for host in ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]:
        try:
            resp = requests.get(
                f"https://{host}/v8/finance/chart/{yf_symbol}",
                params=params,
                headers=_headers("https://finance.yahoo.com/"),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            time_module.sleep(0.2)
    if data is None:
        raise RuntimeError(f"Yahoo港股含权息下载失败 {yf_symbol}: {last_err}")
    chart = ((data or {}).get("chart") or {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo港股含权息返回错误 {yf_symbol}: {chart.get('error')}")
    result = chart.get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo港股含权息为空 {yf_symbol}")
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    indicators = node.get("indicators") or {}
    quote = ((indicators.get("quote") or [{}])[0] or {})
    raw_series = quote.get("close") or []
    volumes = quote.get("volume") or []
    adj_series = ((indicators.get("adjclose") or [{}])[0] or {}).get("adjclose") or []

    events = node.get("events") or {}
    dividend_by_date: Dict[str, float] = {}
    for item in (events.get("dividends") or {}).values():
        try:
            date = _safe_event_date(item.get("date"))
            amount = float(item.get("amount") or 0.0) * float(price_scale)
            if date and amount:
                dividend_by_date[date] = dividend_by_date.get(date, 0.0) + amount
        except Exception:
            continue

    split_by_date: Dict[str, float] = {}
    for item in (events.get("splits") or {}).values():
        try:
            date = _safe_event_date(item.get("date"))
            numerator = float(item.get("numerator") or 0.0)
            denominator = float(item.get("denominator") or 0.0)
            ratio = numerator / denominator if numerator > 0 and denominator > 0 else 1.0
            if date and ratio > 0:
                split_by_date[date] = split_by_date.get(date, 1.0) * ratio
        except Exception:
            continue

    rows = {}
    for i, ts in enumerate(timestamps):
        try:
            if i >= len(raw_series) or raw_series[i] is None:
                continue
            raw_close = float(raw_series[i]) * float(price_scale)
            if raw_close <= 0:
                continue
            volume = _parse_volume(volumes[i] if i < len(volumes) else 1.0, default=1.0)
            if volume <= 0:
                continue
            date = _safe_event_date(ts)
            adj_close = None
            if i < len(adj_series) and adj_series[i] is not None:
                adj_close = float(adj_series[i]) * float(price_scale)
            if adj_close is None or adj_close <= 0:
                adj_close = raw_close
            rows[date] = {
                "raw_close": round(raw_close, 4),
                "adj_close": round(adj_close, 4),
                "dividend": round(float(dividend_by_date.get(date, 0.0)), 8),
                "split_ratio": round(float(split_by_date.get(date, 1.0) or 1.0), 8),
            }
        except Exception:
            continue

    dates = sorted(rows.keys())[-n:]
    if len(dates) < 2:
        raise RuntimeError(f"Yahoo港股含权息有效交易日不足 {yf_symbol}: count={len(dates)}")
    raw_closes = [rows[d]["raw_close"] for d in dates]
    adj_closes = [rows[d]["adj_close"] for d in dates]
    dividends = [rows[d]["dividend"] for d in dates]
    split_ratios = [rows[d]["split_ratio"] for d in dates]
    label = get_source_display_name("historical_hk3")
    snap = MarketSnapshot(
        "HK" + code,
        label,
        raw_closes,
        price_scale,
        dates[-1] if dates else None,
        dates=dates,
        strategy_source=label,
        raw_closes=raw_closes,
        adj_closes=adj_closes,
        dividends=dividends,
        split_ratios=split_ratios,
    )
    _write_cache(snap)
    return snap


def _fetch_yahoo_hk_snapshot(symbol_or_code: str, days: int, price_scale: float) -> MarketSnapshot:
    code = _hk_code(symbol_or_code)
    yf_symbol = _yahoo_hk_symbol(code)
    n = max(int(days), 2)
    years = max(2, int(n / 220) + 2)
    period1 = int((datetime.now() - timedelta(days=years * 370)).timestamp())
    period2 = int((datetime.now() + timedelta(days=2)).timestamp())
    params = {
        "period1": str(period1),
        "period2": str(period2),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    last_err = None
    data = None
    for host in ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]:
        try:
            resp = requests.get(f"https://{host}/v8/finance/chart/{yf_symbol}", params=params, headers=_headers("https://finance.yahoo.com/"), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
    if data is None:
        raise RuntimeError(f"Yahoo港股日K下载失败 {yf_symbol}: {last_err}")
    chart = ((data or {}).get("chart") or {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo港股日K返回错误 {yf_symbol}: {chart.get('error')}")
    result = chart.get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo港股日K为空 {yf_symbol}")
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    quote = (((node.get("indicators") or {}).get("quote") or [{}])[0] or {})
    closes_raw = quote.get("close") or []
    volumes = quote.get("volume") or []
    pairs = []
    for ts, close_value, volume_value in zip(timestamps, closes_raw, volumes):
        try:
            if close_value is None:
                continue
            close_price = float(close_value) * float(price_scale)
            volume = _parse_volume(volume_value, default=0.0)
            if close_price > 0 and volume > 0:
                date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                pairs.append((date, round(close_price, 4)))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[0])
    if len(pairs) < 2:
        raise RuntimeError(f"Yahoo港股日K有效交易日不足 {yf_symbol}: count={len(pairs)}")
    dates = [d for d, _ in pairs[-n:]]
    closes = [c for _, c in pairs[-n:]]
    snap = MarketSnapshot("HK" + code, get_source_display_name("historical_hk2"), closes, price_scale, dates[-1] if dates else None, dates=dates, strategy_source=get_source_display_name("historical_hk2"))
    _write_cache(snap)
    return snap


def _fetch_tencent_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    t_symbol = _tencent_a_symbol(symbol)
    last_err = None
    for url in [f"https://qt.gtimg.cn/q={t_symbol}", f"https://qt.gtimg.cn/q=r_{t_symbol}"]:
        try:
            resp = requests.get(url, headers=_headers("https://gu.qq.com/"), timeout=8)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text.strip()
            data = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = data.split("~")
            price = float(fields[3]) if len(fields) > 3 and fields[3] else 0.0
            if price <= 0:
                raise RuntimeError(f"价格为空: {text[:120]}")
            quote_date = None
            for item in fields:
                s = str(item).strip()
                if len(s) >= 8 and s[:8].isdigit():
                    quote_date = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                    break
            return round(price * float(price_scale), 4), quote_date
        except Exception as e:
            last_err = e
    raise RuntimeError(f"腾讯实时失败: {last_err}")


def _xueqiu_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.startswith(("SH", "SZ")):
        return raw
    if raw.startswith("HK"):
        return _hk_code(raw)
    if raw.isdigit() and len(raw) == 6:
        return ("SH" if raw.startswith("6") else "SZ") + raw
    if raw.isdigit() and len(raw) <= 5:
        return raw.zfill(5)
    return raw


def _xueqiu_headers(symbol: str) -> dict:
    headers = _headers(f"https://xueqiu.com/S/{symbol}")
    token = str(_load_system_config().get("XUEQIU_TOKEN", "") or "").strip()
    if token:
        headers["Cookie"] = token if ("=" in token or ";" in token) else f"xq_a_token={token}; xqat={token}"
    return headers


def _fetch_xueqiu_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    x_symbol = _xueqiu_symbol(symbol)
    resp = requests.get("https://stock.xueqiu.com/v5/stock/realtime/quotec.json", params={"symbol": x_symbol}, headers=_xueqiu_headers(x_symbol), timeout=8)
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("data")
    if isinstance(payload, list):
        if not payload:
            raise RuntimeError("雪球实时行情为空")
        node = payload[0] or {}
    elif isinstance(payload, dict):
        node = payload.get("quote") if isinstance(payload.get("quote"), dict) else payload
    else:
        node = {}
    raw = str(symbol or "").upper().strip()
    if raw.startswith("HK") or (raw.isdigit() and len(raw) <= 5):
        price = node.get("current")
    else:
        price = node.get("current") or node.get("price") or node.get("last")
    if price in (None, "", "-"):
        raise RuntimeError(f"雪球无可信实时价: response={str(data)[:180]}")
    price = float(price)
    if price <= 0:
        raise RuntimeError(f"雪球实时价无效: {price}")
    qdate = None
    for key in ("timestamp", "time", "updated"):
        try:
            ts = node.get(key)
            if ts:
                ts_i = int(float(ts))
                if ts_i > 10_000_000_000:
                    ts_i = ts_i // 1000
                qdate = datetime.fromtimestamp(ts_i).strftime("%Y-%m-%d")
                break
        except Exception:
            pass
    return round(price * float(price_scale), 4), qdate


def _decode_eastmoney_price(data: dict, symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    node = ((data or {}).get("data") or {})
    raw_price = node.get("f43")
    if raw_price in (None, "", "-"):
        raise RuntimeError(f"东方财富实时行情无价格: {symbol}")
    price = float(raw_price)
    if price <= 0:
        raise RuntimeError(f"东方财富实时价无效: {price}")
    qdate = None
    try:
        ts = node.get("f86")
        if ts:
            qdate = datetime.fromtimestamp(int(float(ts))).strftime("%Y-%m-%d")
    except Exception:
        qdate = None
    return round(price * float(price_scale), 4), qdate


def _fetch_eastmoney_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    secid = _eastmoney_a_secid(symbol)
    endpoints = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
    ]
    common = {"fields": "f43,f57,f58,f59,f60,f86", "fltt": "2", "invt": "2"}
    last_err = None
    for endpoint in endpoints:
        try:
            params = dict(common)
            if endpoint.endswith("stock/get"):
                params["secid"] = secid
            else:
                params["secids"] = secid
            resp = requests.get(endpoint, params=params, headers=_headers(), timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if endpoint.endswith("ulist.np/get"):
                diff = (((data or {}).get("data") or {}).get("diff") or [])
                if not diff:
                    raise RuntimeError("diff为空")
                data = {"data": diff[0]}
            return _decode_eastmoney_price(data, symbol, price_scale)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"东方财富A股/ETF实时失败: {last_err}")


def _fetch_tencent_hk_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    code = _hk_code(symbol)
    last_err = None
    for url in [f"https://qt.gtimg.cn/q=hk{code}", f"https://qt.gtimg.cn/q=r_hk{code}", f"http://qt.gtimg.cn/q=hk{code}"]:
        try:
            resp = requests.get(url, headers=_headers("https://gu.qq.com/"), timeout=8)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text.strip()
            data = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = data.split("~")
            price = float(fields[3]) if len(fields) > 3 and fields[3] else 0.0
            if price <= 0:
                raise RuntimeError(f"价格为空: {text[:120]}")
            return round(price * float(price_scale), 4), datetime.now().strftime("%Y-%m-%d")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"腾讯港股实时失败: {last_err}")


def _fetch_eastmoney_hk_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    code = _hk_code(symbol)
    endpoints = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://33.push2.eastmoney.com/api/qt/stock/get",
        "http://push2.eastmoney.com/api/qt/stock/get",
    ]
    params_base = {
        "fields": "f43,f57,f58,f59,f60,f86,f169,f170,f152",
        "fltt": "2",
        "invt": "2",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    last_err = None
    for endpoint in endpoints:
        for secid in _eastmoney_hk_secids(code):
            try:
                params = dict(params_base)
                params["secid"] = secid
                resp = requests.get(endpoint, params=params, headers=_headers("https://quote.eastmoney.com/"), timeout=8)
                resp.raise_for_status()
                return _decode_eastmoney_price(resp.json(), "HK" + code, price_scale)
            except Exception as e:
                last_err = e
    raise RuntimeError(f"东方财富港股实时失败: {last_err}")


# ==================== A股 EasyQuotation 新浪源（替代原东方财富实时） ====================
def _fetch_easyquotation_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str]]:
    """使用 EasyQuotation (新浪源) 获取 A 股/ETF 实时价格"""
    try:
        import easyquotation
    except ImportError as e:
        raise RuntimeError("easyquotation 未安装，请运行 pip install easyquotation") from e

    # 提取纯数字代码
    raw = str(symbol or "").upper().strip()
    if raw.startswith("SH") or raw.startswith("SZ"):
        code = raw[2:]
    else:
        code = raw

    q = easyquotation.use('sina')
    data = q.real(code)
    item = data.get(code)
    if not item:
        raise RuntimeError(f"EasyQuotation(新浪源) 未返回 {symbol} 数据")
    price = float(item.get('now', 0))
    if price <= 0:
        # 非交易时段，尝试使用昨收价
        price = float(item.get('close', 0))
        if price <= 0:
            raise RuntimeError(f"EasyQuotation(新浪源) 返回价格为0: {item}")
    # 新浪源返回的日期格式：'date': '2026-06-02', 'time': '15:00:00'
    date_str = item.get('date')
    time_str = item.get('time')
    quote_date = None
    if date_str:
        if time_str and len(time_str) >= 5:
            quote_date = f"{date_str} {time_str[:5]}"
        else:
            quote_date = date_str
    return round(price * float(price_scale), 4), quote_date
# =========================================================================


def _fetch_a_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str], str]:
    preferred = _load_system_config().get("A_QUOTE_SOURCE", "live_a1")
    sources = _preferred_order([
        ("live_a1", _fetch_tencent_a_realtime_price),
        ("live_a2", _fetch_xueqiu_realtime_price),
        ("live_a3", _fetch_easyquotation_a_realtime_price),   # 替换为 EasyQuotation 新浪源
    ], preferred)
    errors = []
    for key, fn in sources:
        try:
            price, date = fn(symbol, price_scale)
            return price, date, get_source_display_name(key)
        except Exception as e:
            errors.append(f"{get_source_display_name(key)}={e}")
    raise RuntimeError("A股/ETF实时价全部失败: " + "；".join(errors))


def _fetch_hk_realtime_price(symbol: str, price_scale: float) -> Tuple[float, Optional[str], str]:
    preferred = _load_system_config().get("HK_MARKET_SOURCE", "live_hk1")
    sources = _preferred_order([
        ("live_hk1", _fetch_tencent_hk_realtime_price),
        ("live_hk2", _fetch_xueqiu_realtime_price),
        ("live_hk3", _fetch_eastmoney_hk_realtime_price),   # 保留东方财富港股接口
    ], preferred)
    errors = []
    for key, fn in sources:
        try:
            price, date = fn(symbol, price_scale)
            return price, date, get_source_display_name(key)
        except Exception as e:
            errors.append(f"{get_source_display_name(key)}={e}")
    raise RuntimeError("港股实时价全部失败: " + "；".join(errors))


def get_history_snapshot_by_source(symbol: str, days: int = 400, price_scale: float = 1.0, source: str = "") -> MarketSnapshot:
    raw = str(symbol or "").upper().strip()
    cfg = _load_system_config()
    if _is_hk(raw):
        key = normalize_system_source_value("HK_BACKTEST_SOURCE", source or cfg.get("HK_BACKTEST_SOURCE"))
        if key == "historical_hk1":
            return _fetch_tencent_hk_snapshot(raw, days, price_scale)
        if key == "historical_hk2":
            return _fetch_yahoo_hk_snapshot(raw, days, price_scale)
        if key == "historical_hk3":
            try:
                return _parse_yahoo_hk_dividend_snapshot(raw, days, price_scale)
            except Exception as e:
                logging.warning(f"Yahoo港股含权息失败 {raw}: {e}；回退腾讯港股日K")
                snap = _fetch_tencent_hk_snapshot(raw, days, price_scale)
                snap.strategy_status = "OK"
                snap.error = ""
                snap.strategy_source = get_source_display_name('historical_hk1')
                return snap
        raise ValueError(f"不支持的港股回测/策略数据源: {source or key}")
    key = normalize_system_source_value("A_BACKTEST_SOURCE", source or cfg.get("A_BACKTEST_SOURCE"))
    if key == "historical_a1":
        return _fetch_tencent_a_snapshot(raw, days, price_scale)
    if key == "historical_a2":
        return _fetch_sina_a_snapshot(raw, days, price_scale)
    if key == "historical_a3":
        first_error = None
        try:
            return _fetch_baostock_a_snapshot(raw, days, price_scale)
        except Exception as e:
            first_error = e
            logging.warning(f"BaoStock A股含权息失败 {raw}: {e}；回退腾讯A股/ETF日K")
        try:
            snap = _fetch_tencent_a_snapshot(raw, days, price_scale)
            snap.strategy_status = "OK"
            snap.error = ""
            snap.strategy_source = get_source_display_name('historical_a1')
            return snap
        except Exception as e2:
            logging.warning(f"腾讯A股/ETF日K回退失败 {raw}: {e2}；继续回退新浪A股/ETF日K")
            snap = _fetch_sina_a_snapshot(raw, days, price_scale)
            snap.strategy_status = "OK"
            snap.error = ""
            snap.strategy_source = get_source_display_name('historical_a2')
            return snap
    raise ValueError(f"不支持的A股/ETF回测/策略数据源: {source or key}")


def _apply_realtime(snapshot: MarketSnapshot, symbol: str, price_scale: float) -> MarketSnapshot:
    if not snapshot.closes:
        return snapshot
    try:
        if _is_hk(symbol):
            live_price, quote_date, live_source = _fetch_hk_realtime_price(symbol, price_scale)
        else:
            live_price, quote_date, live_source = _fetch_a_realtime_price(symbol, price_scale)
    except Exception as e:
        logging.warning(f"实时价叠加失败 {symbol}: {e}；将使用日K最后收盘价。")
        return snapshot
    if live_price <= 0:
        return snapshot
    closes = list(snapshot.closes)
    closes[-1] = round(live_price, 4)
    dates = list(snapshot.dates or [])
    if quote_date and dates:
        dates[-1] = quote_date
    snap = MarketSnapshot(
        symbol=snapshot.symbol,
        source=live_source,
        closes=closes,
        price_scale=snapshot.price_scale,
        last_bar_date=quote_date or snapshot.last_bar_date,
        error=snapshot.error,
        trade_allowed=snapshot.trade_allowed,
        dates=dates or snapshot.dates,
        strategy_source=snapshot.strategy_source,
        strategy_status=snapshot.strategy_status,
        raw_closes=(list(snapshot.raw_closes[:-1]) + [round(live_price, 4)]) if snapshot.raw_closes and len(snapshot.raw_closes) == len(closes) else snapshot.raw_closes,
        adj_closes=(list(snapshot.adj_closes[:-1]) + [round(live_price, 4)]) if snapshot.adj_closes and len(snapshot.adj_closes) == len(closes) else snapshot.adj_closes,
        dividends=snapshot.dividends,
        split_ratios=snapshot.split_ratios,
    )
    _write_cache(snap)
    return snap


def get_market_snapshot(symbol: str, days: int = 400, price_scale: float = 1.0) -> MarketSnapshot:
    """Return runtime strategy snapshot.

    Historical strategy K data is fetched at most once per symbol/source/day and
    cached before realtime overlay. Realtime price is still refreshed on every
    call according to A_QUOTE_SOURCE / HK_MARKET_SOURCE.
    """
    raw = str(symbol or "").upper().strip()
    cfg = _load_system_config()
    source_key = normalize_system_source_value(
        "HK_BACKTEST_SOURCE" if _is_hk(raw) else "A_BACKTEST_SOURCE",
        cfg.get("HK_BACKTEST_SOURCE" if _is_hk(raw) else "A_BACKTEST_SOURCE"),
    )
    try:
        snap = _read_strategy_history_daily_cache(raw, source_key, days, price_scale)
        if snap is None:
            snap = get_history_snapshot_by_source(raw, days, price_scale, source_key)
            _write_strategy_history_daily_cache(raw, source_key, days, price_scale, snap)
        return _apply_realtime(snap, raw, price_scale)
    except Exception as e:
        return _read_cache(raw, days, price_scale, f"策略/历史数据源失败: {e}")


def _realtime_source_functions_for_symbol(symbol: str):
    raw = str(symbol or "").upper().strip()
    if _is_hk(raw):
        return [
            ("live_hk1", _fetch_tencent_hk_realtime_price),
            ("live_hk2", _fetch_xueqiu_realtime_price),
            ("live_hk3", _fetch_eastmoney_hk_realtime_price),
        ], "HK_MARKET_SOURCE"
    return [
        ("live_a1", _fetch_tencent_a_realtime_price),
        ("live_a2", _fetch_xueqiu_realtime_price),
        ("live_a3", _fetch_easyquotation_a_realtime_price),   # A股使用 EasyQuotation 新浪源
    ], "A_QUOTE_SOURCE"


def get_reference_price_by_source(symbol: str, source_key: str, price_scale: float = 1.0) -> dict:
    """Refresh exactly one realtime source and return the status-card payload."""
    raw = str(symbol or "").upper().strip()
    key = str(source_key or "").strip()
    if not raw:
        raise ValueError("未找到标的代码")
    sources, field = _realtime_source_functions_for_symbol(raw)
    source_map = dict(sources)
    if key not in source_map:
        raise ValueError(f"不支持的实时源: {key}")
    cfg = _load_system_config()
    preferred = cfg.get(field, sources[0][0])
    label = get_source_display_name(key)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        price, date = source_map[key](raw, price_scale)
        return {"key": key, "label": label, "price": price, "source": label, "date": date or "", "updated_at": updated_at, "ok": True, "primary": key == preferred, "error": ""}
    except Exception as e:
        return {"key": key, "label": label, "price": None, "source": label, "date": "", "updated_at": updated_at, "ok": False, "primary": key == preferred, "error": str(e)[:180]}


def get_reference_prices(symbol: str, price_scale: float = 1.0) -> List[dict]:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return []
    sources, _field = _realtime_source_functions_for_symbol(raw)
    return [get_reference_price_by_source(raw, key, price_scale) for key, _fn in sources]


def get_price_from_api(symbol: str, price_scale: float = 1.0, last_known_price=None) -> float:
    if _is_hk(symbol):
        price, _, _ = _fetch_hk_realtime_price(symbol, price_scale)
    else:
        price, _, _ = _fetch_a_realtime_price(symbol, price_scale)
    return price


def get_history_close(symbol: str, days: int = 400, price_scale: float = 1.0) -> List[float]:
    return get_market_snapshot(symbol, days, price_scale=price_scale).closes


def get_hk_history_close(symbol_or_code: str, days: int, price_scale: float = 1.0) -> List[float]:
    return get_history_snapshot_by_source(symbol_or_code, days, price_scale=price_scale, source="historical_hk1").closes


def get_a_history_close(symbol: str, days: int, price_scale: float = 1.0) -> List[float]:
    return get_history_snapshot_by_source(symbol, days, price_scale=price_scale, source="historical_a1").closes


def get_a_price(symbol: str, price_scale: float = 1.0) -> float:
    price, _, _ = _fetch_a_realtime_price(symbol, price_scale)
    return price


def get_hk_price(symbol: str, price_scale: float = 1.0) -> float:
    price, _, _ = _fetch_hk_realtime_price(symbol, price_scale)
    return price


def self_test(symbols: Optional[List[str]] = None, days: int = 30) -> int:
    symbols = symbols or ["SH600036", "HK00700"]
    ok = 0
    for sym in symbols:
        try:
            snap = get_market_snapshot(sym, days=days)
            print(f"PASS {sym}: source={snap.source}, strategy={snap.strategy_source}, count={len(snap.closes)}, last={snap.current_price}, date={snap.last_bar_date}, trade_allowed={snap.trade_allowed}")
            ok += 1
        except Exception as e:
            print(f"FAIL {sym}: {e}")
    return 0 if ok == len(symbols) else 1


if __name__ == "__main__":
    raise SystemExit(self_test())