"""決算ステータスのサイトを data/companies/*.json から作る。

使い方: python3 tools/make_status.py
出力:   pages/ の下
        index.html（検索と業界一覧）／about.html（数字の考え方）
        industry/<業種>.html（規模Lvが高い順）／company/<証券コード>.html
        search-index.js（検索用）／style.css
金額は百万円。式の説明は about.html に書く。式を変えたら about.html の文章も直す。
"""
import json
import math
import shutil
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "companies"
OUT = ROOT / "pages"

SITE = "決算ステータス"
OPERATOR_URL = "https://startwith.org/"

# 東証33業種（証券コード協議会の並び）。金融の4業種は式が合わないので載せない
INDUSTRIES = [
    "水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙", "化学", "医薬品",
    "石油・石炭製品", "ゴム製品", "ガラス・土石製品", "鉄鋼", "非鉄金属", "金属製品", "機械",
    "電気機器", "輸送用機器", "精密機器", "その他製品", "電気・ガス業", "陸運業", "海運業",
    "空運業", "倉庫・運輸関連", "情報・通信業", "卸売業", "小売業", "銀行業",
    "証券、商品先物取引業", "保険業", "その他金融業", "不動産業", "サービス業",
]
EXCLUDED = {"銀行業", "証券、商品先物取引業", "保険業", "その他金融業"}


# ---------- 式 ----------

def r(x, nd=0):
    return Decimal(str(x)).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP)


def clamp(v):
    return max(0, min(100, int(v)))


def lv(rev):
    return min(99, max(1, math.floor(1 + math.log10(max(rev, 100) / 100) * 16)))


def atk(op, rev):
    return clamp(r(op / rev * 100 * 5))


def spd(growth):
    return clamp(r(50 + growth * 5))


def hp(cash, rev, op):
    """1か月の費用（売上−営業利益）が0以下の会社は計算できないので None"""
    cost = (rev - op) / 12
    return float(r(cash / cost, 1)) if cost > 0 else None


def yen(m):
    oku = int(r(abs(m) / 100))
    cho, rest = divmod(oku, 10000)
    s = f"{cho}兆{rest:,}億円" if cho else f"{rest:,}億円"
    return ("−" if m < 0 else "") + s


def diff(cur, prev, nd=0):
    d = r(cur - prev, nd)
    if d == 0:
        return "±0"
    return ("▲" if d > 0 else "▼") + str(abs(d))


def pct(x):
    return f"{r(x, 1)}%".replace("-", "−")


def signed(x):
    return ("+" if x >= 0 else "−") + f"{abs(r(x, 1))}%"


def compute(c):
    rev, op = c["revenue"], c["op"]
    s = {
        "lv": lv(rev),
        "atk": atk(op, rev),
        "atk_prev": atk(c["op_prev"], c["revenue_prev"]),
        "margin": op / rev * 100,
        "margin_prev": c["op_prev"] / c["revenue_prev"] * 100,
        "dfn": clamp(r(c["equity_ratio"])),
        "dfn_prev": clamp(r(c["equity_ratio_prev"])),
        "spd": spd(c["growth"]),
        "spd_prev": spd(c["growth_prev"]) if c.get("growth_prev") is not None else None,
        "hp": hp(c["cash"], rev, op),
        "hp_prev": hp(c["cash_prev"], c["revenue_prev"], c["op_prev"]) if c.get("cash_prev") is not None else None,
        "monthly": (rev - op) / 12,
    }
    ail = []
    if op < 0:
        ail.append("営業赤字")
    if c["net_income"] < 0:
        ail.append("最終赤字（2期連続）" if (c.get("net_income_prev") or 0) < 0 else "最終赤字")
    if c["equity_ratio"] < 0:
        ail.append("債務超過")
    s["ailments"] = ail
    s["ail_short"] = "、".join(a.split("（")[0] for a in ail) if ail else "なし"
    return s


def next_update(c):
    """有価証券報告書は期末から3か月以内に出る。次の期末＋3か月を目安にする"""
    end = c.get("period_end")
    if end:
        y, m = int(end[:4]), int(end[5:7])
    else:
        y, m = (int(x) for x in c["period"].replace("月期", "").split("年"))
    y, m = y + 1, m + 3
    if m > 12:
        y, m = y + 1, m - 12
    return f"{y}年{m}月ごろ"


def updated(c):
    return c.get("submitted") or c.get("fetched_at", "")


def jdate(iso):
    return f"{int(iso[:4])}年{int(iso[5:7])}月{int(iso[8:10])}日" if iso else ""


