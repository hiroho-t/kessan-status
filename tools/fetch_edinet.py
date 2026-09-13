"""EDINET API から有価証券報告書の数字を取り、data/companies/<証券コード>.json を作る。

準備: EDINET API のキーを、環境変数 EDINET_API_KEY か ~/.config/kessan-status/edinet_api_key に置く。
      キーはリポジトリに入れない（このリポジトリは public）。

使い方:
  python3 tools/fetch_edinet.py --codes 7203 6758       # 証券コードを指定
  python3 tools/fetch_edinet.py --industry 輸送用機器     # 業種ごと
  python3 tools/fetch_edinet.py --all --limit 200        # 上場企業を、証券コード順に200社
  オプション --days 400                                  # 何日さかのぼって書類を探すか（既定400日）
取ったあとに python3 tools/make_status.py でページを作り直す。

決まりごと:
- 連結の数字を使う。連結が無い会社だけ個別を使う
- 金融の4業種は取らない（式が合わない）
- 決算短信から手で入れた会社は、同じ期なら数字を上書きしない。新しい期の有報が出たら数字を置き換え、
  その期にしか当てはまらない項目（damages・forecast・highlight）は消す。url と notes は残す
- 取得結果（書類一覧・XBRL）は data/cache/ に置く（git に入れない）
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
CACHE = ROOT / "data" / "cache"
API = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
EXCLUDED = {"銀行業", "証券、商品先物取引業", "保険業", "その他金融業"}

# 要素IDの末尾（名前空間を外したもの）。上から順に探して、最初に見つかったものを使う
REVENUE = [
    "NetSalesSummaryOfBusinessResults", "RevenueIFRSSummaryOfBusinessResults", "NetSalesIFRSSummaryOfBusinessResults",
    "OperatingRevenue1SummaryOfBusinessResults", "OperatingRevenue2SummaryOfBusinessResults",
    "RevenuesUSGAAPSummaryOfBusinessResults", "NetSales", "RevenueIFRS", "NetSalesIFRS", "OperatingRevenue1", "OperatingRevenue2",
]
OPERATING = [
    "OperatingIncome", "OperatingProfitLossIFRS", "OperatingProfitLossIFRSSummaryOfBusinessResults",
    "OperatingIncomeLossUSGAAPSummaryOfBusinessResults", "OperatingIncomeLoss",
]
NET_INCOME = [
    "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
    "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
    "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
    "NetIncomeLossSummaryOfBusinessResults", "ProfitLossAttributableToOwnersOfParent", "ProfitLoss",
]
EQUITY_RATIO = [
    "EquityToAssetRatioSummaryOfBusinessResults", "RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults",
    "EquityToAssetRatioIFRSSummaryOfBusinessResults", "EquityToAssetRatioUSGAAPSummaryOfBusinessResults",
]
CASH = [
    "CashAndCashEquivalentsSummaryOfBusinessResults", "CashAndCashEquivalentsIFRSSummaryOfBusinessResults",
    "CashAndCashEquivalentsUSGAAPSummaryOfBusinessResults", "CashAndCashEquivalents", "CashAndCashEquivalentsIFRS",
]
STANDARD = {"Japan GAAP": "JGAAP", "IFRS": "IFRS", "US GAAP": "USGAAP"}


def api_key():
    key = os.environ.get("EDINET_API_KEY")
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
                "name": clean_name(row["提出者名"]),
                "yomi": clean_yomi(row["提出者名（ヨミ）"]),
                "en_name": row["提出者名（英字）"].strip(),
                "industry": row["提出者業種"],
            }
    return listed


def clean_name(s):
    return s.replace("株式会社", "").strip()


def clean_yomi(s):
    for w in ("カブシキガイシャ", "カブシキカイシャ"):
        s = s.replace(w, "")
    return s.strip()


# ---------- 書類一覧 ----------

def documents_on(d, key):
    path = CACHE / "documents" / f"{d.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    body, _ = get(f"{API}/documents.json?date={d.isoformat()}&type=2&Subscription-Key={key}")
    data = json.loads(body)
    if str(data.get("metadata", {}).get("status")) != "200":
        sys.exit(f"書類一覧を取れませんでした（{d}）: {data.get('metadata', data)}")
    results = data.get("results", [])
    if d < date.today():  # 今日の分は増えるので保存しない
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.5)
    return results


def latest_reports(targets, days, key):
    """対象の会社ごとに、いちばん新しい有価証券報告書を探す"""
    found = {}
    for i in range(days):
        d = date.today() - timedelta(days=i)
        for doc in documents_on(d, key):
            ec = doc.get("edinetCode")
            if (ec in targets and doc.get("docTypeCode") == "120" and doc.get("withdrawalStatus") == "0"
                    and doc.get("csvFlag") == "1"):
                prev = found.get(ec)
                if not prev or (doc["periodEnd"], doc["submitDateTime"]) > (prev["periodEnd"], prev["submitDateTime"]):
                    found[ec] = doc
    return found


# ---------- XBRL（CSV） ----------

def download_csv_zip(doc_id, key):
    path = CACHE / "xbrl" / f"{doc_id}.zip"
    if not path.exists():
        body, ctype = get(f"{API}/documents/{doc_id}?type=5&Subscription-Key={key}")
        if "json" in ctype:
            raise RuntimeError(f"CSVを取れませんでした: {body[:200]!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        time.sleep(0.5)
    return path


def read_facts(zip_path):
    """{(要素IDの末尾, コンテキストID): 値}"""
    facts = {}
    with zipfile.ZipFile(zip_path) as z:
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


def pick(facts, names, ctx):
    for n in names:
        v = number(facts.get((n, ctx)))
        if v is not None:
            return v
    return None


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
    return r1(v * 100 if abs(v) <= 1.5 else v)


def extract(facts):
    """連結 → 個別の順に、必要な数字がそろうほうを使う"""
    for suffix in ("", "_NonConsolidatedMember"):
        cur_d, pre_d, pre2_d = (f"{p}YearDuration{suffix}" for p in ("Current", "Prior1", "Prior2"))
        cur_i, pre_i = (f"{p}YearInstant{suffix}" for p in ("Current", "Prior1"))
        v = {
            "revenue": pick(facts, REVENUE, cur_d), "revenue_prev": pick(facts, REVENUE, pre_d),
            "revenue_prev2": pick(facts, REVENUE, pre2_d),
            "op": pick(facts, OPERATING, cur_d), "op_prev": pick(facts, OPERATING, pre_d),
            "net_income": pick(facts, NET_INCOME, cur_d), "net_income_prev": pick(facts, NET_INCOME, pre_d),
            "equity_ratio": pick(facts, EQUITY_RATIO, cur_i), "equity_ratio_prev": pick(facts, EQUITY_RATIO, pre_i),
            "cash": pick(facts, CASH, cur_i), "cash_prev": pick(facts, CASH, pre_i),
        }
        required = ["revenue", "revenue_prev", "op", "op_prev", "net_income", "equity_ratio", "equity_ratio_prev", "cash"]
        missing = [k for k in required if v[k] is None]
        if not missing and v["revenue"] > 0 and v["revenue_prev"] > 0:
            return v, ("連結" if not suffix else "個別")
    return None, "足りない項目: " + ", ".join(missing)


def build_record(info, doc, facts):
    v, basis = extract(facts)
    if v is None:
        return None, basis
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
        "op": to_million(v["op"]), "op_prev": to_million(v["op_prev"]),
        "net_income": to_million(v["net_income"]),
        "net_income_prev": to_million(v["net_income_prev"]) if v["net_income_prev"] is not None else None,
        "equity_ratio": ratio(v["equity_ratio"]), "equity_ratio_prev": ratio(v["equity_ratio_prev"]),
        "cash": to_million(v["cash"]),
        "cash_prev": to_million(v["cash_prev"]) if v["cash_prev"] is not None else None,
        "source_kind": "有価証券報告書",
        "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc['docID']}",
        "source_title": f"{info['name']} {doc.get('docDescription', '有価証券報告書')}（EDINET）{doc['submitDateTime'][:10]}提出",
        "fetched_at": date.today().isoformat(),
    })
    return rec, basis


def merge(path, rec):
    """手で入れた項目を守りながら書く。書いたら True"""
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        same_period = old.get("period_end", "") == rec["period_end"] or old.get("period") == rec["period"]
        if old.get("source_kind", "決算短信") == "決算短信" and same_period:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", nargs="+", help="証券コード（4桁）")
    g.add_argument("--industry", help="業種名（例：輸送用機器）")
    g.add_argument("--all", action="store_true", help="上場企業すべて")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()

    key = api_key()
    listed = load_codelist()
    targets = {ec: i for ec, i in listed.items() if i["industry"] not in EXCLUDED}
    if args.codes:
        want = {c.upper() for c in args.codes}
        targets = {ec: i for ec, i in targets.items() if i["code"] in want}
    elif args.industry:
        targets = {ec: i for ec, i in targets.items() if i["industry"] == args.industry}
    if args.limit:
        targets = dict(sorted(targets.items(), key=lambda kv: kv[1]["code"])[: args.limit])
    if not targets:
        sys.exit("対象の会社が見つかりません（金融の4業種は対象外）")
    print(f"対象 {len(targets)}社。書類一覧を {args.days}日分さがします")

    reports = latest_reports(set(targets), args.days, key)
    COMPANIES.mkdir(parents=True, exist_ok=True)
    wrote = kept = 0
    skipped = []
    for ec, info in sorted(targets.items(), key=lambda kv: kv[1]["code"]):
        doc = reports.get(ec)
        if not doc:
            skipped.append((info, "有価証券報告書が見つからない"))
            continue
        try:
            rec, basis = build_record(info, doc, read_facts(download_csv_zip(doc["docID"], key)))
        except Exception as e:  # 1社の失敗で全体を止めない
            skipped.append((info, f"読み取りエラー: {e}"))
            continue
        if rec is None:
            skipped.append((info, basis))
            continue
        if merge(COMPANIES / f"{info['code']}.json", rec):
            wrote += 1
            print(f"  {info['code']} {info['name']}：{rec['period']}（{basis}）")
        else:
            kept += 1
    print(f"書いた {wrote}社／決算短信の手入力を残した {kept}社／取れなかった {len(skipped)}社")
    if skipped:
        log = CACHE / "skipped.tsv"
        log.write_text("".join(f"{i['code']}\t{i['name']}\t{i['industry']}\t{why}\n" for i, why in skipped), encoding="utf-8")
        print(f"取れなかった会社の一覧: {log.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
