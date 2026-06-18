#!/usr/bin/env python3
"""
基金即時淨值推估工具
從 MoneyDJ 抓取基金成分股（中文名稱+比例），透過 TWSE/TPEX API 對應股票代號，
再用 Fugle API 取得即時股價，加權推估基金淨值變動，最後輸出 HTML 報表。
"""

import io
import os
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from fugle_marketdata import RestClient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════════
# 設定
# ═══════════════════════════════════════════════════════════════════

FUGLE_API_KEY = os.environ.get("FUGLE_API_KEY", "")

FUND_LIST = [
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACDD04", "台科"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACDD01", "大壩"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACPS02", "黑馬"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACPS10", "奔騰"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACJS13", "富邦台商"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACFH15", "復華"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACKH19", "高科技"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=AC0001", "鴻運"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACKH03", "台運籌"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACNB02", "5G"),
    ("https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=ACYT11", "元大"),
]

MONEYDJ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    "Referer": "https://www.moneydj.com/",
}

TWSE_API = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_API = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"


# ═══════════════════════════════════════════════════════════════════
# Step 0 — 建立「股票名稱 → 代號」對應表
# ═══════════════════════════════════════════════════════════════════

def build_name_map() -> dict:
    name_map = {}
    try:
        twse = requests.get(TWSE_API, timeout=15).json()
        for item in twse:
            code = item.get("公司代號", "").strip()
            abbr = item.get("公司簡稱", "").strip()
            if code and abbr:
                name_map[abbr] = code
    except Exception as e:
        print(f"  [WARN] TWSE API 失敗：{e}", file=sys.stderr)

    try:
        tpex = requests.get(TPEX_API, timeout=15).json()
        for item in tpex:
            code = item.get("SecuritiesCompanyCode", "").strip()
            abbr = item.get("CompanyAbbreviation", "").strip()
            if code and abbr:
                name_map[abbr] = code
    except Exception as e:
        print(f"  [WARN] TPEX API 失敗：{e}", file=sys.stderr)

    return name_map


# ═══════════════════════════════════════════════════════════════════
# Step 1 — MoneyDJ 爬蟲
# ═══════════════════════════════════════════════════════════════════