# ---------- 共通の枠 ----------

def layout(title, body, depth=0, description="", scripts=""):
    p = "../" * depth
    full_title = f"{title}｜{SITE}" if title != SITE else SITE
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(full_title)}</title>
<meta name="description" content="{escape(description)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
<link rel="stylesheet" href="{p}style.css">
</head>
<body>
<header class="site-head">
  <div class="wrap head-inner">
    <a class="brand" href="{p}index.html">{SITE}</a>
    <nav class="nav">
      <a href="{p}index.html#search"><i class="fa-solid fa-magnifying-glass"></i>検索</a>
      <a href="{p}index.html#industries"><i class="fa-solid fa-table-list"></i>業界一覧</a>
      <a href="{p}about.html"><i class="fa-solid fa-calculator"></i>数字の考え方</a>
    </nav>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="site-foot">
  <div class="wrap">
    <nav class="foot-nav">
      <a href="{p}index.html#industries">業界一覧</a>
      <a href="{p}about.html">数字の考え方</a>
      <a href="{OPERATOR_URL}" target="_blank" rel="noopener">運用会社</a>
    </nav>
    <p>このサイトは、会社の状態を知るためのものです。株の売買をすすめるものではありません。</p>
    <p class="copy">© StartWith</p>
  </div>
</footer>
{scripts}
</body>
</html>
"""


LIST_HEAD = """<div class="list-head" aria-hidden="true">
    <span>会社</span>
    <span>規模Lv<small>売上の大きさ</small></span>
    <span><i class="fa-solid fa-hand-fist"></i>攻撃力<small>稼ぐ力</small></span>
    <span><i class="fa-solid fa-shield-halved"></i>防御力<small>倒れにくさ</small></span>
    <span><i class="fa-solid fa-bolt"></i>素早さ<small>伸びる速さ</small></span>
    <span><i class="fa-solid fa-heart"></i>HP<small>現金で何か月もつか</small></span>
    <span>状態異常<small>赤字・債務超過</small></span>
  </div>"""


def hp_text(s):
    return f"{s['hp']}か月" if s["hp"] is not None else "－"


def list_item(c, s, p, show_industry=False):
    sub = f'{c["code"]}　{escape(c["industry"])}' if show_industry else c["code"]
    return f"""
  <a class="item" href="{p}company/{c["code"]}.html">
    <span class="c-name"><b>{escape(c["name"])}</b><small>{sub}</small></span>
    <span class="c-stat"><em>規模Lv</em>{s["lv"]}</span>
    <span class="c-stat"><em>攻撃力</em>{s["atk"]}</span>
    <span class="c-stat"><em>防御力</em>{s["dfn"]}</span>
    <span class="c-stat"><em>素早さ</em>{s["spd"]}</span>
    <span class="c-stat"><em>HP</em>{hp_text(s)}</span>
    <span class="c-ail"><em>状態異常</em>{escape(s["ail_short"])}</span>
  </a>"""


# ---------- 会社のページ ----------

def company_page(c, s):
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

    ratio_name = "親会社所有者帰属持分比率" if c["standard"] == "IFRS" else "自己資本比率"
    op_label = c.get("op_label", "営業利益")
    if s["margin"] >= 0:
        atk_note = f"100円売って{r(s['margin'], 1)}円が本業のもうけ"
    else:
        atk_note = f"100円売って{abs(r(s['margin'], 1))}円の本業の赤字"
    spd_diff = f"前期 {s['spd_prev']} {diff(s['spd'], s['spd_prev'])}" if s["spd_prev"] is not None else ""
    spd_prev_txt = f"（前期 {signed(c['growth_prev'])}）" if c.get("growth_prev") is not None else ""
    hp_diff = f"前期 {s['hp_prev']}か月 {diff(s['hp'], s['hp_prev'], 1)}" if s["hp"] is not None and s["hp_prev"] is not None else ""
    hp_val = f"{s['hp']}か月" if s["hp"] is not None else "－"
    hp_bar = min(100, int(r(s["hp"] / 12 * 100))) if s["hp"] is not None else 0
    hp_note = (f"現金 {yen(c['cash'])} ÷ 1か月の費用 約{yen(s['monthly'])}" if s["hp"] is not None
               else f"現金 {yen(c['cash'])}。売上より費用が小さく出るため、1か月の費用を計算できない")
    highlight = f"。{escape(c['highlight'])}" if c.get("highlight") else ""

    stats = "".join([
        stat("fa-hand-fist", "攻撃力", "稼ぐ力", s["atk"], f"前期 {s['atk_prev']} {diff(s['atk'], s['atk_prev'])}", s["atk"],
             f"{op_label}率 {pct(s['margin'])}（前期 {pct(s['margin_prev'])}）。{atk_note}"),
        stat("fa-shield-halved", "防御力", "倒れにくさ", s["dfn"], f"前期 {s['dfn_prev']} {diff(s['dfn'], s['dfn_prev'])}", s["dfn"],
             f"{ratio_name} {c['equity_ratio']}%（前期 {c['equity_ratio_prev']}%）。資産のうち、借りていないお金の割合"),
        stat("fa-bolt", "素早さ", "伸びる速さ", s["spd"], spd_diff, s["spd"],
             f"売上の伸び {signed(c['growth'])}{spd_prev_txt}。売上は{yen(c['revenue'])}{highlight}"),
        stat("fa-heart", "HP", "手元の現金で何か月もつか", hp_val, hp_diff, hp_bar, hp_note),
    ])

    boxes = [("状態異常", f"<p>{'、'.join(s['ailments']) if s['ailments'] else 'なし（黒字・債務超過ではない）'}</p>")]
    if c.get("damages"):
        boxes.append(("今期受けたダメージ", "".join(f"<p>{escape(x)}</p>" for x in c["damages"])))
    f = c.get("forecast")
    if f:
        op_g = signed(f["op_growth"]) if f.get("op_growth") is not None else "前期が赤字のため増減率なし"
        boxes.append((
            "次の期の見通し（会社の予想）",
            f'<p class="num">攻撃力 {s["atk"]} → {atk(f["op"], f["revenue"])}　／　素早さ {s["spd"]} → {spd(f["growth"])}</p>'
            f'<p class="note">営業収益 {yen(f["revenue"])}（{signed(f["growth"])}）、営業利益 {yen(f["op"])}（{op_g}）の予想から計算</p>',
        ))
    notes = "".join(f"<p>{escape(n)}</p>" for n in c.get("notes", []))
    boxes.append((
        "数字について",
        f'{notes}<p class="note">出典：<a href="{escape(c["source_url"])}" target="_blank" rel="noopener">{escape(c["source_title"])}</a></p>',
    ))
    box_html = "".join(f'\n    <section class="card box">\n      <h2>{t}</h2>\n      {b}\n    </section>' for t, b in boxes)

    url_html = f'\n        <div class="url"><a href="{escape(c["url"])}" target="_blank" rel="noopener">{escape(c["url"])}</a></div>' if c.get("url") else ""
    ind = escape(c["industry"])
    body = f"""<nav class="crumb"><a href="../index.html">トップ</a> / <a href="../industry/{ind}.html">{ind}</a> / {escape(c["name"])}</nav>
