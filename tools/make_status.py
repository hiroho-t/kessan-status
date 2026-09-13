"""決算ステータス画面を companies.json から作る。

使い方: python3 tools/make_status.py
出力:   pages/<短い名前>.html と pages/index.html（一覧）
金額は決算短信どおり百万円で入れる。式は仮（docs/01）。
"""
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLIDES = ROOT / "pages"


def r(x, nd=0):
    q = Decimal(1).scaleb(-nd)
    return Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)


def clamp(v):
    return max(0, min(100, int(v)))


def yen(m):
    oku = int(r(abs(m) / 100))
    cho, rest = divmod(oku, 10000)
    s = f"{cho}兆{rest:,}億円" if cho else f"{rest:,}億円"
    return ("−" if m < 0 else "") + s


def lv(rev):
    return min(99, max(1, math.floor(1 + math.log10(rev / 100) * 16)))


def atk(op, rev):
    return clamp(r(op / rev * 100 * 5))


def spd(growth):
    return clamp(r(50 + growth * 5))


def hp(cash, rev, op):
    return float(r(cash / ((rev - op) / 12), 1))


def diff(cur, prev, nd=0):
    d = r(cur - prev, nd)
    if d == 0:
        return "±0"
    return ("▲" if d > 0 else "▼") + str(abs(d))


def pct(x):
    return f"{r(x, 1)}%"


def signed(x):
    return ("+" if x >= 0 else "−") + f"{abs(r(x, 1))}%"


def build(c):
    rev, op = c["revenue"], c["op"]
    a, a_p = atk(op, rev), atk(c["op_prev"], c["revenue_prev"])
    margin = op / rev * 100
    margin_p = c["op_prev"] / c["revenue_prev"] * 100
    d, d_p = clamp(r(c["equity_ratio"])), clamp(r(c["equity_ratio_prev"]))
    s = spd(c["growth"])
    h = hp(c["cash"], rev, op)
    h_bar = min(100, int(r(h / 12 * 100)))
    monthly = (rev - op) / 12
    ratio_name = "親会社所有者帰属持分比率" if c["standard"] == "IFRS" else "自己資本比率"

    if margin >= 0:
        atk_note = f"100円売って{r(margin, 1)}円が本業のもうけ"
    else:
        atk_note = f"100円売って{abs(r(margin, 1))}円の本業の赤字"

    if c.get("growth_prev") is not None:
        s_p = spd(c["growth_prev"])
        spd_diff = f"前期 {s_p} {diff(s, s_p)}"
        spd_prev_txt = f"（前期 {signed(c['growth_prev'])}）"
    else:
        spd_diff, spd_prev_txt = "", ""

    if c.get("cash_prev") is not None:
        h_p = hp(c["cash_prev"], c["revenue_prev"], c["op_prev"])
        hp_diff = f"前期 {h_p}か月 {diff(h, h_p, 1)}"
    else:
        hp_diff = ""

    ailments = []
    if op < 0:
        ailments.append("営業赤字")
    if c["net_income"] < 0:
        ailments.append("最終赤字（2期連続）" if c.get("net_income_prev", 0) < 0 else "最終赤字")
    if c["equity_ratio"] < 0:
        ailments.append("債務超過")
    ailment_txt = "、".join(ailments) if ailments else "なし（黒字・債務超過ではない）"

    boxes = [("状態異常", f"<p>{ailment_txt}</p>")]
    if c.get("damages"):
        boxes.append(("今期受けたダメージ", "".join(f"<p>{escape(x)}</p>" for x in c["damages"])))
    f = c.get("forecast")
    if f:
        a_n, s_n = atk(f["op"], f["revenue"]), spd(f["growth"])
        boxes.append((
            "次の期の見通し（会社の予想）",
            f'<p class="num">攻撃力 {a} → {a_n}　／　素早さ {s} → {s_n}</p>'
            f'<p class="note">営業収益 {yen(f["revenue"])}（{signed(f["growth"])}）、'
            f'営業利益 {yen(f["op"])}（{signed(f["op_growth"]) if f.get("op_growth") is not None else "前期が赤字のため増減率なし"}）の予想から計算</p>',
        ))
    else:
        boxes.append(("次の期の見通し（会社の予想）", "<p>会社の予想は出ていない</p>"))

    def stat(icon, label, sub, val, dif, bar, note):
        dif_html = f'<span class="diff">{dif}</span>' if dif else ""
        return f"""
    <div class="stat">
      <div class="row">
        <div class="label"><i class="fa-solid {icon}"></i>{label}<em>{sub}</em></div>
        <div class="val num">{val}{dif_html}</div>
      </div>
      <div class="bar"><i style="width:{bar}%"></i></div>
      <div class="note">{note}</div>
    </div>"""

    highlight = f"。{escape(c['highlight'])}" if c.get("highlight") else ""
    stats = "".join([
        stat("fa-hand-fist", "攻撃力", "稼ぐ力", a, f"前期 {a_p} {diff(a, a_p)}", a,
             f"営業利益率 {pct(margin)}（前期 {pct(margin_p)}）。{atk_note}"),
        stat("fa-shield-halved", "防御力", "倒れにくさ", d, f"前期 {d_p} {diff(d, d_p)}", d,
             f"{ratio_name} {c['equity_ratio']}%（前期 {c['equity_ratio_prev']}%）。資産のうち、借りていないお金の割合"),
        stat("fa-bolt", "素早さ", "伸びる速さ", s, spd_diff, s,
             f"売上の伸び {signed(c['growth'])}{spd_prev_txt}。売上は{yen(rev)}{highlight}"),
        stat("fa-heart", "HP", "手元の現金で何か月もつか", f"{h}か月", hp_diff, h_bar,
             f"現金 {yen(c['cash'])} ÷ 1か月の費用 約{yen(monthly)}"),
    ])
    box_html = "".join(f'\n  <div class="box">\n    <h2>{t}</h2>\n    {b}\n  </div>\n' for t, b in boxes)
    notes = "".join(f"<p>{escape(n)}</p>" for n in c.get("notes", []))

    html = TEMPLATE.format(
        name=escape(c["name"]), lv=lv(rev), url=escape(c["url"]), type=escape(c["type"]),
        period=escape(c["period"]), code=c["code"], stats=stats, boxes=box_html, notes=notes,
        source_url=escape(c["source_url"]), source_title=escape(c["source_title"]),
    )
    summary = dict(name=c["name"], slug=c["slug"], lv=lv(rev), atk=a, dfn=d, spd=s, hp=h, ailment=ailment_txt)
    return html, summary


