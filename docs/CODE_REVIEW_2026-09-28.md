# 开发交接代码审查与收尾清单

审查日期：2026-09-28。代码基线：`42f133d`，工作区另有尚未跟踪的 `docs/PROJECT_HANDOVER.md`。

## 0. 四项问题修复追记（2026-09-28）

用户要求修复四项 P1 后，以下改动已实现并在工作区验证，尚未创建 Git 提交。第 1–7 节保留初审证据和当时建议，不能将其中的旧依赖版本、缺陷代码和测试计数当作修复后的状态。

| 问题 | 当前修复 | 回归证据 |
|---|---|---|
| R01 PDF 本地文件/URL 读取 | 原始 HTML 转义；HTML 标签/属性白名单；WeasyPrint 自定义资源获取器仅允许内嵌 PNG；禁止文件、HTTP(S)、SVG、任意 CSS 和附件；图片限制 12 MiB/1600 万像素，并检查 PNG 块与 CRC；导出校验 report_id | 真实 PDF 导出没有网络/文件资源请求、没有附件、正常 PNG 仍生成 PDF 图片对象；恶意协议、尺寸、损坏图片及路径穿越回归通过 |
| R02 Web 依赖漏洞 | FastAPI 0.121.3 + Starlette 0.49.3；显式声明 Starlette >=0.49.1；更新 uv.lock，仅新增 FastAPI 要求的 annotated-doc，未批量升级其他依赖 | 上传、SSE、鉴权报告下载与 Range 回归；干净核心环境启动；MCP stdio/Streamable HTTP 探针通过 |
| R03 Excel 行数上限绕过 | 按内容选择引擎；OOXML 重置 dimension 后流式检查实际行、列和矩形单元格；ZIP 解压预算先于加载；pandas 有界读取并二次检查，超限不发布数据集 | `.xlsx/.xls/.bin` 后缀、缺失/伪造 dimension、巨大的 nrows、稀疏行、压缩工作簿、宽表/单元格超限、API 413 与临时文件清理均覆盖 |
| R04 报告正文虚构数字 | 截图/写文件前校验报告标题、insights、图表标题/caption、统计解读；数值只能来自当前对话选定工件的类型化工具结果，不从旧解读或模型参数取证；成功 Artifact 保存来源数值 hash、匹配引用及 Markdown SHA-256 | 拒绝虚构数字、Markdown 格式拆分/零宽字符绕过、数字标题、图中虚构标题、未选分析和旧解读自证；真实 Agent 收到错误后修正，最终只发布一份有效报告 |

实现入口：[PDF 资源边界](../packages/common/report_safety.py)、[PDF/Markdown 组装](../mcp_servers/report/tools.py)、[Excel 限流](../mcp_servers/excel_parser/tools.py)、[报告数值校验](../apps/orchestrator/control/report_claims.py)、[报告发布封装](../apps/orchestrator/agent_tools.py)。

### 修复后验证

- 完整后端：**698 passed，3 skipped**，62.05 秒；比初审多 53 个通过用例。使用 baseline RAG、独立 `/tmp` 数据目录，在沙箱外运行。
- 三个跳过项均是“缺少 FlagEmbedding 时的行为”测试；当前环境已安装该依赖，不适用缺依赖分支，并非跳过本次安全回归。
- PDF 专项与原报告测试：23 passed；完整套件已包含这些测试。
- Ruff、`git diff --check`：通过。
- 通过 `uv sync --locked --no-dev` 在 `/tmp` 新建核心环境，API import/lifespan/readiness 烟测通过；未覆盖现有 `.venv-core`。
- MCP 官方 SDK 双传输探针：`equivalent=true`，协议 `2025-11-25`，包含认证、作用域、资源及会话关闭检查。
- MyPy 仍因已有 `types-PyYAML` 缺失失败（R09，检查 176 个源码文件），本轮未顺带扩大到类型检查整治。
- 未运行 Docker/Compose，也未重新跑前端构建/浏览器测试、真实付费模型或 BGE/GPU 质量评测；前端源码未改。第 2 节的前端结果来自初审轮。

