import base64
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import json
import os
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 优先加载同目录下的 API key.txt
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "API key.txt")
if os.path.exists(KEY_FILE):
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v)
    except Exception:
        pass

# ==================== 配置区域 ====================
OCTOPUS_API_KEY = os.getenv("OCTOPUS_API_KEY", "")
OCTOPUS_MPAN = os.getenv("OCTOPUS_MPAN", "1200062063391")
OCTOPUS_METER_SERIAL = os.getenv("OCTOPUS_METER_SERIAL", "16K0577482")
PRODUCT_CODE = os.getenv("OCTOPUS_PRODUCT_CODE", "AGILE-24-10-01")
REGION_CODE = os.getenv("OCTOPUS_REGION_CODE", "C")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

START_DATE = "2026-01-01T00:00:00Z"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
LONDON_TZ = ZoneInfo("Europe/London")
# =================================================


def parse_utc(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_paginated_data(url: str, params: dict, auth_header: str = None) -> list:
    results = []
    current_url = f"{url}?{urllib.parse.urlencode(params)}"
    
    while current_url:
        req = urllib.request.Request(current_url, headers={"User-Agent": "OctopusDashboardBuilder/1.0"})
        if auth_header:
            req.add_header("Authorization", auth_header)
        
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            batch = data.get("results", [])
            results.extend(batch)
            current_url = data.get("next")
    return results


def fetch_all_consumption(mpan, serial, api_key, start, end):
    print(f"[*] 正在拉取用电量数据: {start} 至 {end}...")
    url = f"https://api.octopus.energy/v1/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
    params = {"period_from": start, "period_to": end, "page_size": 25000, "order_by": "period"}
    auth_header = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode()
    
    raw = fetch_paginated_data(url, params, auth_header)
    print(f"[+] 获取到 {len(raw)} 条用电记录。")

    consumption_map = {}
    for item in raw:
        start_utc = parse_utc(item["interval_start"])
        end_utc = parse_utc(item["interval_end"])
        key = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        consumption_map[key] = {
            "start_dt": start_utc,
            "end_dt": end_utc,
            "consumption": float(item["consumption"]),
        }
    return consumption_map


def fetch_all_rates(product, tariff, start, end):
    print(f"[*] 正在拉取 Agile 电价数据: {tariff}...")
    url = f"https://api.octopus.energy/v1/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/"
    params = {"period_from": start, "period_to": end, "page_size": 1500}
    
    raw = fetch_paginated_data(url, params)
    print(f"[+] 获取到 {len(raw)} 条电价记录。")

    rates_map = {}
    for item in raw:
        start_utc = parse_utc(item["valid_from"])
        key = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        rates_map[key] = {
            "rate_inc_vat": float(item["value_inc_vat"]),
            "rate_exc_vat": float(item["value_exc_vat"]),
        }
    return rates_map


def build_dashboard_data(consumption_map, rates_map):
    bins = [
        {"id": "neg", "label": "< 0p (负电价)", "color": "#3b82f6", "fn": lambda r: r < 0},
        {"id": "tier1", "label": "0 ~ 10p (超低价)", "color": "#10b981", "fn": lambda r: 0 <= r < 10},
        {"id": "tier2", "label": "10 ~ 15p (优惠谷电)", "color": "#34d399", "fn": lambda r: 10 <= r < 15},
        {"id": "tier3", "label": "15 ~ 20p (基准平电)", "color": "#facc15", "fn": lambda r: 15 <= r < 20},
        {"id": "tier4", "label": "20 ~ 25p (适中偏高)", "color": "#fb923c", "fn": lambda r: 20 <= r < 25},
        {"id": "tier5", "label": "25 ~ 35p (昂贵时段)", "color": "#f87171", "fn": lambda r: 25 <= r < 35},
        {"id": "tier6", "label": "≥ 35p (极高峰)", "color": "#ef4444", "fn": lambda r: r >= 35},
    ]

    bin_stats = {b["id"]: {"label": b["label"], "color": b["color"], "kwh": 0.0, "cost_p": 0.0, "slots": 0} for b in bins}
    
    daily_data = defaultdict(lambda: {
        "kwh": 0.0,
        "cost_p": 0.0,
        "rate_sum": 0.0,
        "count": 0,
        "neg": 0.0,
        "low": 0.0,
        "mid": 0.0,
        "high": 0.0,
        "peak_kwh": 0.0,
        "slots": []
    })

    monthly_data = defaultdict(lambda: {
        "month": "",
        "days": set(),
        "kwh": 0.0,
        "cost_p": 0.0,
        "rate_sum": 0.0,
        "count": 0,
        "peak_kwh": 0.0,
        "peak_cost_p": 0.0,
        "neg_kwh": 0.0,
        "neg_earnings_p": 0.0,
        "neg_slots": 0,
        "low_kwh": 0.0,
        "mid_kwh": 0.0,
        "high_kwh": 0.0
    })

    hourly_kwh = [0.0] * 24
    hourly_rate_sum = [0.0] * 24
    hourly_slots = [0] * 24

    total_kwh = 0.0
    total_cost_p = 0.0
    matched_slots_count = 0

    for key, c_item in consumption_map.items():
        if key not in rates_map:
            continue

        r_item = rates_map[key]
        kwh = c_item["consumption"]
        rate = r_item["rate_inc_vat"]
        cost_p = kwh * rate
        matched_slots_count += 1
        total_kwh += kwh
        total_cost_p += cost_p

        for b in bins:
            if b["fn"](rate):
                bin_stats[b["id"]]["kwh"] += kwh
                bin_stats[b["id"]]["cost_p"] += cost_p
                bin_stats[b["id"]]["slots"] += 1
                break

        dt_local = c_item["start_dt"].astimezone(LONDON_TZ)
        day = dt_local.strftime("%Y-%m-%d")
        time_str = dt_local.strftime("%H:%M")
        hour = dt_local.hour
        is_peak = (16 <= hour < 19)

        d = daily_data[day]
        d["kwh"] += kwh
        d["cost_p"] += cost_p
        d["rate_sum"] += rate
        d["count"] += 1
        if is_peak:
            d["peak_kwh"] += kwh

        if rate < 0:
            d["neg"] += kwh
        elif rate < 15:
            d["low"] += kwh
        elif rate < 25:
            d["mid"] += kwh
        else:
            d["high"] += kwh

        d["slots"].append({
            "t": time_str,
            "kwh": round(kwh, 4),
            "r": round(rate, 2),
            "p": round(cost_p, 2),
            "pk": is_peak
        })

        m = day[:7]
        me = monthly_data[m]
        me["month"] = m
        me["days"].add(day)
        me["kwh"] += kwh
        me["cost_p"] += cost_p
        me["rate_sum"] += rate
        me["count"] += 1
        if is_peak:
            me["peak_kwh"] += kwh
            me["peak_cost_p"] += cost_p
        if rate < 0:
            me["neg_kwh"] += kwh
            me["neg_earnings_p"] += abs(cost_p)
            me["neg_slots"] += 1
            me["low_kwh"] += kwh
        elif rate < 15:
            me["low_kwh"] += kwh
        elif rate < 25:
            me["mid_kwh"] += kwh
        else:
            me["high_kwh"] += kwh

        hourly_kwh[hour] += kwh
        hourly_rate_sum[hour] += rate
        hourly_slots[hour] += 1

    sorted_days = sorted(daily_data.keys())

    frontend_daily = []
    for day in sorted_days:
        d = daily_data[day]
        vwap = (d["cost_p"] / d["kwh"]) if d["kwh"] > 0 else 0
        mkt_avg = (d["rate_sum"] / d["count"]) if d["count"] > 0 else 0
        frontend_daily.append({
            "date": day,
            "kwh": round(d["kwh"], 2),
            "cost": round(d["cost_p"] / 100.0, 2),
            "vwap": round(vwap, 2),
            "mkt": round(mkt_avg, 2),
            "neg": round(d["neg"], 3),
            "low": round(d["low"], 3),
            "mid": round(d["mid"], 3),
            "high": round(d["high"], 3),
            "peak_kwh": round(d["peak_kwh"], 2),
            "slots": d["slots"]
        })

    standing_charge_rate = 0.39535  # £/天 (39.54p)
    frontend_monthly = []
    for m in sorted(monthly_data.keys()):
        me = monthly_data[m]
        days_c = len(me["days"])
        m_kwh = round(me["kwh"], 2)
        m_cost = round(me["cost_p"] / 100.0, 2)
        sc_gbp = round(days_c * standing_charge_rate, 2)
        all_in_cost = round(m_cost + sc_gbp, 2)
        vwap = round((me["cost_p"] / m_kwh), 2) if m_kwh > 0 else 0
        all_in_vwap = round((all_in_cost * 100 / m_kwh), 2) if m_kwh > 0 else 0
        mkt_avg = round((me["rate_sum"] / me["count"]), 2) if me["count"] > 0 else 0
        peak_kwh = round(me["peak_kwh"], 2)
        offpeak_kwh = round(m_kwh - peak_kwh, 2)
        peak_pct = round((peak_kwh / m_kwh * 100), 1) if m_kwh > 0 else 0
        neg_kwh = round(me["neg_kwh"], 2)
        neg_earn = round(me["neg_earnings_p"] / 100.0, 2)

        svt_unit_rate = 0.25 if m < "2026-10" else 0.2632
        svt_sc_rate = 0.609 if m < "2026-10" else 0.5483
        svt_cost = round(m_kwh * svt_unit_rate + days_c * svt_sc_rate, 2)
        savings = round(svt_cost - all_in_cost, 2)
        savings_pct = round((savings / svt_cost * 100), 1) if svt_cost > 0 else 0

        frontend_monthly.append({
            "month": m,
            "days": days_c,
            "kwh": m_kwh,
            "daily_avg": round(m_kwh / days_c, 2) if days_c > 0 else 0,
            "cost": m_cost,
            "sc": sc_gbp,
            "all_in": all_in_cost,
            "vwap": vwap,
            "all_in_vwap": all_in_vwap,
            "mkt": mkt_avg,
            "diff": round(vwap - mkt_avg, 2),
            "peak_kwh": peak_kwh,
            "offpeak_kwh": offpeak_kwh,
            "peak_pct": peak_pct,
            "neg_kwh": neg_kwh,
            "neg_slots": me["neg_slots"],
            "neg_earn": neg_earn,
            "svt_cost": svt_cost,
            "savings": savings,
            "savings_pct": savings_pct,
            "low": round(me["low_kwh"], 2),
            "mid": round(me["mid_kwh"], 2),
            "high": round(me["high_kwh"], 2),
        })

    frontend_bins = []
    for b in bins:
        st = bin_stats[b["id"]]
        kwh = st["kwh"]
        pct = (kwh / total_kwh * 100) if total_kwh > 0 else 0
        avg_r = (st["cost_p"] / kwh) if kwh > 0 else 0
        frontend_bins.append({
            "id": b["id"],
            "label": b["label"],
            "color": b["color"],
            "kwh": round(kwh, 2),
            "pct": round(pct, 1),
            "slots": st["slots"],
            "cost": round(st["cost_p"] / 100.0, 2),
            "avg_r": round(avg_r, 2)
        })

    frontend_hourly = []
    for h in range(24):
        avg_r = (hourly_rate_sum[h] / hourly_slots[h]) if hourly_slots[h] > 0 else 0
        avg_k = (hourly_kwh[h] / (hourly_slots[h] / 2)) if hourly_slots[h] > 0 else 0
        frontend_hourly.append({
            "h": f"{h:02d}:00",
            "rate": round(avg_r, 2),
            "kwh": round(avg_k, 3)
        })

    total_sc = round(len(sorted_days) * standing_charge_rate, 2)
    total_cost_gbp = round(total_cost_p / 100.0, 2)
    total_allin_gbp = round(total_cost_gbp + total_sc, 2)

    summary = {
        "total_kwh": round(total_kwh, 2),
        "total_cost": total_cost_gbp,
        "total_sc": total_sc,
        "total_allin": total_allin_gbp,
        "vwap": round(total_cost_p / total_kwh, 2) if total_kwh > 0 else 0,
        "allin_vwap": round(total_allin_gbp * 100 / total_kwh, 2) if total_kwh > 0 else 0,
        "days_count": len(sorted_days),
        "months_count": len(frontend_monthly),
        "slots_count": matched_slots_count,
        "updated_at": datetime.now(LONDON_TZ).strftime("%Y-%m-%d %H:%M:%S BST"),
        "data_range": f"{sorted_days[0]} 至 {sorted_days[-1]}" if sorted_days else "",
        "first_day": sorted_days[0] if sorted_days else "",
        "last_day": sorted_days[-1] if sorted_days else "",
    }

    return {
        "summary": summary,
        "monthly": frontend_monthly,
        "daily": frontend_daily,
        "bins": frontend_bins,
        "hourly": frontend_hourly
    }


def generate_html(data):
    json_data_str = json.dumps(data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Octopus Agile 2026 全景用电与月度账单分析看板</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background-color: #0b0f19;
      color: #e2e8f0;
      margin: 0;
      padding: 0;
    }}
    .custom-scroll::-webkit-scrollbar {{
      height: 6px;
      width: 6px;
    }}
    .custom-scroll::-webkit-scrollbar-thumb {{
      background: #334155;
      border-radius: 4px;
    }}
    .tooltip-card {{
      pointer-events: none;
      transition: opacity 0.15s ease;
    }}
    .tab-btn.active {{
      background-color: #0284c7;
      color: #ffffff;
      border-color: #38bdf8;
    }}
    .month-card.active {{
      border-color: #38bdf8;
      box-shadow: 0 0 15px rgba(56, 189, 248, 0.25);
      background-color: rgba(30, 41, 59, 0.9);
    }}
  </style>