<div class="company">
  <div class="col-main">
    <section class="card">
      <div class="head-top">
        <h1 class="name">{escape(c["name"])}</h1>
        <div class="lv num"><small>規模Lv.</small>{s["lv"]}</div>
      </div>{url_html}
      <div class="meta"><span>業種：{ind}</span><span>{escape(c["period"])}</span><span>証券コード {c["code"]}</span><span>次の更新 {next_update(c)}</span></div>
    </section>
    <section class="card stats">{stats}
    </section>
  </div>
  <div class="col-side">{box_html}
  </div>
</div>"""
    desc = f"{c['name']}（{c['code']}）の{c['period']}の決算を、規模Lv・攻撃力・防御力・素早さ・HPで表示します。"
    return layout(c["name"], body, depth=1, description=desc)


# ---------- 業界のページ ----------

def industry_page(name, rows):
    items = "".join(list_item(c, s, "../") for c, s in rows)
    body = f"""<nav class="crumb"><a href="../index.html">トップ</a> / <a href="../index.html#industries">業界一覧</a> / {escape(name)}</nav>
<h1>{escape(name)}</h1>
<p class="note">{len(rows)}社を、規模Lvが高い順に並べています。数字は各社の直近の決算です。</p>
<div class="list">
  {LIST_HEAD}{items}
