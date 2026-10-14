# 港股143只｜云端日线自动更新与历史低位筛选

> **状态：已部署到 Superxy/hk143-price-screen；2026-10-14首次手动下载143/143只成功，三组全量排名更新已通过云端测试。** 这是公开行情研究程序；不下单、不访问交易账户、不存放个人资产数据。

## 它如何工作

- **行情**：使用你已在 Windows 验证可用的 `akshare.stock_hk_daily(symbol=..., adjust="")`（新浪财经港股不复权日线）。保留开盘、最高、最低、收盘、成交量、成交额原始字段，筛选计算只使用 OHLC。
- **定时**：GitHub Actions 每周一至周五香港时间 **19:17** 下载，当地次日早上 **06:17** 补检。GitHub 的 `cron` 用 UTC：周一至周五 `11:17`、周日至周四 `22:17`。两次日程都根据香港交易日历确定“最近已完成的交易日”，避免时区和假期误报。
- **存储**：`data/2025/代码_2025.csv`、`data/2026/代码_2026.csv`（年份自动向后扩展）；`data/latest.md`、`data/latest.json` 是晨报的固定入口；`data/覆盖与评分.csv`、`data/全部低位候选.csv` 为核对表，后者包含所有合格股票且不设20只上限。
- **故障恢复**：已覆盖目标日期的股票跳过重复下载；缺少的股票自动重试；接口超时不会用旧数据充当最新数据。每只股票标注最新K线日期；可能停牌的股票显示为 `stale`，而非擅自插值。
- **价格校验**：香港交易日历 `XHKG`、OHLC 合法性、年份首末及交易日覆盖、120个**交易日**前精确日期、隔夜45%以上跳变提示，均内置。历史年份缺少交易日的，不认证为“全年低位”。

## 初次部署（无需自己的电脑24小时开机）

