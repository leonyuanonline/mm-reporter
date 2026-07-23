# 交易所 ETF/上市基金做市公告日报工具

本工具按公告发布日期采集并解析：

- 上交所本所发布的 ETF/上市基金做市公告；
- 深交所官网披露的基金管理人流动性服务商公告。

它只生成本地 CSV 日报，并保存原始 HTML/PDF、规范化正文、SQLite 审计数据和运行日志。无结果日也会生成只有表头的 CSV。

## 输出字段

```text
公告发布日期 | 交易所 | 做市商 | 证券代码 | 生效日期 | 动作 | 服务类型原文 | 来源URL
```

动作统一为“新增、终止、调整”。服务类型通常保留交易所用语；深交所明确写“主流动性服务商”时原样输出，只写未分级的“流动性服务商”时按业务口径输出为“一般流动性服务商”。规则证据和模型证据仍保存公告中的原始文字，便于审计。为兼容已有 CSV/XLSX 下游，列名暂时仍保留“服务类型原文”。

## 运行环境

- Windows 10/11
- Python 3.10+
- 基础依赖：lxml、pypdf
- 可选 OCR：PyMuPDF、rapidocr-onnxruntime

在当前 Codex 工作区中可直接使用 `run.ps1`，它会寻找可用的系统 Python，找不到时使用 Codex 自带运行时。

普通 Windows 环境建议安装到虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

如需处理没有文本层的扫描 PDF：

```powershell
python -m pip install -e ".[ocr]"
```

基础版本无需 OCR 也能处理上交所 HTML 和多数带文本层的深交所 PDF。

## 配置

当前工作区根目录已提供本机可编辑的 `config.json`；如果文件不存在，先从 `config.example.json` 复制一份。可以直接用记事本打开：

```powershell
notepad .\config.json
```

大模型接口全部使用 OpenAI-compatible `chat/completions` 协议。在 `llm.providers` 中可以配置任意多个接口：

```json
{
  "llm": {
    "enabled": true,
    "max_parallel_requests": 4,
    "providers": [
      {
        "name": "model_1",
        "enabled": true,
        "api_base": "https://api.openai.com/v1",
        "api_key": "填写第一个接口的密钥",
        "model": "填写模型名",
        "timeout_seconds": 90
      },
      {
        "name": "model_2",
        "enabled": true,
        "api_base": "https://第二个接口/v1",
        "api_key": "填写第二个接口的密钥",
        "model": "填写模型名",
        "timeout_seconds": 90
      }
    ]
  }
}
```

`api_base` 既可填写 `/v1` 基地址，也可直接填写完整的 `/chat/completions` 地址。接口请求会并行执行，并发上限由 `max_parallel_requests` 控制。`name` 必须唯一，只能使用 1 至 64 位字母、数字、点、下划线或连字符，用于审计日志和共识记录。

每个可用 `provider` 在共识中等权计一票。为提高独立性，建议选用不同模型或不同服务商；不要把同一个底层模型重复配置成多个接口来制造重复票。

程序不再读取 `LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL` 等环境变量；配置文件是唯一的大模型配置来源。`config.json` 已被 `.gitignore` 排除，但其中的密钥仍是本机明文，请勿提交、分享或附在排障材料中。受版本控制的 `config.example.json` 只含空密钥，可用于恢复配置结构：

```powershell
Copy-Item config.example.json config.json -Force
```

缺少 `api_base`、`api_key` 或 `model` 的接口会被跳过，并在日志中以黄色警告列出接口名和缺少的字段。没有可用模型时，工具会只执行规则抽取；也可以显式使用 `--no-llm`。旧版单模型 JSON 结构仍可读取，但原先放在环境变量中的密钥必须手动写入 `llm.api_key`；建议直接迁移到 `providers` 数组。

## 手动运行

处理指定公告日：

```powershell
.\run.ps1 --no-llm run --date 2026-06-08
```

使用已配置的大模型：

```powershell
.\run.ps1 run --date 2026-06-08
```

未指定日期时，处理昨日、回看最近配置天数，并自动补齐计划任务停机期间遗漏的日期：

```powershell
.\run.ps1 run
```

其他命令：

```powershell
.\run.ps1 export --date 2026-06-08
.\run.ps1 reprocess --exchange SZSE --announcement-id 26323676-172a-466b-8890-a4bdec6e60b0
.\run.ps1 audit --date 2026-06-08
.\run.ps1 test-history --from 2026-06-01 --to 2026-06-08
.\run.ps1 status
```

## 查看抽取审计

日报只展示最终业务字段。需要确认规则、各模型和最终共识为何得出某个结果时，使用只读的 `audit` 命令：

```powershell
.\run.ps1 audit --date 2026-06-08
```

输出按公告分组，依次展示：

- 公告原始文件、解析文本和解析警告；
- `RULE` 及每个大模型接口的调用状态；
- 模型返回的原始事件、通过原文证据校验的事件、被拒绝事件及原因；
- 逐字段投票/对账记录；
- 最终事件、证据、置信度、复核状态和警告。

