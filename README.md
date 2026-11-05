# paper-fetch-mcp-safe

MCP 服务：检索论文元数据，解析**有许可依据**的开放 PDF 候选，下载并校验 PDF，同时写入包含来源、版本、许可、许可判定依据、URL 和 SHA-256 的 JSON 记录。

这是本地 `paper-fetch-mcp` 项目的版权安全分支：检索能力保留，下载侧增加强制许可边界。边界定义、判定规则与免责范围见 [COMPLIANCE.md](COMPLIANCE.md)，相对原项目的行为变更清单见 [NOTICE](NOTICE)。

## 版权安全边界（本分支的核心差异）

每个候选 URL 在**发出请求之前**要过两道独立检查：

1. **来源层级**：只接受 `oa`。`gray`（Sci-Hub 镜像）默认关闭，需显式 `allow_scihub = true` 才会被注册。
2. **许可依据**，至少满足其一：
   - 候选自带开放许可（CC0、CC BY、CC BY-SA、CC BY-NC、CC BY-ND、CC BY-NC-SA、CC BY-NC-ND）。各种写法（`"CC BY 4.0"`、`"cc_by"`、`creativecommons.org` URL）统一归一化后记录；
   - 来源本身只提供公开文件（`arxiv`、`europepmc`、`pmc_cloud`、`osf_preprints`、`zenodo`、`hal`、`preprints`）；
   - 仅在 `license_policy = "open"` 时，聚合器给出的 OA 标记（`gold`/`green`/`hybrid`/`bronze`/`diamond`），记为 `oa-declared` 并附警告——"能免费读"不等于"可再分发"。

OpenAlex、Unpaywall、Semantic Scholar、CORE 是聚合器，**不属于**可信来源：它们只是转发第三方链接，必须自带明确许可或 OA 标记。

拒绝是显式的：工具返回 `LICENSE_RESTRICTED` 与具体原因；`resolve` 在 `errors` 中逐条列出被拒候选；`batch` 单独统计 `license_restricted`，不与 `PAYWALL_NO_OA` 混淆。若同时有无关 provider 失败，结论回落到 `PROVIDER_UNAVAILABLE`——检索不完整时不会把拒绝说成最终判定。`search` 结果不会展示下载路径会拒绝的链接。

| `license_policy` | 接受范围 | 适用 |
|---|---|---|
| `strict` | 明确开放许可，或可信来源 | 边界最严，忽略 OA 标记；即使 `allow_scihub = true` 也拒绝 gray |
| `open`（默认） | 上述 + 聚合器 OA 标记（带警告） | 常规科研使用 |
| `any` | 除 gray 外全部接受 | 仅在自备合法依据时使用 |

策略在三处强制执行，任何路径都无法绕过：`router.resolve_candidates()` 过滤每个 provider 的输出；`ServerState.resolve()/download()` 再过滤记录级候选与延后/gray 轮次；`HttpClient.download_pdf()` 在建立连接前复核（或使用调用方传入的判定）。没有判定就无法写 sidecar，因此磁盘上的每个文件事后都可审计。

## 启动

