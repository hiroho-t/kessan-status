"""EDINET API から有価証券報告書の数字を取り、data/companies/<証券コード>.json を作る。

準備: EDINET API のキーを、環境変数 EDINET_API_KEY か ~/.config/kessan-status/edinet_api_key に置く。
      キーはリポジトリに入れない（このリポジトリは public）。GitHub Actions ではリポジトリの Secret に置く。

使い方:
  python3 tools/fetch_edinet.py --update                  # ふだんはこれだけ（毎朝 GitHub Actions が実行する）
  python3 tools/fetch_edinet.py --industry 輸送用機器       # 業種を指定
  python3 tools/fetch_edinet.py --codes 7203 6758         # 証券コードを指定
  オプション --max 1500      1回に読む報告書の上限（初回の取り込みを数日に分ける）
             --force         すでに取り込んだ報告書も読み直す
取ったあとに python3 tools/make_status.py でページを作り直す。

しくみ:
- data/filings.json に「会社ごとの最新の有価証券報告書」と「最後に一覧を見た日」を持つ
- 実行するたびに、最後に見た日の2日前から今日までの書類一覧だけを見る（初回だけ --days 日さかのぼる）
- 会社ページの doc_id と最新の報告書がちがう会社だけ、XBRL を読む
- 読めなかった会社は data/skipped.tsv に理由を書く。同じ報告書は --force まで読み直さない

決まりごと:
- 連結の数字を使う。連結が無い会社だけ個別を使う
- 金融の4業種は取らない（式が合わない）
- 決算短信から手で入れた会社は、同じ期なら数字を上書きしない。新しい期の有報が出たら数字を置き換え、
  その期にしか当てはまらない項目（damages・forecast・highlight）は消す。url と notes は残す
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = ROOT / "data" / "companies"
FILINGS = ROOT / "data" / "filings.json"
SKIPPED = ROOT / "data" / "skipped.tsv"
CACHE = ROOT / "data" / "cache"
API = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
EXCLUDED = {"銀行業", "証券、商品先物取引業", "保険業", "その他金融業"}

# 要素IDの末尾（名前空間を外したもの）。上から順に探して、最初に見つかったものを使う
REVENUE = [
    "NetSalesSummaryOfBusinessResults", "RevenueIFRSSummaryOfBusinessResults", "NetSalesIFRSSummaryOfBusinessResults",
    "OperatingRevenue1SummaryOfBusinessResults", "OperatingRevenue2SummaryOfBusinessResults",
    "RevenuesUSGAAPSummaryOfBusinessResults", "OperatingRevenuesIFRSKeyFinancialData",
    "NetSales", "RevenueIFRS", "NetSalesIFRS", "TotalNetRevenuesIFRS", "SalesRevenuesIFRS", "OperatingRevenue1", "OperatingRevenue2",
]
OPERATING = [
    "OperatingIncome", "OperatingProfitLossIFRS", "OperatingProfitLossIFRSSummaryOfBusinessResults",
    "OperatingIncomeLossUSGAAPSummaryOfBusinessResults", "OperatingIncomeLoss",
]
# IFRSでは営業利益を出さず、事業利益を出す会社がある（例：川崎重工業）。そのときだけ使い、ページに「事業利益」と書く
BUSINESS_PROFIT = ["BusinessProfitLossIFRS"]
NET_INCOME = [
    "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
    "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
    "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
    "NetIncomeLossSummaryOfBusinessResults", "ProfitLossAttributableToOwnersOfParent", "ProfitLoss",
]
# EquityToAssetRatioIFRSSummaryOfBusinessResults は、1株当たりの金額が入っている会社がある（例：ソフトバンクグループ 3057.72）ので使わない
EQUITY_RATIO = [
    "EquityToAssetRatioSummaryOfBusinessResults", "RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults",
    "EquityToAssetRatioUSGAAPSummaryOfBusinessResults",
]
CASH = [
    "CashAndCashEquivalentsSummaryOfBusinessResults", "CashAndCashEquivalentsIFRSSummaryOfBusinessResults",
    "CashAndCashEquivalentsUSGAAPSummaryOfBusinessResults", "CashAndCashEquivalents", "CashAndCashEquivalentsIFRS",
]
STANDARD = {"Japan GAAP": "JGAAP", "IFRS": "IFRS", "US GAAP": "USGAAP"}


def api_key():
    key = os.environ.get("EDINET_API_KEY", "").strip()
    path = Path.home() / ".config" / "kessan-status" / "edinet_api_key"
    if not key and path.exists():
        key = path.read_text().strip()
    if not key:
        sys.exit("EDINET API のキーがありません。EDINET_API_KEY か ~/.config/kessan-status/edinet_api_key に置いてください")
    return key


def get(url, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kessan-status"}), timeout=60) as res:
                return res.read(), res.headers.get("Content-Type", "")
        except urllib.error.URLError as e:
            if i == retries - 1:
                raise
            print(f"  再試行 {i + 1}: {getattr(e, 'reason', e)}")
            time.sleep(3 * (i + 1))


# ---------- EDINETコードリスト（キー不要） ----------

def load_codelist():
    path = CACHE / "EdinetcodeDlInfo.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
        CACHE.mkdir(parents=True, exist_ok=True)
        body, _ = get(CODELIST_URL)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            path.write_bytes(z.read("EdinetcodeDlInfo.csv"))
    with path.open(encoding="cp932") as f:
        next(f)
        rows = list(csv.DictReader(f))
    listed = {}
    for row in rows:
        sec = row["証券コード"].strip()
        if row["上場区分"] == "上場" and sec and row["提出者種別"] == "内国法人・組合":
            listed[row["ＥＤＩＮＥＴコード"]] = {
                "code": sec[:4],
                "edinet_code": row["ＥＤＩＮＥＴコード"],
                "name": row["提出者名"].replace("株式会社", "").strip(),
                "yomi": row["提出者名（ヨミ）"].replace("カブシキガイシャ", "").replace("カブシキカイシャ", "").strip(),
                "en_name": row["提出者名（英字）"].strip(),
                "industry": row["提出者業種"],
            }
    return listed


# ---------- 書類一覧（差分だけ見る） ----------

def documents_on(d, key):
    path = CACHE / "documents" / f"{d.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    body, _ = get(f"{API}/documents.json?date={d.isoformat()}&type=2&Subscription-Key={key}")
    data = json.loads(body)
    if str(data.get("metadata", {}).get("status")) != "200":
        sys.exit(f"書類一覧を取れませんでした（{d}）: {data.get('metadata', data)}")
    results = data.get("results") or []
    if d < date.today() - timedelta(days=2):  # 直近は訂正・取下げが入るので保存しない
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.3)
    return results


def load_filings():
    if FILINGS.exists():
        return json.loads(FILINGS.read_text(encoding="utf-8"))
    return {"last_scanned": None, "reports": {}, "skipped": {}}


def save_filings(filings):
    filings["reports"] = dict(sorted(filings["reports"].items()))
    filings["skipped"] = dict(sorted(filings["skipped"].items()))
    FILINGS.write_text(json.dumps(filings, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def scan(filings, days, key):
    """最後に見た日の2日前から今日までの一覧を見て、会社ごとの最新の有価証券報告書を更新する"""
    today = date.today()
    if filings["last_scanned"]:
        start = date.fromisoformat(filings["last_scanned"]) - timedelta(days=2)
    else:
        start = today - timedelta(days=days)
    n = (today - start).days + 1
    print(f"書類一覧を {start} から {today} まで（{n}日分）見ます")
    reports = filings["reports"]
    for i in range(n):
        for doc in documents_on(start + timedelta(days=i), key):
            ec = doc.get("edinetCode")
            if not (doc.get("secCode") and doc.get("docTypeCode") == "120" and doc.get("csvFlag") == "1"):
                continue
            if doc.get("withdrawalStatus") != "0":
                if reports.get(ec, {}).get("docID") == doc.get("docID"):
                    del reports[ec]
                continue
            new = {k: doc.get(k) for k in ("docID", "periodEnd", "submitDateTime", "docDescription")}
            prev = reports.get(ec)
            if not prev or (new["periodEnd"], new["submitDateTime"]) >= (prev["periodEnd"], prev["submitDateTime"]):
                reports[ec] = new
    filings["last_scanned"] = today.isoformat()


# ---------- XBRL（CSV） ----------

def download_csv_zip(doc_id, key):
    path = CACHE / "xbrl" / f"{doc_id}.zip"
    if path.exists():
        return path.read_bytes()
    body, ctype = get(f"{API}/documents/{doc_id}?type=5&Subscription-Key={key}")
    if "json" in ctype:
        raise RuntimeError(f"CSVを取れませんでした: {body[:200]!r}")
    if not os.environ.get("GITHUB_ACTIONS"):  # 手元では貯めておく。Actions では持たない
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    time.sleep(0.3)
    return body


def read_facts(zip_bytes):
    """{(要素IDの末尾, コンテキストID): 値}"""
    facts = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if not (name.startswith("XBRL_TO_CSV/") and base.endswith(".csv")) or base.startswith("jpaud"):
                continue
            text = z.read(name).decode("utf-16")
            for row in csv.reader(io.StringIO(text), delimiter="\t"):
                if len(row) < 9 or row[0] == "要素ID":
                    continue
                facts.setdefault((row[0].split(":")[-1], row[2]), row[8])
    return facts


def number(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def pick(facts, names, ctx, ok=None):
    for n in names:
        v = number(facts.get((n, ctx)))
        if v is not None and (ok is None or ok(v)):
            return v
    return None


def is_ratio(v):
    """比率は小数（0.378）で入る。−1〜1を超える値は比率ではない"""
    return -1 <= v <= 1


def dei(facts, name):
    for (n, _), v in facts.items():
        if n == name:
            return v
    return None


def r1(x):
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def to_million(v):
    return int(Decimal(str(v / 1_000_000)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ratio(v):
    return r1(v * 100)


def extract(facts):
    """連結 → 個別の順に、必要な数字がそろうほうを使う"""
    missing = []
    for suffix in ("", "_NonConsolidatedMember"):
        cur_d, pre_d, pre2_d = (f"{p}YearDuration{suffix}" for p in ("Current", "Prior1", "Prior2"))
        cur_i, pre_i = (f"{p}YearInstant{suffix}" for p in ("Current", "Prior1"))
        v = {
            "revenue": pick(facts, REVENUE, cur_d), "revenue_prev": pick(facts, REVENUE, pre_d),
            "revenue_prev2": pick(facts, REVENUE, pre2_d),
            "op": pick(facts, OPERATING, cur_d), "op_prev": pick(facts, OPERATING, pre_d), "op_label": "営業利益",
            "net_income": pick(facts, NET_INCOME, cur_d), "net_income_prev": pick(facts, NET_INCOME, pre_d),
            "equity_ratio": pick(facts, EQUITY_RATIO, cur_i, is_ratio), "equity_ratio_prev": pick(facts, EQUITY_RATIO, pre_i, is_ratio),
            "cash": pick(facts, CASH, cur_i), "cash_prev": pick(facts, CASH, pre_i),
        }
        if v["op"] is None and v["op_prev"] is None:
            v["op"], v["op_prev"], v["op_label"] = pick(facts, BUSINESS_PROFIT, cur_d), pick(facts, BUSINESS_PROFIT, pre_d), "事業利益"
        required = ["revenue", "revenue_prev", "op", "op_prev", "net_income", "equity_ratio", "equity_ratio_prev", "cash"]
        miss = [k for k in required if v[k] is None]
        if not miss and v["revenue"] > 0 and v["revenue_prev"] > 0:
            return v, ("連結" if not suffix else "個別")
        if not suffix:
            missing = miss
    return None, "足りない項目: " + ", ".join(missing or ["売上高が0以下"])


def build_record(info, doc, facts):
    v, basis = extract(facts)
    if v is None:
        return None, basis
    if to_million(v["revenue"]) <= 0 or to_million(v["revenue_prev"]) <= 0:
        return None, "売上高が100万円未満（式で割れない）"
    end = dei(facts, "CurrentFiscalYearEndDateDEI") or doc.get("periodEnd", "")
    y, m = int(end[:4]), int(end[5:7])
    rec = dict(info)
    rec.update({
        "period": f"{y}年{m}月期",
        "period_end": end,
        "standard": STANDARD.get(dei(facts, "AccountingStandardsDEI"), "JGAAP"),
        "basis": basis,
        "revenue": to_million(v["revenue"]), "revenue_prev": to_million(v["revenue_prev"]),
        "growth": r1((v["revenue"] / v["revenue_prev"] - 1) * 100),
        "growth_prev": r1((v["revenue_prev"] / v["revenue_prev2"] - 1) * 100) if v["revenue_prev2"] else None,
        "op": to_million(v["op"]), "op_prev": to_million(v["op_prev"]), "op_label": v["op_label"],
        "net_income": to_million(v["net_income"]),
        "net_income_prev": to_million(v["net_income_prev"]) if v["net_income_prev"] is not None else None,
        "equity_ratio": ratio(v["equity_ratio"]), "equity_ratio_prev": ratio(v["equity_ratio_prev"]),
        "cash": to_million(v["cash"]),
        "cash_prev": to_million(v["cash_prev"]) if v["cash_prev"] is not None else None,
        "source_kind": "有価証券報告書",
        "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc['docID']}",
        "source_title": f"{info['name']} {doc.get('docDescription') or '有価証券報告書'}（EDINET）{doc['submitDateTime'][:10]}提出",
        "doc_id": doc["docID"],
        "submitted": doc["submitDateTime"][:10],
    })
    return rec, basis


def merge(path, rec):
    """手で入れた項目を守りながら書く。数字を書いたら True"""
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        same_period = old.get("period_end") == rec["period_end"] or old.get("period") == rec["period"]
        if old.get("source_kind", "決算短信") == "決算短信" and same_period:
            old["doc_id"] = rec["doc_id"]  # 次から読み直さないように、報告書の番号だけ持つ
            path.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return False
        for k in ("url", "notes"):
            if old.get(k):
                rec[k] = old[k]
        if same_period:
            for k in ("highlight", "damages", "forecast"):
                if old.get(k):
                    rec[k] = old[k]
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def current_doc_id(code):
    p = COMPANIES / f"{code}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("doc_id") if p.exists() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--update", action="store_true", help="上場企業すべてのうち、新しい報告書が出た会社だけ")
    g.add_argument("--industry", help="業種名（例：輸送用機器）")
    g.add_argument("--codes", nargs="+", help="証券コード（4桁）")
    ap.add_argument("--days", type=int, default=400, help="初回に何日さかのぼるか")
    ap.add_argument("--max", type=int, default=0, help="1回に読む報告書の上限（0は上限なし）")
    ap.add_argument("--force", action="store_true", help="取り込み済み・読めなかった報告書も読み直す")
    args = ap.parse_args()

    key = api_key()
    listed = load_codelist()
    targets = {ec: i for ec, i in listed.items() if i["industry"] not in EXCLUDED}
    if args.codes:
        want = {c.upper() for c in args.codes}
        targets = {ec: i for ec, i in targets.items() if i["code"] in want}
    elif args.industry:
        targets = {ec: i for ec, i in targets.items() if i["industry"] == args.industry}
    if not targets:
        sys.exit("対象の会社が見つかりません（金融の4業種は対象外）")

    filings = load_filings()
    scan(filings, args.days, key)
    save_filings(filings)

    todo = []
    for ec, info in sorted(targets.items(), key=lambda kv: kv[1]["code"]):
        doc = filings["reports"].get(ec)
        if not doc:
            continue
        if not args.force and (current_doc_id(info["code"]) == doc["docID"] or filings["skipped"].get(ec, {}).get("docID") == doc["docID"]):
            continue
        todo.append((ec, info, doc))
    print(f"対象 {len(targets)}社のうち、新しい報告書を読む会社 {len(todo)}社" + (f"（今回は {args.max}社まで）" if args.max and len(todo) > args.max else ""))
    if args.max:
        todo = todo[: args.max]

    COMPANIES.mkdir(parents=True, exist_ok=True)
    wrote = kept = failed = 0
    for n, (ec, info, doc) in enumerate(todo, 1):
        try:
            rec, basis = build_record(info, doc, read_facts(download_csv_zip(doc["docID"], key)))
        except Exception as e:  # 1社の失敗で全体を止めない
            rec, basis = None, f"読み取りエラー: {e}"
        if rec is None:
            filings["skipped"][ec] = {"docID": doc["docID"], "code": info["code"], "name": info["name"],
                                      "industry": info["industry"], "reason": basis}
            failed += 1
        else:
            filings["skipped"].pop(ec, None)
            if merge(COMPANIES / f"{info['code']}.json", rec):
                wrote += 1
                print(f"  [{n}/{len(todo)}] {info['code']} {info['name']}：{rec['period']}（{basis}）")
            else:
                kept += 1
        if n % 100 == 0:
            save_filings(filings)
    save_filings(filings)
    SKIPPED.write_text("証券コード\t社名\t業種\t理由\n" + "".join(
        f"{s['code']}\t{s['name']}\t{s['industry']}\t{s['reason']}\n"
        for s in sorted(filings["skipped"].values(), key=lambda s: s["code"])), encoding="utf-8")
    print(f"書いた {wrote}社／決算短信の手入力を残した {kept}社／読めなかった {failed}社（一覧は data/skipped.tsv）")


if __name__ == "__main__":
    main()
