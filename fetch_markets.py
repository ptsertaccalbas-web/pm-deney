"""
Adım 1 — Polymarket'ten aktif piyasaları çek, ham JSON + CSV olarak kaydet.

Her çalışmada:
  data/raw/YYYY-MM-DD.json        -> API'nin döndürdüğü ham veri (alan adları değişse bile kaybolmaz)
  data/snapshots/YYYY-MM-DD.csv   -> ayrıştırılmış günlük özet
  data/prices.csv                 -> tüm günlerin birleşik tablosu (append)
  data/run_log.csv                -> her çalışmanın gerçek zamanı, başarı/hata durumu

Kural: bir gün başarısız olursa run_log'a "HATA" yazılır, veri sessizce doldurulmaz.
"""
import csv
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

GAMMA_URL = (
    "https://gamma-api.polymarket.com/markets"
    "?active=true&closed=false&limit={limit}&order=volume24hr&ascending=false"
)
LIMIT = int(os.environ.get("PM_LIMIT", "100"))
DATA_DIR = os.environ.get("PM_DATA_DIR", "data")
MOCK_FILE = os.environ.get("PM_MOCK_FILE")  # test için: API yerine bu dosyayı oku

FIELDS = [
    "run_ts_utc", "date", "market_id", "question", "slug", "end_date",
    "outcome_yes", "price_yes", "outcome_no", "price_no",
    "volume_24h", "volume_total", "liquidity",
]


def now_utc():
    return datetime.now(timezone.utc)


def fetch_markets():
    if MOCK_FILE:
        with open(MOCK_FILE, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(
        GAMMA_URL.format(limit=LIMIT),
        headers={"User-Agent": "pm-deney/0.1 (research)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return ""


def _jsonlist(x):
    """Gamma bazı alanları JSON-kodlu string olarak döndürür ('["Yes","No"]')."""
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse(markets, run_ts):
    rows, skipped = [], 0
    date = run_ts.strftime("%Y-%m-%d")
    for m in markets:
        outcomes = _jsonlist(m.get("outcomes"))
        prices = _jsonlist(m.get("outcomePrices"))
        if len(outcomes) < 2 or len(prices) < 2:
            skipped += 1
            continue
        rows.append({
            "run_ts_utc": run_ts.isoformat(timespec="seconds"),
            "date": date,
            "market_id": m.get("id", ""),
            "question": (m.get("question") or "").strip(),
            "slug": m.get("slug", ""),
            "end_date": m.get("endDate", ""),
            "outcome_yes": outcomes[0],
            "price_yes": _num(prices[0]),
            "outcome_no": outcomes[1],
            "price_no": _num(prices[1]),
            "volume_24h": _num(m.get("volume24hr")),
            "volume_total": _num(m.get("volumeNum", m.get("volume"))),
            "liquidity": _num(m.get("liquidityNum", m.get("liquidity"))),
        })
    return rows, skipped


def write_csv(path, rows, append=False):
    exists = os.path.exists(path)
    mode = "a" if append else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not append or not exists:
            w.writeheader()
        w.writerows(rows)


def log_run(run_ts, status, n_rows, n_skipped, note=""):
    path = os.path.join(DATA_DIR, "run_log.csv")
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["run_ts_utc", "status", "rows", "skipped", "note"])
        w.writerow([run_ts.isoformat(timespec="seconds"), status, n_rows, n_skipped, note])


def main():
    run_ts = now_utc()
    date = run_ts.strftime("%Y-%m-%d")
    os.makedirs(os.path.join(DATA_DIR, "raw"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "snapshots"), exist_ok=True)

    try:
        markets = fetch_markets()
    except Exception as e:  # ne olursa olsun run_log'a HATA düşmeli
        log_run(run_ts, "HATA", 0, 0, f"fetch: {type(e).__name__}: {e}")
        print(f"HATA: veri çekilemedi: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(markets, list):
        log_run(run_ts, "HATA", 0, 0, "beklenmeyen yanıt tipi")
        print("HATA: API liste döndürmedi", file=sys.stderr)
        sys.exit(1)

    with open(os.path.join(DATA_DIR, "raw", f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump(markets, f, ensure_ascii=False)

    rows, skipped = parse(markets, run_ts)
    write_csv(os.path.join(DATA_DIR, "snapshots", f"{date}.csv"), rows)
    write_csv(os.path.join(DATA_DIR, "prices.csv"), rows, append=True)

    status = "OK" if rows else "HATA"
    log_run(run_ts, status, len(rows), skipped)
    print(f"{status}: {len(rows)} piyasa yazıldı, {skipped} atlandı ({date})")
    if not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
