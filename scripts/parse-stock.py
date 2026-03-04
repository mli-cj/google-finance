#!/usr/bin/env python3
# SECURITY MANIFEST
# Accessed variables : none (no environment variables read)
# File operations    : READ/WRITE ~/.openclaw/workspace/stock-tracker-state.json only
# Network            : none (this script is offline; fetching is done by the agent browser)
# External endpoints : none
# Credentials        : none handled or stored
# User data leaving machine: none
"""
parse-stock.py — Helper script for the google-finance OpenClaw skill.

Usage:
  python3 parse-stock.py --symbol AAPL:NASDAQ
  python3 parse-stock.py --symbol AAPL:NASDAQ --json
  python3 parse-stock.py --list
  python3 parse-stock.py --add AAPL:NASDAQ
  python3 parse-stock.py --remove TSLA:NASDAQ
  python3 parse-stock.py --update-state --symbol AAPL:NASDAQ --price 182.30 --change 1.4

This script handles:
  1. State file read/write (~/.openclaw/workspace/stock-tracker-state.json)
  2. Volume/price string parsing (e.g. "62.3M" → 62300000)
  3. 52-week position calculation
  4. Scoring calculation based on analysis-framework.md rules
  5. Formatted report generation
"""

import json
import os
import sys
import argparse
import math
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# State management
# ──────────────────────────────────────────────────────────────────────────────

STATE_PATH = Path.home() / ".openclaw" / "workspace" / "stock-tracker-state.json"

DEFAULT_WATCHLIST = ["NVDA:NASDAQ", "AAPL:NASDAQ", "META:NASDAQ", "GOOGL:NASDAQ"]

def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    # First run: initialise with the four default stocks
    return {"watchlist": list(DEFAULT_WATCHLIST), "lastChecked": None, "snapshots": {}}

def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def add_symbol(symbol: str):
    symbol = symbol.upper()
    state = load_state()
    if symbol not in state["watchlist"]:
        state["watchlist"].append(symbol)
        save_state(state)
        print(f"✅ Added {symbol} to watchlist.")
    else:
        print(f"ℹ️  {symbol} already in watchlist.")

def remove_symbol(symbol: str):
    symbol = symbol.upper()
    state = load_state()
    if symbol in state["watchlist"]:
        state["watchlist"].remove(symbol)
        state["snapshots"].pop(symbol, None)
        save_state(state)
        print(f"🗑️  Removed {symbol} from watchlist.")
    else:
        print(f"⚠️  {symbol} not found in watchlist.")

def list_watchlist():
    state = load_state()
    if not state["watchlist"]:
        print("📋 Watchlist is empty. Use --add SYMBOL:EXCHANGE to add stocks.")
    else:
        print("📋 Current watchlist:")
        for sym in state["watchlist"]:
            snap = state["snapshots"].get(sym)
            if snap:
                ts = snap.get("ts", "never")
                price = snap.get("price", "?")
                change = snap.get("change_pct", "?")
                print(f"  • {sym:<20} ${price:<10} {change:+.1f}%  (last: {ts})")
            else:
                print(f"  • {sym:<20} (no data yet)")

def update_snapshot(symbol: str, price: float, change_pct: float,
                    extra: dict = None):
    symbol = symbol.upper()
    state = load_state()
    state["lastChecked"] = datetime.now(timezone.utc).isoformat()
    state["snapshots"][symbol] = {
        "price": price,
        "change_pct": change_pct,
        "ts": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }
    save_state(state)

# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

def parse_volume(s: str) -> float:
    """Parse '62.3M', '1.2B', '450K' to float."""
    s = s.replace(",", "").strip()
    multipliers = {"B": 1e9, "M": 1e6, "K": 1e3}
    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            try:
                return float(s[:-1]) * mult
            except ValueError:
                return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def parse_price(s: str) -> float:
    """Parse '$182.30' or '182.30' to float."""
    return float(s.replace("$", "").replace(",", "").strip())

def parse_change_pct(s: str) -> float:
    """Parse '+1.40%' or '-2.3%' to float."""
    return float(s.replace("%", "").replace("+", "").strip())