</head>
<body class="p-4 md:p-6 text-slate-200">
  <div class="max-w-7xl mx-auto space-y-6">

    <!-- 顶部概览 Header -->
    <header class="flex flex-col lg:flex-row justify-between items-start lg:items-center bg-slate-900/90 border border-slate-800 p-5 rounded-2xl shadow-xl backdrop-blur">
      <div>
        <div class="flex items-center gap-2">
          <span class="inline-block w-3 h-3 rounded-full bg-emerald-400 animate-pulse"></span>
          <h1 class="text-xl md:text-2xl font-bold text-white tracking-wide">Octopus Agile 2026 全景用电与月度账单分析看板</h1>
        </div>
        <p class="text-xs md:text-sm text-slate-400 mt-1">
          数据范围: <span id="hdr-range" class="text-slate-300 font-medium">-</span> (共 <span id="hdr-days" class="text-cyan-400 font-semibold">-</span> 天，<span id="hdr-slots" class="text-slate-300">-</span> 个半小时时段)
        </p>
      </div>
      <div class="flex flex-wrap gap-3 mt-4 lg:mt-0">
        <div class="bg-slate-800/80 px-3.5 py-2 rounded-xl border border-slate-700/60 text-right">
          <div class="text-xs text-slate-400">总用电量</div>
          <div class="text-base md:text-lg font-bold text-emerald-400" id="stat-kwh">- kWh</div>
        </div>
        <div class="bg-slate-800/80 px-3.5 py-2 rounded-xl border border-slate-700/60 text-right">
          <div class="text-xs text-slate-400">纯电费支出</div>
          <div class="text-base md:text-lg font-bold text-amber-400" id="stat-cost">£-</div>
        </div>
        <div class="bg-slate-800/80 px-3.5 py-2 rounded-xl border border-slate-700/60 text-right">
          <div class="text-xs text-slate-400">全包总账单 (含固定费)</div>
          <div class="text-base md:text-lg font-bold text-rose-400" id="stat-allin">£-</div>
        </div>
        <div class="bg-slate-800/80 px-3.5 py-2 rounded-xl border border-slate-700/60 text-right">
          <div class="text-xs text-slate-400">纯用电均价 (VWAP)</div>
          <div class="text-base md:text-lg font-bold text-cyan-400" id="stat-vwap">- p/kWh</div>
        </div>
      </div>
    </header>

    <!-- 【核心新增】月度使用总览与历史对比视图 (Monthly Overview & History) -->
    <section class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-lg space-y-4">
      <div class="flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
        <div>
          <div class="flex items-center gap-2">
            <span class="text-xl">📅</span>
            <h2 class="text-lg md:text-xl font-bold text-white tracking-wide">2026 月度使用与电费账单总览 (Monthly Overview)</h2>
            <span class="px-2 py-0.5 text-[11px] bg-cyan-900/60 text-cyan-300 rounded border border-cyan-700/50">每月一个总和</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">
            横向对比 2026 年各月的累计用电量、纯电费、全包总额（含每天39.5p固定费）与避峰表现，点击月份可立即下钻查看
          </p>
        </div>

        <!-- 视图切换 Tabs -->
        <div class="flex items-center gap-1.5 bg-slate-950/70 p-1 rounded-xl border border-slate-800 text-xs">
          <button id="tab-monthly-chart" class="tab-btn active px-3 py-1.5 rounded-lg border border-transparent font-medium transition">📊 月度走势图</button>
          <button id="tab-monthly-cards" class="tab-btn px-3 py-1.5 rounded-lg border border-transparent text-slate-400 hover:text-white font-medium transition">🗂️ 月度总和卡片</button>
          <button id="tab-monthly-table" class="tab-btn px-3 py-1.5 rounded-lg border border-transparent text-slate-400 hover:text-white font-medium transition">📑 对比明细表</button>
        </div>
      </div>

      <!-- 子视图 1: 月度柱状走势图 (Monthly Canvas Chart) -->
      <div id="view-monthly-chart" class="space-y-2">
        <div class="relative w-full h-80 bg-slate-950/60 rounded-xl border border-slate-800/80 p-2">
          <canvas id="monthly-canvas" class="w-full h-full block cursor-pointer"></canvas>
          <div id="monthly-tooltip" class="tooltip-card absolute hidden bg-slate-900/95 border border-cyan-500/60 p-3 rounded-xl text-xs shadow-2xl z-20 pointer-events-none"></div>
        </div>
        <div class="flex flex-wrap items-center justify-between text-[11px] text-slate-400 px-1 gap-2">
          <div class="flex items-center gap-4">
            <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-emerald-500/80 inline-block"></span> 柱形底段: 平谷用电量 (kWh)</span>
            <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-rose-500/80 inline-block"></span> 柱形顶段: 16-19点晚高峰用电量</span>
            <span class="flex items-center gap-1.5"><span class="w-4 h-0.5 bg-amber-400 inline-block"></span> 黄色折线: 纯用电均价 (p/kWh)</span>
            <span class="flex items-center gap-1.5"><span class="w-4 h-0.5 bg-cyan-400 inline-block"></span> 青色虚线: 全包综合单价 (含固定费)</span>
          </div>
          <div>💡 提示: 鼠标悬浮查看详细收支，点击任意月份柱形可直接下钻</div>
        </div>
      </div>

      <!-- 子视图 2: 月度总和卡片网格 (Monthly Cards Grid) -->
      <div id="view-monthly-cards" class="hidden">
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3.5" id="monthly-cards-container">
          <!-- 动态渲染月度卡片 -->
        </div>
      </div>

      <!-- 子视图 3: 全量对比明细表 (Monthly Detailed Table) -->
      <div id="view-monthly-table" class="hidden overflow-x-auto custom-scroll">
        <table class="w-full text-left text-xs text-slate-300 border-collapse">
          <thead>
            <tr class="bg-slate-800/90 text-slate-400 border-b border-slate-700">
              <th class="py-2.5 px-3 font-semibold">月份</th>
              <th class="py-2.5 px-2 font-semibold">天数</th>
              <th class="py-2.5 px-3 font-semibold text-right">总用电量</th>
              <th class="py-2.5 px-2 font-semibold text-right">日均用电</th>
              <th class="py-2.5 px-3 font-semibold text-right">纯电费支出</th>
              <th class="py-2.5 px-2 font-semibold text-right">固定底费</th>
              <th class="py-2.5 px-3 font-semibold text-right text-rose-300">全包总电费</th>
              <th class="py-2.5 px-3 font-semibold text-right text-amber-300">纯电均价</th>
              <th class="py-2.5 px-3 font-semibold text-right text-cyan-300">全包综合单价</th>
              <th class="py-2.5 px-2 font-semibold text-right">晚高峰占比</th>
              <th class="py-2.5 px-2 font-semibold text-right">负电价度数</th>
              <th class="py-2.5 px-3 font-semibold text-right text-emerald-400">较SVT节省</th>
              <th class="py-2.5 px-2 font-semibold text-center">操作</th>
            </tr>
          </thead>
          <tbody id="monthly-table-body" class="divide-y divide-slate-800">
            <!-- 动态渲染月度数据行 -->
          </tbody>
        </table>
      </div>
    </section>

    <!-- 核心视图 1: 各电价区间用电量累计分布 (Price Tier Breakdown) -->
    <section class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-lg">
      <div class="flex flex-col md:flex-row justify-between items-start md:items-center mb-4 gap-2">
        <div>
          <h2 class="text-lg font-semibold text-white flex items-center gap-2">
            <span>📊</span> 各电价区间用电量累计分布 (Price Tier Breakdown)
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">直观回答：“在多少便士的电价下，你用了多少度电？”</p>
        </div>
        <div class="text-xs text-slate-400 bg-slate-800/80 px-3 py-1.5 rounded-lg border border-slate-700/50">
          绿色/蓝色 = 谷电及优惠 | 黄色 = 正常 | 红色 = 昂贵高峰
        </div>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-7 gap-3" id="bins-container">
        <!-- 动态生成 7 个梯度的卡片 -->
      </div>
    </section>

    <!-- 核心视图 2: 单日 48 时段精细下钻 (Daily 48-Slot Drilldown) -->
    <section id="drilldown-section" class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-lg space-y-4">
      <div class="flex flex-col lg:flex-row justify-between items-start lg:items-center gap-4">
        <div>
          <h2 class="text-lg font-semibold text-white flex items-center gap-2">
            <span>🔍</span> 单日 48 个半小时用电与电价双轴对齐图
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">
            观察任一天的用电波峰（柱状）是否巧妙避开了电价高峰（折线）
          </p>
        </div>

        <!-- 日期控制器 -->
        <div class="flex flex-wrap items-center gap-2">
          <button id="btn-prev-day" class="px-2.5 py-1.5 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 transition">◀ 前一天</button>
          <input type="date" id="date-picker" class="bg-slate-800 text-white text-xs px-3 py-1.5 rounded-lg border border-slate-700 focus:outline-none focus:border-cyan-500">
          <button id="btn-next-day" class="px-2.5 py-1.5 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 transition">后一天 ▶</button>
          <div class="h-4 w-[1px] bg-slate-700 mx-1 hidden sm:block"></div>
          <button class="quick-date-btn text-xs px-2.5 py-1.5 bg-slate-800 hover:bg-cyan-900/40 text-cyan-300 border border-slate-700 rounded-lg transition" id="btn-latest-day">最新一天</button>
          <button class="quick-date-btn text-xs px-2.5 py-1.5 bg-slate-800 hover:bg-blue-900/40 text-blue-300 border border-slate-700 rounded-lg transition" data-date="2026-05-18">典型负电价日</button>
          <button class="quick-date-btn text-xs px-2.5 py-1.5 bg-slate-800 hover:bg-rose-900/40 text-rose-300 border border-slate-700 rounded-lg transition" data-date="2026-11-04">近期高电价日</button>
        </div>
      </div>

      <!-- 当日小计面板 -->
      <div id="day-kpi-bar" class="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-slate-800/40 p-3 rounded-xl border border-slate-800 text-xs">
        <div>当天总用电: <span id="day-kwh" class="font-bold text-emerald-400">-</span></div>
        <div>当天纯电费: <span id="day-cost" class="font-bold text-amber-400">-</span></div>
        <div>当天纯均价: <span id="day-vwap" class="font-bold text-cyan-400">-</span></div>
        <div>市场基准均价: <span id="day-mkt" class="font-bold text-slate-300">-</span></div>
      </div>

      <!-- 48 时段 Canvas 图表 -->
      <div class="relative w-full h-80 bg-slate-950/60 rounded-xl border border-slate-800/80 p-2">
        <canvas id="drilldown-canvas" class="w-full h-full block cursor-crosshair"></canvas>
        <div id="drilldown-tooltip" class="tooltip-card absolute hidden bg-slate-900/95 border border-cyan-500/50 p-2.5 rounded-lg text-xs shadow-2xl z-20 pointer-events-none"></div>
      </div>
      <div class="flex items-center justify-between text-[11px] text-slate-400 px-1">
        <div class="flex items-center gap-4">
          <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-cyan-500/70 inline-block"></span> 柱状图: 半小时用电量 (kWh)</span>
          <span class="flex items-center gap-1.5"><span class="w-4 h-0.5 bg-amber-400 inline-block"></span> 折线图: Agile 实时单价 (p/kWh)</span>
          <span class="flex items-center gap-1.5"><span class="w-3 h-3 rounded bg-rose-500/20 border border-rose-500/40 inline-block"></span> 红色阴影: 16:00-19:00 高峰禁区</span>
        </div>
        <div>提示: 鼠标悬浮在柱形上方可查看半小时精确读数</div>
      </div>
    </section>

    <!-- 核心视图 3: 每天不同电价区间的堆叠柱状图 (Daily Stacked Bar Chart) -->
    <section class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-lg space-y-4">
      <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3">
        <div>
          <h2 class="text-lg font-semibold text-white flex items-center gap-2">
            <span>📅</span> 2026 全年每日用电堆叠分布 (Daily Stacked Tiers)
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">
            每天一根柱子，拆解展示当天在各电价区间（负电、谷电、平电、峰电）所消耗的电量
          </p>
        </div>
        <!-- 月份过滤 -->
        <div class="flex items-center gap-1.5 flex-wrap" id="month-filters">
          <!-- 动态注入月份过滤按钮 -->
        </div>
      </div>

      <div class="relative w-full h-72 bg-slate-950/60 rounded-xl border border-slate-800/80 p-2 overflow-x-auto custom-scroll">
        <canvas id="stacked-canvas" class="w-full h-full block cursor-pointer"></canvas>
        <div id="stacked-tooltip" class="tooltip-card absolute hidden bg-slate-900/95 border border-slate-700 p-2.5 rounded-lg text-xs shadow-2xl z-20 pointer-events-none"></div>
      </div>

      <div class="flex flex-wrap items-center justify-between text-[11px] text-slate-400 gap-2">
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded bg-blue-500 inline-block"></span> 负电价 (&lt;0p)</span>
          <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded bg-emerald-400 inline-block"></span> 低谷优惠 (0~15p)</span>
          <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded bg-amber-400 inline-block"></span> 平段电价 (15~25p)</span>
          <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded bg-rose-500 inline-block"></span> 昂贵高峰 (&ge;25p)</span>
        </div>
        <div>点击任意一根柱子，即可在上方加载该天的 48 时段精细图</div>
      </div>
    </section>

    <!-- 核心视图 4: 24 小时综合负荷与电价曲线 -->
    <section class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-lg">
      <div class="mb-4">
        <h2 class="text-lg font-semibold text-white flex items-center gap-2">
          <span>🕒</span> 全天 24 小时平均负荷作息 vs 典型电价走势
        </h2>
        <p class="text-xs text-slate-400 mt-0.5">
          反映你在全天各小时的生活作息负荷与电价峰谷的整体宏观重叠度
        </p>
      </div>
      <div class="relative w-full h-64 bg-slate-950/60 rounded-xl border border-slate-800/80 p-2">
        <canvas id="diurnal-canvas" class="w-full h-full block"></canvas>
      </div>
    </section>

  </div>

  <script>
    const DATA = {json_data_str};

    // 1. 初始化顶部指标
    document.getElementById('hdr-range').innerText = DATA.summary.first_day + ' 至 ' + DATA.summary.last_day;
    document.getElementById('hdr-days').innerText = DATA.summary.days_count;
    document.getElementById('hdr-slots').innerText = DATA.summary.slots_count.toLocaleString();
    document.getElementById('stat-kwh').innerText = DATA.summary.total_kwh.toLocaleString() + ' kWh';
    document.getElementById('stat-cost').innerText = '£' + DATA.summary.total_cost.toLocaleString();
    document.getElementById('stat-allin').innerText = '£' + DATA.summary.total_allin.toLocaleString();
    document.getElementById('stat-vwap').innerText = DATA.summary.vwap.toFixed(2) + ' p/kWh';

    // 2. 渲染电价阶梯卡片
    const binsContainer = document.getElementById('bins-container');
    DATA.bins.forEach(b => {{
      const card = document.createElement('div');
      card.className = "bg-slate-800/60 border border-slate-700/60 rounded-xl p-3 flex flex-col justify-between hover:border-slate-500 transition";
      card.innerHTML = `
        <div>
          <div class="flex items-center gap-1.5 mb-1.5">
            <span class="w-2.5 h-2.5 rounded-full" style="background-color: ${{b.color}}"></span>
            <span class="text-xs font-semibold text-slate-200">${{b.label}}</span>
          </div>
          <div class="text-lg font-bold text-white">${{b.kwh.toLocaleString()}} <span class="text-xs font-normal text-slate-400">kWh</span></div>
          <div class="text-xs text-slate-400 mt-0.5">占总电量: <span class="font-semibold text-slate-200">${{b.pct}}%</span></div>
        </div>
        <div class="pt-2 mt-2 border-t border-slate-700/50 text-[11px] text-slate-400 flex justify-between">
          <span>${{b.slots.toLocaleString()}} 时段</span>
          <span class="text-amber-300 font-medium">£${{b.cost}}</span>
        </div>
      `;
      binsContainer.appendChild(card);
    }});

    // ==========================================
    // 3. 【全新】月度使用视图交互逻辑
    // ==========================================
    let activeSelectedMonth = 'all';

    // 视图切换 Tab 逻辑
    const btnTabChart = document.getElementById('tab-monthly-chart');
    const btnTabCards = document.getElementById('tab-monthly-cards');
    const btnTabTable = document.getElementById('tab-monthly-table');
    const viewMonthlyChart = document.getElementById('view-monthly-chart');
    const viewMonthlyCards = document.getElementById('view-monthly-cards');
    const viewMonthlyTable = document.getElementById('view-monthly-table');

    function switchMonthlyTab(tab) {{
      [btnTabChart, btnTabCards, btnTabTable].forEach(b => b.classList.remove('active', 'text-white'));
      [viewMonthlyChart, viewMonthlyCards, viewMonthlyTable].forEach(v => v.classList.add('hidden'));

      if (tab === 'chart') {{
        btnTabChart.classList.add('active', 'text-white');
        viewMonthlyChart.classList.remove('hidden');
        renderMonthlyChart();
      }} else if (tab === 'cards') {{
        btnTabCards.classList.add('active', 'text-white');
        viewMonthlyCards.classList.remove('hidden');
      }} else if (tab === 'table') {{
        btnTabTable.classList.add('active', 'text-white');
        viewMonthlyTable.classList.remove('hidden');
      }}
    }}

    btnTabChart.addEventListener('click', () => switchMonthlyTab('chart'));
    btnTabCards.addEventListener('click', () => switchMonthlyTab('cards'));
    btnTabTable.addEventListener('click', () => switchMonthlyTab('table'));

    // 渲染月度卡片网格
    const cardsContainer = document.getElementById('monthly-cards-container');
    DATA.monthly.forEach(m => {{
      const card = document.createElement('div');
      card.id = `m-card-${{m.month}}`;
      card.className = "month-card bg-slate-800/60 border border-slate-700/70 hover:border-cyan-500/80 rounded-xl p-4 cursor-pointer transition flex flex-col justify-between";
      
      let badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] bg-slate-700 text-slate-300">正常</span>';
      if (m.peak_pct <= 14.5) {{
        badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] bg-emerald-900/60 text-emerald-300 border border-emerald-700/50">⚡ 避峰极佳</span>';
      }} else if (m.vwap >= 35.0) {{
        badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] bg-rose-900/60 text-rose-300 border border-rose-700/50">❄️ 冬季高价</span>';
      }} else if (m.vwap <= 18.0) {{
        badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] bg-blue-900/60 text-blue-300 border border-blue-700/50">🌊 谷电超便宜</span>';
      }}

      card.innerHTML = `
        <div>
          <div class="flex items-center justify-between mb-2">
            <span class="text-sm font-bold text-white tracking-wide">${{m.month}}</span>
            ${{badgeHtml}}
          </div>
          <div class="text-2xl font-black text-emerald-400">${{m.kwh.toLocaleString()}} <span class="text-xs font-normal text-slate-400">kWh</span></div>
          <div class="text-[11px] text-slate-400 mt-0.5">日均 ${{m.daily_avg}} 度 | 共 ${{m.days}} 天</div>
          
          <div class="grid grid-cols-2 gap-2 mt-3 pt-3 border-t border-slate-700/50 text-xs">
            <div>
              <div class="text-[11px] text-slate-400">纯电费支出</div>
              <div class="font-bold text-amber-300">£${{m.cost}}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-400">全包总额 (含底费)</div>
              <div class="font-bold text-rose-300">£${{m.all_in}}</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-400">纯均价 (VWAP)</div>
              <div class="font-bold text-cyan-400">${{m.vwap}} p/度</div>
            </div>
            <div>
              <div class="text-[11px] text-slate-400">全包综合单价</div>
              <div class="font-bold text-slate-200">${{m.all_in_vwap}} p/度</div>
            </div>
          </div>
        </div>

        <div class="mt-3 pt-2.5 border-t border-slate-700/40 flex items-center justify-between text-[11px]">
          <span class="text-slate-400">晚高峰用电: <b class="text-slate-200">${{m.peak_pct}}%</b></span>
          <span class="text-emerald-400 font-medium">省 £${{m.savings}} (${{m.savings_pct}}%)</span>
        </div>
      `;
      card.addEventListener('click', () => {{
        selectMonthFilter(m.month);
      }});
      cardsContainer.appendChild(card);
    }});

    // 渲染月度对比明细表
    const tableBody = document.getElementById('monthly-table-body');
    let totalKwh = 0, totalCost = 0, totalSc = 0, totalAllIn = 0, totalDays = 0, totalSvt = 0;
    DATA.monthly.forEach(m => {{
      totalKwh += m.kwh;
      totalCost += m.cost;
      totalSc += m.sc;
      totalAllIn += m.all_in;
      totalDays += m.days;
      totalSvt += m.svt_cost;

      const row = document.createElement('tr');
      row.className = "hover:bg-slate-800/60 transition cursor-pointer";
      row.innerHTML = `
        <td class="py-2.5 px-3 font-semibold text-white">${{m.month}}</td>
        <td class="py-2.5 px-2 text-slate-400">${{m.days}}天</td>
        <td class="py-2.5 px-3 text-right font-medium text-emerald-400">${{m.kwh.toLocaleString()}}</td>
        <td class="py-2.5 px-2 text-right text-slate-400">${{m.daily_avg}}</td>
        <td class="py-2.5 px-3 text-right font-medium text-amber-300">£${{m.cost.toFixed(2)}}</td>
        <td class="py-2.5 px-2 text-right text-slate-400">£${{m.sc.toFixed(2)}}</td>
        <td class="py-2.5 px-3 text-right font-bold text-rose-300">£${{m.all_in.toFixed(2)}}</td>
        <td class="py-2.5 px-3 text-right font-semibold text-cyan-400">${{m.vwap.toFixed(2)}}p</td>
        <td class="py-2.5 px-3 text-right text-slate-200">${{m.all_in_vwap.toFixed(2)}}p</td>
        <td class="py-2.5 px-2 text-right">${{m.peak_pct}}%</td>
        <td class="py-2.5 px-2 text-right text-blue-400">${{m.neg_kwh > 0 ? m.neg_kwh + ' kWh' : '-'}}</td>
        <td class="py-2.5 px-3 text-right font-medium text-emerald-400">£${{m.savings.toFixed(2)}} (${{m.savings_pct}}%)</td>
        <td class="py-2.5 px-2 text-center">
          <button class="px-2 py-0.5 bg-slate-800 hover:bg-cyan-600 text-slate-300 hover:text-white rounded text-[10px] transition" onclick="selectMonthFilter('${{m.month}}')">下钻</button>
        </td>
      `;
      tableBody.appendChild(row);
    }});

    // 添加总计行
    const grandVwap = (totalCost * 100 / totalKwh).toFixed(2);
    const grandAllInVwap = (totalAllIn * 100 / totalKwh).toFixed(2);
    const grandSavings = (totalSvt - totalAllIn).toFixed(2);
    const grandSavingsPct = ((totalSvt - totalAllIn) / totalSvt * 100).toFixed(1);
    const totalRow = document.createElement('tr');
    totalRow.className = "bg-slate-800/90 font-bold border-t-2 border-slate-600 text-white";
    totalRow.innerHTML = `
      <td class="py-3 px-3">2026 全年总计</td>
      <td class="py-3 px-2 text-slate-300">${{totalDays}}天</td>
      <td class="py-3 px-3 text-right text-emerald-300">${{totalKwh.toLocaleString(undefined, {{minimumFractionDigits: 1, maximumFractionDigits: 1}})}}</td>
      <td class="py-3 px-2 text-right text-slate-300">${{(totalKwh / totalDays).toFixed(2)}}</td>
      <td class="py-3 px-3 text-right text-amber-300">£${{totalCost.toFixed(2)}}</td>
      <td class="py-3 px-2 text-right text-slate-300">£${{totalSc.toFixed(2)}}</td>
      <td class="py-3 px-3 text-right text-rose-300">£${{totalAllIn.toFixed(2)}}</td>
      <td class="py-3 px-3 text-right text-cyan-300">${{grandVwap}}p</td>
      <td class="py-3 px-3 text-right text-white">${{grandAllInVwap}}p</td>
      <td class="py-3 px-2 text-right text-slate-300">-</td>
      <td class="py-3 px-2 text-right text-slate-300">-</td>
      <td class="py-3 px-3 text-right text-emerald-300">£${{grandSavings}} (${{grandSavingsPct}}%)</td>
      <td class="py-3 px-2 text-center text-slate-400">汇总</td>
    `;
    tableBody.appendChild(totalRow);

    // 绘制月度复合走势图 (Monthly Canvas)
    function renderMonthlyChart() {{
      const canvas = document.getElementById('monthly-canvas');
      if (!canvas || canvas.offsetParent === null) return;
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.scale(dpr, dpr);

      const W = rect.width;
      const H = rect.height;
      const padL = 45, padR = 45, padT = 30, padB = 30;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      ctx.clearRect(0, 0, W, H);

      const mList = DATA.monthly;
      const n = mList.length;
      if (n === 0) return;

      const maxKwh = Math.max(300, ...mList.map(m => m.kwh)) * 1.15;
      const maxRate = Math.max(45, ...mList.map(m => Math.max(m.vwap, m.all_in_vwap))) * 1.15;

      // 绘制背景参考网格
      ctx.strokeStyle = 'rgba(51, 65, 85, 0.4)';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {{
        const y = padT + (i / 4) * plotH;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(padL + plotW, y);
        ctx.stroke();

        // 左Y轴文字 (kWh)
        const kVal = Math.round(maxKwh * (1 - i / 4));
        ctx.fillStyle = '#64748b';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText(kVal, padL - 6, y + 3);

        // 右Y轴文字 (p/kWh)
        const rVal = Math.round(maxRate * (1 - i / 4));
        ctx.fillStyle = '#f59e0b';
        ctx.textAlign = 'left';
        ctx.fillText(rVal + 'p', padL + plotW + 6, y + 3);
      }}

      // 绘制柱状图 (堆叠：底段为平时电量，顶段为晚高峰电量)
      const slotW = plotW / n;
      const barW = Math.min(46, slotW * 0.6);

      mList.forEach((m, i) => {{
        const cx = padL + i * slotW + slotW / 2;
        const x = cx - barW / 2;

        const offPeakH = (m.offpeak_kwh / maxKwh) * plotH;
        const peakH = (m.peak_kwh / maxKwh) * plotH;
        const totalH = offPeakH + peakH;
        const yTop = padT + plotH - totalH;

        // 高亮当前选中月
        if (activeSelectedMonth === m.month) {{
          ctx.fillStyle = 'rgba(56, 189, 248, 0.15)';
          ctx.fillRect(cx - slotW / 2 + 2, padT, slotW - 4, plotH);
        }}

        // 平谷电量柱 (翡翠绿)
        ctx.fillStyle = (activeSelectedMonth === m.month || activeSelectedMonth === 'all') ? '#10b981' : '#065f46';
        ctx.fillRect(x, padT + plotH - offPeakH, barW, offPeakH);

        // 晚高峰电量柱 (亮玫瑰红)
        ctx.fillStyle = (activeSelectedMonth === m.month || activeSelectedMonth === 'all') ? '#f43f5e' : '#881337';
        ctx.fillRect(x, yTop, barW, peakH);

        // 柱顶数字 (总用电量)
        ctx.fillStyle = '#f8fafc';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(Math.round(m.kwh), cx, yTop - 6);

        // X轴月份标签
        ctx.fillStyle = (activeSelectedMonth === m.month) ? '#38bdf8' : '#94a3b8';
        ctx.font = (activeSelectedMonth === m.month) ? 'bold 11px sans-serif' : '10px sans-serif';
        ctx.fillText(m.month.slice(5) + '月', cx, padT + plotH + 18);
      }});

      // 绘制均价折线 (纯均价 VWAP - 黄色)
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      mList.forEach((m, i) => {{
        const cx = padL + i * slotW + slotW / 2;
        const cy = padT + plotH - (m.vwap / maxRate) * plotH;
        if (i === 0) ctx.moveTo(cx, cy);
        else ctx.lineTo(cx, cy);
      }});
      ctx.stroke();

      // 绘制全包均价折线 (青色虚线)
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 1.8;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      mList.forEach((m, i) => {{
        const cx = padL + i * slotW + slotW / 2;
        const cy = padT + plotH - (m.all_in_vwap / maxRate) * plotH;
        if (i === 0) ctx.moveTo(cx, cy);
        else ctx.lineTo(cx, cy);
      }});
      ctx.stroke();
      ctx.setLineDash([]);

      // 绘制数据折线圆点
      mList.forEach((m, i) => {{
        const cx = padL + i * slotW + slotW / 2;
        const cy = padT + plotH - (m.vwap / maxRate) * plotH;
        ctx.fillStyle = '#facc15';
        ctx.beginPath();
        ctx.arc(cx, cy, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#0b0f19';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }});
    }}

    // 月度走势图 Tooltip 与点击事件
    const monthlyCanvas = document.getElementById('monthly-canvas');
    const monthlyTooltip = document.getElementById('monthly-tooltip');

    monthlyCanvas.addEventListener('mousemove', e => {{
      const rect = monthlyCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const padL = 45, padR = 45;
      const plotW = rect.width - padL - padR;
      const n = DATA.monthly.length;
      if (x < padL || x > rect.width - padR || n === 0) {{
        monthlyTooltip.classList.add('hidden');
        return;
      }}
      const idx = Math.floor(((x - padL) / plotW) * n);
      if (idx >= 0 && idx < n) {{
        const m = DATA.monthly[idx];
        monthlyTooltip.classList.remove('hidden');
        monthlyTooltip.style.left = (e.clientX - rect.left + 15) + 'px';
        monthlyTooltip.style.top = Math.max(10, e.clientY - rect.top - 120) + 'px';
        monthlyTooltip.innerHTML = `
          <div class="font-bold text-white text-sm mb-1.5 flex items-center justify-between gap-4">
            <span>📅 ${{m.month}} 月度账单总览</span>
            <span class="text-cyan-400 font-medium">${{m.days}} 天数据</span>
          </div>
          <div class="space-y-1 text-slate-300">
            <div class="flex justify-between gap-3"><span>总用电量:</span><b class="text-emerald-400">${{m.kwh}} kWh (日均 ${{m.daily_avg}}度)</b></div>
            <div class="flex justify-between gap-3"><span>纯用电费:</span><b class="text-amber-300">£${{m.cost}}</b></div>
            <div class="flex justify-between gap-3"><span>固定费(SC):</span><b class="text-slate-300">£${{m.sc}} (39.5p/天)</b></div>
            <div class="flex justify-between gap-3 border-t border-slate-700/60 pt-1"><span>全包总支出:</span><b class="text-rose-400">£${{m.all_in}}</b></div>
            <div class="flex justify-between gap-3"><span>纯用电均价:</span><b class="text-cyan-300">${{m.vwap}} p/kWh</b></div>
            <div class="flex justify-between gap-3"><span>全包折合均价:</span><b class="text-slate-200">${{m.all_in_vwap}} p/kWh</b></div>
            <div class="flex justify-between gap-3"><span>晚高峰用电:</span><b class="text-rose-300">${{m.peak_pct}}% (${{m.peak_kwh}} kWh)</b></div>
            <div class="flex justify-between gap-3 text-emerald-400 border-t border-slate-700/60 pt-1"><span>较标准电价省:</span><b>£${{m.savings}} (${{m.savings_pct}}%)</b></div>
          </div>
          <div class="mt-2 pt-1.5 border-t border-slate-700/50 text-[10px] text-cyan-400 text-center">点击即可下钻查看该月每天用电分布 ➔</div>
        `;
      }}
    }});

    monthlyCanvas.addEventListener('mouseleave', () => {{
      monthlyTooltip.classList.add('hidden');
    }});

    monthlyCanvas.addEventListener('click', e => {{
      const rect = monthlyCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const padL = 45, padR = 45;
      const plotW = rect.width - padL - padR;
      const n = DATA.monthly.length;
      if (x < padL || x > rect.width - padR || n === 0) return;
      const idx = Math.floor(((x - padL) / plotW) * n);
      if (idx >= 0 && idx < n) {{
        selectMonthFilter(DATA.monthly[idx].month);
      }}
    }});

    // ==========================================
    // 4. 单日 48 时段精细图交互
    // ==========================================
    let currentSelectedDate = DATA.daily[DATA.daily.length - 1].date; // 默认最新一天
    const datePicker = document.getElementById('date-picker');
    datePicker.min = DATA.daily[0].date;
    datePicker.max = DATA.daily[DATA.daily.length - 1].date;
    datePicker.value = currentSelectedDate;

    function renderDrilldown(dateStr) {{
      currentSelectedDate = dateStr;
      datePicker.value = dateStr;
      const dayData = DATA.daily.find(d => d.date === dateStr);
      if (!dayData) return;

      // 更新单日 KPI
      document.getElementById('day-kwh').innerText = dayData.kwh.toFixed(2) + ' kWh';
      document.getElementById('day-cost').innerText = '£' + dayData.cost.toFixed(2);
      document.getElementById('day-vwap').innerText = dayData.vwap.toFixed(2) + ' p/kWh';
      document.getElementById('day-mkt').innerText = dayData.mkt.toFixed(2) + ' p/kWh';

      // 绘制 Canvas
      const canvas = document.getElementById('drilldown-canvas');
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.scale(dpr, dpr);

      const W = rect.width;
      const H = rect.height;
      const padL = 45, padR = 45, padT = 25, padB = 30;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      ctx.clearRect(0, 0, W, H);

      const slots = dayData.slots; // 48 slots
      const maxKwh = Math.max(0.5, ...slots.map(s => s.kwh)) * 1.15;
      const maxRate = Math.max(40, ...slots.map(s => s.r)) * 1.1;
      const minRate = Math.min(0, ...slots.map(s => s.r));

      // 绘制 16:00 - 19:00 高峰期背景阴影
      const peakX1 = padL + (32 / 48) * plotW;
      const peakX2 = padL + (38 / 48) * plotW;
      ctx.fillStyle = 'rgba(239, 68, 68, 0.12)';
      ctx.fillRect(peakX1, padT, peakX2 - peakX1, plotH);
      ctx.fillStyle = 'rgba(239, 68, 68, 0.7)';
      ctx.font = '10px sans-serif';
      ctx.fillText('晚高峰 (16:00 - 19:00)', peakX1 + 4, padT + 12);

      // 绘制 0 水平线
      if (minRate < 0) {{
        const zeroY = padT + plotH - ((0 - minRate) / (maxRate - minRate)) * plotH;
        ctx.strokeStyle = 'rgba(59, 130, 246, 0.4)';
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(padL, zeroY);
        ctx.lineTo(padL + plotW, zeroY);
        ctx.stroke();
        ctx.setLineDash([]);
      }}

      // 绘制柱状图 (用电量 kWh)
      const barW = (plotW / 48) * 0.7;
      slots.forEach((s, i) => {{
        const x = padL + (i / 48) * plotW + (plotW / 48 - barW) / 2;
        const bH = (s.kwh / maxKwh) * plotH;
        const y = padT + plotH - bH;

        if (s.r < 0) ctx.fillStyle = '#3b82f6';
        else if (s.r < 15) ctx.fillStyle = '#10b981';
        else if (s.r < 25) ctx.fillStyle = '#f59e0b';
        else ctx.fillStyle = '#ef4444';

        ctx.fillRect(x, y, barW, bH);
      }});

      // 绘制折线图 (电价 p/kWh)
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2;
      ctx.beginPath();
      slots.forEach((s, i) => {{
        const x = padL + (i / 48) * plotW + (plotW / 48) / 2;
        const y = padT + plotH - ((s.r - minRate) / (maxRate - minRate)) * plotH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      ctx.stroke();

      // 绘制坐标轴文字
      ctx.fillStyle = '#94a3b8';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      for (let h = 0; h <= 24; h += 4) {{
        const x = padL + (h / 24) * plotW;
        ctx.fillText(`${{h.toString().padStart(2, '0')}}:00`, x, padT + plotH + 18);
      }}

      // 左轴刻度 (kWh)
      ctx.textAlign = 'right';
      ctx.fillText(maxKwh.toFixed(2) + 'k', padL - 6, padT + 10);
      ctx.fillText('0', padL - 6, padT + plotH);

      // 右轴刻度 (p)
      ctx.textAlign = 'left';
      ctx.fillText(maxRate.toFixed(0) + 'p', padL + plotW + 6, padT + 10);
      ctx.fillText(minRate.toFixed(0) + 'p', padL + plotW + 6, padT + plotH);
    }}

    // 日期选择事件
    datePicker.addEventListener('change', e => renderDrilldown(e.target.value));
    document.getElementById('btn-prev-day').addEventListener('click', () => {{
      const idx = DATA.daily.findIndex(d => d.date === currentSelectedDate);
      if (idx > 0) renderDrilldown(DATA.daily[idx - 1].date);
    }});
    document.getElementById('btn-next-day').addEventListener('click', () => {{
      const idx = DATA.daily.findIndex(d => d.date === currentSelectedDate);
      if (idx < DATA.daily.length - 1) renderDrilldown(DATA.daily[idx + 1].date);
    }});
    document.getElementById('btn-latest-day').addEventListener('click', () => {{
      renderDrilldown(DATA.daily[DATA.daily.length - 1].date);
    }});
    document.querySelectorAll('.quick-date-btn').forEach(btn => {{
      if (btn.dataset.date) {{
        btn.addEventListener('click', () => renderDrilldown(btn.dataset.date));
      }}
    }});

    // 48 时段 Tooltip
    const drillCanvas = document.getElementById('drilldown-canvas');
    const drillTooltip = document.getElementById('drilldown-tooltip');
    drillCanvas.addEventListener('mousemove', e => {{
      const rect = drillCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const padL = 45, padR = 45;
      const plotW = rect.width - padL - padR;
      if (x < padL || x > rect.width - padR) {{
        drillTooltip.classList.add('hidden');
        return;
      }}
      const slotIdx = Math.floor(((x - padL) / plotW) * 48);
      const dayData = DATA.daily.find(d => d.date === currentSelectedDate);
      if (dayData && slotIdx >= 0 && slotIdx < 48) {{
        const s = dayData.slots[slotIdx];
        drillTooltip.classList.remove('hidden');
        drillTooltip.style.left = (e.clientX - rect.left + 15) + 'px';
        drillTooltip.style.top = (e.clientY - rect.top - 50) + 'px';
        drillTooltip.innerHTML = `
          <div class="font-bold text-white text-xs mb-1 flex items-center gap-1.5">
            <span>🕒 ${{s.t}}</span>
            ${{s.pk ? '<span class="text-[10px] px-1 bg-red-800 text-red-200 rounded">高峰</span>' : ''}}
          </div>
          <div>用电量: <b class="text-cyan-400">${{s.kwh}} kWh</b></div>
          <div>电价: <b class="text-amber-400">${{s.r}} p/kWh</b></div>
          <div>支出: <b class="text-slate-200">${{s.p}} p</b></div>
        `;
      }}
    }});
    drillCanvas.addEventListener('mouseleave', () => drillTooltip.classList.add('hidden'));

    // ==========================================
    // 5. 每日堆叠图与月份过滤逻辑 (Daily Stacked)
    // ==========================================
    let currentFilteredDaily = DATA.daily;
    const monthFiltersContainer = document.getElementById('month-filters');

    function initMonthFilters() {{
      monthFiltersContainer.innerHTML = '';
      
      // 全部按钮
      const allBtn = document.createElement('button');
      allBtn.className = `month-btn text-xs px-2.5 py-1 rounded-md transition ${{activeSelectedMonth === 'all' ? 'bg-cyan-600 text-white active font-medium' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}}`;
      allBtn.innerText = `全部 (${{DATA.summary.days_count}}天)`;
      allBtn.dataset.month = 'all';
      allBtn.addEventListener('click', () => selectMonthFilter('all'));
      monthFiltersContainer.appendChild(allBtn);

      // 各月按钮
      DATA.monthly.forEach(m => {{
        const btn = document.createElement('button');
        const isActive = activeSelectedMonth === m.month;
        btn.className = `month-btn text-xs px-2.5 py-1 rounded-md transition ${{isActive ? 'bg-cyan-600 text-white active font-medium' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}}`;
        btn.innerText = `${{m.month.slice(5)}}月 (${{m.days}}天)`;
        btn.dataset.month = m.month;
        btn.addEventListener('click', () => selectMonthFilter(m.month));
        monthFiltersContainer.appendChild(btn);
      }});
    }}

    function selectMonthFilter(month) {{
      activeSelectedMonth = month;
      
      // 更新月度过滤按钮样式
      document.querySelectorAll('.month-btn').forEach(btn => {{
        if (btn.dataset.month === month) {{
          btn.className = 'month-btn active text-xs px-2.5 py-1 bg-cyan-600 text-white rounded-md transition font-medium';
        }} else {{
          btn.className = 'month-btn text-xs px-2.5 py-1 bg-slate-800 text-slate-300 hover:bg-slate-700 rounded-md transition';
        }}
      }});

      // 更新月度卡片高亮
      document.querySelectorAll('.month-card').forEach(c => c.classList.remove('active'));
      const activeCard = document.getElementById(`m-card-${{month}}`);
      if (activeCard) activeCard.classList.add('active');

      // 过滤每日堆叠图数据
      if (month === 'all') {{
        currentFilteredDaily = DATA.daily;
      }} else {{
        currentFilteredDaily = DATA.daily.filter(d => d.date.startsWith(month));
      }}

      // 如果有数据，自动将 48 时段精细图切换到该月的第一天
      if (currentFilteredDaily.length > 0) {{
        renderDrilldown(currentFilteredDaily[0].date);
      }}

      renderStackedChart();
      renderMonthlyChart();
    }}

    function renderStackedChart() {{
      const canvas = document.getElementById('stacked-canvas');
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.scale(dpr, dpr);

      const W = rect.width;
      const H = rect.height;
      const padL = 40, padR = 20, padT = 20, padB = 25;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      ctx.clearRect(0, 0, W, H);

      const items = currentFilteredDaily;
      const n = items.length;
      if (n === 0) return;

      const maxKwh = Math.max(10, ...items.map(d => d.kwh)) * 1.1;
      const slotW = plotW / n;
      const barW = Math.max(1, slotW * 0.8);

      items.forEach((d, i) => {{
        const x = padL + i * slotW + (slotW - barW) / 2;
        let curY = padT + plotH;

        // 1. 负电
        if (d.neg > 0) {{
          const h = (d.neg / maxKwh) * plotH;
          ctx.fillStyle = '#3b82f6';
          ctx.fillRect(x, curY - h, barW, h);
          curY -= h;
        }}
        // 2. 低电 (<15p)
        if (d.low > 0) {{
          const h = (d.low / maxKwh) * plotH;
          ctx.fillStyle = '#10b981';
          ctx.fillRect(x, curY - h, barW, h);
          curY -= h;
        }}
        // 3. 平电 (15-25p)
        if (d.mid > 0) {{
          const h = (d.mid / maxKwh) * plotH;
          ctx.fillStyle = '#f59e0b';
          ctx.fillRect(x, curY - h, barW, h);
          curY -= h;
        }}
        // 4. 高电 (>=25p)
        if (d.high > 0) {{
          const h = (d.high / maxKwh) * plotH;
          ctx.fillStyle = '#ef4444';
          ctx.fillRect(x, curY - h, barW, h);
          curY -= h;
        }}
      }});

      // 坐标轴文字
      ctx.fillStyle = '#94a3b8';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(maxKwh.toFixed(0) + 'k', padL - 4, padT + 10);
      ctx.fillText('0', padL - 4, padT + plotH);

      // X 轴日期标签
      ctx.textAlign = 'center';
      const labelStep = Math.max(1, Math.floor(n / 8));
      for (let i = 0; i < n; i += labelStep) {{
        const x = padL + i * slotW + slotW / 2;
        ctx.fillText(items[i].date.slice(5), x, padT + plotH + 18);
      }}
    }}

    // 堆叠图点击事件
    const stackedCanvas = document.getElementById('stacked-canvas');
    const stackedTooltip = document.getElementById('stacked-tooltip');
    stackedCanvas.addEventListener('click', e => {{
      const rect = stackedCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const padL = 40, padR = 20;
      const plotW = rect.width - padL - padR;
      const n = currentFilteredDaily.length;
      if (x < padL || x > rect.width - padR || n === 0) return;
      const idx = Math.floor(((x - padL) / plotW) * n);
      if (idx >= 0 && idx < n) {{
        renderDrilldown(currentFilteredDaily[idx].date);
        document.getElementById('drilldown-section').scrollIntoView({{ behavior: 'smooth' }});
      }}
    }});

    // 堆叠图 Tooltip
    stackedCanvas.addEventListener('mousemove', e => {{
      const rect = stackedCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const padL = 40, padR = 20;
      const plotW = rect.width - padL - padR;
      const n = currentFilteredDaily.length;
      if (x < padL || x > rect.width - padR || n === 0) {{
        stackedTooltip.classList.add('hidden');
        return;
      }}
      const idx = Math.floor(((x - padL) / plotW) * n);
      if (idx >= 0 && idx < n) {{
        const d = currentFilteredDaily[idx];
        stackedTooltip.classList.remove('hidden');
        stackedTooltip.style.left = (e.clientX - rect.left + 15) + 'px';
        stackedTooltip.style.top = (e.clientY - rect.top - 80) + 'px';
        stackedTooltip.innerHTML = `
          <div class="font-bold text-white text-xs mb-1">📅 ${{d.date}}</div>
          <div>总用电: <b class="text-emerald-400">${{d.kwh}} kWh</b></div>
          <div>纯电费: <b class="text-amber-400">£${{d.cost}}</b> (均价 ${{d.vwap}}p)</div>
          <div class="text-[10px] text-slate-400 mt-1 border-t border-slate-700/60 pt-1">
            <span class="text-blue-400">负电: ${{d.neg}}</span> | 
            <span class="text-emerald-400">谷电: ${{d.low}}</span> | 
            <span class="text-amber-400">平电: ${{d.mid}}</span> | 
            <span class="text-rose-400">峰电: ${{d.high}}</span>
          </div>
        `;
      }}
    }});
    stackedCanvas.addEventListener('mouseleave', () => stackedTooltip.classList.add('hidden'));

    // ==========================================
    // 6. 24小时综合走势图
    // ==========================================
    function renderDiurnalChart() {{
      const canvas = document.getElementById('diurnal-canvas');
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.scale(dpr, dpr);

      const W = rect.width;
      const H = rect.height;
      const padL = 45, padR = 45, padT = 20, padB = 25;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      ctx.clearRect(0, 0, W, H);

      const hData = DATA.hourly;
      const maxKwh = Math.max(0.6, ...hData.map(d => d.kwh)) * 1.15;
      const maxRate = Math.max(45, ...hData.map(d => d.rate)) * 1.15;

      // 高峰背景
      const pX1 = padL + (16 / 24) * plotW;
      const pX2 = padL + (19 / 24) * plotW;
      ctx.fillStyle = 'rgba(239, 68, 68, 0.1)';
      ctx.fillRect(pX1, padT, pX2 - pX1, plotH);

      // 柱状图 (每小时平均用电)
      const barW = (plotW / 24) * 0.65;
      hData.forEach((d, i) => {{
        const x = padL + (i / 24) * plotW + (plotW / 24 - barW) / 2;
        const bH = (d.kwh / maxKwh) * plotH;
        const y = padT + plotH - bH;
        ctx.fillStyle = 'rgba(16, 185, 129, 0.75)';
        ctx.fillRect(x, y, barW, bH);
      }});

      // 折线图 (平均电价)
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      hData.forEach((d, i) => {{
        const x = padL + (i / 24) * plotW + (plotW / 24) / 2;
        const y = padT + plotH - (d.rate / maxRate) * plotH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      ctx.stroke();

      // X 轴时间
      ctx.fillStyle = '#94a3b8';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      for (let h = 0; h <= 24; h += 4) {{
        const x = padL + (h / 24) * plotW;
        ctx.fillText(`${{h.toString().padStart(2, '0')}}:00`, x, padT + plotH + 18);
      }}

      // 左轴 (kWh)
      ctx.textAlign = 'right';
      ctx.fillStyle = '#10b981';
      ctx.fillText(maxKwh.toFixed(2) + 'k', padL - 6, padT + 10);

      // 右轴 (p)
      ctx.textAlign = 'left';
      ctx.fillStyle = '#facc15';
      ctx.fillText(maxRate.toFixed(0) + 'p', padL + plotW + 6, padT + 10);
    }}

    // 初始化渲染
    window.addEventListener('resize', () => {{
      renderMonthlyChart();
      renderDrilldown(currentSelectedDate);
      renderStackedChart();
      renderDiurnalChart();
    }});

    initMonthFilters();
    renderMonthlyChart();
    renderDrilldown(currentSelectedDate);
    renderStackedChart();
    renderDiurnalChart();
  </script>
</body>
</html>
"""
    return html


def generate_html_content(data):
    return generate_html(data)


def send_monthly_slack_alert(summary):
    if not SLACK_WEBHOOK_URL:
        return
    
    text = (
        f"📊 *【Octopus Agile 月度全景仪表盘已更新】*\n"
        f"• 📅 *数据范围:* `{summary.get('data_range', '')}` (共 {summary.get('days_count', 0)} 天)\n"
        f"• ⚡ *累计总用电:* `{summary.get('total_kwh', 0):,.1f} kWh` | *纯电费:* `£{summary.get('total_cost', 0):,.2f}` | *全包总计:* `£{summary.get('total_allin', 0):,.2f}`\n"
        f"• 🎯 *你的加权均价:* `{summary.get('vwap', 0):.2f} p/kWh` (全包单价: `{summary.get('allin_vwap', 0):.2f} p`)\n"
        f"👉 *在线交互式全景看板:* https://tsubasafly.github.io/Agile-Octopus/"
    )
    payload = {"text": text}
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "OctopusAgileSlackBot/3.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("[+] Slack 月度更新提醒推送成功！")
    except Exception as e:
        print(f"[!] 推送 Slack 失败: {e}")


def main():
    if not (OCTOPUS_API_KEY and OCTOPUS_MPAN and OCTOPUS_METER_SERIAL):
        print("[!] 错误: 未设置 OCTOPUS_API_KEY, OCTOPUS_MPAN 或 OCTOPUS_METER_SERIAL 环境变量。")
        sys.exit(1)

    tariff_code = f"E-1R-{PRODUCT_CODE}-{REGION_CODE}"
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")

    consumption = fetch_all_consumption(OCTOPUS_MPAN, OCTOPUS_METER_SERIAL, OCTOPUS_API_KEY, START_DATE, end_date)
    rates = fetch_all_rates(PRODUCT_CODE, tariff_code, START_DATE, end_date)

    data = build_dashboard_data(consumption, rates)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html_output = os.path.join(OUTPUT_DIR, "index.html")
    html_content = generate_html_content(data)

    with open(html_output, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n[+] 仪表盘已成功构建为: {html_output}")

    # 发送月度通知
    send_monthly_slack_alert(data["summary"])


if __name__ == "__main__":
    main()
