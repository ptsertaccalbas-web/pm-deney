"""
Adım 2 — Evreni dondur, evrendeki piyasaların fiyatını her gün kaydet, Gemini ile p̂ üret.

İlk çalışmada:
  data/universe.json           -> dondurulmuş piyasa listesi (bir daha değişmez)
Her çalışmada:
  data/universe_prices.csv     -> evrendeki her piyasanın o günkü fiyatı / kapanış durumu (append)
  data/universe_raw/DATE.json  -> evren piyasalarının ham API kayıtları (çözümleme bilgisi burada)
  data/predictions.csv         -> model tahminleri: p̂, o anki piyasa fiyatı, gerekçe (append)
  data/pred_raw/DATE.jsonl     -> Gemini'nin ham cevapları
  data/run_log.csv             -> "predict" satırı: kaç tahmin üretildi, kaç hata

Kurallar:
- Model piyasa fiyatını GÖRMEZ (market-blind). Skill = piyasaya karşı ölçülecek, ona bakarak tahmin anlamsız.
- Gemini hata verirse o piyasa için o gün p̂ boş kalır ve HATA sayılır; doldurulmaz.
- Evren dondurulduktan sonra prompt/model değiştirilmez (değişirse run_log'a not düşülmeli).
"""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DATA_DIR = os.environ.get("PM_DATA_DIR", "data")
GAMMA = "https://gamma-api.polymarket.com"
UNIVERSE_SIZE = int(os.environ.get("PM_UNIVERSE_SIZE", "100"))
MIN_DAYS, MAX_DAYS = 7, 90            # bitişe kalan gün penceresi
MIN_P, MAX_P = 0.03, 0.97             # neredeyse çözülmüş piyasaları ele
PM_MOCK_FILE = os.environ.get("PM_MOCK_FILE")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_GROUNDING = os.environ.get("GEMINI_GROUNDING", "1") == "1"
GEMINI_SLEEP = float(os.environ.get("GEMINI_SLEEP", "12"))  # ücretsiz katman RPM limiti için
GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "1"))  # kalıcı 429'da uzun bekleme faydasız
GEMINI_MOCK = os.environ.get("GEMINI_MOCK") == "1"
PREDICT_LIMIT = int(os.environ.get("PM_PREDICT_LIMIT", "0"))  # test: 0 = hepsi

SPORTS_PATTERNS = re.compile(
    r"\bvs\.?\b|\bv\.\b|US Open|Wimbledon|Roland Garros|ATP|WTA|NFL|NBA|NHL|MLB|MLS|UFC|"
    r"Premier League|La Liga|Serie A|Bundesliga|Ligue 1|Champions League|Europa League|"
    r"World Cup|Super Bowl|Grand Prix|F1\b|NCAA|Stanley Cup|World Series|"
    r"\bmatch\b|\bgame\b|\bwin by\b|spread|O/U|over/under|\bset\b|\bgoals?\b|\bpoints\b",
    re.IGNORECASE,
)

PROMPT = """Bugünün tarihi: {today}. Sen dikkatli, kalibre olmuş bir tahmincisin.

Aşağıdaki sorunun EVET ile sonuçlanma olasılığını tahmin et. Güncel haberleri araştır.
Kural: Herhangi bir tahmin piyasasının (Polymarket, Kalshi vb.) bu soru için verdiği fiyata/orana BAKMA ve
cevabında ona atıf yapma; kendi bağımsız tahminini üret.

SORU: {question}
ÇÖZÜMLEME KURALLARI: {description}
SON TARİH: {end_date}

Önce 3-5 cümlelik gerekçeni yaz (en önemli kanıtlar, belirsizlikler, temel oran).
En son satırda, tam olarak şu formatta olasılığı ver (0.01 ile 0.99 arası ondalık):
P_YES: 0.xx"""


# ---------- yardımcılar ----------
def now_utc():
    return datetime.now(timezone.utc)


