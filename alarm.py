#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ربات آلارم قیمت به تومان — طلا، سکه، نقره، مس، ارز، کریپتو (تک‌فایلی)

منابع: tgju.org (قیمت بازار ایران) + gold-api.com (انس و مس) + coingecko (کریپتو)
اجرا:  python3 alarm.py --once        یک بار چک کند
        python3 alarm.py --digest      قیمت‌های الان را بفرستد
        python3 alarm.py --test-alert  مسیر ارسال تلگرام را تست کند
        python3 alarm.py --selftest    صحتِ فایل را بعد از کپی‌کردن بررسی کند
        python3 alarm.py --loop        forever (روی کامپیوتر)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

try:
    import requests
    import yaml
except ImportError:  # pragma: no cover
    print("کتابخانه لازم نیست. اجرا کنید:  pip install -r requirements.txt")
    raise SystemExit(1)

HERE = Path(__file__).resolve().parent
TZ_OFFSET_HOURS = 3  # تهران
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept-Language": "fa,en;q=0.8",
}

# ------------------------------------------------------------------ منابع داده
TGJU_URLS = ["https://www.tgju.org/", "https://www.tgju.org/gold-chart/", "https://www.tgju.org/local-markets"]

# اسلاگ در تیکر tgju -> (کلید استاندارد، واحد)
TGJU_MAP = {
    "geram18": ("gold18_toman", "TOMAN"),
    "geram24": ("gold24_toman", "TOMAN"),
    "gold-chart": ("gold18_toman", "TOMAN"),
    "mesghal": ("mesghal_toman", "TOMAN"),
    "gold_melted_transfer": ("melted_toman", "TOMAN"),
    "silver_999": ("silver_g999_toman", "TOMAN"),
    "sekee": ("coin_full_toman", "TOMAN"),
    "retail_sekeb": ("coin_bahar_toman", "TOMAN"),
    "retail_sekee": ("coin_emami_toman", "TOMAN"),
    "retail_nim": ("coin_half_toman", "TOMAN"),
    "retail_rob": ("coin_quarter_toman", "TOMAN"),
    "retail_gerami": ("coin_gram_toman", "TOMAN"),
    "قیمت-دلار": ("usd_toman", "TOMAN"),
    "price_dollar_rl": ("usd_toman", "TOMAN"),
    "crypto-tether": ("tether_toman", "TOMAN"),
    "price_eur": ("eur_toman", "TOMAN"),
    "price_aed": ("aed_toman", "TOMAN"),
    "price_gbp": ("gbp_toman", "TOMAN"),
    "price_try": ("try_toman", "TOMAN"),
    "price_cny": ("cny_toman", "TOMAN"),
    "ons": ("gold_oz_usd", "USD"),
    "silver": ("silver_oz_usd", "USD"),
    "platinum": ("platinum_oz_usd", "USD"),
    "palladium": ("palladium_oz_usd", "USD"),
    "crypto-bitcoin": ("bitcoin_usd", "USD"),
}

# gold-api برای انس و مس (HG)؛ بدون کلید API
METAL_SYMBOLS = {
    "XAU": "gold_oz_usd",
    "XAG": "silver_oz_usd",
    "HG": "copper_lb_usd",
    "XPT": "platinum_oz_usd",
    "XPD": "palladium_oz_usd",
}

PRICE_ATTR_RE = re.compile(r"""data-price=\\?["']([0-9][0-9,\.]*)""")
SLUG_NEAR_RE = re.compile(r"""window\.location=\\?["']([^"'\\]{2,60})\\?["']""")


