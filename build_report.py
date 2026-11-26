import base64
import glob
import html
import json
import os
import re
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def parse_roc_date_key(d: str) -> tuple:
    """Convert ROC date like '115/11/26' to sortable tuple (115, 11, 26)."""
    m = re.match(r"(\d+)/(\d+)/(\d+)", d.strip())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


def parse_lab_files():
    seen = set()
    records = []
    files = sorted(glob.glob("檢驗檢查結果_*.html"))
    # Prefer main file first
    if "檢驗檢查結果_1151126.html" in files:
        files.remove("檢驗檢查結果_1151126.html")
        files.insert(0, "檢驗檢查結果_1151126.html")

    for f in files:
        with open(f, "r", encoding="utf-8") as fp:
            soup = BeautifulSoup(fp.read(), "html.parser")
        tbl = soup.select_one("table.list")
        if not tbl:
            continue
        tbody = tbl.tbody or tbl
        for tr in tbody.find_all("tr", recursive=False):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
            if len(tds) != 11:
                continue
            key = tuple(tds)
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "region": tds[0],
                "hospital": tds[1],
                "visit_date": tds[2],
                "test_date": tds[3],
                "upload_date": tds[4],
                "order_code": tds[5],
                "order_name": tds[6],
                "item_name": tds[7],
                "value": tds[8],
                "unit": "" if tds[9] == "無" else tds[9],
                "ref_raw": tds[10],
            })
    return records


def format_ref_and_status(val_str: str, ref_raw: str):
    """Parse reference range like '[8.8][10.2]' or '[＜100][]' and determine status."""
    parts = re.findall(r"\[([^\]]*)\]", ref_raw)
    low_str = parts[0].strip() if len(parts) >= 1 else ""
    high_str = parts[1].strip() if len(parts) >= 2 else ""

    # Clean display string for reference range
    if low_str in ("", "無") and high_str in ("", "無"):
        ref_disp = "—"
    elif low_str == "-" and high_str == "":
        ref_disp = "陰性 (-)"
    elif low_str and high_str:
        ref_disp = f"{low_str} ～ {high_str}"
    else:
        ref_disp = low_str or high_str

    # Try numeric comparison
    status = "normal"  # normal, high, low, warn, none
    status_text = ""

    # Extract leading float from val_str
    m_val = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)", val_str)
    num_val = float(m_val.group(1)) if m_val else None

    def to_float(s):
        try:
            return float(s)
        except Exception:
            return None

    low_num = to_float(low_str)
    high_num = to_float(high_str)

    if num_val is not None:
        if low_num is not None and high_num is not None:
            if num_val > high_num:
                status = "high"
                status_text = "偏高 ↑"
            elif num_val < low_num:
                status = "low"
                status_text = "偏低 ↓"
            else:
                status = "normal"
                status_text = "正常"
        else:
            # Handle patterns like '＜100', '< 100', 'Negative:＜ 1.0', '＞60'
            combined = f"{low_str} {high_str}"
            m_lt = re.search(r"[＜<]\s*=?\s*(\d+(?:\.\d+)?)", combined)
            m_gt = re.search(r"[＞>]\s*=?\s*(\d+(?:\.\d+)?)", combined)
            if m_lt:
                limit = float(m_lt.group(1))
                if num_val >= limit:
                    status = "high"
                    status_text = "偏高 ↑"
                else:
                    status = "normal"
                    status_text = "正常"
            elif m_gt:
                limit = float(m_gt.group(1))
                if num_val <= limit:
                    status = "low"
                    status_text = "偏低 ↓"
                else:
                    status = "normal"
                    status_text = "正常"
    else:
        # Qualitative check
        v_clean = val_str.strip()
        if low_str == "-" and v_clean not in ("-", "Negative", "陰性", ""):
            status = "warn"
            status_text = "異常"
        elif "+" in v_clean and low_str in ("-", "無", ""):
            status = "warn"
            status_text = "陽性反應"

    return ref_disp, status, status_text


