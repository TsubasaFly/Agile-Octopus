import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

# ==================== 配置区域 ====================
SLACK_WEBHOOK_URL = os.getenv(
    "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
)
PRODUCT_CODE = os.getenv("OCTOPUS_PRODUCT_CODE", "AGILE-24-04-03")
REGION_CODE = os.getenv("OCTOPUS_REGION_CODE", "C")  # 英国电网区域，如 C 为伦敦
LOW_PRICE_THRESHOLD = 10.0  # 低价报警阈值 (p/kWh)
# =================================================


def fetch_agile_prices():
    """从 Octopus 官方 API 获取当天全部 48 个半小时时段电价"""
    london_tz = ZoneInfo("Europe/London")
    now = datetime.now(london_tz)

    start_of_day = datetime.combine(now.date(), time.min, tzinfo=london_tz)
    end_of_day = datetime.combine(now.date(), time.max, tzinfo=london_tz)

    tariff_code = f"E-1R-{PRODUCT_CODE}-{REGION_CODE}"
    base_url = f"https://api.octopus.energy/v1/products/{PRODUCT_CODE}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    params = {
        "period_from": start_of_day.isoformat(),
        "period_to": end_of_day.isoformat(),
        "page_size": 100,
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": "OctopusAgileSlackBot/2.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            results.sort(key=lambda x: x["valid_from"])
            return results
    except Exception as e:
        print(f"获取 Octopus 电价失败: {e}", file=sys.stderr)
        return []


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


def analyze_prices(rates):
    """直接使用 48 个半小时原始数据分析，保留最高精度"""
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

    # 1. 负电价与低价时段筛选 (<10p)
    negative_slots = [s for s in slots if s["price"] < 0]
    low_price_slots = [s for s in slots if s["price"] < LOW_PRICE_THRESHOLD]

    # 2. 48 个半小时柱状图数据准备
    chart_labels = []
    chart_prices = []
    chart_colors = []

    for s in slots:
        chart_prices.append(round(s["price"], 1))

        # X 轴标签：如果是整点（:00）显示小时，半点（:30）留空，保证 48 根柱子整齐清晰
        if s["from"].minute == 0:
            chart_labels.append(s["from"].strftime("%H"))
        else:
            chart_labels.append("")

        # 颜色分级
        p = s["price"]
        if p < 0:
            chart_colors.append("#9B59B6")  # 紫色：负电价（倒贴）
        elif p < LOW_PRICE_THRESHOLD:
            chart_colors.append("#2ECC71")  # 绿色：低于 10p 极划算
        elif p < 22:
            chart_colors.append("#3498DB")  # 蓝色：日常平价
        elif p < 32:
            chart_colors.append("#E67E22")  # 橙色：偏贵
        else:
            chart_colors.append("#E74C3C")  # 红色：高峰期（避开用电）

    # 3. 计算连续 2 小时（4 个半小时）最划算窗口
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
    }


def generate_chart_url(analysis, date_str):
    """生成 48 根半小时柱状图的高清图表 URL"""
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
                "text": f"全天48个半小时电价走势 (p/kWh) - {date_str}",
                "fontSize": 14,
            },
            "legend": {"display": False},
            "scales": {
                "yAxes": [
                    {
                        "ticks": {"beginAtZero": False},
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

    config_str = urllib.parse.quote(json.dumps(chart_config, separators=(",", ":")))
    # 加宽至 740px，确保 48 根柱子每根都有充足宽度
    chart_url = f"https://quickchart.io/chart?w=740&h=280&devicePixelRatio=2&bkg=white&c={config_str}"
    return chart_url


def send_to_slack(analysis):
    """发送包含半小时柱状图和 10p 报警的 Slack 卡片"""
    if not analysis:
        return

    today_str = datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d (%A)")
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

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"⚡ Octopus Agile 电价日报 ({today_str})",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*📊 全天均价:*\n`{analysis['avg']:.1f} p/kWh`"},
                {"type": "mrkdwn", "text": f"*🧺 连续2小时最佳用电:*\n{best_2h_str}"},
                {
                    "type": "mrkdwn",
                    "text": f"*🟢 今日谷值:*\n`{analysis['min_slot']['price']:.1f} p` ({min_time})",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*🔴 今日峰值:*\n`{analysis['max_slot']['price']:.1f} p` ({max_time})",
                },
            ],
        },
    ]

    # 1. 低于 10p 警报模块
    if analysis["low_price_slots"]:
        low_groups = group_consecutive_slots(analysis["low_price_slots"])
        low_text = "\n".join(low_groups)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🚨 *【低价特惠报警】今日有低于 10p 的半小时时段！*\n"
                        f"{low_text}\n"
                        f"> 💡 _建议安排洗烘衣物、洗碗机、储能电池充电或电动车充电！_"
                    ),
                },
            }
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"ℹ️ 今日全天无低于 10p 时段（最低为 `{analysis['min_slot']['price']:.1f} p/kWh`，时段 {min_time}）",
                    }
                ],
            }
        )

    # 负电价强预警（如有）
    if analysis["negative_slots"]:
        neg_groups = group_consecutive_slots(analysis["negative_slots"])
        neg_text = "\n".join(neg_groups)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔥 *【负电价报警！倒贴用电！】*\n{neg_text}",
                },
            }
        )

    # 2. 48 根半小时柱状图
    chart_url = generate_chart_url(analysis, today_str)
    blocks.append(
        {
            "type": "image",
            "title": {
                "type": "plain_text",
                "text": "📊 全天 48 个半小时时段电价走势 (绿色为 <10p 低价，红色为高峰)",
                "emoji": True,
            },
            "image_url": chart_url,
            "alt_text": "48个半小时电价走势图",
        }
    )

    slack_payload = {
        "text": f"Octopus Agile 今日电价提醒 ({today_str})",
        "blocks": blocks,
    }

    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(slack_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("48 时段 Slack 消息与高清柱状图推送成功！")
            else:
                print(f"Slack 返回状态码: {resp.status}")
    except Exception as e:
        print(f"发送 Slack 失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    rates = fetch_agile_prices()
    if not rates:
        print("未获取到有效电价数据。")
        sys.exit(1)

    analysis = analyze_prices(rates)
    send_to_slack(analysis)
