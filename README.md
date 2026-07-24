# 交易所 ETF/上市基金做市公告日报工具

本工具按公告发布日期采集并解析：

- 上交所发布的 ETF/上市基金做市公告；
- 深交所披露的基金管理人流动性服务商公告。

工具生成 CSV 日报，并保存原始公告、规范化正文、SQLite 审计数据和运行日志。无结果日也会生成只有表头的 CSV。

## 输出字段

```text
公告发布日期 | 交易所 | 做市商 | 证券代码 | 生效日期 | 动作 | 服务类型原文 | 来源URL
```

动作统一为“新增、终止、调整”。服务类型保留交易所用语；深交所只写“流动性服务商”时，按业务口径输出为“一般流动性服务商”。

## 安装

要求 Python 3.10+。

Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

## 配置

复制示例配置并填写本机使用的参数：

Linux：

```bash
cp config.example.json config.json
```

Windows PowerShell：

```powershell
Copy-Item config.example.json config.json
```

大模型接口配置位于 `config.json` 的 `llm.providers`。`config.json` 包含本机密钥且已被 Git 忽略，请勿提交或分享。

## 手动运行

处理指定公告日：

```bash
./run.sh run --date 2026-06-08
```

```powershell
.\run.ps1 run --date 2026-06-08
```

不指定日期时，程序处理昨日、回看最近配置天数，并补齐遗漏日期：

```bash
./run.sh run
```

## 自动运行

Windows 可安装当前用户的计划任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_scheduled_task.ps1
```

删除计划任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\remove_scheduled_task.ps1
```

仓库还包含 GitHub Actions：

- `Daily report`：定时处理前一天公告，也可手动指定日期补跑；
- `Pages`：发布静态历史查询页。

生产配置通过仓库 Actions Secret `APP_CONFIG_JSON` 提供。静态页面源码位于 `site/`，报告索引可用以下命令更新：

```bash
python scripts/build_report_index.py
```

## 文件目录

```text
data/raw/    原始 HTML/PDF
data/text/   规范化正文和解析元数据
data/app.db  SQLite 公告、事件、来源和运行记录
reports/     CSV 日报和静态页面索引
logs/        运行日志
```

运行状态分为：

- `SUCCESS`：全部来源和候选公告处理成功；
- `PARTIAL`：部分来源、文件或抽取失败；
- `FAILED`：所有来源失败或发生未处理异常。

## 测试

```bash
python -m unittest discover -s tests -v
```

上交所、深交所使用官网当前接口；官网接口或页面改版后，可能需要更新对应的数据源适配器。