def parse_opd_files():
    files = sorted(glob.glob("門診資料_*.html"))
    if "門診資料_1151126.html" in files:
        files.remove("門診資料_1151126.html")
        files.insert(0, "門診資料_1151126.html")

    seen_visits = set()
    visits = []

    for f in files:
        with open(f, "r", encoding="utf-8") as fp:
            soup = BeautifulSoup(fp.read(), "html.parser")
        tbl = soup.select_one("table.list")
        if not tbl:
            continue
        tbody = tbl.tbody or tbl
        trs = tbody.find_all("tr", recursive=False)
        file_visits = []
        cur = None
        for i in range(4, len(trs)):
            tds = [td.get_text(" ", strip=True) for td in trs[i].find_all(["th", "td"], recursive=False)]
            if not any(tds):
                continue
            if len(tds) == 11 and tds[0] != "":
                if cur:
                    file_visits.append(cur)
                cur = {
                    "region": tds[0],
                    "hospital": tds[1],
                    "visit_date": tds[2],
                    "dispense_date": tds[3],
                    "seq": tds[4],
                    "dx_code": tds[5],
                    "dx_name": tds[6],
                    "proc_code": tds[7],
                    "proc_name": tds[8],
                    "copay": tds[9],
                    "points": tds[10],
                    "sec_dx": [],
                    "sec_proc": [],
                    "orders": [],
                    "stage": "sec_dx",
                }
            elif len(tds) == 11 and tds[0] == "" and cur is not None:
                if cur["stage"] == "sec_dx":
                    for k in range(1, 10, 2):
                        if tds[k] or tds[k + 1]:
                            cur["sec_dx"].append((tds[k], tds[k + 1]))
                    cur["stage"] = "sec_proc"
                elif cur["stage"] == "sec_proc":
                    for k in range(1, 5, 2):
                        if tds[k] or tds[k + 1]:
                            cur["sec_proc"].append((tds[k], tds[k + 1]))
                    cur["stage"] = "orders"
            elif len(tds) == 4 and cur is not None:
                cur["orders"].append((tds[1], tds[2], tds[3]))
        if cur:
            file_visits.append(cur)

        for v in file_visits:
            v.pop("stage", None)
            sig = (
                v["hospital"], v["visit_date"], v["dispense_date"], v["seq"],
                v["dx_code"], v["copay"], v["points"], tuple(v["orders"])
            )
            if sig not in seen_visits:
                seen_visits.add(sig)
                visits.append(v)

    return visits


def is_admin_fee(code: str, name: str) -> bool:
    """Distinguish administrative/service fees from actual drugs, tests, or treatments."""
    if code.startswith(("00", "05", "P")):
        return True
    keywords = ("診察費", "藥事服務費", "護理費", "管理費", "調劑費", "個案追蹤")
    return any(k in name for k in keywords)