def week52_position(price: float, low: float, high: float) -> float:
    """Returns 0.0–1.0 position within 52-week range."""
    if high <= low:
        return 0.5
    return (price - low) / (high - low)

# ──────────────────────────────────────────────────────────────────────────────
# Scoring engine
# ──────────────────────────────────────────────────────────────────────────────

SECTOR_PE = {
    "technology": 28, "consumer discretionary": 24, "healthcare": 20,
    "financials": 14, "energy": 12, "utilities": 16, "industrials": 20,
}

def score_momentum(change_pct: float, position_52w: float) -> tuple[int, list[str]]:
    reasons = []
    score = 0

    if change_pct > 3:
        score += 2; reasons.append(f"✅ Strong daily gain (+{change_pct:.1f}%)")
    elif change_pct > 1:
        score += 1; reasons.append(f"✅ Positive daily change (+{change_pct:.1f}%)")
    elif change_pct < -3:
        score -= 2; reasons.append(f"❌ Sharp daily drop ({change_pct:.1f}%)")
    elif change_pct < -1:
        score -= 1; reasons.append(f"❌ Negative daily change ({change_pct:.1f}%)")
    else:
        reasons.append(f"→ Flat day ({change_pct:+.1f}%)")

    if position_52w is not None:
        if position_52w > 0.90:
            score -= 1; reasons.append(f"⚠️  Near 52-week high ({position_52w*100:.0f}%) — overbought")
        elif position_52w > 0.75:
            score += 1; reasons.append(f"✅ In upper 25% of 52-week range ({position_52w*100:.0f}%)")
        elif position_52w < 0.10:
            score += 1; reasons.append(f"✅ Near 52-week low ({position_52w*100:.0f}%) — oversold bounce potential")
        elif position_52w < 0.25:
            score -= 1; reasons.append(f"⚠️  In lower 25% of 52-week range ({position_52w*100:.0f}%)")

    return score, reasons

def score_volume(current_vol: float, avg_vol: float,
                  change_pct: float) -> tuple[int, list[str]]:
    if avg_vol <= 0 or current_vol <= 0:
        return 0, []
    ratio = current_vol / avg_vol
    score = 0
    reasons = []
    up = change_pct >= 0

    if ratio > 2.0:
        if up:
            score += 2; reasons.append(f"✅ Volume {ratio:.1f}× avg with price up (strong buying)")
        else:
            score -= 2; reasons.append(f"❌ Volume {ratio:.1f}× avg with price down (strong selling)")
    elif ratio > 1.5:
        if up:
            score += 1; reasons.append(f"✅ Volume {ratio:.1f}× avg, price up")
        else:
            score -= 1; reasons.append(f"❌ Volume {ratio:.1f}× avg, price down")
    elif ratio < 0.5:
        score -= 1; reasons.append(f"⚠️  Low volume {ratio:.1f}× avg (low conviction)")
    else:
        reasons.append(f"→ Normal volume ({ratio:.1f}× avg)")

    return score, reasons

def score_valuation(pe: float, sector: str = "default") -> tuple[int, list[str]]:
    if pe is None:
        return 0, []
    benchmark = SECTOR_PE.get(sector.lower(), 22)
    reasons = []
    score = 0

    if pe < 0:
        score -= 1; reasons.append(f"⚠️  Negative P/E (company reporting losses)")
    else:
        ratio = pe / benchmark
        if ratio < 0.7:
            score += 2; reasons.append(f"✅ P/E {pe:.1f}× — undervalued vs sector ({benchmark}×)")
        elif ratio < 1.0:
            score += 1; reasons.append(f"✅ P/E {pe:.1f}× — below sector avg ({benchmark}×)")
        elif ratio < 1.5:
            reasons.append(f"→ P/E {pe:.1f}× — inline with sector ({benchmark}×)")
        elif ratio < 2.0:
            score -= 1; reasons.append(f"⚠️  P/E {pe:.1f}× — elevated vs sector ({benchmark}×)")
        else:
            score -= 2; reasons.append(f"❌ P/E {pe:.1f}× — significantly above sector ({benchmark}×)")

    return score, reasons