</div>"""
    return layout(name, body, depth=1, description=f"{name}の上場企業を、規模Lvが高い順に並べた一覧です。")


# ---------- トップ ----------

SEARCH_JS = """<script src="search-index.js"></script>
<script>
(function () {
  var data = window.KESSAN_INDEX || [];
  var q = document.getElementById('q');
  var out = document.getElementById('results');
  var count = document.getElementById('count');
  var head = document.getElementById('list-head');

  function norm(s) {
    s = String(s || '').normalize('NFKC').toLowerCase().replace(/\\s+/g, '').replace(/株式会社|\\(株\\)/g, '');
    return s.replace(/[\\u30a1-\\u30f6]/g, function (ch) { return String.fromCharCode(ch.charCodeAt(0) - 0x60); });
  }
  data.forEach(function (d) { d.key = norm([d.name, d.yomi, d.en, d.code, d.industry].join('|')); });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function cell(cls, label, value) {
    var s = el('span', cls);
    s.appendChild(el('em', null, label));
    s.appendChild(document.createTextNode(value));
    return s;
  }

  function render() {
    var v = norm(q.value);
    out.textContent = '';
    if (!v) { out.hidden = true; count.textContent = ''; return; }
    var hits = data.filter(function (d) { return d.key.indexOf(v) >= 0; })
      .sort(function (a, b) { return b.lv - a.lv || b.rev - a.rev; });
    if (!hits.length) { out.hidden = true; count.textContent = '見つかりませんでした'; return; }
    count.textContent = hits.length + '社見つかりました' + (hits.length > 50 ? '（規模Lvが高い50社を表示）' : '');
    out.appendChild(head.content.cloneNode(true));
    hits.slice(0, 50).forEach(function (d) {
      var a = el('a', 'item');
      a.href = 'company/' + d.code + '.html';
      var n = el('span', 'c-name');
      n.appendChild(el('b', null, d.name));
      n.appendChild(el('small', null, d.code + '　' + d.industry));
      a.appendChild(n);
      a.appendChild(cell('c-stat', '規模Lv', d.lv));
      a.appendChild(cell('c-stat', '攻撃力', d.atk));
      a.appendChild(cell('c-stat', '防御力', d.dfn));
      a.appendChild(cell('c-stat', '素早さ', d.spd));
      a.appendChild(cell('c-stat', 'HP', d.hp));
      a.appendChild(cell('c-ail', '状態異常', d.ail));
      out.appendChild(a);
    });
    out.hidden = false;
  }

  q.addEventListener('input', render);
  var init = new URLSearchParams(location.search).get('q');
  if (init) { q.value = init; render(); }
})();
</script>"""


def index_page(by_ind, total, companies):
    recent = sorted(companies, key=lambda cs: (updated(cs[0]), cs[0]["revenue"]), reverse=True)[:10]
    last = updated(recent[0][0]) if recent else ""
    recent_html = "".join(
        f'<li><span class="date">{jdate(updated(c))}</span><a href="company/{c["code"]}.html">{escape(c["name"])}</a>'
        f'<small>{escape(c["industry"])}／{escape(c["period"])}</small></li>'
        for c, _ in recent
    )
    cells = []
    for name in INDUSTRIES:
        if name in EXCLUDED:
            continue
        n = len(by_ind.get(name, []))
        if n:
            cells.append(f'<li><a href="industry/{escape(name)}.html"><span>{escape(name)}</span><small>{n}社</small></a></li>')
        else:
            cells.append(f'<li><span class="empty"><span>{escape(name)}</span><small>準備中</small></span></li>')
    body = f"""<section class="hero">
  <h1>{SITE}</h1>
  <p>上場企業の決算を、ゲームのステータスの形で見せるサイトです。決算書を読まなくても、会社の大きさや稼ぐ力を比べられます。</p>
  <p class="note">掲載 {total}社（{jdate(last)}更新）。毎朝、金融庁のEDINETを確認し、新しい有価証券報告書が出た会社を更新しています。ステータスの意味は<a href="about.html">数字の考え方</a>に書いています。</p>
</section>

<section id="search" class="section">
  <h2>会社を探す</h2>
  <label class="search">
    <i class="fa-solid fa-magnifying-glass"></i>
    <input id="q" type="search" placeholder="社名・よみがな・証券コード" autocomplete="off" aria-label="社名・よみがな・証券コードで探す">
  </label>
  <p id="count" class="note" aria-live="polite"></p>
  <div id="results" class="list" hidden></div>
  <template id="list-head">{LIST_HEAD}</template>
</section>

<section id="recent" class="section">
  <h2>最近更新した会社</h2>
  <ul class="recent">
    {recent_html}
  </ul>
</section>