### 部署与剩余边界

1. 接手者须同步新锁文件（按实际启用能力安装 extras），重建部署镜像；工作区修复不等于运行中的旧容器已更新。完整报告/截图能力仍需 `report`、`chart-screenshot` extras。
2. Excel 新默认预算是解压总量 256 MiB、1024 列、5000000 矩形单元格，行上限仍为 500000；配置见 `.env.example` 的 `EXCEL_MAX_*` 和 `LARGE_TABLE_ROW_THRESHOLD`。表头/前导行计入单元格预算，中间/格式化空行计入行预算；显式合法 nrows 可采样，但不绕过其他预算。真实旧格式 XLS 的支持问题 R07 仍未处理。
3. 这是确定性数值校验，不是业务语义验证：沿用现有数字提取、显示舍入/单位匹配及每份工件最多 256 个索引值的边界；数值匹配不证明因果关系、指标语义或文字结论正确。索引外数字、仅有文字来源的年份等可能被保守拒绝，需选择更直接的工具结果或调整报告。
4. 没有新增可硬终止的 Excel/PDF 子进程、总渲染时间/内存配额，也未进行全依赖漏洞扫描；应用级预算不等于处理任意恶意文件的完整沙箱。
5. **已有 Markdown/PDF 不会自动重写或重新验真**。本轮未读取、删除或修改现有业务报告；交接时应人工确认并重新生成需要沿用的报告。
6. R05–R09 和其他文档/产品收尾待办保持未关闭。本轮更新了交接文档中的解析/报告流程以及 README 导航，没有删除 Markdown 或创建提交。

## 1. 初审交接结论（修复前）

项目可以作为“单机、单租户技术预览”交给同事继续开发，主要业务链和自动化测试已经成形。但在处理真实敏感数据、扩大使用范围前，应先关闭本文四项 P1 问题：PDF 渲染读取本地文件/请求 URL、已知有漏洞的 Web 依赖、Excel 行数限制绕过、报告正文绕过数值证据校验。

现有交接文档写得详细，覆盖架构、17 个工具、SQLite v11、运行流程、部署、恢复及功能边界；主要缺口是部分结论过时、若干现行文档互相矛盾、缺少本次发现的安全问题，以及交接文档本身没有加入 Git 和文档导航。

本次没有确认应直接删除的项目 Markdown。29 份历史设计/实施文档可以继续作为档案保存，3 份知识语料有运行或评测用途。需要精简的是当前入口中的重复历史叙述，并统一现行事实。

初审轮只新增本审查报告；随后按用户要求进行的四项修复见第 0 节。

## 2. 初审范围与验证结果

检查了 API 鉴权与资源作用域、上传与文件路径、Agent/Verifier/报告链、MCP 服务边界、会话与恢复、知识库、前端认证与渲染、依赖锁、Docker/Compose/Nginx/CI，以及全部跟踪的 Markdown 和未跟踪的交接文档。采用全仓检索、关键调用链手审、现有测试及小规模复现；不代表逐行证明所有代码安全。

| 检查 | 本次结果 |
|---|---|
| 后端全部测试 | **645 passed，3 skipped**，60.03 秒；沙箱外运行，运行数据隔离到 `/tmp` |
| Python Ruff | `apps packages mcp_servers scripts tests` 全部通过 |
| Python MyPy | **失败**：`packages/governance/data_boundary.py:20` 缺少 `types-PyYAML`；共检查 174 个源码文件 |
| 前端 TypeScript / 构建 | `pnpm lint`、`pnpm build` 通过 |
| 前端构建体积 | 主 JS 1,432.04 kB，gzip 468.06 kB，仍有大包警告 |
| 前端浏览器协议测试 | **18 passed，1 skipped**；跳过的是需要 Compose 环境的恢复场景 |
| 真实 Web/API 测试 | **1 passed**；覆盖 Bearer 登录、记忆纠正/删除、Excel 上传、血缘查看；在临时源码副本运行 |
| 核心依赖环境启动烟测 | 既有 `.venv-core` 的 API import、lifespan、readiness 通过 |
| 安全/正确性复现 | PDF 本地文件附件、HTTP 取资源尝试、两种 Excel 行数限制绕过、报告虚构数字均复现 |
| Markdown 文件链接 | 审查开始时 45 份；对非代码块中的常规本地 Markdown 文件链接检查，未发现不存在的目标；未校验所有标题锚点和远程链接 |
| Docker/Compose 实测 | 未运行：当前 WSL 未启用 Docker Desktop 集成 |