POSITIVE_KEYWORDS = [
    "beat", "record", "growth", "partnership", "raised guidance",
    "buyback", "dividend", "approved", "launched", "upgrade",
    "exceeded", "strong demand", "acquisition",
]
NEGATIVE_KEYWORDS = [
    "miss", "cut guidance", "layoffs", "recall", "investigation",
    "sec", "lawsuit", "fine", "downgrade", "loss",
    "bankruptcy", "resigned", "delay", "tariff", "ban",
]
OVERRIDES = [
    (["earnings beat", "beat estimates"], +2),
    (["earnings miss", "missed estimates"], -2),
    (["merger", "acquisition"], +3),
    (["bankruptcy", "chapter 11"], -5),
    (["ceo resign", "cfo resign"], -2),
    (["stock split"], +1),
    (["fda approved"], +3),
    (["fda rejected"], -4),
]

def score_news(headlines: list[str]) -> tuple[int, list[str]]:
    pos = 0
    neg = 0
    override_score = 0
    reasons = []
    combined = " ".join(h.lower() for h in headlines)

    for kw in POSITIVE_KEYWORDS:
        if kw in combined:
            pos += 1
    for kw in NEGATIVE_KEYWORDS:
        if kw in combined:
            neg += 1

    for triggers, val in OVERRIDES:
        for trigger in triggers:
            if trigger in combined:
                override_score += val
                reasons.append(f"{'✅' if val > 0 else '❌'} Override: '{trigger}' detected in news ({val:+d})")
                break

    raw = pos * 0.5 - neg * 0.5
    score = max(-3, min(3, int(math.ceil(raw + override_score))))

    if pos > 0 or neg > 0:
        reasons.append(f"→ News: {pos} positive / {neg} negative keywords")

    return score, reasons

def compute_signal(total: int) -> tuple[str, str]:
    if total >= 4:
        signal = "🟢 BUY"
    elif total <= -4:
        signal = "🔴 SELL"
    else:
        signal = "🟡 HOLD"

    abs_t = abs(total)
    if abs_t >= 7:
        confidence = "HIGH"
    elif abs_t >= 4:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return signal, confidence

# ──────────────────────────────────────────────────────────────────────────────
# Report formatter
# ──────────────────────────────────────────────────────────────────────────────