<section id="industries" class="section">
  <h2>業界一覧</h2>
  <ul class="ind-grid">
    {"".join(cells)}
  </ul>
  <p class="note">銀行業、証券・商品先物取引業、保険業、その他金融業は、売上や営業利益の考え方がほかの業種とちがうため、まだ載せていません。</p>
</section>"""
    return layout(SITE, body, depth=0, description="上場企業の決算を、規模Lv・攻撃力・防御力・素早さ・HPで見せるサイトです。", scripts=SEARCH_JS)


# ---------- 数字の考え方 ----------

ABOUT_BODY = """<nav class="crumb"><a href="index.html">トップ</a> / 数字の考え方</nav>
<article class="prose">
<h1>数字の考え方</h1>
<p>「決算ステータス」は、会社の決算の数字を、ゲームのステータスの形に置きかえて見せるサイトです。</p>
<p>このページでは、それぞれのステータスを、どの数字から、どんな式で出しているかを説明します。</p>

<h2><i class="fa-solid fa-file-lines"></i>使っている数字</h2>
<p>数字は、会社が出している決算短信と、有価証券報告書から取っています。まとめサイトの数字は、使いません。</p>
<p>子会社をふくめた、グループ全体の数字（連結）を使います。子会社がない会社だけは、その会社1社の数字（個別）を使います。</p>

<h2>規模Lv（売上の大きさ）</h2>
<p>規模Lvは、1年間の売上高の大きさだけを表します。経営がうまいかどうかは、表しません。</p>
<p class="formula">規模Lv ＝ 1 ＋ log10（売上高を億円で表した数）× 16</p>
<p>売上高が10倍になると、規模Lvが16上がります。上限は99です。</p>
<div class="scroll"><table>
<thead><tr><th>売上高</th><th>規模Lv</th></tr></thead>
<tbody>
<tr><td>1億円</td><td>1</td></tr>
<tr><td>10億円</td><td>17</td></tr>
<tr><td>100億円</td><td>33</td></tr>
<tr><td>1,000億円</td><td>49</td></tr>
<tr><td>1兆円</td><td>65</td></tr>
<tr><td>10兆円</td><td>81</td></tr>
</tbody></table></div>
<p>売上高は、会社によって1万倍以上ちがいます。そのまま点数にすると、ほとんどの会社が0に近くなるため、桁の数で比べています。</p>

<h2><i class="fa-solid fa-hand-fist"></i>攻撃力（稼ぐ力）</h2>
<p>攻撃力は、本業でどれだけもうけを出しているかを表します。</p>
<p>使う数字は、営業利益率です。営業利益率は、売上高のうち、本業のもうけ（営業利益）が何％あるかを表します。</p>
<p class="formula">攻撃力 ＝ 営業利益率（％）× 5</p>
<div class="scroll"><table>
<thead><tr><th>営業利益率</th><th>攻撃力</th></tr></thead>
<tbody>
<tr><td>0％以下（本業が赤字）</td><td>0</td></tr>
<tr><td>5％</td><td>25</td></tr>
<tr><td>10％</td><td>50</td></tr>
<tr><td>20％以上</td><td>100</td></tr>
</tbody></table></div>
<p>営業利益率の高さは、業界によってちがいます。攻撃力は、同じ業界の会社どうしで比べてください。</p>

<h2><i class="fa-solid fa-shield-halved"></i>防御力（倒れにくさ）</h2>
<p>防御力は、会社が持っている資産のうち、返す必要のないお金（自己資本）でまかなっている割合を表します。</p>
<p>この割合が高い会社は、借金にたよる分が少ないため、売上が急に落ちても倒れにくくなります。</p>
<p class="formula">防御力 ＝ 自己資本比率（％）の値</p>
<p>国際会計基準（IFRS）で決算を出している会社は、自己資本比率のかわりに、親会社所有者帰属持分比率を使います。</p>
<p>自動車ローンなどの金融事業を持つ会社は、お客様に貸すお金を借りて集めるため、防御力が低めに出ます。</p>

<h2><i class="fa-solid fa-bolt"></i>素早さ（伸びる速さ）</h2>
<p>素早さは、売上高が、前の期から何％伸びたかを表します。</p>
<p class="formula">素早さ ＝ 50 ＋ 売上高の伸び率（％）× 5</p>
<div class="scroll"><table>
<thead><tr><th>売上高の伸び率</th><th>素早さ</th></tr></thead>
<tbody>
<tr><td>−10％以下</td><td>0</td></tr>
<tr><td>−5％</td><td>25</td></tr>
<tr><td>0％（前の期と同じ）</td><td>50</td></tr>
<tr><td>+5％</td><td>75</td></tr>
<tr><td>+10％以上</td><td>100</td></tr>
</tbody></table></div>
<p>素早さが50より低いときは、売上高が前の期より減っています。</p>

