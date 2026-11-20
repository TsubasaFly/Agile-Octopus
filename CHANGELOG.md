# Octopus Agile 分析系统变更日志 (CHANGELOG)

本文档记录 `Octopus Analysis` 项目的所有功能迭代、架构调整、数据同步与 Bug 修复记录。

---

## [v1.3.0] - 2026-11-20

### 🌟 新增功能 (Features)
1. **月度使用与账单全景视图 (Monthly Overview)**:
   - 新增 11 个月（2026-01 至 2026-11）的月度聚合分析引擎，按月计算用电总量、纯电费、每日固定费（Standing Charge）、全包总电费、纯均价（VWAP）、全包综合单价、晚高峰用电占比及对比标准浮动电价（Ofgem SVT Price Cap）节省额度。
   - 增加 **3 种前端交互子视图**：
     - `📊 月度走势图`：平谷电量与晚高峰电量堆叠柱形图，配合加权纯单价与全包单价双折线（HTML5 Canvas 响应式渲染）。
     - `🗂️ 月度总和卡片网格`：每张卡片展示当月核心指标总和，支持点击快速下钻当月。
     - `📑 对比明细表`：展示完整财务与能耗指标，包含负电价收益与全年 Grand Total 汇总行。
   - 增加**全局月份筛选联动**：顶部提供 `全部月份` 及各单月筛选胶囊，选中后自动下钻联动下方的“每日用电走势图”与“48 时段精细走势图”。

2. **自动加载本地凭据 (`API key.txt`)**:
   - `download_and_analyze.py` 与 `build_dashboard.py` 支持自动识别并读取同目录下 `API key.txt`，支持格式 `OCTOPUS_API_KEY=xxx`，避免在代码中硬编码敏感信息。

3. **双构建引擎同步升级**:
   - `generate_dashboard.py`：基于本地 `octopus_agile_usage_2026.csv` 高速生成本地 `dashboard.html`。
   - `build_dashboard.py`：独立支持直接调用 Octopus REST API 增量拉取用电与电价数据，并生成 `public/index.html`，完美适配 GitHub Actions 自动化部署。
   - 同步更新了 Slack 月度通知 Payload，支持推送全包费用与全包均价。

### 🔄 数据同步 (Data Sync)
- 全量拉取 2026-01-01 至 2026-11-18 的 15,446 条半小时计量数据。
- 2026 年累计用电量达 `2,193.27 kWh`，累计纯电费 `£511.01`，固定费 `£127.18`，全包总电费 `£638.19`，加权纯均价 `23.30 p/kWh`，全包综合均价 `29.10 p/kWh`。

### 🐛 修复与优化 (Bug Fixes & Improvements)
- **修复 401 Unauthorized 凭证失效**：由于此前 API Key 轮换失效，更新为新 Key 并增加异常捕获与非零退出码（exit code 1），便于外部调度器监控。
- **修复 UTF-8 输出重定向乱码**：在 `run_update.bat` 中加入 `chcp 65001`，并确保各 Python 脚本重定向输出流至 UTF-8，解决 Windows CMD 环境下日志中文字符乱码问题。
- **动态截止时间**：修复此前数据拉取时间硬编码为 9 月底的问题，改为动态获取当前 UTC 时间。

---

## [v1.2.0] - 2026-10-04

### 🌟 新增功能 (Features)
- 增加本地 Windows 自动化更新批处理脚本 [run_update.bat](file:///F:/Octopus%20Analysis/run_update.bat)。
- 配合 Windows 任务计划程序（Task Scheduler），每月初自动唤醒执行数据同步。
- 引入用户空闲检测与防打扰安全休眠脚本联动（`safe_sleep.ps1`）。
- 建立统一更新流水线 [update_all.py](file:///F:/Octopus%20Analysis/update_all.py)，顺序调度下载与页面生成脚本，并输出执行日志至 [update.log](file:///F:/Octopus%20Analysis/update.log)。

---

## [v1.1.0] - 2026-09-29

### 🌟 新增功能 (Features)
- 新增 GitHub Actions 自动部署工作流 [monthly_dashboard.yml](file:///F:/Octopus%20Analysis/monthly_dashboard.yml)。
- 设定每月 2 号 UTC 09:00 自动触发 GitHub Actions 构建，拉取 DCC 完整结算数据并发布至 GitHub Pages (`https://tsubasafly.github.io/Agile-Octopus/`)。
- 集成 Slack Incoming Webhook 消息通知机制，构建成功后自动发送包含关键能耗指标的月度简报。
- 制作发布小红书能耗分享长图及封面图（`xiaohongshu_card.png`, `xhs_page1_cover.png`, `xhs_page2_details.png`）。

---

## [v1.0.0] - 2026-09-29

### 🚀 初始版本上线 (Initial Release)
- 完成与 Octopus Energy REST API 的对接（MPAN: `1200062063391`, Serial: `16K0577482`, Tariff: `AGILE-24-10-01-C`）。
- 实现 2026 年半小时颗粒度用电数据与实时 Agile 含税电价的时序精准对齐。
- 构建 [generate_dashboard.py](file:///F:/Octopus%20Analysis/generate_dashboard.py) 交互式单页仪表盘：
  - 核心指标卡片（VWAP、纯电费、总用电量、节省估算）。
  - 7 档价格区间（负电价至极高峰）负荷分布堆叠柱状图。
  - 每日用电走势与 48 时段下钻图表。
  - 24 小时分时负荷特征诊断图。
- 建立初始数据文件 [octopus_agile_usage_2026.csv](file:///F:/Octopus%20Analysis/octopus_agile_usage_2026.csv) 与项目文档 [README.md](file:///F:/Octopus%20Analysis/README.md)。