CSS = """
  :root { --ink:#111; --sub:#666; --line:#ddd; --bar:#eee; }
  * { box-sizing:border-box; }
  body { margin:0; background:#fff; color:var(--ink); font-family:"Noto Sans JP",sans-serif; line-height:1.7; }
  .wrap { max-width:640px; margin:0 auto; padding:40px 20px 60px; }
  .num { font-variant-numeric:tabular-nums; }
  .back { font-size:13px; margin-bottom:12px; }
  .back a, .url a { color:var(--ink); }

  .head { border:2px solid var(--ink); padding:20px 24px; }
  .head-top { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
  .name { font-size:24px; font-weight:700; margin:0; }
  .lv { font-size:28px; font-weight:700; }
  .lv small { font-size:14px; font-weight:500; margin-right:4px; }
  .meta { color:var(--sub); font-size:13px; margin-top:4px; }
  .url { font-size:13px; margin-top:2px; }
  .label i { width:1.3em; text-align:center; margin-right:6px; }
  .meta span + span::before { content:"／"; }

  .stats { border:2px solid var(--ink); border-top:none; padding:8px 24px 16px; }
  .stat { padding:14px 0; border-bottom:1px solid var(--line); }
  .stat:last-child { border-bottom:none; }
  .row { display:flex; justify-content:space-between; align-items:baseline; gap:12px; }
  .label { font-weight:700; font-size:16px; }
  .label em { font-style:normal; font-weight:400; font-size:12px; color:var(--sub); margin-left:8px; }
  .val { font-size:22px; font-weight:700; white-space:nowrap; }
  .diff { font-size:13px; color:var(--sub); margin-left:8px; font-weight:400; }
  .bar { height:10px; background:var(--bar); margin:6px 0 4px; }
  .bar i { display:block; height:100%; background:var(--ink); }
  .note { font-size:12px; color:var(--sub); }

  .box { border:2px solid var(--ink); border-top:none; padding:16px 24px; }
  .box h2 { font-size:14px; margin:0 0 6px; }
  .box p { margin:0; font-size:14px; }

  .foot { margin-top:28px; font-size:12px; color:var(--sub); }
  .foot p { margin:0 0 8px; }
  .foot a { color:var(--sub); }

  table { width:100%; border-collapse:collapse; border:2px solid var(--ink); font-size:14px; }
  th, td { padding:10px 8px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  th { font-size:12px; font-weight:700; }
  th i { margin-right:4px; }
  th small { display:block; font-size:11px; font-weight:400; color:var(--sub); }
  th { vertical-align:bottom; }
  td a { color:var(--ink); font-weight:700; }
  .scroll { overflow-x:auto; }
  h1.page { font-size:22px; margin:0 0 4px; }
"""