class Point:
    __slots__ = ("key", "value", "unit", "source", "ts")

    def __init__(self, key, value, unit, source="", ts=None):
        self.key, self.value, self.unit, self.source = key, float(value), unit, source
        self.ts = ts if ts is not None else time.time()


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _retry(fn, tries=3, delay=2.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            last = exc
            wait = delay * (i + 1)
            if "429" in str(exc):
                wait = max(wait, 8.0 + 6 * i)
            if i < tries - 1:
                time.sleep(wait)
    raise RuntimeError(f"منبع در دسترس نیست: {last}")


def _get(url, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_ticker(html):
    out = {}
    for m in PRICE_ATTR_RE.finditer(html):
        head = html[max(0, m.start() - 300) : m.start()]
        found = None
        for sm in SLUG_NEAR_RE.finditer(head):
            found = sm
        if not found:
            continue
        slug = urllib.parse.unquote(found.group(1).strip()).replace("profile/", "")
        out.setdefault(slug, m.group(1))
    return out


def fetch_tgju():
    out, last = {}, None
    for url in TGJU_URLS:
        try:
            html = _retry(lambda u=url: _get(u))
        except Exception as exc:
            last = exc
            continue
        for slug, raw in parse_ticker(html).items():
            meta = TGJU_MAP.get(slug)
            if not meta:
                continue
            key, unit = meta
            val = _num(raw)
            if val is None or key in out:
                continue
            out[key] = Point(key, val, unit, "tgju")
        if len(out) >= 8:
            return out
    if not out:
        raise RuntimeError(f"tgju نرخ نداد ({last or 'ساختار صفحه عوض شده؟'})")
    return out


def fetch_metals():
    out = {}
    for sym, key in METAL_SYMBOLS.items():
        try:
            price = _retry(lambda s=sym: json.loads(_get(f"https://api.gold-api.com/price/{s}", 20))["price"],
                           tries=2, delay=2)
            out[key] = Point(key, price, "USD", f"gold-api {sym}")
        except Exception:
            pass
    return out


def fetch_copper_yahoo():
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        try:
            r = requests.get(
                f"https://{host}/v8/finance/chart/HG=F?interval=1d&range=1d",
                headers={**UA, "Accept": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            price = float(json.loads(r.text)["chart"]["result"][0]["meta"]["regularMarketPrice"])
            if price > 0:
                return {"copper_lb_usd": Point("copper_lb_usd", price, "USD", "yahoo HG=F")}
        except Exception:
            time.sleep(2)
    return {}


def fetch_crypto(ids):
    if not ids:
        return {}
    data = _retry(
        lambda: json.loads(
            _get("https://api.coingecko.com/api/v3/simple/price?ids=" + ",".join(ids) + "&vs_currencies=usd", 25)
        )
    )
    out = {}
    for cid in ids:
        usd = _num((data.get(cid) or {}).get("usd"))
        if usd:
            out[f"{cid}_usd"] = Point(f"{cid}_usd", usd, "USD", "coingecko")
    return out


def cache_file():
    return HERE / "out" / "cache.json"


def load_cache():
    fp = cache_file()
    if not fp.exists():
        return {}
    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: Point(k, v["value"], v.get("unit", "TOMAN"), v.get("source", "cache"), float(v.get("ts", 0)))
            for k, v in raw.items()}


def save_cache(points):
    fp = cache_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: {"value": p.value, "unit": p.unit, "source": p.source, "ts": p.ts}
            for k, p in points.items() if time.time() - p.ts <= 6 * 3600}
    fp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def collect(cfg):
    """همه منابع + معادل تومانی + کشِ نجات‌بخش."""
    settings = cfg.get("settings", {})
    usd_basis = settings.get("usd_basis", "tether")
    crypto = sorted(set(settings.get("crypto_ids") or []) | set(crypto_from_watch(cfg)))
    points, errors = {}, []

    for name, fn in (("tgju", fetch_tgju), ("metals", fetch_metals)):
        try:
            points.update(fn())
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if "copper_lb_usd" not in points:
        points.update(fetch_copper_yahoo())
    if "copper_lb_usd" in points:
        lb = points["copper_lb_usd"]
        points["copper_kg_usd"] = Point("copper_kg_usd", lb.value * 2.2046226218, "USD", lb.source, lb.ts)
    try:
        points.update(fetch_crypto(crypto))
    except Exception as exc:
        errors.append(f"coingecko: {exc}")

    rate = points.get("tether_toman" if usd_basis == "tether" else "usd_toman") or points.get(
        "tether_toman") or points.get("usd_toman")
    if rate:
        for key in [k for k, p in points.items() if p.unit == "USD"]:
            base = points[key]
            points[key[: -len("_usd")] + "_toman"] = Point(
                key[: -len("_usd")] + "_toman", base.value * rate.value, "TOMAN",
                f"{base.source} × نرخ ارز", base.ts)
        if "gold_oz_usd" in points:
            points["gold18_calc_toman"] = Point(
                "gold18_calc_toman", points["gold_oz_usd"].value / 31.1034768 * 0.750 * rate.value,
                "TOMAN", "انس × ۰٫۷۵ × نرخ ارز")

    # آیتمی که coingecko دارد ولی کلیدش فرق می‌کند
    for key, wc in (cfg.get("watch") or {}).items():
        cid = (wc or {}).get("coingecko")
        if not cid:
            continue
        src = f"{cid}_toman" if key.endswith("toman") else f"{cid}_usd"
        if src in points:
            base = points[src]
            points[key] = Point(key, base.value, base.unit, base.source, base.ts)

    # اگر منبعی قطع بود، آخرین مقدار سالم
    stale = []
    for key, p in load_cache().items():
        if key not in points:
            points[key] = Point(key, p.value, p.unit, f"کش ({int((time.time() - p.ts) / 60)} دقیقه پیش)", p.ts)
            stale.append(key)
    if stale:
        errors.append("از کش استفاده شد: " + ", ".join(sorted(stale)))
    if points:
        save_cache(points)
    return points, errors


def crypto_from_watch(cfg):
    native = {k for k, _ in TGJU_MAP.values()} | set(METAL_SYMBOLS.values()) | {
        "copper_kg_usd", "gold18_calc_toman"}
    native |= {k[: -len("_usd")] + "_toman" for k in native if k.endswith("_usd")}
    ids = []
    for key, wc in (cfg.get("watch") or {}).items():
        if key in native:
            continue
        cid = (wc or {}).get("coingecko")
        base = cid or (key[: -len("_toman")] if key.endswith("_toman") else key)
        base = base[: -len("_usd")] if base.endswith("_usd") else base
        if base:
            ids.append(base)
    return ids


# ------------------------------------------------------------------- قوانین
def tehran_now(fmt="%Y-%m-%d %H:%M"):
    return (datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)).strftime(fmt)


def fmt(value, unit="TOMAN"):
    if unit == "USD":
        return f"{value:,.2f} دلار"
    return f"{value:,.0f} تومان" if abs(value) >= 1000 else f"{value:,.2f} تومان"


def note_line(note):
    return f"\n📝 یادداشت شما: {note}" if note else ""


def state_path():
    return HERE / "out" / "state.json"


def load_state():
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rules": {}, "daily": {}}


def save_state(state):
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def evaluate(points, watch, state, now=None, rearm_pct=0.4, cooldown_min=60.0):
    now = now or time.time()
    rules = state.setdefault("rules", {})
    daily = state.setdefault("daily", {})
    today = date.fromtimestamp(now + TZ_OFFSET_HOURS * 3600).isoformat()
    fires, warns = [], []

    for key, wc in watch.items():
        p = points.get(key)
        label = (wc or {}).get("label", key)
        if p is None:
            warns.append(f"نرخ «{label}» این نوبت خوانده نشد ({key})")
            continue
        price, unit = float(p.value), p.unit
        item_note = str((wc or {}).get("note") or "").strip()

        if daily.get(key, {}).get("date") != today:
            daily[key] = {"date": today, "open": price}
        day_open = float(daily[key]["open"]) or price

        for idx, rule in enumerate((wc or {}).get("alerts") or []):
            kind = next((k for k in ("above", "below", "pct") if k in rule), None)
            if kind is None:
                warns.append(f"{key}[{idx}]: قانون نامعتبر {rule}")
                continue
            rid = f"{key}#{idx}#{kind}"
            rs = rules.setdefault(rid, {"fired": False, "ts": 0})
            cool_ok = (now - float(rs.get("ts", 0))) / 60.0 >= cooldown_min
            note = str(rule.get("note") or item_note).strip()

            if kind in ("above", "below"):
                target = float(rule[kind])
                crossed = price >= target if kind == "above" else price <= target
                edge = target * (1 - rearm_pct / 100.0) if kind == "above" else target * (1 + rearm_pct / 100.0)
                if rs.get("fired") and (price < edge if kind == "above" else price > edge):
                    rs["fired"] = False
                if crossed and not rs.get("fired") and cool_ok:
                    rs.update(fired=True, ts=now)
                    pct = (price / target - 1) * 100 if target else 0
                    arrow, verb = ("▲", "از سقف تعیین‌شده گذشت") if kind == "above" else ("▼", "به کف تعیین‌شده رسید")
                    fires.append(
                        f"⏱ {tehran_now('%Y-%m-%d %H:%M')}\n"
                        f"{arrow} {label} {verb}\n"
                        f"قیمت فعلی: {fmt(price, unit)}\n"
                        f"قیمت هدف شما: {fmt(target, unit)}   (فاصله {pct:+.2f}٪)" + note_line(note))
                elif crossed and not rs.get("fired"):
                    warns.append(f"{label}: شرط برقرار است ولی در زمان استراحت (cooldown) هستیم")
                continue

            pct_target = float(rule["pct"])
            move = (price / day_open - 1.0) * 100.0
            only = rule.get("direction", "both")
            dir_ok = only == "both" or (only == "up" and move > 0) or (only == "down" and move < 0)
            already = rs.get("day") == today and rs.get("fired")
            need = pct_target * (2.0 if already else 1.0)
            if dir_ok and abs(move) >= need and cool_ok:
                rs.update(fired=True, ts=now, day=today)
                arrow = "▲" if move > 0 else "▼"
                fires.append(
                    f"⏱ {tehran_now('%Y-%m-%d %H:%M')}\n"
                    f"{arrow} {label}: {move:+.2f}٪ نسبت به بازگشایی امروز\n"
                    f"قیمت فعلی: {fmt(price, unit)}\n"
                    f"نرخ بازگشایی امروز: {fmt(day_open, unit)}" + note_line(note))
            elif rs.get("day") != today:
                rs.update(fired=False, day=today)

    return fires, warns


# ------------------------------------------------------------------- ارسال
def send_telegram(text, token, chat_id):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=20,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description")))
    return f"تلگرام ✓ (پیام شماره {data['result']['message_id']})"


def send_log(text, path):
    fp = Path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(f"### {datetime.now().isoformat(timespec='seconds')}\n{text}\n\n")
    return f"لاگ فایل ✓ ({fp.name})"


def dispatch(cfg, text, subject="آلارم قیمت"):
    notify = cfg.get("notify", {}) or {}
    tg = notify.get("telegram", {}) or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or str(tg.get("bot_token") or "")
    token = token.strip() if token else ""
    if token.startswith("${"):
        token = ""
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id") or "")
    results = {}
    for ch in notify.get("channels", ["telegram", "file"]):
        if ch == "telegram":
            if not token or not chat:
                results[ch] = "تلگرام ✗ — توکن یا chat_id تنظیم نشده"
                continue
            try:
                results[ch] = send_telegram(text, token, str(chat).strip())
            except Exception as exc:
                results[ch] = f"تلگرام ✗ — {exc}"
        elif ch == "file":
            results[ch] = send_log(text, HERE / (notify.get("file", {}) or {}).get("path", "out/alerts.log"))
    return results


def load_config(path):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg.setdefault("settings", {})
    cfg.setdefault("watch", {})
    cfg["notify"] = cfg.get("notify") or {}
    return cfg


# ------------------------------------------------------------------- دستورها
def cmd_once(cfg, quiet=False):
    st = cfg["settings"]
    points, errors = collect(cfg)
    state = load_state()
    fires, warns = evaluate(
        points, cfg["watch"], state,
        rearm_pct=float(st.get("rearm_pct", 0.4)), cooldown_min=float(st.get("cooldown_minutes", 60)))
    save_state(state)

    if not quiet:
        print(f"قیمت‌ها — {tehran_now('%Y-%m-%d %H:%M')} (تهران)")
        for key, wc in cfg["watch"].items():
            p = points.get(key)
            unit = p.unit if p else ("USD" if key.endswith("_usd") else "TOMAN")
            limits = "، ".join(
                f"{w} {fmt(float(v), unit) if w != 'pct' else f'{v}٪'}"
                for rule in (wc or {}).get("alerts") or [] for w, v in rule.items() if w in ("above", "below", "pct"))
            note = str((wc or {}).get("note") or "").strip()
            line = f"  • {(wc or {}).get('label', key)}: {fmt(p.value, p.unit) if p else 'ناموفق'}"
            if limits:
                line += f"   |   {limits}"
            if note:
                line += f"   |   📝 {note}"
            print(line)
        for w in warns:
            print(f"⚠️  {w}")
        for e in errors:
            print(f"ℹ️  {e}")

    if fires:
        text = "\n\n———\n\n".join(fires)
        print("\n🔔 " + text[:20] + "\n")
        for ch, res in dispatch(cfg, text).items():
            print(f"   ارسال → {res}")
        return len(fires)
    if not quiet:
        print("\n✅ آلارمی فعال نشد.")
    return 0


def cmd_digest(cfg):
    points, errors = collect(cfg)
    lines = [f"📊 گزارش قیمت‌ها — {tehran_now()} (تهران)"]
    for key, wc in cfg["watch"].items():
        p = points.get(key)
        unit = p.unit if p else ("USD" if key.endswith("_usd") else "TOMAN")
        line = f"• {(wc or {}).get('label', key)}: {fmt(p.value, p.unit) if p else 'ناموفق'}"
        note = str((wc or {}).get("note") or "").strip()
        lines.append(line + (f"  📝 {note}" if note else ""))
    if errors:
        lines.append("⚠️ " + " | ".join(errors))
    text = "\n".join(lines)
    print(text)
    for ch, res in dispatch(cfg, text, "گزارش قیمت").items():
        print(f"   ارسال → {res}")


def cmd_test_alert(cfg):
    text = (f"🧪 تست آلارم — {tehran_now()}\n"
            "اگر این پیام را در تلگرام دیدید، مسیر ارسال کاملاً درست کار می‌کند.\n"
            "📝 یادداشت شما: از این به بعد آلارم‌ها همین‌طور می‌آیند")
    print(text)
    print()
    for ch, res in dispatch(cfg, text, "تست آلارم قیمت").items():
        print(f"ارسال → {res}")


def cmd_selftest():
    """بعد از کپی‌کردن فایل این را اجرا کنید؛ اگر همه ✔ شد، فایل کامل و سالم است."""
    ok = True
    P = Point("gold18_toman", 250, "TOMAN")
    watch = {"gold18_toman": {"label": "طلا", "note": "یادداشت آیتم", "alerts": [{"above": 240}, {"below": 200}]}}
    st = {"rules": {}, "daily": {}}
    f, w = evaluate({"gold18_toman": P}, watch, st)
    ok &= len(f) == 1 and "از سقف" in f[0] and "📝 یادداشت شما: یادداشت آیتم" in f[0]
    f2, _ = evaluate({"gold18_toman": Point("gold18_toman", 251, "TOMAN")}, watch, st)
    ok &= f2 == []                      # تکراری نمی‌فرستد
    f3, _ = evaluate({"gold18_toman": Point("gold18_toman", 230, "TOMAN")}, watch, st)
    f4, _ = evaluate({"gold18_toman": Point("gold18_toman", 245, "TOMAN")}, watch, st, now=time.time() + 3600 * 2)
    ok &= len(f4) == 1                  # بعد از بازگشت قیمت، دوباره آلارم می‌دهد
    fp, fn = evaluate({"gold18_toman": Point("gold18_toman", 199, "TOMAN")}, watch, {"rules": {}, "daily": {}})
    ok &= len(fp) == 1 and "به کف" in fp[0]
    _, w5 = evaluate({}, watch, {"rules": {}, "daily": {}})
    ok &= any("خوانده نشد" in x for x in w5)      # آیتم غایب => هشدار، نه خطا
    ok &= fmt(236000000) == "236,000,000 تومان"   # واحد تومان
    ok &= fmt(4100, "USD") == "4,100.00 دلار"      # واحد دلار
    # پارسر تیکر باید هر دو شکلِ نقل‌قول (ساده و اسکیپ‌شده) را بفهمد
    q, bs = chr(34), chr(92)
    plain = f"window.location={chr(39)}profile/sekee{chr(39)}{q} data-price={q}2,415,100,000{q}"
    escaped = f"window.location={bs}{chr(39)}profile/sekee{bs}{chr(39)}{q} data-price={q}2,415,100,000{q}"
    ok &= parse_ticker(plain).get("sekee") == "2,415,100,000"
    ok &= parse_ticker(escaped).get("sekee") == "2,415,100,000"
    print("✅ فایل سالم است؛ هر ۱۰ بررسی پاس شد" if ok else "❌ فایل ناقص کپی شده! دوباره کپی کنید")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="آلارم قیمت به تومان")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return cmd_selftest()
    cfg = load_config(a.config)
    if a.test_alert:
        cmd_test_alert(cfg)
    elif a.digest:
        cmd_digest(cfg)
    elif a.loop:
        step = float(cfg["settings"].get("interval_minutes", 5)) * 60
        while True:
            try:
                cmd_once(cfg, quiet=a.quiet)
            except Exception as exc:
                print(f"❌ خطا در این نوبت: {exc}")
            time.sleep(step)
    else:
        cmd_once(cfg, quiet=a.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