def http_json(url, method="GET", body=None, headers=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"User-Agent": "pm-deney/0.2 (research)"}
    if headers:
        h.update(headers)
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def jlist(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_end(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def append_csv(path, fields, rows):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
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


# ---------- 1) evren ----------
def fetch_candidates():
    if PM_MOCK_FILE:
        with open(PM_MOCK_FILE, encoding="utf-8") as f:
            return json.load(f)
    out, offset, page_size, max_pages = [], 0, 100, 30   # ~3000 aday üst sınır
    for _ in range(max_pages):
        url = f"{GAMMA}/markets?active=true&closed=false&limit={page_size}&offset={offset}&order=liquidityNum&ascending=false"
        try:
            page = http_json(url)
        except Exception as e:
            print(f"UYARI: sayfa offset={offset} çekilemedi: {e}", file=sys.stderr)
            break
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < page_size:   # son sayfa
            break
        offset += page_size
        time.sleep(0.3)
    print(f"aday tarama: {len(out)} piyasa, {offset // page_size + 1} sayfa")
    return out


def market_row(m):
    outcomes, prices = jlist(m.get("outcomes")), jlist(m.get("outcomePrices"))
    if len(outcomes) < 2 or len(prices) < 2:
        return None
    return {
        "market_id": str(m.get("id", "")),
        "question": (m.get("question") or "").strip(),
        "slug": m.get("slug", ""),
        "description": (m.get("description") or "").strip(),
        "end_date": m.get("endDate", ""),
        "outcome_yes": outcomes[0],
        "price_yes": num(prices[0]),
        "liquidity": num(m.get("liquidityNum", m.get("liquidity")), 0.0),
        "volume_24h": num(m.get("volume24hr"), 0.0),
    }


def build_universe(today):
    cands = fetch_candidates()
    kept, reasons = [], {"parse": 0, "sport": 0, "date": 0, "price": 0, "yesno": 0}
    for m in cands:
        r = market_row(m)
        if not r:
            reasons["parse"] += 1
            continue
        if SPORTS_PATTERNS.search(r["question"]):
            reasons["sport"] += 1
            continue
        if r["outcome_yes"].strip().lower() != "yes":
            reasons["yesno"] += 1
            continue
        end = parse_end(r["end_date"])
        if not end:
            reasons["date"] += 1
            continue
        days = (end - today).days
        if days < MIN_DAYS or days > MAX_DAYS:
            reasons["date"] += 1
            continue
        if r["price_yes"] is None or not (MIN_P <= r["price_yes"] <= MAX_P):
            reasons["price"] += 1
            continue
        kept.append(r)
    kept.sort(key=lambda r: r["liquidity"], reverse=True)
    universe = kept[:UNIVERSE_SIZE]
    return universe, reasons, len(cands)


def load_or_freeze_universe(run_ts):
    path = os.path.join(DATA_DIR, "universe.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), False
    universe, reasons, n = build_universe(run_ts)
    doc = {
        "frozen_at_utc": run_ts.isoformat(timespec="seconds"),
        "rules": {"min_days": MIN_DAYS, "max_days": MAX_DAYS, "min_p": MIN_P, "max_p": MAX_P,
                  "size": UNIVERSE_SIZE, "sports_excluded": True, "order": "liquidity desc"},
        "candidates_scanned": n, "excluded": reasons,
        "markets": [{k: v for k, v in r.items() if k != "price_yes" and k != "volume_24h"} for r in universe],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return doc, True


# ---------- 2) evren fiyatları ----------
def fetch_market(market_id, mock_map=None):
    if mock_map is not None:
        return mock_map.get(market_id)
    return http_json(f"{GAMMA}/markets/{market_id}", timeout=30)


def snapshot_universe(doc, run_ts, mock_map=None):
    date = run_ts.strftime("%Y-%m-%d")
    os.makedirs(os.path.join(DATA_DIR, "universe_raw"), exist_ok=True)
    rows, raw, errors = [], [], 0
    for u in doc["markets"]:
        mid = u["market_id"]
        try:
            m = fetch_market(mid, mock_map)
            if not m:
                raise ValueError("boş cevap")
        except Exception as e:
            errors += 1
            rows.append({"date": date, "run_ts_utc": run_ts.isoformat(timespec="seconds"), "market_id": mid,
                         "price_yes": "", "closed": "", "active": "", "note": f"HATA {type(e).__name__}"})
            continue
        raw.append(m)
        prices = jlist(m.get("outcomePrices"))
        rows.append({
            "date": date, "run_ts_utc": run_ts.isoformat(timespec="seconds"), "market_id": mid,
            "price_yes": num(prices[0]) if prices else "",
            "closed": m.get("closed", ""), "active": m.get("active", ""), "note": "",
        })
        if mock_map is None:
            time.sleep(0.15)
    with open(os.path.join(DATA_DIR, "universe_raw", f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    append_csv(os.path.join(DATA_DIR, "universe_prices.csv"),
               ["date", "run_ts_utc", "market_id", "price_yes", "closed", "active", "note"], rows)
    price_map = {r["market_id"]: r["price_yes"] for r in rows}
    open_ids = {r["market_id"] for r in rows if r["closed"] is False or r["closed"] == "False" or r["closed"] == ""}
    return price_map, open_ids, errors


# ---------- 3) Gemini ----------
def gemini_predict(question, description, end_date, today):
    prompt = PROMPT.format(today=today, question=question, description=description[:1500] or "(yok)", end_date=end_date)
    if GEMINI_MOCK:
        return "MOCK gerekçe: temel oran ve son haberler dengeli.\nP_YES: 0.42", {"mock": True}
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY tanımlı değil")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if GEMINI_GROUNDING:
        body["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    last_err = None
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            resp = http_json(url, "POST", body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=120)
            parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "\n".join(p.get("text", "") for p in parts if "text" in p)
            return text, resp
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                wait = 15 * (attempt + 1)   # 15s, 30s — kalıcı kotada uzatmanın anlamı yok
                print(f"  {e.code} alındı, {wait}s bekleyip tekrar denenecek ({attempt+1}/{GEMINI_MAX_RETRIES})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise   # 4xx (400/401/403 vb.) tekrar denemeye değmez
    raise last_err


P_RE = re.compile(r"P_YES\s*[:=]\s*([01](?:[.,]\d+)?)", re.IGNORECASE)


def extract_p(text):
    ms = P_RE.findall(text or "")
    if not ms:
        return None
    p = float(ms[-1].replace(",", "."))
    return min(0.99, max(0.01, p))


def predict_all(doc, price_map, open_ids, run_ts):
    date = run_ts.strftime("%Y-%m-%d")
    os.makedirs(os.path.join(DATA_DIR, "pred_raw"), exist_ok=True)
    rows, errors, done = [], 0, 0
    consecutive_429 = 0
    circuit_open = False
    raw_path = os.path.join(DATA_DIR, "pred_raw", f"{date}.jsonl")
    with open(raw_path, "a", encoding="utf-8") as rawf:
        for u in doc["markets"]:
            mid = u["market_id"]
            if mid not in open_ids:
                continue                      # kapanmış piyasaya tahmin üretme
            if PREDICT_LIMIT and done >= PREDICT_LIMIT:
                break
            if circuit_open:
                break                          # kalıcı kota sorunu: kalanları deneme, zaman kaybetme
            done += 1
            row = {"date": date, "run_ts_utc": run_ts.isoformat(timespec="seconds"), "market_id": mid,
                   "p_hat": "", "market_price_yes": price_map.get(mid, ""), "model": GEMINI_MODEL,
                   "grounding": int(GEMINI_GROUNDING), "rationale": "", "note": ""}
            try:
                text, resp = gemini_predict(u["question"], u.get("description", ""), u["end_date"], date)
                p = extract_p(text)
                rawf.write(json.dumps({"market_id": mid, "text": text, "raw": resp}, ensure_ascii=False) + "\n")
                if p is None:
                    raise ValueError("P_YES bulunamadı")
                row["p_hat"] = p
                row["rationale"] = re.sub(r"\s+", " ", text.split("P_YES")[0]).strip()[:600]
                consecutive_429 = 0
            except urllib.error.HTTPError as e:
                errors += 1
                row["note"] = f"HATA HTTPError: {e}"
                if e.code == 429:
                    consecutive_429 += 1
                    if consecutive_429 >= 5:
                        circuit_open = True
                        row["note"] += " | devre kesici: art arda 5 kota hatası, kalanlar denenmedi"
            except Exception as e:
                errors += 1
                row["note"] = f"HATA {type(e).__name__}: {str(e)[:120]}"
            rows.append(row)
            if not GEMINI_MOCK:
                time.sleep(GEMINI_SLEEP)
    append_csv(os.path.join(DATA_DIR, "predictions.csv"),
               ["date", "run_ts_utc", "market_id", "p_hat", "market_price_yes", "model", "grounding", "rationale", "note"],
               rows)
    return len(rows), errors


# ---------- main ----------
def main():
    run_ts = now_utc()
    os.makedirs(DATA_DIR, exist_ok=True)
    mock_map = None
    try:
        doc, frozen_now = load_or_freeze_universe(run_ts)
        if PM_MOCK_FILE:
            with open(PM_MOCK_FILE, encoding="utf-8") as f:
                mock_map = {str(m.get("id")): m for m in json.load(f)}
        if frozen_now:
            print(f"EVREN DONDURULDU: {len(doc['markets'])} piyasa (taranan {doc['candidates_scanned']}, elenen {doc['excluded']})")
            log_run(run_ts, "OK", len(doc["markets"]), 0, f"universe frozen; scanned={doc['candidates_scanned']} excluded={doc['excluded']}")
        if not doc["markets"]:
            log_run(run_ts, "HATA", 0, 0, "evren boş")
            print("HATA: evren boş", file=sys.stderr)
            sys.exit(1)
        price_map, open_ids, snap_err = snapshot_universe(doc, run_ts, mock_map)
        n_pred, pred_err = predict_all(doc, price_map, open_ids, run_ts)
    except Exception as e:
        log_run(run_ts, "HATA", 0, 0, f"predict: {type(e).__name__}: {e}")
        print(f"HATA: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    status = "OK" if pred_err == 0 and snap_err == 0 else ("KISMI" if n_pred - pred_err > 0 else "HATA")
    note = f"predict: snapshot_err={snap_err} open={len(open_ids)} model={GEMINI_MODEL} grounding={int(GEMINI_GROUNDING)}"
    log_run(run_ts, status, n_pred - pred_err, pred_err, note)
    print(f"{status}: {n_pred - pred_err} tahmin üretildi, {pred_err} hata, {snap_err} fiyat hatası, {len(open_ids)} açık piyasa")
    if status == "HATA":
        sys.exit(1)


if __name__ == "__main__":
    main()