HEAD = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
<style>""" + CSS.replace("{", "{{").replace("}", "}}") + """</style>
</head>
<body>
<div class="wrap">
"""

FORMULA = """    <p><strong>数字の変え方（仮）</strong><br>
    規模Lv＝1＋log10(売上高・億円)×16（売上1億円でLv.1、1兆円でLv.65、上限99）。会社の大きさだけを表し、経営の良し悪しは表さない<br>
    攻撃力＝営業利益率×5（20%で100）／防御力＝自己資本の比率そのまま（100%で100）<br>
    素早さ＝50＋売上の伸び率×5（伸び0%で50、+10%で100）／HP＝現金÷（営業収益−営業利益）の1か月分。バーは12か月で満タン<br>
    ステータスは0〜100で止める。式は会計の専門家の確認前。</p>
    <p>この画面は会社の状態を知るためのもので、株の売買をすすめるものではない。</p>"""

TEMPLATE = HEAD.replace("{title}", "決算ステータス｜{name}") + """
  <div class="back"><a href="index.html">← 自動車の一覧</a></div>

  <div class="head">
    <div class="head-top">
      <h1 class="name">{name}</h1>
      <div class="lv num"><small>規模Lv.</small>{lv}</div>
    </div>
    <div class="url"><a href="{url}" target="_blank" rel="noopener">{url}</a></div>
    <div class="meta"><span>タイプ：{type}</span><span>{period}</span><span>証券コード {code}</span></div>
  </div>

  <div class="stats">{stats}
  </div>
{boxes}
  <div class="foot">
""" + FORMULA.replace("{", "{{").replace("}", "}}") + """
    {notes}
    <p>出典：<a href="{source_url}">{source_title}</a></p>
  </div>

</div>
</body>
</html>
"""


def build_list(rows):
    body = "".join(
        f'<tr><td><a href="{x["slug"]}.html">{escape(x["name"])}</a></td>'
        f'<td class="num">{x["lv"]}</td><td class="num">{x["atk"]}</td><td class="num">{x["dfn"]}</td>'
        f'<td class="num">{x["spd"]}</td><td class="num">{x["hp"]}か月</td><td>{escape(x["ailment"].split("（")[0])}</td></tr>'
        for x in rows
    )
    return HEAD.format(title="決算ステータス｜自動車") + f"""
  <h1 class="page">決算ステータス　自動車</h1>
  <p class="note" style="margin:0 0 16px">2026年3月期の決算短信（連結）から。売上の大きい順</p>
  <div class="scroll">
  <table>
    <thead><tr><th>会社</th><th>規模Lv<small>売上の大きさ</small></th><th><i class="fa-solid fa-hand-fist"></i>攻撃力<small>稼ぐ力</small></th><th><i class="fa-solid fa-shield-halved"></i>防御力<small>倒れにくさ</small></th><th><i class="fa-solid fa-bolt"></i>素早さ<small>伸びる速さ</small></th><th><i class="fa-solid fa-heart"></i>HP<small>現金で何か月もつか</small></th><th>状態異常<small>赤字・債務超過</small></th></tr></thead>
    <tbody>{body}</tbody>
  </table>
  </div>
  <div class="foot">
{FORMULA}
  </div>

</div>
</body>
</html>
"""


def main():
    companies = json.loads((ROOT / "data" / "companies.json").read_text(encoding="utf-8"))
    companies.sort(key=lambda c: -c["revenue"])
    rows = []
    for c in companies:
        html, summary = build(c)
        (SLIDES / f"{c['slug']}.html").write_text(html, encoding="utf-8")
        rows.append(summary)
        print(summary)
    (SLIDES / "index.html").write_text(build_list(rows), encoding="utf-8")


if __name__ == "__main__":
    main()