1. 登录 [GitHub](https://github.com)，建立名称如 **`hk143-price-screen`** 的**公开（Public）**仓库，默认分支 **main**，建议勾选 Add README 初始化仓库。仓库只能放本项目的**公开股票代码与公开价格资料**，不要上传身份证、交易流水、持仓成本、访问令牌。
2. 将本压缩包**解压**，进入解压后的 `hk143_cloud_project` 文件夹，把**里面所有文件夹和文件**上传到仓库根目录（注意保留隐藏目录 `.github/workflows/update.yml`）。在仓库中选择 **Add file → Upload files**，可以拖入文件夹后提交；**不要把 ZIP 文件直接当成唯一文件上传**。
3. 打开仓库的 **Actions** 页，找到 **HK143 unadjusted daily prices and low-position screen**，点击 **Run workflow**（手动执行一次）。如果仓库提示 Actions 不可用，先按其提示启用。
4. 如果手动执行失败且日志显示 `403 Permission` 或 `git push` 失败，进入 **Settings → Actions → General → Workflow permissions**，检查是否允许 Actions 写入仓库内容；本项目 workflow 已设置最小范围的 `permissions: contents: write`。若平台/组织策略禁止写入，需由账号所有者授权，不要在代码中写个人 Token。
5. 查看 Actions 日志中是否出现 `REPORT target=... fresh=.../143`，随后检查仓库里是否自动出现 `data/latest.md` 和 `data/latest.json`。**必须核对 `target_session_hkt` 和 `fresh_count`，而不是只看绿色勾号**。
6. 把仓库的完整网页网址（例如 `https://github.com/你的用户名/hk143-price-screen`）发回当前 ChatGPT 对话，再把现有 8:30 定时任务的提示词指向真实仓库 URL。**在网址确认之前，ChatGPT 不会自动读取本仓库。**

GitHub 官方文档：
- [上传文件](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)
- [Scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [GITHUB_TOKEN/权限](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token)

### 已部署仓库的最新数据固定地址

- 浏览器查看（供人工检查，亦可供联网 AI 读取）：`https://github.com/Superxy/hk143-price-screen/blob/main/data/latest.md`
- 结构化完整数据：`https://raw.githubusercontent.com/Superxy/hk143-price-screen/main/data/latest.json`
- 纯文本日榜：`https://raw.githubusercontent.com/Superxy/hk143-price-screen/main/data/latest.md`

**注意**：以上链接已指向实际仓库。GitHub Raw 链接有时会有缓存延迟，晨报务必核对报告中的目标交易日。

### 自定义运行

GitHub Actions 的 **Run workflow** 为手工重试入口。也可以本地执行：

```bash
python -m pip install -r requirements.txt
python cloud_run.py --output .
```

`--asof 2026-10-13` 用于**指定日期重算或测试**，平时不加，不应对未来日期使用该参数。当前历史数据从2025年开始；2027年会自动将2026年设为“上一年度”。

## 固定的纯价格筛选口径

1. 上一年度低位：当前收盘价 `P` 相对**上一自然年度**不复权价格 `L=min(low)`、`H=max(high)`，区间位置 `(P-L)/(H-L)≤20%` 且 `P/L−1≤15%`；要求该自然年度所需港股交易日日线覆盖完整。
2. 年内低位：同样按当前年度1月1日至**最近已完成交易日**的 `L、H` 计算；年内交易日覆盖不全者，不认证“年内低位”。
3. 长期下跌：只筛 `P / C[t-120个港股交易日] − 1≤−20%`，若精确参考交易日数据缺失就不给交易日跌幅。
4. 阴线是 `收盘<开盘`；连续收盘下跌是 `今日收盘<上个交易日收盘`。两项**严格区分**。
5. **最新全量排序、不限制20只**：第一组上年+本年同时低位，第二组仅本年低位，第三组仅上年低位；三组互斥不重复，同组连续收阴≥3天优先，其次按本年价格区间位置（第三组按上年）升序。正式报告显示每组全部合格股票。
6. **不使用成交量、均线、MACD、RSI或基本面评分，也不编造反弹概率。** 缺少充足样本外回测时标题明确写“低位形态匹配榜（未经概率验证）”。

*年内价格可能跌破上一年度最低价，区间位置可以是负数；程序不会把“跌破”当作已确认支撑。若观察到大于45%的隔夜价格跳变，会提示可能存在公司行为或价格不连续并暂不入榜。此阈值是风险警戒，不是对拆合股的确认。*

## 运行限制和故障排查

- 新浪的公共接口可能因IP、频率、连接或供应商变更而失败；在 GitHub 云主机上的连通性**未经过真实验证**，必须手动触发首次运行确认。
- GitHub 的计划任务并非有服务等级保障的精准定时器，可能延迟或被跳过；公开仓库长期没有活动可能被自动停用计划任务。晚间+清晨两次可以增加成功率，但不能保证100%。
- 暂停交易或不同步的数据会被列为 `stale`，不会强行把最后旧K线当作目标日期。
- 即便 `fresh_count=143`，也只代表143只股票**在目标日期都有K线**，不代表全年日线、公司行为或历史数据完全无误。
- 不复权价格受拆股、合股、配股、除权等影响；只有可靠的公司行为信息确认可比后，才适合解释“多年低位”和反弹统计。
- GitHub Raw可能有缓存；同时查看 `data/latest.md` 的仓库页面与 `data/latest.json` 的日期。无法核实时不要输出新的“推荐”。
- GitHub 仓库若设为 **Private**，8:30任务不保证能联网读取；如希望保持私密，需要额外授权的存储/连接方式，而不是把私有访问令牌公开。
- 使用前自行确认数据供应商的接口使用规则和历史行情数据转存/公开传播许可；公开仓库会公开缓存的行情 CSV。若无法确认许可，先运行在私有仓库、只公开你自己计算的汇总结果，或改用获授权的数据源。

## 只读数据说明

工作流仅使用 GitHub 仓库自带的 `GITHUB_TOKEN` 写入**本仓库的 data/**，不会持有任何富途账号或券商密码；不读取/操作你的交易账户。所有财务和交易决策由你自行判断。