def build_html():
    labs = parse_lab_files()
    for r in labs:
        ref_disp, status, status_text = format_ref_and_status(r["value"], r["ref_raw"])
        r["ref_disp"] = ref_disp
        r["status"] = status
        r["status_text"] = status_text

    # Sort labs newest first by test_date, then visit_date
    labs.sort(
        key=lambda x: (parse_roc_date_key(x["test_date"]), parse_roc_date_key(x["visit_date"])),
        reverse=True,
    )

    visits = parse_opd_files()
    for v in visits:
        v["eff_date"] = v["visit_date"] or v["dispense_date"]
        v["is_dispense_only"] = (not v["visit_date"]) and bool(v["dispense_date"])
        # Classify orders into clinical vs admin fees
        clinical_orders = []
        admin_orders = []
        for code, name, qty in v["orders"]:
            item = {"code": code, "name": name, "qty": qty}
            if is_admin_fee(code, name):
                admin_orders.append(item)
            else:
                clinical_orders.append(item)
        v["clinical_orders"] = clinical_orders
        v["admin_orders"] = admin_orders

    visits.sort(key=lambda x: parse_roc_date_key(x["eff_date"]), reverse=True)

    abnormal_count = sum(1 for r in labs if r["status"] in ("high", "low", "warn"))

    data_payload = {
        "labs": labs,
        "visits": visits,
        "meta": {
            "id": "T20168****",
            "generated": "115/11/26",
            "period": "112/11/01 ～ 115/11/26",
            "lab_count": len(labs),
            "abnormal_count": abnormal_count,
            "visit_count": len(visits),
        },
    }

    json_data = json.dumps(data_payload, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-Hant-TW">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>個人健康存摺整合報告（112/11/01 ～ 115/11/26）</title>
  <style>
    :root {{
      --base-font: 21px;
      --bg: #f4f6f8;
      --card: #ffffff;
      --text: #181a1f;
      --muted: #4b5563;
      --border: #cbd5e1;
      --primary: #0f4c81;
      --primary-light: #e6f0fa;
      --accent: #0d7a5f;
      --danger-bg: #fee2e2;
      --danger-text: #b91c1c;
      --danger-border: #ef4444;
      --low-bg: #fff3cd;
      --low-text: #92400e;
      --low-border: #f59e0b;
      --ok-bg: #dcfce7;
      --ok-text: #166534;
    }}

    body.size-large {{ --base-font: 21px; }}
    body.size-xlarge {{ --base-font: 25px; }}
    body.size-xxlarge {{ --base-font: 29px; }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      padding: 0;
      background-color: var(--bg);
      color: var(--text);
      font-family: "Microsoft JhengHei", "微軟正黑體", "PingFang TC", "Noto Sans TC", sans-serif;
      font-size: var(--base-font);
      line-height: 1.65;
    }}

    .container {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 20px 18px 60px;
    }}

    /* Header */
    .header-card {{
      background: linear-gradient(135deg, #0f4c81 0%, #1565a8 100%);
      color: #ffffff;
      border-radius: 18px;
      padding: 24px 28px;
      margin-bottom: 20px;
      box-shadow: 0 4px 14px rgba(15, 76, 129, 0.18);
    }}

    .header-title {{
      margin: 0 0 12px 0;
      font-size: 1.5em;
      font-weight: 800;
      letter-spacing: 0.5px;
    }}

    .meta-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 28px;
      font-size: 0.95em;
      opacity: 0.96;
    }}

    .meta-item strong {{
      font-weight: 700;
    }}

    /* Sticky Toolbar */
    .toolbar {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(244, 246, 248, 0.96);
      backdrop-filter: blur(6px);
      padding: 12px 0 14px;
      border-bottom: 2px solid var(--border);
      margin-bottom: 20px;
    }}

    .toolbar-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
    }}

    .tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}

    .tab-btn {{
      font-family: inherit;
      font-size: 1em;
      font-weight: 800;
      padding: 14px 24px;
      border-radius: 14px;
      border: 3px solid var(--primary);
      background: #ffffff;
      color: var(--primary);
      cursor: pointer;
      transition: all 0.15s ease;
    }}

    .tab-btn.active {{
      background: var(--primary);
      color: #ffffff;
      box-shadow: 0 4px 10px rgba(15, 76, 129, 0.25);
    }}

    .font-controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }}

    .font-label {{
      font-weight: 700;
      color: var(--text);
      margin-right: 4px;
    }}

    .ctrl-btn {{
      font-family: inherit;
      font-size: 0.88em;
      font-weight: 700;
      padding: 10px 16px;
      border-radius: 10px;
      border: 2px solid #64748b;
      background: #ffffff;
      color: var(--text);
      cursor: pointer;
    }}

    .ctrl-btn.active {{
      background: #1e293b;
      color: #ffffff;
      border-color: #1e293b;
    }}

    .print-btn {{
      font-family: inherit;
      font-size: 0.88em;
      font-weight: 700;
      padding: 10px 18px;
      border-radius: 10px;
      border: 2px solid var(--accent);
      background: var(--accent);
      color: #ffffff;
      cursor: pointer;
    }}

    /* Filter Bar */
    .filter-bar {{
      background: var(--card);
      border: 2px solid var(--border);
      border-radius: 16px;
      padding: 16px 20px;
      margin-bottom: 20px;
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      align-items: center;
    }}

    .search-input {{
      font-family: inherit;
      font-size: 0.95em;
      padding: 12px 16px;
      border: 2px solid #94a3b8;
      border-radius: 12px;
      flex: 1 1 260px;
      min-width: 220px;
    }}

    .search-input:focus {{
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(15, 76, 129, 0.18);
    }}

    .select-input {{
      font-family: inherit;
      font-size: 0.92em;
      padding: 12px 14px;
      border: 2px solid #94a3b8;
      border-radius: 12px;
      background: #ffffff;
      cursor: pointer;
    }}

    .toggle-btn {{
      font-family: inherit;
      font-size: 0.92em;
      font-weight: 700;
      padding: 12px 18px;
      border-radius: 12px;
      border: 2px solid var(--danger-border);
      background: #ffffff;
      color: var(--danger-text);
      cursor: pointer;
    }}

    .toggle-btn.active {{
      background: var(--danger-text);
      color: #ffffff;
    }}

    .sort-btn {{
      font-family: inherit;
      font-size: 0.92em;
      font-weight: 700;
      padding: 12px 18px;
      border-radius: 12px;
      border: 2px solid var(--primary);
      background: var(--primary-light);
      color: var(--primary);
      cursor: pointer;
    }}

    .summary-count {{
      font-weight: 700;
      color: var(--muted);
      margin-bottom: 14px;
      font-size: 0.95em;
    }}

    /* Lab Cards (Grouped by Date + Hospital) */
    .group-card {{
      background: var(--card);
      border: 2px solid var(--border);
      border-radius: 18px;
      margin-bottom: 24px;
      overflow: hidden;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
    }}

    .group-header {{
      background: var(--primary-light);
      padding: 16px 22px;
      border-bottom: 2px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }}

    .group-title {{
      font-size: 1.12em;
      font-weight: 800;
      color: var(--primary);
    }}

    .group-sub {{
      font-size: 0.9em;
      color: var(--muted);
      font-weight: 700;
    }}

    .lab-table {{
      width: 100%;
      border-collapse: collapse;
    }}

    .lab-table th {{
      background: #f1f5f9;
      text-align: left;
      padding: 14px 16px;
      font-size: 0.88em;
      color: #334155;
      border-bottom: 2px solid var(--border);
    }}

    .lab-table td {{
      padding: 16px;
      border-bottom: 1px solid #e2e8f0;
      vertical-align: middle;
    }}

    .lab-table tr:last-child td {{
      border-bottom: none;
    }}

    .lab-table tr.row-high {{
      background-color: #fff5f5;
    }}

    .lab-table tr.row-low {{
      background-color: #fffbeb;
    }}

    .lab-table tr.row-warn {{
      background-color: #fff5f5;
    }}

    .item-main {{
      font-weight: 800;
      color: var(--text);
      display: block;
    }}

    .item-sub {{
      font-size: 0.84em;
      color: var(--muted);
      display: block;
    }}

    .val-num {{
      font-weight: 800;
      font-size: 1.15em;
    }}

    .val-high, .val-warn {{
      color: var(--danger-text);
    }}

    .val-low {{
      color: var(--low-text);
    }}

    .badge {{
      display: inline-block;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 0.82em;
      font-weight: 800;
      margin-left: 6px;
      white-space: nowrap;
    }}

    .badge-high, .badge-warn {{
      background: var(--danger-bg);
      color: var(--danger-text);
      border: 1.5px solid var(--danger-border);
    }}

    .badge-low {{
      background: var(--low-bg);
      color: var(--low-text);
      border: 1.5px solid var(--low-border);
    }}

    .badge-normal {{
      background: var(--ok-bg);
      color: var(--ok-text);
    }}

    /* Outpatient Cards */
    .visit-card {{
      background: var(--card);
      border: 2px solid var(--border);
      border-radius: 18px;
      margin-bottom: 24px;
      overflow: hidden;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
    }}

    .visit-header {{
      background: #eaf4f0;
      padding: 16px 22px;
      border-bottom: 2px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }}

    .visit-date-hosp {{
      font-size: 1.15em;
      font-weight: 800;
      color: #0b5345;
    }}

    .visit-cost {{
      font-size: 0.9em;
      font-weight: 700;
      color: #334155;
      background: #ffffff;
      padding: 6px 14px;
      border-radius: 10px;
      border: 1.5px solid #cbd5e1;
    }}

    .visit-body {{
      padding: 18px 22px;
    }}

    .dx-box {{
      background: #f8fafc;
      border-left: 6px solid var(--primary);
      padding: 12px 16px;
      border-radius: 8px;
      margin-bottom: 16px;
    }}

    .dx-main {{
      font-size: 1.08em;
      font-weight: 800;
      color: var(--text);
    }}

    .dx-sec {{
      margin-top: 8px;
      font-size: 0.9em;
      color: var(--muted);
    }}

    .order-section-title {{
      font-weight: 800;
      font-size: 0.95em;
      color: var(--primary);
      margin: 12px 0 8px;
    }}

    .order-list {{
      list-style: none;
      padding: 0;
      margin: 0;
    }}

    .order-list li {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 14px;
      padding: 10px 12px;
      border-bottom: 1px dashed #cbd5e1;
    }}

    .order-list li:last-child {{
      border-bottom: none;
    }}

    .order-name {{
      font-weight: 700;
      flex: 1;
    }}

    .order-qty {{
      font-weight: 800;
      color: var(--primary);
      white-space: nowrap;
      background: var(--primary-light);
      padding: 3px 12px;
      border-radius: 8px;
    }}

    .admin-toggle {{
      margin-top: 12px;
      font-family: inherit;
      font-size: 0.82em;
      font-weight: 700;
      color: var(--muted);
      background: #f1f5f9;
      border: 1.5px solid #cbd5e1;
      border-radius: 8px;
      padding: 6px 14px;
      cursor: pointer;
    }}

    .admin-box {{
      display: none;
      margin-top: 10px;
      padding: 10px 14px;
      background: #f8fafc;
      border-radius: 10px;
      font-size: 0.85em;
      color: var(--muted);
    }}

    .admin-box.open {{
      display: block;
    }}

    .back-top-btn {{
      position: fixed;
      right: 22px;
      bottom: 22px;
      font-family: inherit;
      font-size: 0.95em;
      font-weight: 800;
      padding: 14px 20px;
      border-radius: 999px;
      border: none;
      background: var(--primary);
      color: #ffffff;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);
      cursor: pointer;
      z-index: 200;
    }}

    .hidden {{
      display: none !important;
    }}

    /* Responsive table for narrow screens */
    @media (max-width: 760px) {{
      .lab-table thead {{
        display: none;
      }}
      .lab-table, .lab-table tbody, .lab-table tr, .lab-table td {{
        display: block;
        width: 100%;
      }}
      .lab-table tr {{
        padding: 12px 14px;
        border-bottom: 2px solid var(--border);
      }}
      .lab-table td {{
        padding: 6px 4px;
        border-bottom: none;
      }}
    }}

    @media print {{
      .toolbar, .filter-bar, .back-top-btn, .admin-toggle {{
        display: none !important;
      }}
      body {{
        background: #ffffff;
        font-size: 16pt;
      }}
      .group-card, .visit-card {{
        break-inside: avoid;
        box-shadow: none;
      }}
    }}
  </style>
