import base64
from datetime import datetime, time, timedelta, timezone
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 配置区域 ====================
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
PRODUCT_CODE = os.getenv("OCTOPUS_PRODUCT_CODE", "AGILE-24-10-01")
REGION_CODE = os.getenv("OCTOPUS_REGION_CODE", "C")  # 英国电网区域，C 为伦敦
LOW_PRICE_THRESHOLD = 10.0  # 低价报警阈值 (p/kWh)

# 用户电表凭证（通过 GitHub Secrets / 环境变量安全读取，代码内无任何明文凭证）
OCTOPUS_API_KEY = os.getenv("OCTOPUS_API_KEY", "")
OCTOPUS_MPAN = os.getenv("OCTOPUS_MPAN", "")
OCTOPUS_METER_SERIAL = os.getenv("OCTOPUS_METER_SERIAL", "")
# =================================================


def create_quickchart_short_url(chart_config, width=740, height=290, bkg="white"):
    """通过 QuickChart POST /chart/create 生成永久短链接（彻底解决 Slack 3000 字符限制）"""
    try:
        post_body = {
            "backgroundColor": bkg,
            "width": width,
            "height": height,
            "devicePixelRatio": 2,
            "chart": chart_config,
        }
        req = urllib.request.Request(
            "https://quickchart.io/chart/create",
            data=json.dumps(post_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OctopusAgileSlackBot/3.1",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("success") and res.get("url"):
                return res["url"]
    except Exception as e:
        print(f"提示: 生成 QuickChart 短链接失败，平滑回退长链接: {e}", file=sys.stderr)

    # 兜底回退长链接
    config_str = urllib.parse.quote(json.dumps(chart_config, separators=(",", ":")))
    return f"https://quickchart.io/chart?w={width}&h={height}&devicePixelRatio=2&bkg={bkg}&c={config_str}"


def fetch_recent_usage_summary():
    """获取前一日（或最近一个完整回传日）的用电量、电费、高峰用电及 48 时段精细列表。
    若未配置 API Key 或电表，平滑返回 None，不影响明日电价预报。
    """
    if not (OCTOPUS_API_KEY and OCTOPUS_MPAN and OCTOPUS_METER_SERIAL):
        print("提示: 未配置 OCTOPUS_API_KEY / MPAN / METER_SERIAL，跳过昨日用电复盘。")
        return None

    london_tz = ZoneInfo("Europe/London")
    now = datetime.now(london_tz)

    # 优先检查昨天 (T-1)，若智能电表回传未满 40 条，则平滑回退检查前天 (T-2)
    candidate_dates = [
        now.date() - timedelta(days=1),
        now.date() - timedelta(days=2),
    ]

    for target_date in candidate_dates:
        start_dt = datetime.combine(target_date, time.min, tzinfo=london_tz)
        end_dt = datetime.combine(target_date, time.max, tzinfo=london_tz)

        params = {
            "period_from": start_dt.isoformat(),
            "period_to": end_dt.isoformat(),
            "page_size": 100,
            "order_by": "period",
        }

        # 1. 抓取用电量
        c_url = f"https://api.octopus.energy/v1/electricity-meter-points/{OCTOPUS_MPAN}/meters/{OCTOPUS_METER_SERIAL}/consumption/?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(c_url, headers={"User-Agent": "OctopusAgileSlackBot/3.1"})
        auth_header = "Basic " + base64.b64encode(f"{OCTOPUS_API_KEY}:".encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", auth_header)

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                c_results = json.loads(resp.read().decode("utf-8")).get("results", [])
        except Exception as e:
            print(f"获取 {target_date} 用电数据失败: {e}", file=sys.stderr)
            continue

        if len(c_results) < 40:
            print(f"提示: {target_date} 智能电表数据尚未完全同步 (仅 {len(c_results)} 条)，尝试前一日...")
            continue

        # 2. 抓取该日对应电价
        tariff_code = f"E-1R-{PRODUCT_CODE}-{REGION_CODE}"
        r_url = f"https://api.octopus.energy/v1/products/{PRODUCT_CODE}/electricity-tariffs/{tariff_code}/standard-unit-rates/?{urllib.parse.urlencode(params)}"
        req_r = urllib.request.Request(r_url, headers={"User-Agent": "OctopusAgileSlackBot/3.1"})
        try:
            with urllib.request.urlopen(req_r, timeout=12) as resp:
                r_results = json.loads(resp.read().decode("utf-8")).get("results", [])
        except Exception as e:
            print(f"获取 {target_date} 对应电价失败: {e}", file=sys.stderr)
            continue

        # 3. 按 UTC 时间戳对齐并计算
        rates_map = {}
        for r in r_results:
            dt = datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00")).astimezone(timezone.utc)
            rates_map[dt.strftime("%Y-%m-%dT%H:%M:%SZ")] = float(r["value_inc_vat"])

        total_kwh = 0.0
        total_cost_p = 0.0
        peak_kwh = 0.0
        peak_cost_p = 0.0
        rates_sum = 0.0
        matched = 0
        matched_slots = []

        for c in c_results:
            dt = datetime.fromisoformat(c["interval_start"].replace("Z", "+00:00")).astimezone(timezone.utc)
            key = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if key in rates_map:
                matched += 1
                k = float(c["consumption"])
                rate = rates_map[key]
                cost = k * rate
                total_kwh += k
                total_cost_p += cost
                rates_sum += rate

                dt_local = dt.astimezone(london_tz)
                is_peak = (16 <= dt_local.hour < 19)
                if is_peak:
                    peak_kwh += k
                    peak_cost_p += cost

                matched_slots.append({
                    "time": dt_local.strftime("%H:%M"),
                    "kwh": round(k, 4),
                    "price": round(rate, 2),
                    "hour": dt_local.hour,
                    "is_peak": is_peak,
                })

        matched_slots.sort(key=lambda x: x["time"])

        vwap = (total_cost_p / total_kwh) if total_kwh > 0 else 0
        mkt_avg = (rates_sum / matched) if matched > 0 else 0
        peak_pct = (peak_kwh / total_kwh * 100) if total_kwh > 0 else 0
        peak_vwap = (peak_cost_p / peak_kwh) if peak_kwh > 0 else 0
        is_yesterday = (target_date == now.date() - timedelta(days=1))

        return {
            "date": target_date,
            "is_yesterday": is_yesterday,
            "total_kwh": total_kwh,
            "total_cost_gbp": total_cost_p / 100.0,
            "vwap": vwap,
            "mkt_avg": mkt_avg,
            "peak_kwh": peak_kwh,
            "peak_cost_gbp": peak_cost_p / 100.0,
            "peak_pct": peak_pct,
            "peak_vwap": peak_vwap,
            "diff": mkt_avg - vwap,
            "matched_slots": matched,
            "slots": matched_slots,
        }

    return None


def generate_usage_chart_url(usage_summary):
    """生成前一日【半小时用电量(柱) vs 实时电价(折线)】高清双轴走势图 URL (短链接)"""
    if not usage_summary:
        return None
    slots = usage_summary.get("slots", [])
    if not slots:
        return None

    date_str = usage_summary["date"].strftime("%Y-%m-%d")
    labels = []
    kwh_data = []
    price_data = []
    bar_colors = []

    for i, s in enumerate(slots):
        if i % 4 == 0:
            labels.append(s["time"][:2])
        elif i == 32:  # 16:00
            labels.append("16")
        elif i == 38:  # 19:00
            labels.append("19")
        else:
            labels.append("")

        kwh_data.append(s["kwh"])
        price_data.append(s["price"])

        p = s["price"]
        if p < 0:
            bar_colors.append("#3B82F6")  # 蓝色：负电价
        elif p < 12:
            bar_colors.append("#10B981")  # 绿色：超低电价
        elif p < 22:
            bar_colors.append("#34D399")  # 浅绿：常规平价
        elif s["is_peak"] or p >= 32:
            bar_colors.append("#EF4444")  # 红色：高峰期 / 高昂电价
        else:
            bar_colors.append("#F59E0B")  # 橙黄：适中偏贵

    chart_config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "type": "line",
                    "label": "实时电价 (p/kWh)",
                    "data": price_data,
                    "borderColor": "#FBBF24",
                    "backgroundColor": "transparent",
                    "borderWidth": 2.5,
                    "pointRadius": 2,
                    "pointBackgroundColor": "#FBBF24",
                    "yAxisID": "yPrice",
                },
                {
                    "type": "bar",
                    "label": "半小时用电量 (kWh)",
                    "data": kwh_data,
                    "backgroundColor": bar_colors,
                    "yAxisID": "yKwh",
                    "categoryPercentage": 0.92,
                    "barPercentage": 0.95,
                },
            ],
        },
        "options": {
            "title": {
                "display": True,
                "text": f"用电复盘 ({date_str}): 半小时用电量(柱) vs 实时电价(折线)",
                "fontSize": 14,
                "fontColor": "#1E293B",
            },
            "legend": {
                "display": True,
                "position": "bottom",
                "labels": {"boxWidth": 12, "fontSize": 11},
            },
            "scales": {
                "yAxes": [
                    {
                        "id": "yKwh",
                        "type": "linear",
                        "position": "left",
                        "ticks": {"beginAtZero": True, "fontSize": 10},
                        "scaleLabel": {
                            "display": True,
                            "labelString": "用电量 (kWh)",
                            "fontSize": 11,
                        },
                    },
                    {
                        "id": "yPrice",
                        "type": "linear",
                        "position": "right",
                        "ticks": {"fontSize": 10},
                        "scaleLabel": {
                            "display": True,
                            "labelString": "单价 (p/kWh)",
                            "fontSize": 11,
                        },
                        "gridLines": {"drawOnChartArea": False},
                    },
                ],
                "xAxes": [
                    {
                        "gridLines": {"display": False},
                        "ticks": {
                            "autoSkip": False,
                            "maxRotation": 0,
                            "fontSize": 10,
                        },
                    }
                ],
            },
        },
    }

    return create_quickchart_short_url(chart_config, width=740, height=290)


