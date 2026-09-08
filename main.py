import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

# ==================== 配置区域 ====================
# 1. 你的 Slack Webhook URL (建议通过环境变量 SLACK_WEBHOOK_URL 传入，或直接填在此处)
SLACK_WEBHOOK_URL = os.getenv(
    "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
)

# 2. Octopus Agile 产品代码（当前主流标准版为 AGILE-24-04-03）
PRODUCT_CODE = os.getenv("OCTOPUS_PRODUCT_CODE", "AGILE-24-04-03")

# 3. 你的英国电网区域代码（DNO Region Code，如伦敦是 C，英格兰东部是 A，详见文末列表）
REGION_CODE = os.getenv("OCTOPUS_REGION_CODE", "C")
# =================================================


def fetch_agile_prices():
    """从 Octopus 官方开放 API 获取当天的 Agile 电价"""
    london_tz = ZoneInfo("Europe/London")
    now = datetime.now(london_tz)

    # 当天的 00:00:00 到 23:59:59 (带英国时区/夏令时处理)
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

    req = urllib.request.Request(url, headers={"User-Agent": "OctopusAgileSlackBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            results = data.get("results", [])
            # 按时间先后顺序升序排列
            results.sort(key=lambda x: x["valid_from"])
            return results
    except Exception as e:
        print(f"获取 Octopus 电价失败: {e}", file=sys.stderr)
        return []


def analyze_prices(rates):
    """分析全天电价数据"""
    if not rates:
        return None

    london_tz = ZoneInfo("Europe/London")
    slots = []

    for r in rates:
        # ISO 时间转伦敦本地时间
        dt_from = datetime.fromisoformat(
            r["valid_from"].replace("Z", "+00:00")
        ).astimezone(london_tz)
        dt_to = datetime.fromisoformat(
            r["valid_to"].replace("Z", "+00:00")
        ).astimezone(london_tz)
        price = r["value_inc_vat"]  # 含税单价 (p/kWh)

        slots.append({"from": dt_from, "to": dt_to, "price": price})

    # 1. 均价、最低、最高
    prices = [s["price"] for s in slots]
    avg_price = sum(prices) / len(prices)
    min_slot = min(slots, key=lambda x: x["price"])
    max_slot = max(slots, key=lambda x: x["price"])

    # 2. 负电价检查（Plunge Pricing）
    negative_slots = [s for s in slots if s["price"] < 0]

    # 3. 寻找连续 2 小时（4个半小时槽）最划算用电窗口（适合大功率洗碗机/洗烘机/充车）
    best_2h_avg = float("inf")
    best_2h_window = None
    for i in range(len(slots) - 3):
        window = slots[i : i + 4]
        w_avg = sum(s["price"] for s in window) / 4
        if w_avg < best_2h_avg:
            best_2h_avg = w_avg
            best_2h_window = (window[0]["from"], window[-1]["to"], best_2h_avg)

    return {
        "slots": slots,
        "avg": avg_price,
        "min_slot": min_slot,
        "max_slot": max_slot,
        "negative_slots": negative_slots,
        "best_2h_window": best_2h_window,
    }


def send_to_slack(analysis):
    """构造 Slack 消息并推送"""
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
        f"(`{analysis['best_2h_window'][2]:.2f} p/kWh`)"
    )

    # 负电价提示
    plunge_msg = ""
    if analysis["negative_slots"]:
        plunge_times = ", ".join(
            [s["from"].strftime("%H:%M") for s in analysis["negative_slots"]]
        )
        plunge_msg = (
            f"\n> 🚨 *发现负电价（用电倒贴钱）！* 时段开始于: `{plunge_times}`"
        )

    # Slack Block Kit 富文本卡片
    slack_payload = {
        "text": f"Octopus Agile 今日电价提醒 ({today_str})",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"⚡ Octopus Agile 今日电价提醒 ({today_str})",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*📊 全天平均:*\n`{analysis['avg']:.2f} p/kWh`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*🧺 连续2小时最佳用电:*\n{best_2h_str}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*🟢 今日谷值 (最低):*\n`{analysis['min_slot']['price']:.2f} p` ({min_time})",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*🔴 今日峰值 (最高):*\n`{analysis['max_slot']['price']:.2f} p` ({max_time})",
                    },
                ],
            },
        ],
    }

    if plunge_msg:
        slack_payload["blocks"].append(
            {"type": "section", "text": {"type": "mrkdwn", "text": plunge_msg}}
        )

    # 发送请求到 Slack
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(slack_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("Slack 消息推送成功！")
            else:
                print(f"Slack 返回异常状态码: {resp.status}")
    except Exception as e:
        print(f"发送 Slack 失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    rates = fetch_agile_prices()
    if not rates:
        print("未获取到有效电价数据，可能该地区电价未及时更新。")
        sys.exit(1)

    analysis = analyze_prices(rates)
    send_to_slack(analysis)