后端运行使用 Python 3.11.15；已安装 FastAPI 0.115.14、Starlette 0.46.2、AnyIO 4.14.1、httpx 0.28.1、WeasyPrint 62.3。本次使用已有依赖环境，没有重新安装全部依赖；真实全栈测试副本复用了已安装依赖。

没有调用付费模型，没有验证真实 BGE/GPU 语义质量，没有重新核验文档中 GitHub Actions 历史运行。依赖漏洞核查覆盖下文明确列出的公告，不是全依赖、全镜像或 Git 全历史密钥扫描。

### 需要纠正的原测试结论

沙箱内后端测试确实会等待不结束，但以下不含项目代码的最小检查也会如此：

```python
import asyncio
import anyio
print(asyncio.run(anyio.to_thread.run_sync(lambda: 42)))
```

同一检查在沙箱外立即输出 `42`，随后完整后端测试通过。因此，本次可观察到的阻塞与执行沙箱有关，不能继续作为项目自身的 P0 缺陷。原交接文档第 15.4、18.4、20.1 节及 README 第 102 行附近应更新为本次证据；不应为了消除该现象而改动业务线程池逻辑。

## 3. 初审发现的代码与部署问题

P1 表示应在面向真实数据扩大使用前修复；P2 表示明确的功能、部署或维护缺口，应列入交接待办。本次没有把单机架构或未立项的企业功能自动列为 P0/P1。

| 编号 | 优先级 | 问题 | 证据类型 |
|---|---|---|---|
| R01 | P1 | PDF 导出可读取渲染进程可访问的本地文件并触发 URL 请求 | 临时文件实际复现 |
| R02 | P1 | 锁定的 Starlette 0.46.2 存在已公开拒绝服务漏洞 | 锁文件、安装版本及官方公告 |
| R03 | P1 | Excel 改后缀或去掉 dimension 可绕过行数上限 | 5 行合成工作簿实际复现 |
| R04 | P1 | 报告 insights 中的虚构数字未经过 Claim 校验 | 真实 Agent/工具/SQLite 链复现 |
| R05 | P2 | Nginx 默认 1 MiB 请求上限与 API 50 MiB 上传上限冲突 | 配置审查及官方默认值 |
| R06 | P2 | 生产环境文件未被 Git 忽略，存在误提交密钥风险 | `git check-ignore` 验证 |
| R07 | P2 | Web/API 接受旧 `.xls`，但运行依赖没有 `xlrd` | 代码、依赖清单和安装环境 |
| R08 | P2 | 已保存令牌失效后无法从界面返回登录 | 前端调用链审查 |
| R09 | P2 | MyPy 约定与依赖、CI 不一致 | 本次执行失败及 CI 配置 |

### R01 — 限制 PDF 渲染器的文件与网络访问

位置：[报告生成与导出](../mcp_servers/report/tools.py)，第 58–61、114–118 行；[Agent 报告封装](../apps/orchestrator/agent_tools.py)，第 1251–1257 行。

`title`、`insights`、caption 和数据字段进入 Markdown；Python Markdown 会保留输入中的 HTML。`weasyprint.HTML(string=html).write_pdf()` 没有提供受限 `url_fetcher`，也没有剥离附件标签和外部资源。

本次只使用新建的临时标记文件：在 `insights` 中放入指向该文件的 HTML 附件声明，导出的 PDF 确实包含该文件内容。另一个指向本机测试 URL 的图片引用触发了 HTTP 读取；请求在探针中被截获，没有真正连接服务。