def build_report(symbol: str, data: dict) -> str:
    price = data.get("price", 0.0)
    change_pct = data.get("change_pct", 0.0)
    volume = data.get("volume", 0.0)
    avg_volume = data.get("avg_volume", 0.0)
    pe = data.get("pe")
    week52_high = data.get("week52_high")
    week52_low = data.get("week52_low")
    company_name = data.get("company_name", symbol)
    sector = data.get("sector", "default")
    headlines = data.get("headlines", [])

    pos52 = None
    if week52_high and week52_low:
        pos52 = week52_position(price, week52_low, week52_high)

    m_score, m_reasons = score_momentum(change_pct, pos52)
    v_score, v_reasons = score_volume(volume, avg_volume, change_pct)
    val_score, val_reasons = score_valuation(pe, sector)
    n_score, n_reasons = score_news(headlines)

    total = max(-10, min(10, m_score + v_score + val_score + n_score))
    signal, confidence = compute_signal(total)

    lines = [
        "─" * 54,
        f"📈 {symbol} ({company_name})",
        "─" * 54,
        f"Price:        ${price:,.2f}   ({change_pct:+.1f}% today)",
    ]

    if week52_high and week52_low:
        lines.append(f"52-week:      ${week52_low:,.2f} – ${week52_high:,.2f}  "
                     f"(position: {(pos52 or 0)*100:.0f}%)")
    if volume > 0:
        vol_str = f"{volume/1e6:.1f}M" if volume >= 1e6 else f"{volume/1e3:.0f}K"
        avg_str = (f" ({volume/avg_volume:.1f}× avg)" if avg_volume > 0 else "")
        lines.append(f"Volume:       {vol_str}{avg_str}")

    lines += [
        "",
        "Scoring:",
        f"  Momentum    {m_score:+d}",
        f"  Volume      {v_score:+d}",
        f"  Valuation   {val_score:+d}",
        f"  News        {n_score:+d}",
        "  " + "─" * 16,
        f"  Total       {total:+d}  →  {signal}  [Confidence: {confidence}]",
        "",
        "Factors:",
    ]
    for r in m_reasons + v_reasons + val_reasons + n_reasons:
        lines.append(f"  {r}")

    if headlines:
        lines += ["", "Top headlines:"]
        for h in headlines[:5]:
            lines.append(f"  • {h}")

    lines += ["", "Recommendation:"]

    if "BUY" in signal:
        sl = price * 0.95
        t1 = price * 1.08
        t2 = (week52_high * 1.02) if week52_high else price * 1.15
        lines.append(f"  Consider buying. Stop-loss: ${sl:,.2f}  "
                     f"| Target 1: ${t1:,.2f}  | Target 2: ${t2:,.2f}")
    elif "SELL" in signal:
        cover = price * 0.92
        lines.append(f"  Consider reducing position. Cover target: ${cover:,.2f}")
    else:
        lines.append("  Hold current position. Wait for stronger signal.")

    lines += [
        "",
        "─" * 54,
        "⚠️  Not financial advice. Data sourced from Google Finance.",
        "─" * 54,
    ]
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _safe_symbol(value: str) -> str:
    """Validate symbol format (e.g. AAPL:NASDAQ) to prevent path/shell injection."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9.^]{1,12}(:[A-Za-z]{1,8})?", value):
        raise argparse.ArgumentTypeError(
            f"Invalid symbol format: {value!r}. Expected e.g. AAPL:NASDAQ"
        )
    return value.upper()

def main():
    parser = argparse.ArgumentParser(description="Google Finance skill helper")
    parser.add_argument("--symbol",  type=_safe_symbol, help="Symbol:Exchange (e.g. AAPL:NASDAQ)")
    parser.add_argument("--add",     type=_safe_symbol, help="Add symbol to watchlist")
    parser.add_argument("--remove",  type=_safe_symbol, help="Remove symbol from watchlist")
    parser.add_argument("--list",    action="store_true", help="List watchlist")
    parser.add_argument("--json",    action="store_true", help="Output as JSON")
    parser.add_argument("--update-state", action="store_true")
    parser.add_argument("--price",   type=float)
    parser.add_argument("--change",  type=float)
    # Pass full data as JSON string for report generation
    parser.add_argument("--data",    help="JSON string with stock data for report")
    args = parser.parse_args()

    if args.add:
        add_symbol(args.add)
    elif args.remove:
        remove_symbol(args.remove)
    elif args.list:
        list_watchlist()
    elif args.update_state and args.symbol and args.price is not None:
        update_snapshot(args.symbol, args.price, args.change or 0.0)
        print(f"✅ Snapshot updated for {args.symbol}")
    elif args.data and args.symbol:
        data = json.loads(args.data)
        report = build_report(args.symbol.upper(), data)
        if args.json:
            total = max(-10, min(10,
                score_momentum(data.get("change_pct", 0), None)[0] +
                score_volume(data.get("volume", 0), data.get("avg_volume", 0),
                             data.get("change_pct", 0))[0] +
                score_valuation(data.get("pe"), data.get("sector", "default"))[0] +
                score_news(data.get("headlines", []))[0]
            ))
            signal, confidence = compute_signal(total)
            print(json.dumps({"symbol": args.symbol, "signal": signal,
                               "confidence": confidence, "score": total,
                               "report": report}))
        else:
            print(report)
    else:
        # Self-test: demo report
        demo = {
            "price": 182.30, "change_pct": 1.4,
            "volume": 62_300_000, "avg_volume": 48_000_000,
            "pe": 28.5, "sector": "technology",
            "week52_high": 199.62, "week52_low": 143.90,
            "company_name": "Apple Inc.",
            "headlines": [
                "Apple beats Q2 revenue estimates by 4% — Reuters (3h)",
                "iPhone 17 demand stronger than expected — Bloomberg (6h)",
                "EU antitrust probe expanded to App Store — FT (8h)",
            ]
        }
        print(build_report("AAPL:NASDAQ", demo))

if __name__ == "__main__":
    main()