<h2><i class="fa-solid fa-heart"></i>HP（手元の現金で何か月もつか）</h2>
<p>HPは、手元の現金だけで、会社の1か月分の費用を何か月まかなえるかを表します。</p>
<p class="formula">HP（か月）＝ 現金及び現金同等物 ÷（（売上高 − 営業利益）÷ 12）</p>
<p>1か月分の費用は、売上高から営業利益を引いて、12で割った金額にしています。バーは、12か月分で満タンになります。</p>
<p>現金をためずに、工場や研究に使っている会社ほど、HPは低く出ます。HPが低いことだけで、その会社が危ないとはいえません。</p>

<h2>状態異常</h2>
<p>次のどれかに当てはまる会社には、状態異常を表示します。</p>
<div class="scroll"><table>
<thead><tr><th>状態異常</th><th>当てはまる条件</th></tr></thead>
<tbody>
<tr><td>営業赤字</td><td>本業のもうけ（営業利益）がマイナス</td></tr>
<tr><td>最終赤字</td><td>税金などを引いた最後のもうけ（親会社株主に帰属する当期純利益）がマイナス</td></tr>
<tr><td>最終赤字（2期連続）</td><td>最終赤字が、前の期から2期続いている</td></tr>
<tr><td>債務超過</td><td>自己資本比率がマイナス（資産より、借金のほうが多い）</td></tr>
</tbody></table></div>

<h2>今期受けたダメージ</h2>
<p>決算短信の本文に、金額つきで書かれている大きな損失を載せます。たとえば、米国の関税の影響や、減損損失です。</p>
<p>金額が書かれていない影響は、載せません。</p>

<h2>次の期の見通し</h2>
<p>決算短信に載っている、会社自身の業績予想から、次の期の攻撃力と素早さを計算します。</p>
<p>業績予想は、あとから会社が変えることがあります。</p>

<h2><i class="fa-solid fa-rotate"></i>更新のタイミング</h2>
<p>ステータスは、1社につき、年に1回更新します。</p>
<p>上場企業は、決算期末から3か月以内に、有価証券報告書を金融庁のEDINETに出します。このサイトは毎朝EDINETを確認し、新しい有価証券報告書が出た会社のページを、提出の翌朝に作り直します。</p>
<p>たとえば3月決算の会社は、6月に出る有価証券報告書をもとに、翌朝に新しいステータスになります。会社のページの「次の更新」に、次に更新する月の目安を書いています。</p>

<h2>載せていない業種</h2>
<p>銀行業、証券・商品先物取引業、保険業、その他金融業の会社は、まだ載せていません。</p>
<p>これらの業種は、売上高や営業利益の考え方がほかの業種とちがうため、このページの式が当てはまらないからです。</p>