</head>
<body class="size-large">
  <div class="container">
    <div class="header-card">
      <h1 class="header-title">📋 個人健康存摺整合報告</h1>
      <div class="meta-grid">
        <div class="meta-item"><strong>身分證號：</strong><span id="meta-id"></span></div>
        <div class="meta-item"><strong>資料期間：</strong><span id="meta-period"></span></div>
        <div class="meta-item"><strong>產製日期：</strong><span id="meta-gen"></span></div>
      </div>
    </div>

    <div class="toolbar">
      <div class="toolbar-row">
        <div class="tabs">
          <button class="tab-btn active" id="tab-lab" onclick="switchTab('lab')">
            🧪 檢驗檢查結果 (<span id="count-lab"></span>)
          </button>
          <button class="tab-btn" id="tab-opd" onclick="switchTab('opd')">
            🏥 門診與領藥紀錄 (<span id="count-opd"></span>)
          </button>
        </div>

        <div class="font-controls">
          <span class="font-label">字體大小：</span>
          <button class="ctrl-btn active" id="btn-size-large" onclick="setFontSize('large', this)">大</button>
          <button class="ctrl-btn" id="btn-size-xlarge" onclick="setFontSize('xlarge', this)">特大</button>
          <button class="ctrl-btn" id="btn-size-xxlarge" onclick="setFontSize('xxlarge', this)">超大</button>
          <button class="print-btn" onclick="window.print()">🖨️ 列印</button>
        </div>
      </div>
    </div>

    <!-- LAB SECTION -->
    <section id="section-lab">
      <div class="filter-bar">
        <input type="text" id="lab-search" class="search-input" placeholder="🔍 搜尋檢驗項目、醫院或日期（例：膽固醇、義大）" oninput="renderLabs()" />
        <select id="lab-hospital" class="select-input" onchange="renderLabs()">
          <option value="">全部醫院</option>
        </select>
        <button id="btn-abnormal-only" class="toggle-btn" onclick="toggleAbnormalOnly()">
          ⚠️ 只看異常紅字 (<span id="count-abnormal"></span>)
        </button>
        <button id="btn-lab-sort" class="sort-btn" onclick="toggleLabSort()">
          ⬇️ 日期：新到舊
        </button>
      </div>
      <div class="summary-count" id="lab-summary"></div>
      <div id="lab-container"></div>
    </section>

    <!-- OPD SECTION -->
    <section id="section-opd" class="hidden">
      <div class="filter-bar">
        <input type="text" id="opd-search" class="search-input" placeholder="🔍 搜尋診斷、藥品名稱或醫院（例：骨質疏鬆、高血壓）" oninput="renderOpd()" />
        <select id="opd-hospital" class="select-input" onchange="renderOpd()">
          <option value="">全部醫療院所</option>
        </select>
        <button id="btn-opd-sort" class="sort-btn" onclick="toggleOpdSort()">
          ⬇️ 日期：新到舊
        </button>
      </div>
      <div class="summary-count" id="opd-summary"></div>
      <div id="opd-container"></div>
    </section>
  </div>

  <button class="back-top-btn" onclick="window.scrollTo({{top: 0, behavior: 'smooth'}})">⬆️ 回頂部</button>

  <script>
    const DATA = {json_data};

    let abnormalOnly = false;
    let labNewestFirst = true;
    let opdNewestFirst = true;

    function esc(str) {{
      if (str === null || str === undefined) return "";
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function init() {{
      document.getElementById("meta-id").textContent = DATA.meta.id;
      document.getElementById("meta-period").textContent = DATA.meta.period;
      document.getElementById("meta-gen").textContent = DATA.meta.generated;
      document.getElementById("count-lab").textContent = DATA.meta.lab_count + "項";
      document.getElementById("count-opd").textContent = DATA.meta.visit_count + "次";
      document.getElementById("count-abnormal").textContent = DATA.meta.abnormal_count;

      // Populate hospital dropdowns
      const labHosps = [...new Set(DATA.labs.map(x => x.hospital))].sort();
      const labSel = document.getElementById("lab-hospital");
      labHosps.forEach(h => {{
        const opt = document.createElement("option");
        opt.value = h;
        opt.textContent = h;
        labSel.appendChild(opt);
      }});

      const opdHosps = [...new Set(DATA.visits.map(x => x.hospital))].sort();
      const opdSel = document.getElementById("opd-hospital");
      opdHosps.forEach(h => {{
        const opt = document.createElement("option");
        opt.value = h;
        opt.textContent = h;
        opdSel.appendChild(opt);
      }});

      renderLabs();
      renderOpd();
    }}

    function setFontSize(size, btn) {{
      document.body.classList.remove("size-large", "size-xlarge", "size-xxlarge");
      document.body.classList.add("size-" + size);
      document.querySelectorAll(".font-controls .ctrl-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    }}

    function switchTab(tab) {{
      document.getElementById("section-lab").classList.toggle("hidden", tab !== "lab");
      document.getElementById("section-opd").classList.toggle("hidden", tab !== "opd");
      document.getElementById("tab-lab").classList.toggle("active", tab === "lab");
      document.getElementById("tab-opd").classList.toggle("active", tab === "opd");
    }}

    function toggleAbnormalOnly() {{
      abnormalOnly = !abnormalOnly;
      document.getElementById("btn-abnormal-only").classList.toggle("active", abnormalOnly);
      renderLabs();
    }}

    function toggleLabSort() {{
      labNewestFirst = !labNewestFirst;
      document.getElementById("btn-lab-sort").textContent = labNewestFirst ? "⬇️ 日期：新到舊" : "⬆️ 日期：舊到新";
      renderLabs();
    }}

    function toggleOpdSort() {{
      opdNewestFirst = !opdNewestFirst;
      document.getElementById("btn-opd-sort").textContent = opdNewestFirst ? "⬇️ 日期：新到舊" : "⬆️ 日期：舊到新";
      renderOpd();
    }}

    function dateKey(d) {{
      const parts = (d || "").trim().split("/");
      if (parts.length !== 3) return 0;
      const y = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const day = parseInt(parts[2], 10);
      if (isNaN(y) || isNaN(m) || isNaN(day)) return 0;
      return y * 10000 + m * 100 + day;
    }}

    function renderLabs() {{
      const q = document.getElementById("lab-search").value.trim().toLowerCase();
      const hosp = document.getElementById("lab-hospital").value;

      let filtered = DATA.labs.filter(r => {{
        if (hosp && r.hospital !== hosp) return false;
        if (abnormalOnly && !["high", "low", "warn"].includes(r.status)) return false;
        if (q) {{
          const hay = (r.hospital + " " + r.test_date + " " + r.order_name + " " + r.item_name + " " + r.value).toLowerCase();
          if (!hay.includes(q)) return false;
        }}
        return true;
      }});

      filtered.sort((a, b) => {{
        const diff = dateKey(a.test_date) - dateKey(b.test_date);
        return labNewestFirst ? -diff : diff;
      }});

      document.getElementById("lab-summary").textContent = `目前顯示 ${{filtered.length}} 筆檢驗項目`;

      // Group by test_date + hospital
      const groups = [];
      const map = new Map();
      filtered.forEach(r => {{
        const gk = r.test_date + "||" + r.hospital;
        if (!map.has(gk)) {{
          const g = {{ test_date: r.test_date, visit_date: r.visit_date, hospital: r.hospital, items: [] }};
          map.set(gk, g);
          groups.push(g);
        }}
        map.get(gk).items.push(r);
      }});

      const container = document.getElementById("lab-container");
      if (groups.length === 0) {{
        container.innerHTML = `<div class="group-card" style="padding: 30px; text-align: center; color: #64748b;">找不到符合條件的檢驗資料</div>`;
        return;
      }}

      container.innerHTML = groups.map(g => {{
        const rowsHtml = g.items.map(r => {{
          const rowCls = r.status === "high" ? "row-high" : (r.status === "low" ? "row-low" : (r.status === "warn" ? "row-warn" : ""));
          const valCls = r.status === "high" ? "val-high" : (r.status === "low" ? "val-low" : (r.status === "warn" ? "val-warn" : ""));
          let badgeHtml = "";
          if (r.status === "high") badgeHtml = `<span class="badge badge-high">${{esc(r.status_text)}}</span>`;
          else if (r.status === "low") badgeHtml = `<span class="badge badge-low">${{esc(r.status_text)}}</span>`;
          else if (r.status === "warn") badgeHtml = `<span class="badge badge-warn">${{esc(r.status_text)}}</span>`;
          else if (r.status === "normal" && r.status_text) badgeHtml = `<span class="badge badge-normal">正常</span>`;

          return `
            <tr class="${{rowCls}}">
              <td>
                <span class="item-main">${{esc(r.order_name)}}</span>
                <span class="item-sub">${{esc(r.item_name)}}</span>
              </td>
              <td>
                <span class="val-num ${{valCls}}">${{esc(r.value)}}</span>
                <span style="font-size:0.88em; color:#475569;">${{esc(r.unit)}}</span>
                ${{badgeHtml}}
              </td>
              <td>${{esc(r.ref_disp)}}</td>
            </tr>
          `;
        }}).join("");

        return `
          <div class="group-card">
            <div class="group-header">
              <div class="group-title">📅 檢驗日期：${{esc(g.test_date)}} ｜ 🏥 ${{esc(g.hospital)}}</div>
              <div class="group-sub">就醫日期：${{esc(g.visit_date)}}（共 ${{g.items.length}} 項）</div>
            </div>
            <table class="lab-table">
              <thead>
                <tr>
                  <th style="width: 44%;">檢驗項目</th>
                  <th style="width: 32%;">檢查結果</th>
                  <th style="width: 24%;">標準參考值</th>
                </tr>
              </thead>
              <tbody>${{rowsHtml}}</tbody>
            </table>
          </div>
        `;
      }}).join("");
    }}

    function toggleAdmin(idx) {{
      const el = document.getElementById("admin-box-" + idx);
      if (el) el.classList.toggle("open");
    }}

    function renderOpd() {{
      const q = document.getElementById("opd-search").value.trim().toLowerCase();
      const hosp = document.getElementById("opd-hospital").value;

      let filtered = DATA.visits.filter(v => {{
        if (hosp && v.hospital !== hosp) return false;
        if (q) {{
          const orderText = v.orders.map(o => o[1]).join(" ");
          const secDxText = v.sec_dx.map(d => d[1]).join(" ");
          const hay = (v.hospital + " " + v.eff_date + " " + v.dx_name + " " + v.proc_name + " " + secDxText + " " + orderText).toLowerCase();
          if (!hay.includes(q)) return false;
        }}
        return true;
      }});

      filtered.sort((a, b) => {{
        const diff = dateKey(a.eff_date) - dateKey(b.eff_date);
        return opdNewestFirst ? -diff : diff;
      }});

      document.getElementById("opd-summary").textContent = `目前顯示 ${{filtered.length}} 次門診／領藥紀錄`;

      const container = document.getElementById("opd-container");
      if (filtered.length === 0) {{
        container.innerHTML = `<div class="visit-card" style="padding: 30px; text-align: center; color: #64748b;">找不到符合條件的門診資料</div>`;
        return;
      }}

      container.innerHTML = filtered.map((v, idx) => {{
        const dateLabel = v.is_dispense_only ? `💊 藥局調劑日期：${{esc(v.eff_date)}}` : `📅 看診日期：${{esc(v.eff_date)}}`;
        const secDxList = v.sec_dx.map(d => d[1]).filter(Boolean);
        const secDxHtml = secDxList.length ? `<div class="dx-sec">其他診斷：${{esc(secDxList.join("、"))}}</div>` : "";
        const procList = [];
        if (v.proc_name) procList.push(v.proc_name);
        v.sec_proc.forEach(p => {{ if (p[1]) procList.push(p[1]); }});
        const procHtml = procList.length ? `<div class="dx-sec">處置項目：${{esc(procList.join("、"))}}</div>` : "";

        const clinicalHtml = v.clinical_orders.length
          ? `<div class="order-section-title">💊 用藥與檢查項目</div>
             <ul class="order-list">
               ${{v.clinical_orders.map(o => `
                 <li>
                   <span class="order-name">${{esc(o.name)}}</span>
                   <span class="order-qty">數量：${{esc(o.qty)}}</span>
                 </li>
               `).join("")}}
             </ul>`
          : `<div style="color:#64748b; font-size:0.92em;">（本次無開立藥品或檢驗項目）</div>`;

        const adminHtml = v.admin_orders.length
          ? `<button class="admin-toggle" onclick="toggleAdmin(${{idx}})">顯示健保行政診察項目 (${{v.admin_orders.length}}項)</button>
             <div class="admin-box" id="admin-box-${{idx}}">
               ${{v.admin_orders.map(o => `<div>• ${{esc(o.name)}}（數量：${{esc(o.qty)}}）</div>`).join("")}}
             </div>`
          : "";

        return `
          <div class="visit-card">
            <div class="visit-header">
              <div class="visit-date-hosp">${{dateLabel}} ｜ 🏥 ${{esc(v.hospital)}}</div>
              <div class="visit-cost">自付部分負擔：${{esc(v.copay)}} 元</div>
            </div>
            <div class="visit-body">
              <div class="dx-box">
                <div class="dx-main">🩺 主要診斷：${{esc(v.dx_name || "未填寫")}}</div>
                ${{secDxHtml}}
                ${{procHtml}}
              </div>
              ${{clinicalHtml}}
              ${{adminHtml}}
            </div>
          </div>
        `;
      }}).join("");
    }}

    init();
  </script>
</body>
</html>
"""

    out_path = "健康存摺整合報告_1151126.html"
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(html_content)
    print(f"Generated {out_path} successfully! Labs={len(labs)} (abnormal={abnormal_count}), Visits={len(visits)}")

    # Also generate AES-256-GCM encrypted index.html for GitHub Pages
    password = os.environ.get("MHB_PASSWORD", "1151126")
    salt = os.urandom(16)
    iv = os.urandom(12)
    iterations = 200000
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    key = kdf.derive(password.encode("utf-8"))
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(iv, json_data.encode("utf-8"), None)

    enc_bundle = json.dumps({
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "iter": iterations,
    })

    # Inject lock screen + decryption logic into index.html
    enc_html = html_content
    enc_html = enc_html.replace(
        "<title>個人健康存摺整合報告（112/11/01 ～ 115/11/26）</title>",
        '<meta name="robots" content="noindex, nofollow" />\n  <title>個人健康存摺整合報告</title>',
    )
    enc_html = enc_html.replace(
        "  <div class=\"container\">",
        """  <div id="lock-screen" style="max-width:520px;margin:60px auto;padding:34px 28px;background:#ffffff;border:2px solid #cbd5e1;border-radius:20px;box-shadow:0 8px 24px rgba(0,0,0,0.08);text-align:center;">
    <div style="font-size:2.4em;margin-bottom:8px;">🔒</div>
    <h1 style="margin:0 0 10px;font-size:1.45em;color:#0f4c81;">個人健康存摺報告</h1>
    <p style="margin:0 0 22px;color:#4b5563;font-size:0.95em;">為保護個人醫療隱私，請輸入觀看密碼</p>
    <input id="pwd-input" type="password" inputmode="numeric" placeholder="請輸入密碼"
      style="width:100%;font-size:1.15em;padding:14px 16px;border:2px solid #94a3b8;border-radius:12px;text-align:center;margin-bottom:14px;font-family:inherit;"
      onkeydown="if(event.key==='Enter') unlockReport()" />
    <label style="display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:18px;font-size:0.9em;color:#334155;cursor:pointer;">
      <input type="checkbox" id="remember-pwd" checked style="width:20px;height:20px;" />
      <span>在此裝置記住密碼（下次免輸入）</span>
    </label>
    <button onclick="unlockReport()"
      style="width:100%;font-family:inherit;font-size:1.1em;font-weight:800;padding:15px;border:none;border-radius:14px;background:#0f4c81;color:#ffffff;cursor:pointer;">
      🔓 確認開啟
    </button>
    <div id="pwd-error" style="display:none;margin-top:14px;color:#b91c1c;font-weight:800;background:#fee2e2;padding:10px;border-radius:10px;">
      密碼不正確，請再試一次
    </div>
  </div>
  <div id="app-root" class="container hidden">""",
    )
    enc_html = enc_html.replace(
        '<button class="back-top-btn"',
        '<button id="back-top-btn" class="back-top-btn hidden"',
    )
    enc_html = enc_html.replace(
        f"const DATA = {json_data};",
        f"const ENC_BUNDLE = {enc_bundle};\n    let DATA = null;",
    )
    enc_html = enc_html.replace(
        "    init();\n  </script>",
        """    function b64ToBytes(b64) {
      const bin = atob(b64);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      return arr;
    }

    async function decryptWithPassword(pwd) {
      const enc = new TextEncoder();
      const keyMat = await crypto.subtle.importKey(
        "raw", enc.encode(pwd), { name: "PBKDF2" }, false, ["deriveKey"]
      );
      const aesKey = await crypto.subtle.deriveKey(
        { name: "PBKDF2", salt: b64ToBytes(ENC_BUNDLE.salt), iterations: ENC_BUNDLE.iter, hash: "SHA-256" },
        keyMat,
        { name: "AES-GCM", length: 256 },
        false,
        ["decrypt"]
      );
      const plainBuf = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: b64ToBytes(ENC_BUNDLE.iv) },
        aesKey,
        b64ToBytes(ENC_BUNDLE.ct)
      );
      return JSON.parse(new TextDecoder().decode(plainBuf));
    }

    async function unlockReport(autoPwd) {
      const pwd = autoPwd !== undefined ? autoPwd : document.getElementById("pwd-input").value.trim();
      const errEl = document.getElementById("pwd-error");
      if (!pwd) {
        errEl.style.display = "block";
        return;
      }
      try {
        DATA = await decryptWithPassword(pwd);
        if (autoPwd === undefined && document.getElementById("remember-pwd").checked) {
          try { localStorage.setItem("mhb_saved_pwd", pwd); } catch (e) {}
        }
        document.getElementById("lock-screen").classList.add("hidden");
        document.getElementById("app-root").classList.remove("hidden");
        document.getElementById("back-top-btn").classList.remove("hidden");
        init();
      } catch (e) {
        if (autoPwd !== undefined) {
          try { localStorage.removeItem("mhb_saved_pwd"); } catch (err) {}
        } else {
          errEl.style.display = "block";
        }
      }
    }

    (function autoCheck() {
      try {
        const saved = localStorage.getItem("mhb_saved_pwd");
        if (saved) unlockReport(saved);
      } catch (e) {}
    })();
  </script>""",
    )

    with open("index.html", "w", encoding="utf-8") as fp:
        fp.write(enc_html)
    print("Generated encrypted index.html successfully!")


if __name__ == "__main__":
    build_html()
