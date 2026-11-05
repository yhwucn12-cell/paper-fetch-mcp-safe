# paper-fetch-mcp-safe

[中文](README.md) · [English](README_EN.md)

[![Tests](https://github.com/yhwucn12-cell/paper-fetch-mcp-safe/actions/workflows/tests.yml/badge.svg)](https://github.com/yhwucn12-cell/paper-fetch-mcp-safe/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

一个带**版权与许可边界**的论文检索、解析和开放 PDF 下载 MCP 服务。

- 搜索 OpenAlex、arXiv、PubMed / Europe PMC
- 解析 DOI、PMID、PMCID、arXiv、OpenReview、ACL、PMLR、HAL、Zenodo、OSF 标识符
- 下载前检查开放许可或 OA 依据；默认拒绝无法确认许可的候选
- 校验 PDF、计算 SHA-256，并生成可审计 JSON sidecar
- 支持单篇、按标题和最多 500 篇批量下载

> 默认不启用 Sci-Hub。完整版权策略与限制见 [COMPLIANCE.md](COMPLIANCE.md)。

## 快速开始

### Windows PowerShell

```powershell
git clone https://github.com/yhwucn12-cell/paper-fetch-mcp-safe.git
cd paper-fetch-mcp-safe
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
Copy-Item paper-fetch.toml.example paper-fetch.toml
```

### macOS / Linux

```bash
git clone https://github.com/yhwucn12-cell/paper-fetch-mcp-safe.git
cd paper-fetch-mcp-safe
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp paper-fetch.toml.example paper-fetch.toml
```

编辑 `paper-fetch.toml`，至少填写联系邮箱：

```toml
mailto = "you@example.org"
license_policy = "open"
allow_scihub = false
# download_directory = "downloads"
```

启动服务：

```powershell
.venv\Scripts\python.exe -m paper_fetch_mcp_safe.server --config paper-fetch.toml
```

macOS / Linux 使用：

```bash
.venv/bin/python -m paper_fetch_mcp_safe.server --config paper-fetch.toml
```

## MCP 客户端配置

将下列配置加入支持 stdio MCP 的客户端，并替换为实际绝对路径：

```json
{
  "mcpServers": {
    "paper-fetch-safe": {
      "command": "D:/path/to/paper-fetch-mcp-safe/.venv/Scripts/python.exe",
      "args": [
        "-m",
        "paper_fetch_mcp_safe.server",
        "--config",
        "D:/path/to/paper-fetch-mcp-safe/paper-fetch.toml"
      ]
    }
  }
}
```

服务提供两个工具：

| 工具 | 用途 |
|---|---|
| `execute_paper_request` | 搜索、解析、单篇下载和批量下载 |
| `get_server_status` | 查看 provider、限速状态和当前许可策略 |

## 配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `mailto` | 必填 | 提交给学术 API 的联系邮箱 |
| `download_directory` | 未设置 | 默认下载目录；也可在请求中传入 `directory` |
| `license_policy` | `open` | `strict`、`open` 或 `any` |
| `allow_scihub` | `false` | 启用 gray 来源；启动时会警告 |
| `openalex_api_key` | 空 | 可选 OpenAlex API key |
| `s2_api_key` | 空 | 可选 Semantic Scholar API key |
| `core_api_key` | 空 | 可选 CORE API key；未配置时自动跳过 CORE |
| `timeout_seconds` | `45.0` | HTTP 超时时间 |
| `host_rates` | 内置值 | 按主机设置请求速率 |

许可策略：

| 策略 | 接受范围 |
|---|---|
| `strict` | 明确开放许可或可信开放来源；始终拒绝 gray |
| `open` | 默认；在 `strict` 基础上接受 provider 的 OA 标记并附警告 |
| `any` | 接受无许可证据的非 gray 候选；仅在自备合法依据时使用 |

## 用法

`execute_paper_request` 接受结构化参数。`accepted=true` 只表示请求已执行，实际结果位于 `result`。

### 搜索论文

```json
{
  "action": "search",
  "query": "water oxidation catalyst",
  "source": "auto",
  "limit": 5,
  "year_min": 2022
}
```

### 检查开放 PDF

```json
{
  "action": "resolve",
  "identifiers": ["10.1021/jacs.4c00478"],
  "verbosity": "full"
}
```

### 下载单篇论文

```json
{
  "action": "download",
  "identifiers": ["arxiv:1706.03762"],
  "directory": "D:/papers"
}
```

也可按准确标题下载：

```json
{
  "action": "download",
  "query": "A Precisely Specified Article Title",
  "directory": "D:/papers"
}
```

唯一准确匹配会直接下载；无法唯一匹配时返回 `needs_review=true`。

### 批量下载

```json
{
  "action": "batch",
  "identifiers": [
    "10.1021/jacs.4c00478",
    "PMC10009416",
    "arxiv:1706.03762"
  ],
  "directory": "D:/papers",
  "concurrency": 4
}
```

批量请求支持 1–500 个标识符。默认返回摘要，逐项结果写入下载目录下的 `batch-results-*.json`。

## 下载结果

每个成功下载的 PDF 都会生成同名 `.pdf.json` sidecar，包含：

- 来源 provider、原始 URL 和文献标识符
- 原始及归一化许可、许可类别和判定依据
- 当前策略版本、PDF 版本、时间戳和 SHA-256

常见错误：

| 错误码 | 含义 |
|---|---|
| `PAYWALL_NO_OA` | 未找到开放 PDF 候选 |
| `LICENSE_RESTRICTED` | 找到链接，但没有足够许可依据或被策略拒绝 |
| `PROVIDER_UNAVAILABLE` | provider 请求失败，结论不完整 |
| `RATE_LIMITED` | provider 限流；可能包含 `retry_after` |
| `DOWNLOAD_CORRUPT` | 响应不是有效 PDF 或超过 200 MiB |

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m local_subprocess
.venv\Scripts\python.exe scripts\paper_download_test.py audit
```

实时 provider 测试：

```powershell
.venv\Scripts\python.exe -m pytest -q -m network
.venv\Scripts\python.exe scripts\paper_download_test.py search resolve download batch
```

## 更多文档

- [版权与许可边界](COMPLIANCE.md)
- [相对原项目的变更](NOTICE)
- [MCP skill](skills/paper-fetch-safe/SKILL.md)
- [来源和限速](skills/paper-fetch-safe/references/channels.md)
- [错误处理](skills/paper-fetch-safe/references/errors.md)

## 许可证

代码以 [MIT License](LICENSE) 发布。第三方论文与 API 仍受各自许可和服务条款约束；本软件不构成法律意见。