def scrape_fund_holdings(session: requests.Session, url: str, nickname: str) -> dict:
    result = {
        "nickname": nickname,
        "full_name": nickname,
        "url": url,
        "holdings_date": None,
        "holdings": [],
        "error": None,
    }

    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()

        raw = resp.content
        text = None
        for enc in ("utf-8", "big5", "cp950"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")

        soup = BeautifulSoup(text, "html.parser")

        title_tag = soup.find("title")
        if title_tag:
            parts = title_tag.get_text(strip=True).split("-")
            if parts and parts[0].strip():
                result["full_name"] = parts[0].strip()

        date_re = re.compile(r"資料月份[：:]\s*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})")
        for string in soup.stripped_strings:
            m = date_re.search(string)
            if m:
                result["holdings_date"] = m.group(1)
                break

        result["holdings"] = _parse_holdings(soup)
        if not result["holdings"]:
            result["error"] = "未找到成分股（頁面無資料或已更新結構）"

    except requests.RequestException as e:
        result["error"] = f"連線失敗：{e}"
    except Exception as e:
        result["error"] = f"解析錯誤：{e}"

    return result


def _parse_holdings(soup: BeautifulSoup) -> list:
    PCT_RE = re.compile(r"^\d+\.?\d*$")

    for table in soup.find_all("table", class_="t01"):
        rows = table.find_all("tr", recursive=False)
        if not rows:
            continue

        hdr_row = None
        for row in rows:
            cells_text = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if "投資名稱" in cells_text and "比例" in cells_text:
                hdr_row = row
                break

        if hdr_row is None:
            continue

        headers = [c.get_text(strip=True) for c in hdr_row.find_all(["th", "td"])]
        pairs = []
        seen_name = False
        name_idx = pct_idx = None
        for i, h in enumerate(headers):
            if "投資名稱" in h:
                if seen_name:
                    pairs.append((i, i + 2))
                else:
                    name_idx = i
                    seen_name = True
            if "比例" in h:
                if name_idx is not None and pct_idx is None:
                    pct_idx = i
                    pairs.append((name_idx, pct_idx))

        if not pairs:
            pairs = [(0, 2), (4, 6)]

        holdings = []
        data_rows = [r for r in rows if r is not hdr_row]
        for row in data_rows:
            cells = row.find_all("td")
            cell_texts = [c.get_text(strip=True) for c in cells]
            if len(cell_texts) < 4:
                continue
            for name_col, pct_col in pairs:
                if name_col >= len(cell_texts) or pct_col >= len(cell_texts):
                    continue
                name = cell_texts[name_col].strip()
                pct_str = cell_texts[pct_col].strip()
                if not name or name in ("投資名稱", "—", "-"):
                    continue
                if PCT_RE.match(pct_str):
                    pct = float(pct_str)
                    if 0 < pct <= 100:
                        holdings.append({
                            "name": name,
                            "code": None,
                            "weight_pct": pct,
                            "weight": pct / 100,
                            "current_price": None,
                            "prev_close": None,
                            "change_pct": None,
                        })

        if holdings:
            return holdings

    return []


# ═══════════════════════════════════════════════════════════════════
# Step 2 — 名稱對應代號
# ═══════════════════════════════════════════════════════════════════

def map_codes(funds: list, name_map: dict) -> None:
    unresolved = set()
    for fund in funds:
        for h in fund["holdings"]:
            name = h["name"]
            code = name_map.get(name)
            if code:
                h["code"] = code
            else:
                for abbr, c in name_map.items():
                    if name in abbr or abbr in name:
                        code = c
                        h["code"] = code
                        break
            if not h["code"]:
                unresolved.add(name)

    if unresolved:
        print(f"  [INFO] 未對應到代號：{', '.join(sorted(unresolved))}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════
# Step 3 — Fugle 即時報價
# ═══════════════════════════════════════════════════════════════════

def fetch_all_quotes(client: RestClient, symbols: list) -> dict:
    quotes = {}
    total = len(symbols)
    for i, symbol in enumerate(symbols, 1):
        try:
            data = client.stock.intraday.quote(symbol=symbol)
            if data:
                close_p = (
                    data.get("closePrice")
                    or data.get("lastPrice")
                    or data.get("price")
                    or 0
                )
                prev_p = data.get("previousClose") or data.get("referencePrice") or 0
                chg_pct = data.get("changePercent")
                if chg_pct is None and prev_p and close_p:
                    chg_pct = (float(close_p) - float(prev_p)) / float(prev_p) * 100
                quotes[symbol] = {
                    "name": data.get("name", symbol),
                    "current": float(close_p) if close_p else None,
                    "prev_close": float(prev_p) if prev_p else None,
                    "change_pct": round(float(chg_pct), 2) if chg_pct is not None else None,
                }
        except Exception as e:
            print(f"    [{i}/{total}] {symbol} 取價失敗：{e}", file=sys.stderr)
        if i % 20 == 0:
            print(f"    進度：{i}/{total}")
        time.sleep(0.2)
    return quotes


# ═══════════════════════════════════════════════════════════════════
# Step 4 — 淨值推估
# ═══════════════════════════════════════════════════════════════════

def estimate_fund(fund: dict, quotes: dict) -> dict:
    weighted_sum = 0.0
    covered_weight = 0.0
    for h in fund["holdings"]:
        code = h.get("code")
        if code and code in quotes:
            q = quotes[code]
            h["current_price"] = q["current"]
            h["prev_close"] = q["prev_close"]
            h["change_pct"] = q["change_pct"]
            if q["change_pct"] is not None:
                weighted_sum += h["weight"] * q["change_pct"]
                covered_weight += h["weight"]
    fund["est_change_pct"] = round(weighted_sum, 3) if covered_weight > 0 else None
    fund["covered_weight_pct"] = round(covered_weight * 100, 1)
    return fund


# ═══════════════════════════════════════════════════════════════════
# Step 5 — HTML 報表
# ═══════════════════════════════════════════════════════════════════

def _cls(pct):
    if pct is None: return "neutral"
    if pct > 0: return "up"
    if pct < 0: return "down"
    return "flat"


def _fmt(pct):
    if pct is None: return "—"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.2f}%"


def _price(p):
    if p is None or p == 0: return "—"
    return f"{p:,.2f}"


def generate_html(funds: list, generated_at: str, repo_url: str = "", dispatch_token: str = "") -> str:
    ranked = sorted(funds, key=lambda f: f.get("est_change_pct") or -9999, reverse=True)

    summary_rows = ""
    for rank, f in enumerate(ranked, 1):
        est = f.get("est_change_pct")
        cov = f.get("covered_weight_pct", 0)
        cls = _cls(est)
        summary_rows += f"""
        <tr class="{cls}">
          <td class="r">{rank}</td>
          <td><a href="#c-{f['nickname']}">{f['nickname']}</a></td>
          <td class="e {cls}">{_fmt(est)}</td>
          <td class="v">{cov:.1f}%</td>
        </tr>"""

    cards = ""
    for fund in funds:
        est = fund.get("est_change_pct")
        cov = fund.get("covered_weight_pct", 0)
        cls = _cls(est)
        arrow = "▲" if cls == "up" else ("▼" if cls == "down" else "◆")

        h_rows = ""
        for h in sorted(fund["holdings"], key=lambda x: -x["weight"]):
            chg = h.get("change_pct")
            c = _cls(chg)
            bar = min(h["weight_pct"] * 2.0, 100)
            code_disp = h.get("code") or "?"
            h_rows += f"""
            <tr>
              <td class="code">{code_disp}</td>
              <td class="sname" title="{h['name']}">{h['name']}</td>
              <td class="wt">
                <span class="bar-bg"><span class="bar-fg" style="width:{bar:.1f}%"></span></span>
                {h['weight_pct']:.2f}%
              </td>
              <td class="px">{_price(h.get('current_price'))}</td>
              <td class="chg {c}">{_fmt(chg)}</td>
            </tr>"""

        if not h_rows:
            err = fund.get("error") or "無成分股資料"
            h_rows = f'<tr><td colspan="5" class="empty">{err}</td></tr>'

        date_lbl = f" · 持股日期 {fund['holdings_date']}" if fund.get("holdings_date") else ""

        cards += f"""
  <div class="card" id="c-{fund['nickname']}">
    <div class="hdr {cls}">
      <div class="htitle">
        <span class="nick">{fund['nickname']}</span>
        <span class="fname">{fund['full_name']}{date_lbl}</span>
      </div>
      <div class="hnav">
        <span class="arr">{arrow}</span>
        <span class="pct">{_fmt(est)}</span>
        <span class="sub">覆蓋率 {cov:.1f}%</span>
      </div>
    </div>
    <div class="tbody">
      <table>
        <thead><tr>
          <th>代號</th><th>名稱</th><th>權重</th><th>即時價</th><th>漲跌幅</th>
        </tr></thead>
        <tbody>{h_rows}</tbody>
      </table>
    </div>
  </div>"""

    actions_link = f"{repo_url}/actions" if repo_url else "#"

    # 手動觸發按鈕：token 存在瀏覽器 localStorage，不嵌入 HTML
    trigger_btn = '<button id="btn-trigger" class="btn btn-gh" onclick="triggerUpdate()">&#9654; 手動觸發更新</button>'
    trigger_js = """
function _ghTok() {
  var t = localStorage.getItem('ghpat');
  if (!t) {
    t = prompt('請輸入 GitHub PAT（需有 workflow 權限）：');
    if (t) localStorage.setItem('ghpat', t.trim());
  }
  return t ? t.trim() : null;
}
async function triggerUpdate() {
  var btn = document.getElementById('btn-trigger');
  var tok = _ghTok();
  if (!tok) return;
  btn.textContent = '⏳ 觸發中...';
  btn.disabled = true;
  try {
    var r = await fetch(
      'https://api.github.com/repos/delezue/fund-nav-report/actions/workflows/update.yml/dispatches',
      {
        method: 'POST',
        headers: {
          'Authorization': 'token ' + tok,
          'Accept': 'application/vnd.github.v3+json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ref: 'main'})
      }
    );
    if (r.status === 204) {
      btn.textContent = '✅ 已觸發！約2分鐘後自動重整';
      setTimeout(function() { location.reload(); }, 120000);
    } else if (r.status === 401) {
      localStorage.removeItem('ghpat');
      btn.textContent = '❌ Token 無效，請重新點擊設定';
      btn.disabled = false;
    } else {
      btn.textContent = '❌ 失敗(' + r.status + ')';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = '❌ 網路錯誤';
    btn.disabled = false;
  }
}"""

    css = """
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang TC","Microsoft JhengHei",Arial,sans-serif;
         background:#eef1f7;color:#1a202c;padding:24px 16px;min-height:100vh}
    a{color:inherit;text-decoration:none}
    a:hover{text-decoration:underline}
    .ph{text-align:center;margin-bottom:28px}
    .ph h1{font-size:1.8rem;font-weight:800;letter-spacing:-.5px}
    .ph p{margin-top:6px;color:#718096;font-size:.84rem;line-height:1.6}
    .toolbar{display:flex;justify-content:center;gap:12px;margin:0 auto 24px;flex-wrap:wrap}
    .btn{display:inline-flex;align-items:center;gap:6px;padding:8px 18px;border-radius:8px;
         font-size:.85rem;font-weight:600;cursor:pointer;border:none;transition:opacity .15s}
    .btn-gh{background:#24292e;color:#fff}
    .btn:hover{opacity:.85}
    .sum-wrap{max-width:600px;margin:0 auto 32px;background:#fff;border-radius:14px;
              box-shadow:0 2px 16px rgba(0,0,0,.09);overflow:hidden}
    .sum-wrap h2{padding:14px 20px 10px;font-size:.95rem;font-weight:700;color:#2d3748;
                 border-bottom:1px solid #e2e8f0}
    .sum-tbl{width:100%;border-collapse:collapse}
    .sum-tbl th{padding:8px 16px;background:#f7fafc;text-align:left;font-size:.75rem;
                color:#718096;border-bottom:2px solid #e2e8f0}
    .sum-tbl td{padding:9px 16px;border-bottom:1px solid #f0f4f8;font-size:.9rem}
    .sum-tbl tr:last-child td{border:none}
    .sum-tbl td.r{font-weight:700;color:#a0aec0;width:36px}
    .sum-tbl td.e{font-weight:800;font-size:1rem;text-align:right}
    .sum-tbl td.v{color:#a0aec0;font-size:.8rem;text-align:right}
    .sum-tbl tr.up td.e  {color:#276749}
    .sum-tbl tr.down td.e{color:#9b2c2c}
    .sum-tbl tr.flat td.e,.sum-tbl tr.neutral td.e{color:#4a5568}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(500px,1fr));
          gap:20px;max-width:1440px;margin:0 auto}
    .card{background:#fff;border-radius:14px;overflow:hidden;
          box-shadow:0 2px 16px rgba(0,0,0,.09)}
    .hdr{display:flex;justify-content:space-between;align-items:center;
         padding:16px 20px;gap:12px}
    .hdr.up    {background:linear-gradient(120deg,#f0fff4,#c6f6d5);border-left:5px solid #276749}
    .hdr.down  {background:linear-gradient(120deg,#fff5f5,#fed7d7);border-left:5px solid #9b2c2c}
    .hdr.flat  {background:linear-gradient(120deg,#ebf8ff,#bee3f8);border-left:5px solid #2b6cb0}
    .hdr.neutral{background:linear-gradient(120deg,#f7fafc,#edf2f7);border-left:5px solid #a0aec0}
    .htitle{flex:1;min-width:0}
    .nick{display:block;font-size:1.2rem;font-weight:800}
    .fname{display:block;font-size:.7rem;color:#718096;margin-top:3px;
           white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .hnav{text-align:right;white-space:nowrap;flex-shrink:0}
    .arr{display:block;font-size:1.1rem;line-height:1.3}
    .pct{display:block;font-size:1.6rem;font-weight:800;line-height:1.1}
    .sub{display:block;font-size:.68rem;color:#a0aec0;margin-top:2px}
    .up   .arr,.up   .pct{color:#276749}
    .down .arr,.down .pct{color:#9b2c2c}
    .flat .arr,.flat .pct{color:#2b6cb0}
    .neutral .pct{color:#718096}
    .tbody{overflow-x:auto}
    .tbody table{width:100%;border-collapse:collapse;font-size:.83rem}
    .tbody thead th{padding:7px 12px;text-align:left;font-size:.73rem;font-weight:700;
                    color:#718096;background:#f7fafc;border-bottom:2px solid #e2e8f0}
    .tbody td{padding:6px 12px;border-bottom:1px solid #f0f4f8;vertical-align:middle}
    .tbody tr:last-child td{border:none}
    .tbody tr:hover{background:#fafbfc}
    td.code{font-family:monospace;color:#2b6cb0;font-weight:700;white-space:nowrap}
    td.sname{max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    td.wt{white-space:nowrap;color:#4a5568}
    td.px{font-family:monospace;text-align:right}
    td.chg{font-family:monospace;text-align:right;font-weight:700}
    td.chg.up  {color:#276749}
    td.chg.down{color:#9b2c2c}
    td.chg.flat{color:#2b6cb0}
    td.chg.neutral{color:#a0aec0}
    .bar-bg{display:inline-block;width:54px;height:5px;background:#e2e8f0;
            border-radius:3px;vertical-align:middle;margin-right:5px}
    .bar-fg{display:block;height:100%;background:#4299e1;border-radius:3px}
    .empty{text-align:center;color:#a0aec0;font-style:italic;padding:20px}
    @media(max-width:560px){
      .grid{grid-template-columns:1fr}
      .hdr{flex-direction:column;align-items:flex-start}
      .hnav{text-align:left}
    }
    """

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>基金即時淨值推估</title>
  <style>{css}</style>
</head>
<body>
  <div class="ph">
    <h1>基金即時淨值推估報表</h1>
    <p>產生時間：{generated_at}（台灣時間）　｜　資料來源：MoneyDJ 成分股 × Fugle 即時報價<br>
    ⚠️ 本報表以最新揭露持股加權計算，為推估值，不代表實際淨值，僅供參考。</p>
  </div>

  <div class="toolbar">
    {trigger_btn}
  </div>

  <div class="sum-wrap">
    <h2>各基金推估排名</h2>
    <table class="sum-tbl">
      <thead><tr>
        <th>#</th><th>基金</th>
        <th style="text-align:right">推估漲跌</th>
        <th style="text-align:right">覆蓋率</th>
      </tr></thead>
      <tbody>{summary_rows}</tbody>
    </table>
  </div>

  <div class="grid">
    {cards}
  </div>

  <script>
    {trigger_js}
  </script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main():
    repo_url = os.environ.get("REPO_URL", "")
    dispatch_token = os.environ.get("DISPATCH_TOKEN", "")

    print("=" * 55)
    print("  基金即時淨值推估工具")
    print("=" * 55)

    print("\n[0/4] 建立股票名稱對應表（TWSE + TPEX）...")
    name_map = build_name_map()
    print(f"  -> 共 {len(name_map)} 支股票")

    print("\n[1/4] 從 MoneyDJ 抓取基金成分股...")
    session = requests.Session()
    session.headers.update(MONEYDJ_HEADERS)

    funds = []
    for url, nickname in FUND_LIST:
        print(f"  -> {nickname} ...", end="", flush=True)
        fund = scrape_fund_holdings(session, url, nickname)
        n = len(fund["holdings"])
        if fund["error"] and n == 0:
            print(f"  FAIL  {fund['error']}")
        else:
            print(f"  OK  ({n} 檔)")
        funds.append(fund)
        time.sleep(1.2)

    print("\n[2/4] 對應股票代號...")
    map_codes(funds, name_map)
    mapped = sum(1 for f in funds for h in f["holdings"] if h.get("code"))
    total = sum(len(f["holdings"]) for f in funds)
    print(f"  -> {mapped}/{total} 筆對應成功")

    all_symbols = sorted({h["code"] for f in funds for h in f["holdings"] if h.get("code")})
    print(f"\n[3/4] 透過 Fugle 取得 {len(all_symbols)} 支股票即時報價...")

    quotes: dict = {}
    if not all_symbols:
        print("  WARNING: 無可查詢的股票代號")
    else:
        fugle = RestClient(api_key=FUGLE_API_KEY)
        quotes = fetch_all_quotes(fugle, all_symbols)
        print(f"  -> 成功 {len(quotes)} / {len(all_symbols)} 支")

    print("\n[4/4] 推估各基金淨值變動...")
    for fund in funds:
        estimate_fund(fund, quotes)
        est = fund.get("est_change_pct")
        cov = fund.get("covered_weight_pct", 0)
        arrow = "up" if (est and est > 0) else ("dn" if (est and est < 0) else "--")
        print(f"  {fund['nickname']:10s}  [{arrow}] {_fmt(est):>9s}   (covered {cov:.1f}%)")

    # 台灣時間 UTC+8
    now = datetime.utcnow()
    tw_hour = (now.hour + 8) % 24
    generated_at = now.strftime(f"%Y-%m-%d {tw_hour:02d}:%M:%S")

    html = generate_html(funds, generated_at, repo_url, dispatch_token)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("\nDone -> index.html")


if __name__ == "__main__":
    main()