默认每个公告只展示最新一次运行的三层快照，避免历史重跑结果重复。需要比较历次运行时追加 `--all-runs`：

```powershell
.\run.ps1 audit --date 2026-06-08 --all-runs
```

只查看一个交易所官方公告 ID：

```powershell
.\run.ps1 audit --date 2026-06-08 --announcement-id 26323676-172a-466b-8890-a4bdec6e60b0
```

输出机器可读 JSON（便于留档或二次分析）：

```powershell
.\run.ps1 audit --date 2026-06-08 --json > .\reports\audit_2026-06-08.json
```

`audit` 只读取本地 SQLite，不会重新访问交易所或调用模型，也不会输出大模型密钥。JSON 输出会对疑似密钥、令牌和鉴权字段再次脱敏。旧版本运行产生的公告可能没有逐抽取器快照，可重新运行历史测试或 `reprocess` 后再查看完整审计。

## 每日上午 9 点自动运行

安装当前用户的 Windows 计划任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_scheduled_task.ps1
```

计划任务启用 `StartWhenAvailable`。如果上午 9 点电脑未开机，下次可用时会运行；程序还会依据运行数据库补齐遗漏日期。删除任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\remove_scheduled_task.ps1
```

## 文件目录

```text
data/raw/       按交易所和日期保存的原始 HTML/PDF，内容修订时保留不同哈希版本
data/text/      正文、OCR结果和解析元数据
data/app.db     SQLite公告、事件、来源和运行记录
reports/        CSV 日报；运行状态、错误和统计保存在 SQLite 数据库中
logs/           运行日志；需复核事件以 WARNING 记录并在控制台显示黄色
```

运行状态可通过 `.\run.ps1 status` 查询，详细统计和错误保存在 SQLite 中：

- `SUCCESS`：两个来源清单及全部候选公告均成功处理；
- `PARTIAL`：来源、文件或候选抽取存在失败；命令返回非零退出码；
- `FAILED`：所有来源失败或发生未处理异常。

低置信或模型校验失败的事件仍进入 CSV 日报，并在日志中记录原因。

## GitHub Actions 与静态查询页

仓库内置两个自动工作流：

- `Daily report`：每天北京时间 03:17 处理前一天公告，也可手动指定日期补跑；
- `Pages`：当静态页面或历史报告变化时发布 GitHub Pages。

部署前在仓库的 `Settings > Secrets and variables > Actions` 中创建
`APP_CONFIG_JSON`，值为完整的生产配置 JSON。工作流只在临时运行环境中写入
`config.json`，不会提交密钥。随后在 `Settings > Pages` 中将 Source 设为
`GitHub Actions`。

历史查询页源码位于 `site/`，支持日期、交易所、动作、服务类型、做市商和证券
代码筛选。页面通过 `reports/index.json` 读取可用日期；更新索引可运行：

```powershell
python .\scripts\build_report_index.py
```

## 抽取与去重

规则抽取和各大模型抽取彼此独立，模型看不到规则或其他模型的结果。全部可用模型针对同一公告并发请求。系统优先用“做市商规范名 + 证券代码”对事件做逐接口一对一匹配；若其中一个身份字段有分歧，只有动作、日期、服务类型、基金名称等信息能唯一佐证时才合并投票，无法唯一匹配的结果会拆开并标记低置信。模型字段及其引文只有通过原文语义一致性校验后才能进入共识。

- 规则和所有模型对事件及核心字段完全一致时为 `HIGH / AUTO_ACCEPTED`；
- 存在严格多数但有漏报、缺字段、少数异议或接口失败时采用多数值，并标记 `MEDIUM / NEEDS_REVIEW`；
- 平票或没有严格多数时保留规则值（规则没有值时使用配置顺序中的首个有效模型值），标记 `LOW / NEEDS_REVIEW`；
- 接口请求失败视为弃权并降低置信度；请求成功但返回空事件是有效的“未发现事件”意见。

所有非 `HIGH` 结果都会在 XLSX 中整行标黄，警告中保留参与共识的接口名及分歧字段。服务类型最终仍保留公告原词，不会用内部分类替换。

公告按交易所官方 ID 幂等保存。业务事件按公告日、交易所、做市商、证券代码、生效日、动作和内部服务分类去重；同一事件的多个深交所官方链接会全部保留。

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前回归覆盖：鹏华多基金样例、PDF断词、上交所新增/终止、深交所裸括号代码、“选定”动作、服务等级调整、多接口配置、并发模型调用、严格多数/平票/接口故障共识、空日报、黄色复核行、幂等和候选抽取失败状态。

上交所、深交所使用的是官网当前内部接口。程序已做分页完整性、响应结构和文件格式校验，但官网接口或页面改版后仍可能需要更新相应 source adapter。