这意味着“报告下载检查项目权限”仍不足以保护内容：用户可以合法下载自己的报告，但报告可能已经嵌入渲染进程有权限读取的其他文件。Compose 的 report-tools 挂载了数据库目录、报告卷和服务 secrets；只读挂载并不能防止读取。实际可读文件范围取决于部署文件权限，本次未读取任何真实密钥或他人数据。

建议：

- 将非格式化字段按文本转义，对允许的 Markdown/HTML 做标签与属性白名单；禁止附件、任意 CSS 和外部图片等入口。
- 为 WeasyPrint 配置明确的资源白名单，默认拒绝 `file:`、HTTP(S) 和其他协议；仅允许应用生成、大小受限的内嵌图像或受控资源 ID。
- 为渲染设置独立的内存、时间和输出大小限制；不能依赖 Agent 提示词约束。
- 验收应包含：HTML 附件无法读取临时标记文件、外部资源不发起请求、正常图表 PDF 仍可生成。

WeasyPrint 官方也明确提示，不可信 HTML/CSS 会引入安全问题，并提供自定义资源获取机制。见 [WeasyPrint 62.3 使用说明](https://doc.courtbouillon.org/weasyprint/v62.3/first_steps.html)。

### R02 — 更新 FastAPI/Starlette 的兼容依赖组合

位置：[依赖声明](../pyproject.toml)，第 17 行；[锁文件](../uv.lock)，第 2378 行附近；[报告下载](../apps/api/routers/report.py)；[文件上传](../apps/api/routers/upload.py)。

锁定并实际安装的 Starlette 是 0.46.2，落在以下官方公告的受影响范围：

- `GHSA-7f5h-v6xp-fcq8`：`FileResponse` 处理特定 Range 请求的复杂度问题，修复于 0.49.1。项目的报告下载实际使用 `FileResponse`。此项目先做报告权限检查，所以不能直接照搬公告的“任意未认证用户可触发”描述。[官方公告](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8)
- `GHSA-2c2j-9gv5-cj73`：multipart 文件从内存转存磁盘时可能阻塞事件线程，公告列出的修复版本为 0.47.2。项目使用 `UploadFile`。当前 Nginx 小体积限制会收窄部署路径上的暴露面，但不能作为依赖修复；直连 API 和放宽上传限制后都需要考虑。[官方公告](https://github.com/Kludex/starlette/security/advisories/GHSA-2c2j-9gv5-cj73)

本次没有对运行中的服务发送拒绝服务载荷。FastAPI 0.115.14 的依赖元数据要求 `starlette<0.47.0`，所以只强行覆盖 Starlette 版本会破坏声明的兼容关系。应一起调整 FastAPI 与相关依赖、重新生成锁文件，再跑上传、Range 下载、SSE、MCP 和完整回归，并加入依赖公告检查。

### R03 — 不应信任扩展名和工作簿自报的行数

位置：[Excel 解析](../mcp_servers/excel_parser/tools.py)，第 46–48、111–134 行；[上传允许后缀](../apps/api/routers/upload.py)，第 49 行。

`_guard_row_limit` 只检查 `.xlsx/.xlsm` 后缀，并在工作簿缺少 dimension 元数据时告警后放行。随后 `pd.read_excel` 没有默认限定读取行数，也没有读取后的硬上限检查。

本次把阈值设为 2，用 5 行合成数据验证：

| 输入 | 结果 |
|---|---|
| 正常 `.xlsx` | 正确拒绝 |
| 内容不变，只重命名为 `.xls` | pandas 自动识别真实格式并读入全部 5 行，绕过预检 |
| 移除 sheet XML 中的 dimension | 读入全部 5 行，绕过预检 |

因此 `.xls` 缺少 `xlrd` 并不能阻止第一种绕过，因为其实际内容仍是 XLSX。大文件可能造成内存和 CPU 压力；本次仅证明边界绕过，没有进行大文件耗尽试验。

建议按文件内容识别格式，默认至多读取 `limit + 1` 行并拒绝超限；同时限制解压总量、列数、单元格总量和解析时间。仅在 DataFrame 全部读入后检查行数，不足以防止读取阶段耗尽资源。验收需覆盖正常文件、后缀伪装、缺失/伪造 dimension 及压缩体积小但展开较大的文件。

### R04 — 报告正文也必须经过证据校验

位置：[报告参数传递](../apps/orchestrator/agent_tools.py)，第 1251–1257 行；[报告 insights 拼接](../mcp_servers/report/tools.py)，第 60–61 行；[最终 Claim 提取](../apps/orchestrator/agent_loop.py)，第 2441–2445 行；[报告有效性检查](../apps/orchestrator/control/verifier.py)，第 314–329 行。

模型传入的 `insights` 被直接写入报告。最终 Claim 提取只检查 `turn_text`，报告 Artifact 的验证主要检查文件、URL 和非空状态，并未验证报告中的叙述。

本次使用真实 Agent 主循环、真实工具注册表和报告生成、临时 SQLite，只替换模型为确定性脚本：

1. 建立包含 4 行画像和既有统计结果的合成对话。
2. 模型调用 `generate_report`，传入数据中不存在的“收入达到 987654321 万元”。
3. 模型最后只回复“报告已生成”。
4. 实际结果为 `run_status=completed`、报告存在、虚构数字保留、无 error 事件。

建议在报告发布前对所有生成式段落提取 Claim，并绑定被选中分析的真实数据 Evidence；不能把新生成报告本身当作这些数字的证明。报告文本、验证结果和内容 hash 应一起绑定，Markdown 与 PDF 使用同一已验证版本。将上述情形加入回归，确保无依据数字无法被导出或下载为已通过报告。

### R05 — 对齐容器入口与 API 上传限制

位置：[Nginx 配置](../apps/web/nginx.conf)，第 1–33 行；[Settings](../packages/common/config.py)，第 85 行。

API 默认为 50 MiB，Nginx 配置没有 `client_max_body_size`。按照 Nginx 的默认值，超过 1 MiB 的请求会先得到 413，API 的上传逻辑不会执行。这会造成“本机开发可上传、Compose 入口失败”。当前 Compose E2E 使用的小表不能覆盖此问题。[Nginx 官方默认值](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size)

这是根据仓库配置和官方默认值确认的配置缺口；本机无法启动 Docker，未实际向该容器发送 2 MiB 请求。

建议统一边缘层和 API 的限制，边缘层为 multipart 包装保留少量余量，同时保留后端文件大小检查。补一个通过 Nginx 上传超过 1 MiB 合法 XLSX 的测试，以及超过产品上限时的友好错误测试。

### R06 — 生产 `.env` 缺少 Git 忽略规则

位置：[Git 忽略规则](../.gitignore)，第 10–13 行；[生产部署步骤](./全栈部署与E2E.md)，第 47–50 行。

部署文档让开发者创建 `deploy/.env.production` 并填入 API、模型密钥。但 `.gitignore` 只忽略名为 `.env` 的文件，未覆盖 `.env.production`；`git check-ignore` 确认该路径不被忽略。按文档操作后执行 `git add .` 可能误提交真实凭据。

本次未发现或声称已发生真实生产密钥泄漏。仓库已有的 `deploy/secrets/*.dev` 明确是公开开发凭据，不应误报为泄漏。

建议忽略真实环境文件并明确保留 `*.example`，补相应 secret 文件规则或统一放到仓库外；交接打包时只包含示例配置。增加密钥扫描，并让面向外部地址的部署拒绝沿用公开开发凭据。

### R07 — 明确 Excel 格式支持与错误语义

位置：[上传 API](../apps/api/routers/upload.py)，第 49–50、73–80 行；[前端选择器](../apps/web/src/components/ChatPanel.tsx)，第 233 行；[依赖](../pyproject.toml)，第 30–33 行。

Web 和 API 都宣传接受 `.xls`，但依赖清单没有 `xlrd`，当前环境也没有。真实旧格式 XLS 需要该解析依赖，其缺失产生的 ImportError 不在当前解析异常映射中，可能成为 500。现有上传测试均以 XLSX 为主。

收尾时应选择并落实一个契约：只支持 XLSX 并统一 UI/API/文档，或补齐 XLS 依赖、文件内容识别和同等资源限制。损坏、加密、格式伪装等输入也应返回可理解的 4xx，并确认临时文件被清理。

### R08 — 增加令牌失效后的重新登录路径

位置：[App 登录状态](../apps/web/src/App.tsx)，第 18–27 行；[API 错误处理](../apps/web/src/api/client.ts)，第 60–81 行。

启动时只要 `sessionStorage` 存在令牌便进入工作区；API 返回 401 只转换成普通 Error，不清除令牌或通知 App 返回登录。前端也没有正常的退出/更换令牌入口。因此管理员轮换令牌或会话保留了旧令牌时，刷新仍会重复进入无法加载的工作区，用户需要清理浏览器存储或另开会话。

建议保留 HTTP 状态信息，对 401 统一退出当前身份、终止当前订阅、清理工作区缓存并回到登录页；增加退出/更换令牌入口。补充“已有无效令牌”和“运行中令牌失效”浏览器场景。

### R09 — 让类型检查能按交接命令执行

位置：[开发依赖及 MyPy 配置](../pyproject.toml)；[CI](../.github/workflows/ci.yml)；[README 检查命令](../README.md)，第 459–462 行。

本次 `mypy apps packages mcp_servers scripts` 在 YAML stub 上失败。README 列出了 `mypy .`，CI 实际只跑 Ruff，没有跑 MyPy。扫描整个仓库还会把本机运行目录纳入检查，原交接文档已记录 Milvus 卷权限问题。

建议把 `types-PyYAML` 纳入锁定的开发依赖，统一使用明确的源码目录，并在 CI 执行同一命令。前端名为 `lint` 的脚本实际是 TypeScript 类型检查；交接说明应明确这一点，不能让接手者误认为已有 ESLint 规则检查。

## 4. Markdown 文件取舍

审查开始时有 44 份受 Git 跟踪的 Markdown，加上未跟踪的交接文档，共 45 份；本报告是本次新增产物，不计入前述统计。

| 分类 | 数量 | 建议 |
|---|---:|---|
| 当前说明、运维、安全、交接及开发约束 | 13 | 保留，修正事实并减少重复状态播报 |
| `docs/v2.4/`、`docs/v2.5/` 历史设计与实施记录 | 29 | 保留为历史档案；不要求新同事逐篇阅读 |
| 知识库种子和评测干扰语料 | 3 | 保留，不能按“无人链接”删除 |

### 建议保留并作为接手入口

- 根 `README.md`：缩短为产品边界、启动方式、验证命令和导航。
- `docs/PROJECT_HANDOVER.md`：作为代码地图和维护手册，加入 Git 并从两级 README 链接。
- `docs/全栈部署与E2E.md`、`docs/本地完整BGE与Milvus启动指南.md`、`docs/知识库部署与运维.md`：分别负责根 Compose、本地完整 RAG、KB 运维。
- `docs/数据画像安全策略.md` 和 `docs/知识库升级验收基线.md`：保留为明确的安全/质量契约。
- `CLAUDE.md`：仍被根 README 和设计文档引用，属于项目开发约束，不是可随手删除的聊天残留。
- `deploy/secrets/README.md`：虽然没有普通 Markdown 入链，仍解释公开开发 secret 与生产 secret 的区别。

### 可以移出主阅读路线，但不建议直接删

`docs/v2.4/阶段0继续计划.md` 和 `docs/v2.4/Planner与Verifier评测设计.md` 包含已经删除的 Planner 脚本/旧评审入口，可标成“历史、命令不可直接运行”。检索到的失效代码引用包括 `scripts/agent_planner_eval.py`、`scripts/g7_review_gate.py`、`apps/orchestrator/control/planner_prompt.py`。

阶段 3–6 实施记录大量重复状态横幅和 CI 时间线，可统一由阶段索引承载当前状态，正文保留当时的实现与验证事实。若决定迁入 `docs/archive/`，应同时修复引用；目前目录已分版本，改路径本身不是交接前必做项。

没有发现必须立即删除的重复文件。把历史文档视为“无用”批量删除，会丢失 SQLite 迁移、恢复和安全约束的由来。

### 不能按普通文档删除

`docs/kb_samples/指标口径说明.md`、`docs/kb_samples/留存与转化说明.md` 是种子语料；`scripts/kb_eval_distractors.md` 是评测语料。`scripts/kb_eval.py:102–103` 实际读取它们，Dockerfile 也复制种子目录。不要给这些文件加项目进度说明，避免污染检索与质量指标。

`.data/reports/*.md`、依赖目录内的 README、Playwright 报告等属于运行或依赖产物，不属于交接文档目录；不要将整个工作目录直接打包给同事。

## 5. 交接说明的详细程度与一致性

### 详细程度：主体足够，缺少可执行的交付封面

`PROJECT_HANDOVER.md` 已有 1,044 行，架构、调用链、工具、迁移、部署和调试入口足够详细。继续增加大段历史叙述的收益有限；建议在开头增加一页：交接 commit/tag、接收人、运行档位、已知问题负责人、验收命令、数据迁移选择、外部资源访问权和回滚位置。

它目前未被 Git 跟踪，也没有来自其他 Markdown 的普通链接。使用仓库克隆或 `git archive` 交接时，这份最重要的说明不会进入交付物。

### 必须同步修正的事实

| 文件/位置 | 不一致或缺口 | 应调整为 |
|---|---|---|
| `PROJECT_HANDOVER.md` §15.4、§18.4、§20.1；README 第 102 行附近 | 将本机测试等待列为未关闭重点问题，甚至 P0 | 本次沙箱内外对照、645 passed/3 skipped；保留具体运行环境，移除“项目回归未收敛”的现行结论 |
| `PROJECT_HANDOVER.md` §5.4、§6.4、§12、§18 | 描述 Evidence 与报告安全，但遗漏报告正文绕过及 PDF 资源访问 | 明确区分聊天正文校验与报告正文当前缺口，加入 R01/R04 |
| `PROJECT_HANDOVER.md` §18.4 | 单实例和企业身份都列成 P1 | 放入“部署限制/扩展项目”；当前定位就是单机单租户，不能与可复现漏洞混排 |
| `docs/知识库升级验收基线.md:34` | 阶段 5/6 增量统称“尚未实现” | 版本化定义与目录冻结等已有实现，应区分工程完成、语义等价未验证及未来项 |
| 同文件第 32、42 行 | CPU/GPU 等价仍写待验收，阶段 8 仍要求跨租户测试 | 对齐根 README：等价签字取消且未验证，完整多租户已取消；不能继续写成承诺中的阶段任务 |
| `docs/全栈部署与E2E.md:143` | “租户级隔离留在后续阶段” | 对齐单租户范围和已取消的完整多租户计划 |
| 同文件第 26 行及备份说明 | 仍写“六个独立卷”、`chatbi-kb` | 对齐实际 9 个命名卷，明确 KB index/source/backup 与 model-cache 的备份职责 |
| `docs/ChatBI设计文档.md:306` | 将浏览器池化写进已完成 v2.4 容器化叙述 | 当前 `renderer.py` 每次创建浏览器，池化只是后续优化 |
| `.env.example` 末尾 | 预留变量说明写“Settings 已定义” | Redis/Postgres/MinIO 等部分字段未被 Settings 消费；标为未接入的设计占位或从启动样例移除 |
| README 的测试段 | 简写 `uv run pytest` / `mypy .`，没有说明测试 extras 与 full-profile 干扰 | 给出与 CI 一致的完整依赖组、baseline 环境和源码范围命令，说明后续 `uv run` 的同步行为 |

历史实施记录中的旧 Planner、审批流和三档模式可以保留，但需要局部历史标记。不能把它们和当前的确定性提纲、单 Agent 执行、无公开审批产品流混为一套运行规范。

## 6. 可延后优化与明确边界

这些事项有维护价值，但不能替代 R01–R04 的修复：

- **前端加载**：ECharts 目前整包导入；按图表、报告、治理面板的使用时机拆包，并测首屏体积和交互延迟。
- **代码复杂度**：`agent_loop.py` 5,428 行，`task_store.py` 6,136 行，前端 `workspace.ts` 1,103 行。按执行、恢复、验证、事件持久化拆分时，先保留现有事务和状态不变量；避免交接前大改整套架构。
- **图表资源**：截图每次启动 Chromium，散点和 `agg=none` 缺少系统性的降采样。用数据规模和并发指标决定池化/下采样，不能仅靠浏览器端截断。
- **文件一致性**：sidecar 采用直接覆盖，部分数据库删除后的文件清理是 best-effort。可加入原子替换、待清理记录和定期对账。
- **运营控制**：现有单任务预算不等于全实例并发/用户配额。内部使用量增加时，需要运行数、暂停任务保留时间、上传/渲染队列和磁盘容量门限。
- **管理体验**：领域定义没有管理 UI；知识库按钮未充分按角色裁剪。后端权限仍须保留，界面补齐用于减少无权限操作和支持成本。
- **业务质量**：工程测试与确定性模型 fixture 不能代表真实供应商模型的业务成功率。应在同事将实际使用的模型、数据和问题集上复测聊天、澄清、统计、图表、报告与失败说明。

单 API worker、SQLite/本地文件、实例共享 KB、静态 Bearer、无任意 SQL/代码执行，都属于当前明确范围。若同事只延续单机技术预览，不必先完成 OIDC、多实例、对象存储或多 Agent 才能交接。

数据策略仍有已约定的助手通道例外、聚合预览/原始点边界，不能写成“任何场景均只传脱敏聚合数据”。交接时应指向现有安全策略并明确允许进入模型的内容；本次没有擅自修改该产品决策。

## 7. 初审建议收尾顺序与验收条件

### 交接前整理

1. 审阅后将 `PROJECT_HANDOVER.md` 与本报告纳入版本控制，并补 README 导航；固定交付 commit/tag。
2. 更新第 5 节列出的矛盾，把 R01–R09 转成有负责人、优先级和验收条件的待办。
3. 修正生产环境文件忽略规则；交付源码、锁文件、示例配置和匿名样例，真实密钥经独立渠道配置。
4. 明确同事是空工作区启动还是恢复现有数据。恢复时按现有离线流程备份 DB/Dataset/Artifact，KB 原文和索引另行处理，并在目标机器演练恢复。

### 扩大使用前修复

1. R01 PDF 资源访问限制。
2. R02 兼容依赖升级与公告检查。
3. R03 上传格式识别和实际资源上限。
4. R04 报告正文证据校验。
5. 一并处理 R05 上传入口、R07 格式支持、R08 令牌恢复和 R09 类型检查。

### 交付验证

- 后端测试、Ruff、MyPy、前端类型检查和构建通过；跳过项逐项说明。
- 浏览器协议测试及真实 Web/API 测试通过。
- Docker 可用的机器上重跑 Compose 上传、报告、重启与离线恢复门禁；包含超过 1 MiB 的合法文件。
- 新安全回归明确验证“本地附件不能嵌入、URL 不会被请求、行数绕过拒绝、虚构报告数字拒绝”。
- 若交付完整 RAG，额外验收模型权重、Milvus 权限、索引重建/备份和目标机器的语义质量。
- 记录测试对应的提交、配置档位、依赖、输出和已接受限制。历史 CI 链接作为历史证据保存，不代替新交付提交的验收。

验收后可将本报告归入历史审查档案；长期维护的入口应是精简 README、更新后的交接手册和实际待办，避免继续累积多个相互竞争的“当前状态”。