<h2>式について</h2>
<p>このページの式は、仮のものです。会計の専門家に確認してもらったあとで、変えることがあります。</p>
<p>このサイトは、会社の状態を知るためのものです。株の売買をすすめるものではありません。</p>
</article>"""


def about_page():
    return layout("数字の考え方", ABOUT_BODY, depth=0,
                  description="決算ステータスの規模Lv・攻撃力・防御力・素早さ・HPを、どの数字からどんな式で出しているかを説明します。")


# ---------- CSS ----------

CSS = """:root { --ink:#111; --sub:#666; --line:#ddd; --bar:#eee; --hover:#f4f4f4; --muted:#aaa; }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:#fff; color:var(--ink); font-family:"Noto Sans JP",sans-serif; line-height:1.7; overflow-wrap:anywhere; }
a { color:var(--ink); }
[hidden] { display:none !important; }
.wrap { width:100%; max-width:1200px; margin:0 auto; padding-left:clamp(16px,4vw,40px); padding-right:clamp(16px,4vw,40px); }
.num { font-variant-numeric:tabular-nums; }
.note { font-size:12px; color:var(--sub); }

.site-head { border-bottom:2px solid var(--ink); }
.head-inner { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px 24px; padding-top:14px; padding-bottom:14px; }
.brand { font-weight:700; font-size:18px; text-decoration:none; white-space:nowrap; }
.nav { display:flex; flex-wrap:wrap; gap:4px 20px; font-size:14px; }
.nav a { text-decoration:none; white-space:nowrap; }
.nav i { margin-right:6px; }
main.wrap { padding-top:clamp(24px,4vw,48px); padding-bottom:clamp(48px,6vw,96px); }

.site-foot { border-top:2px solid var(--ink); padding:24px 0 32px; }
.foot-nav { display:flex; flex-wrap:wrap; gap:8px 24px; margin-bottom:12px; font-size:14px; }
.site-foot p { margin:0 0 6px; color:var(--sub); font-size:12px; }

h1 { font-size:clamp(22px,3vw,30px); margin:0 0 8px; line-height:1.4; }
.hero p { margin:0 0 6px; max-width:760px; }
.section { margin-top:clamp(36px,5vw,64px); }
.section > h2 { font-size:18px; margin:0 0 12px; }
.crumb { font-size:13px; color:var(--sub); margin-bottom:16px; }

.search { display:flex; align-items:center; gap:10px; border:2px solid var(--ink); padding:0 14px; max-width:640px; }
.search input { flex:1; min-width:0; border:0; outline:0; font:inherit; font-size:16px; padding:12px 0; background:transparent; color:var(--ink); }
.search:focus-within { outline:2px solid var(--ink); outline-offset:2px; }
#count { margin:8px 0 12px; min-height:1em; }

.ind-grid { list-style:none; margin:0 0 12px; padding:0; display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,220px),1fr)); gap:8px; }
.ind-grid a, .ind-grid .empty { display:flex; justify-content:space-between; align-items:baseline; gap:8px; border:1px solid var(--ink); padding:10px 14px; text-decoration:none; height:100%; }
.ind-grid a:hover { background:var(--hover); }
.ind-grid .empty { border-color:var(--line); color:var(--muted); }
.ind-grid small { font-size:12px; color:var(--sub); white-space:nowrap; }

.recent { list-style:none; margin:0; padding:0; border-top:1px solid var(--line); max-width:760px; }
.recent li { display:flex; flex-wrap:wrap; align-items:baseline; gap:2px 16px; padding:10px 0; border-bottom:1px solid var(--line); }
.recent .date { font-size:13px; color:var(--sub); min-width:9em; font-variant-numeric:tabular-nums; }
.recent a { font-weight:700; }
.recent small { font-size:12px; color:var(--sub); }