需要 Python 3.11+。在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
Copy-Item paper-fetch.toml.example paper-fetch.toml
```

在 `paper-fetch.toml` 填写 `mailto` 联系邮箱，然后启动：

```powershell
.venv\Scripts\python.exe -m paper_fetch_mcp_safe.server --config paper-fetch.toml
```

`openalex_api_key`、`s2_api_key` 和 `core_api_key` 都是可选项。未配置 CORE key 时，CORE 自动跳过，状态接口会显示 `enabled: false`。`download_directory` 可设置结构化下载请求的默认目录，相对路径以配置文件所在目录为基准。本地配置文件由 Git 忽略。`allow_scihub` 默认为 `false`；设为 `true` 会在启动时打印警告，结果标记为 `gray`。`license_policy` 默认为 `open`；设为 `any` 同样会在启动时警告。`get_server_status().compliance` 会回报当前策略。

## 工具与标识符

MCP 只暴露两个工具：`execute_paper_request` 与 `get_server_status`。前者是自然语言经 skill 规范化后的统一入口：`action` 为 `search`、`resolve`、`download` 或 `batch`，检索填写 `query`，按标识符解析或下载填写 `identifiers`。按准确题名下载可填写 `action="download"` 和 `query`；`review_search_results=false` 为默认值，唯一准确匹配会直接下载，无法唯一匹配时返回 `needs_review=true`。工具先校验模板，再调用现有流程；`accepted` 仅表示输入已执行，实际结果在 `result` 中。模板及映射规则见 [paper-fetch-safe skill](skills/paper-fetch-safe/SKILL.md)。解析和下载支持 DOI、PMID、PMCID、arXiv ID，以及下表中的站内标识符。

`verbosity` 默认 `minimal`，只回模型实际需要的字段：`search` 回 `title`/`year`/`doi`/`id`（`id` 可直接回填下一次下载），`resolve` 回候选的 `provider`、`compliance_tier` 与 `license_class`，`batch` 回 `succeeded`/`failed`/`skipped`/`failures`/`license_restricted` 与逐项记录文件 `results_path`（`<directory>/batch-results-<UTC>-<pid>-<n>.json`，原子写入且并发不覆盖；写盘失败时回退为内联 `items` 并附 `note`；命中 gray 源时额外回 `gray` 计数与 `warning`）。单篇 `download` 始终回 `ok`/`path`/`sha256`/`provider`/`compliance_tier`/`license`/`license_class`/`license_evidence`。需要候选 URL、许可或逐项原始记录时传 `verbosity="full"`。错误只回归一化的 `code` 与一行 `message`，上游原文（凭据已脱敏为 `REDACTED`）写入服务日志。

下载侧的两条传输硬边界：单个响应超过 200 MiB（`http.MAX_DOWNLOAD_BYTES`，含 `Content-Length` 预检与流式累计）视为 `DOWNLOAD_CORRUPT` 并清理临时文件；指向本机或链路本地地址（loopback、`169.254.0.0/16`、`.localhost`）的候选 URL 在发请求前即被拒，因为候选 URL 来自第三方元数据。RFC1918 等私有段**刻意放行**——机构镜像、企业代理与 TUN/VPN 解析常落在那里。

| 输入示例 | 来源 |
|---|---|
| `10.1080/00220388.2011.561330`、`hal:hal-00721635` | HAL 主文件 |
| `10.5281/zenodo.1062358`、`zenodo:1062358` | Zenodo 公开论文记录 |
| `osf:hqb2p_v1`、`https://osf.io/preprints/socarxiv/hqb2p_v1/` | OSF Preprints 主文件 |
| `PMC10009416`、可映射到 PMCID 的 DOI | Europe PMC、PMC Cloud |

HAL、Zenodo、OSF 和 PMC Cloud 目前用于**精确标识符解析与下载**；自由文本搜索仍由 OpenAlex、arXiv 和 PubMed/Europe PMC 提供。Zenodo 的通用 DOI 搜索只接受 DOI 精确匹配或明确关联的论文记录，避免把数据集附件误当论文。下载器会拒绝 HTML 等非 PDF 响应。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp\pytest-local
.venv\Scripts\python.exe -m pytest -m local_subprocess -q --basetemp=.tmp\pytest-stdio
.venv\Scripts\python.exe -m pytest -m network -q --basetemp=.tmp\pytest-network
.venv\Scripts\python.exe scripts\paper_download_test.py audit
.venv\Scripts\python.exe scripts\paper_download_test.py search resolve download batch
```

`tests/test_compliance.py` 是边界的契约：许可写法归一化、三类许可依据、三档策略、gray 拒绝、下载器零请求拒绝、sidecar 字段、批量计数。它完全离线，不需要网络或 API key。

`scripts/paper_download_test.py` 的 `audit` 阶段同样离线，断言"无许可候选被拒且不落盘、gray 候选被拒、默认注册表不含 gray provider"。`search`/`resolve`/`download`/`batch` 阶段通过统一的 `execute_request` 入口跑真实站点，并对每个 PDF 独立校验 `%PDF` 头、SHA-256（返回值、sidecar、重算三方一致）、pypdf 页数、标题文本，以及 sidecar 中是否存在可接受的 `license_evidence`。结果写入 Git 忽略的 `.tmp/lit-download-test/`。

实时测试依赖站点和本机网络；OSF 的文件存储链路可能间歇性 TLS/超时，测试会将 API 不可用标记为跳过。

## 许可

本仓库以 MIT 许可发布，见 [LICENSE](LICENSE)。运行时调用的第三方服务（OpenAlex、Crossref、Unpaywall、Semantic Scholar、CORE、Europe PMC、PMC、arXiv、HAL、Zenodo、OSF、bioRxiv/medRxiv、OpenReview、ACL Anthology、PMLR）各自受其服务条款约束；本项目只读取其公开 API 以及这些 API 指明为开放的文件。

本软件不构成法律意见。CC BY-NC 限制商业使用、CC BY-ND 限制演绎、bronze OA 可能只允许阅读；工具会给出警告，但判断由使用者负责。