def fetch_agile_prices():
    """智能获取电价：
    - 优先获取【次日（明天）】全部 48 个时段电价（每天 16:00 左右由 Octopus 发布）；
    - 若明天数据尚未发布（例如在下午 16:00 前手动测试），自动平滑回退为【今日数据】，保证绝不报错。
    """
    london_tz = ZoneInfo("Europe/London")
    now = datetime.now(london_tz)

    tariff_code = f"E-1R-{PRODUCT_CODE}-{REGION_CODE}"
    base_url = f"https://api.octopus.energy/v1/products/{PRODUCT_CODE}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    # 1. 优先尝试拉取明天（Next Day）的数据
    tomorrow = now.date() + timedelta(days=1)
    start_tomorrow = datetime.combine(tomorrow, time.min, tzinfo=london_tz)
    end_tomorrow = datetime.combine(tomorrow, time.max, tzinfo=london_tz)

    params_tomorrow = {
        "period_from": start_tomorrow.isoformat(),
        "period_to": end_tomorrow.isoformat(),
        "page_size": 100,
    }
    url_tomorrow = f"{base_url}?{urllib.parse.urlencode(params_tomorrow)}"
    req_tomorrow = urllib.request.Request(
        url_tomorrow, headers={"User-Agent": "OctopusAgileSlackBot/3.1"}
    )

    try:
        with urllib.request.urlopen(req_tomorrow, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            if results and len(results) >= 24:
                results.sort(key=lambda x: x["valid_from"])
                print(f"成功获取到明天 ({tomorrow}) 全部 {len(results)} 个时段电价！")
                return results, tomorrow, True
    except Exception as e:
        print(f"尝试拉取明天电价时提示: {e}", file=sys.stderr)

    # 2. 如果明天数据尚未发布，自动回退拉取今天（Today）的数据
    print("提示：明天电价尚未发布（通常在英国时间 16:00 左右公布），自动回退显示今日数据。")
    today = now.date()
    start_today = datetime.combine(today, time.min, tzinfo=london_tz)
    end_today = datetime.combine(today, time.max, tzinfo=london_tz)

    params_today = {
        "period_from": start_today.isoformat(),
        "period_to": end_today.isoformat(),
        "page_size": 100,
    }
    url_today = f"{base_url}?{urllib.parse.urlencode(params_today)}"
    req_today = urllib.request.Request(
        url_today, headers={"User-Agent": "OctopusAgileSlackBot/3.1"}
    )

    try:
        with urllib.request.urlopen(req_today, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            results.sort(key=lambda x: x["valid_from"])
            return results, today, False
    except Exception as e:
        print(f"获取今日电价失败: {e}", file=sys.stderr)
        return [], today, False


def group_consecutive_slots(slots):
    """将连续的半小时低价时段合并为区间展示"""
    if not slots:
        return []

    groups = []
    for s in slots:
        if not groups or groups[-1][-1]["to"] != s["from"]:
            groups.append([s])
        else:
            groups[-1].append(s)

    formatted = []
    for g in groups:
        start_t = g[0]["from"].strftime("%H:%M")
        end_t = g[-1]["to"].strftime("%H:%M")
        prices = [x["price"] for x in g]
        min_p = min(prices)
        avg_p = sum(prices) / len(prices)
        formatted.append(
            f"• *{start_t} - {end_t}*（均价 `{avg_p:.1f}p`，最低 `{min_p:.1f}p`）"
        )
    return formatted


def analyze_prices(rates, target_date, is_tomorrow):
    """分析 48 个半小时原始数据"""
    if not rates:
        return None

    london_tz = ZoneInfo("Europe/London")
    slots = []

    for r in rates:
        dt_from = datetime.fromisoformat(
            r["valid_from"].replace("Z", "+00:00")
        ).astimezone(london_tz)
        dt_to = datetime.fromisoformat(
            r["valid_to"].replace("Z", "+00:00")
        ).astimezone(london_tz)
        price = r["value_inc_vat"]
        slots.append({"from": dt_from, "to": dt_to, "price": price})

    all_prices = [s["price"] for s in slots]
    avg_price = sum(all_prices) / len(all_prices)
    min_slot = min(slots, key=lambda x: x["price"])
    max_slot = max(slots, key=lambda x: x["price"])

    negative_slots = [s for s in slots if s["price"] < 0]
    low_price_slots = [s for s in slots if s["price"] < LOW_PRICE_THRESHOLD]

    chart_labels = []
    chart_prices = []
    chart_colors = []

    for s in slots:
        chart_prices.append(round(s["price"], 1))
        if s["from"].minute == 0:
            chart_labels.append(s["from"].strftime("%H"))
        else:
            chart_labels.append("")

        p = s["price"]
        if p < 0:
            chart_colors.append("#9B59B6")
        elif p < LOW_PRICE_THRESHOLD:
            chart_colors.append("#2ECC71")
        elif p < 22:
            chart_colors.append("#3498DB")
        elif p < 32:
            chart_colors.append("#E67E22")
        else:
            chart_colors.append("#E74C3C")

    best_2h_avg = float("inf")
    best_2h_window = None
    for i in range(len(slots) - 3):
        win = slots[i : i + 4]
        w_avg = sum(s["price"] for s in win) / 4
        if w_avg < best_2h_avg:
            best_2h_avg = w_avg
            best_2h_window = (win[0]["from"], win[-1]["to"], best_2h_avg)

    return {
        "slots": slots,
        "avg": avg_price,
        "min_slot": min_slot,
        "max_slot": max_slot,
        "negative_slots": negative_slots,
        "low_price_slots": low_price_slots,
        "best_2h_window": best_2h_window,
        "chart_labels": chart_labels,
        "chart_prices": chart_prices,
        "chart_colors": chart_colors,
        "target_date": target_date,
        "is_tomorrow": is_tomorrow,
    }


def generate_forecast_chart_url(analysis, date_str):
    """生成次日 48 根半小时柱状图的高清图表 URL (短链接)"""
    day_type = "明日" if analysis["is_tomorrow"] else "全天"
    chart_config = {
        "type": "bar",
        "data": {
            "labels": analysis["chart_labels"],
            "datasets": [
                {
                    "label": "半小时电价 (p/kWh)",
                    "data": analysis["chart_prices"],
                    "backgroundColor": analysis["chart_colors"],
                    "categoryPercentage": 0.95,
                    "barPercentage": 0.95,
                }
            ],
        },
        "options": {
            "title": {
                "display": True,
                "text": f"{day_type}48个半小时电价走势 (p/kWh) - {date_str}",
                "fontSize": 14,
            },
            "legend": {"display": False},
            "scales": {
                "yAxes": [
                    {
                        "ticks": {"beginAtZero": True},
                        "gridLines": {"color": "rgba(0,0,0,0.06)"},
                    }
                ],
                "xAxes": [
                    {
                        "gridLines": {"display": False},
                        "ticks": {"autoSkip": False, "maxRotation": 0},
                    }
                ],
            },
        },
    }

    return create_quickchart_short_url(chart_config, width=740, height=280)


def send_to_slack(analysis, usage_summary=None):
    """发送包含昨日用电复盘(数据+双轴图)、次日半小时柱状图和 10p 报警的 Slack 卡片"""
    if not analysis:
        return

    date_str = analysis["target_date"].strftime("%Y-%m-%d (%A)")
    is_tom = analysis["is_tomorrow"]
    prefix = "明日" if is_tom else "今日"

    min_time = (
        f"{analysis['min_slot']['from'].strftime('%H:%M')} - "
        f"{analysis['min_slot']['to'].strftime('%H:%M')}"
    )
    max_time = (
        f"{analysis['max_slot']['from'].strftime('%H:%M')} - "
        f"{analysis['max_slot']['to'].strftime('%H:%M')}"
    )
    best_2h_str = (
        f"{analysis['best_2h_window'][0].strftime('%H:%M')} - "
        f"{analysis['best_2h_window'][1].strftime('%H:%M')} "
        f"(`{analysis['best_2h_window'][2]:.1f} p/kWh`)"
    )

    header_title = (
        f"⚡ Octopus Agile {prefix}电价预报 ({date_str})"
        if is_tom
        else f"⚡ Octopus Agile 今日电价提醒 ({date_str})"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_title,
                "emoji": True,
            },
        }
    ]

    # ========== 模块 1: 前一日（或最新）用电量与电费复盘 + 双轴对齐图 ==========
    if usage_summary:
        u_date = usage_summary["date"].strftime("%Y-%m-%d (%A)")
        title_tag = (
            "📋 *【昨日用电复盘】*"
            if usage_summary["is_yesterday"]
            else "📋 *【最新完整用电复盘】* _(昨日电表未回传完全)_"
        )

        diff = usage_summary["diff"]
        if diff > 0.5:
            eval_str = f"🎉 *避峰出色！* 比市场均价低 `{diff:.1f}p` (省约 `{(diff / usage_summary['mkt_avg']) * 100:.0f}%`)"
        elif diff < -0.5:
            eval_str = f"⚠️ *高峰用电偏多*，均价比市场高 `{abs(diff):.1f}p`"
        else:
            eval_str = "⚖️ 与市场基准均价基本持平"

        usage_fields = [
            {
                "type": "mrkdwn",
                "text": (
                    f"*⚡ 结算总用电:*\n`{usage_summary['total_kwh']:.2f} kWh`"
                    f" (加权均价 `{usage_summary['vwap']:.1f}p`)"
                ),
            },
            {
                "type": "mrkdwn",
                "text": f"*💰 纯电费支出:*\n`£{usage_summary['total_cost_gbp']:.2f}`",
            },
            {
                "type": "mrkdwn",
                "text": (
                    f"*🔥 晚高峰 (16-19点):*\n`{usage_summary['peak_kwh']:.2f} kWh`"
                    f" (占 `{usage_summary['peak_pct']:.1f}%`，花费"
                    f" `£{usage_summary['peak_cost_gbp']:.2f}`)"
                ),
            },
            {"type": "mrkdwn", "text": f"*🎯 避峰评价:*\n{eval_str}"},
        ]

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{title_tag} *{u_date}*",
            },
            "fields": usage_fields,
        })

        # 嵌入前一日【用电量柱状图 + 电价折线图】双轴图 (短链接)
        usage_chart_url = generate_usage_chart_url(usage_summary)
        if usage_chart_url:
            blocks.append({
                "type": "image",
                "title": {
                    "type": "plain_text",
                    "text": f"📊 用电复盘走势: 半小时用电量(柱) vs 实时电价(折线) - {u_date}",
                    "emoji": True,
                },
                "image_url": usage_chart_url,
                "alt_text": "用电量与电价双轴对齐图",
            })

        blocks.append({"type": "divider"})

    # ========== 模块 2: 次日电价预报概览 ==========
    blocks.append({
        "type": "section",
        "fields": [
            {
                "type": "mrkdwn",
                "text": f"*📊 全天均价:*\n`{analysis['avg']:.1f} p/kWh`",
            },
            {"type": "mrkdwn", "text": f"*🧺 连续2小时最佳用电:*\n{best_2h_str}"},
            {
                "type": "mrkdwn",
                "text": f"*🟢 {prefix}谷值:*\n`{analysis['min_slot']['price']:.1f} p` ({min_time})",
            },
            {
                "type": "mrkdwn",
                "text": f"*🔴 {prefix}峰值:*\n`{analysis['max_slot']['price']:.1f} p` ({max_time})",
            },
        ],
    })

    # 低于 10p 警报模块
    if analysis["low_price_slots"]:
        low_groups = group_consecutive_slots(analysis["low_price_slots"])
        low_text = "\n".join(low_groups)
        tip_text = (
            "💡 _建议提前预约明天的洗烘衣物、洗碗机、储能电池或电动车充电！_"
            if is_tom
            else "💡 _建议安排洗烘衣物、洗碗机、储能电池或电动车充电！_"
        )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🚨 *【{prefix}低价特惠】有低于 10p 的半小时时段！*\n"
                    f"{low_text}\n"
                    f"> {tip_text}"
                ),
            },
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"ℹ️ {prefix}全天无低于 10p 时段（最低为 `{analysis['min_slot']['price']:.1f} p/kWh`，时段 {min_time}）"
                    ),
                }
            ],
        })

    # 负电价警报（如有）
    if analysis["negative_slots"]:
        neg_groups = group_consecutive_slots(analysis["negative_slots"])
        neg_text = "\n".join(neg_groups)
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔥 *【负电价报警！倒贴用电！】*\n{neg_text}",
            },
        })

    # 次日 48 根半小时柱状图 (短链接)
    forecast_chart_url = generate_forecast_chart_url(analysis, date_str)
    blocks.append({
        "type": "image",
        "title": {
            "type": "plain_text",
            "text": f"📊 {prefix} 48 个半小时时段电价走势 (绿色为 <10p 低价，红色为高峰)",
            "emoji": True,
        },
        "image_url": forecast_chart_url,
        "alt_text": f"{prefix}48个半小时电价走势图",
    })

    slack_payload = {
        "text": f"Octopus Agile {prefix}电价提醒 ({date_str})",
        "blocks": blocks,
    }

    if not SLACK_WEBHOOK_URL:
        print("警告: 未设置 SLACK_WEBHOOK_URL，跳过发送。Payload 构造正常。")
        return

    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(slack_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8")
            if resp.status == 200:
                print(f"48 时段 {prefix} Slack 消息与图表推送成功！")
            else:
                print(f"Slack 返回状态码: {resp.status} - {resp_body}")
    except urllib.error.HTTPError as e:
        err_detail = e.read().decode("utf-8", errors="ignore")
        print(f"发送 Slack 失败 (HTTP {e.code}): {err_detail}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"发送 Slack 失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # 1. 尝试拉取昨日用电与电费
    usage_summary = fetch_recent_usage_summary()

    # 2. 拉取今日/次日电价并推送
    rates, target_date, is_tomorrow = fetch_agile_prices()
    if not rates:
        print("未获取到有效电价数据。")
        sys.exit(1)

    analysis = analyze_prices(rates, target_date, is_tomorrow)
    send_to_slack(analysis, usage_summary)