/* 一覧（業界ページと検索結果） */
.list { border:2px solid var(--ink); }
.list-head, .item { display:grid; grid-template-columns:minmax(0,2.6fr) repeat(5,minmax(0,1fr)) minmax(0,1.6fr); gap:8px 12px; padding:12px 16px; align-items:center; }
/* PCでは項目名をスクロールに追従させる（スマホは項目名を出さず、1社ずつラベルを付ける） */
.list-head { font-size:12px; font-weight:700; border-bottom:2px solid var(--ink); align-items:end; position:sticky; top:0; z-index:2; background:#fff; }
.list-head small { display:block; font-weight:400; color:var(--sub); font-size:11px; }
.list-head i { margin-right:4px; }
.list-head span:nth-child(n+2):nth-child(-n+6), .c-stat { text-align:right; }
.item { color:inherit; text-decoration:none; border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
.item:last-child { border-bottom:none; }
.item:hover { background:var(--hover); }
.c-name b { display:block; }
.c-name small { font-size:12px; color:var(--sub); }
.c-stat { font-size:15px; }
.c-ail { font-size:13px; }
.c-stat em, .c-ail em { display:none; font-style:normal; }

@media (max-width:767px) {
  .list-head { display:none; }
  .item { grid-template-columns:repeat(3,minmax(0,1fr)); row-gap:8px; padding:14px 16px; }
  .c-name, .c-ail { grid-column:1 / -1; }
  .c-stat { text-align:left; }
  .c-stat em { display:block; font-size:11px; color:var(--sub); }
  .c-ail em { display:inline; font-size:11px; color:var(--sub); margin-right:8px; }
}

/* 会社のページ */
.company { display:grid; gap:16px; grid-template-columns:minmax(0,1fr); }
@media (min-width:960px) { .company { grid-template-columns:minmax(0,3fr) minmax(0,2fr); align-items:start; } }
.col-main, .col-side { display:grid; gap:16px; align-content:start; }
.card { border:2px solid var(--ink); padding:clamp(16px,3vw,24px); }
.head-top { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:4px 12px; }
.name { font-size:clamp(20px,3vw,26px); margin:0; }
.lv { font-size:28px; font-weight:700; white-space:nowrap; }
.lv small { font-size:13px; font-weight:500; margin-right:4px; }
.url { font-size:13px; margin-top:4px; }
.meta { color:var(--sub); font-size:13px; margin-top:4px; }
.meta span { display:inline-block; }
.meta span + span::before { content:"／"; }
.stats { padding-top:6px; padding-bottom:6px; }
.stat { padding:14px 0; border-bottom:1px solid var(--line); }
.stat:last-child { border-bottom:none; }
.row { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:4px 12px; }
.label { font-weight:700; font-size:16px; }
.label i { width:1.3em; text-align:center; margin-right:6px; }
.label em { font-style:normal; font-weight:400; font-size:12px; color:var(--sub); margin-left:8px; }
.val { font-size:22px; font-weight:700; white-space:nowrap; margin-left:auto; }
.diff { font-size:13px; color:var(--sub); margin-left:8px; font-weight:400; }
.bar { height:10px; background:var(--bar); margin:6px 0 4px; }
.bar i { display:block; height:100%; background:var(--ink); }
.box h2 { font-size:14px; margin:0 0 6px; }
.box p { margin:0 0 4px; font-size:14px; }
.box p.note { font-size:12px; margin-top:8px; }
@media (max-width:420px) {
  .label em { display:block; margin-left:0; }
  .val { font-size:20px; }
  .diff { display:block; text-align:right; margin-left:0; }
}

/* 数字の考え方 */
.prose { max-width:760px; line-height:2; }
.prose h1 { margin-bottom:16px; }
.prose h2 { font-size:18px; line-height:1.6; margin:48px 0 12px; padding-top:20px; border-top:2px solid var(--ink); }
.prose h2 i { margin-right:8px; }
.prose p { margin:0 0 16px; }
.prose .formula { border:1px solid var(--ink); padding:12px 16px; font-weight:700; line-height:1.8; }
.prose .scroll { overflow-x:auto; margin:0 0 16px; }
.prose table { border-collapse:collapse; font-size:14px; line-height:1.7; }
.prose th, .prose td { border-bottom:1px solid var(--line); padding:8px 24px 8px 0; text-align:left; vertical-align:top; }
.prose th { font-size:12px; border-bottom:2px solid var(--ink); white-space:nowrap; }
"""


# ---------- 実行 ----------

def main():
    companies, broken = [], []
    for p in sorted(DATA.glob("*.json")):
        c = json.loads(p.read_text(encoding="utf-8"))
        if c["industry"] in EXCLUDED:
            continue
        try:  # 1社の数字がおかしくても、ほかの会社のページは作る
            s = compute(c)
            company_page(c, s)
        except Exception as e:
            broken.append(f"{p.stem}\t{c.get('name', '')}\t{type(e).__name__}: {e}")
            continue
        companies.append((c, s))
    if broken:
        print(f"計算できずに飛ばした会社 {len(broken)}社:\n  " + "\n  ".join(broken))
    companies.sort(key=lambda cs: (-cs[1]["lv"], -cs[0]["revenue"]))

    for sub in ("company", "industry"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
        (OUT / sub).mkdir(parents=True)
    for old in OUT.glob("*.html"):
        if old.name not in ("index.html", "about.html"):
            old.unlink()

    by_ind = {}
    for c, s in companies:
        (OUT / "company" / f"{c['code']}.html").write_text(company_page(c, s), encoding="utf-8")
        by_ind.setdefault(c["industry"], []).append((c, s))
    for name, rows in by_ind.items():
        (OUT / "industry" / f"{name}.html").write_text(industry_page(name, rows), encoding="utf-8")

    index = [
        {"code": c["code"], "name": c["name"], "yomi": c.get("yomi", ""), "en": c.get("en_name", ""),
         "industry": c["industry"], "rev": c["revenue"], "lv": s["lv"], "atk": s["atk"], "dfn": s["dfn"],
         "spd": s["spd"], "hp": hp_text(s), "ail": s["ail_short"]}
        for c, s in companies
    ]
    (OUT / "search-index.js").write_text(
        "window.KESSAN_INDEX=" + json.dumps(index, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    (OUT / "style.css").write_text(CSS, encoding="utf-8")
    (OUT / "index.html").write_text(index_page(by_ind, len(companies), companies), encoding="utf-8")
    (OUT / "about.html").write_text(about_page(), encoding="utf-8")
    print(f"{len(companies)}社・{len(by_ind)}業種を書き出しました")


if __name__ == "__main__":
    main()
