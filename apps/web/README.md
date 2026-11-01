# Paper2Run Web（apps/web）

论文复现工作台前端。当前阶段：**F1 GUI Mock 完成**（四视图 + 路由 + 唯一 api-client + M01–M12 Mock 场景），尚未进入 live 联调。

- 契约：`docs/engineering/09-前后端公共接口契约.md` v0.2（DTO 逐字转录于 `src/api-client/types.ts`，后端交付 OpenAPI 生成类型后替换）
- 页面与状态规范：`docs/engineering/10-前端GUI交接说明.md`
- 视觉与工程约束：工作区根 `AGENTS.md`（前端 Agent 宪法）及 `docs/12-前端宪法.md`（注释版）

## 启动

```bash
cd apps/web
pnpm install
pnpm dev        # http://127.0.0.1:5173 （dev proxy /api/v1 -> 127.0.0.1:8000）
pnpm build      # tsc --noEmit + vite build
pnpm typecheck
```

数据模式由 `VITE_DATA_MODE` 控制（`mock` 默认 / `live` / `replay`），切换只在 `src/api-client/config.ts` 层发生，组件不感知。mock 模式登录：任意非空邀请码。

## Mock 场景（右上角切换，仅 mock 模式）

| 场景 | 入口 | 必须看到 |
|---|---|---|
| M01 空项目 | /projects | 空态与上传引导，无假历史 |
| M02 上传/解析中 | 解析页 | 202 + 轮询，无假百分比（约 45 秒后 ready，新上传约 25 秒） |
| M03 解析失败/不支持 | 解析页 | 错误与支持范围，不进入执行页 |
| M04 缺参数/冲突 | 解析页 | blocking 禁确认；首次保存返回 409，"加载最新版本"后重存成功 |
| M05 排队至执行 | 执行页 | 快照驱动 queued→generating→executing，不自动假成功 |
| M06 一次修复 | 执行页 | attempt1 失败证据 + 补丁 diff + attempt2（约 30 秒触发） |
| M07 修复耗尽 | 执行页 | REPAIRS_EXHAUSTED 终态与 5 次尝试证据 |
| M08 取消竞态 | 执行页 | cancel_requested →（约 5 秒）完成胜出，报告延迟 |
| M09 会话过期 | 顶栏按钮 | 下一次请求 401 → 登录 → 重登回原页状态保留 |
| M10 报告延迟 | 报告页 | "报告生成中"pending 卡（约 40 秒后 ready，不显示假分） |
| M11 不可比结果 | 报告页 | total=null 显示"不可比较"、差异"不可比较"、仅实测曲线 |
| M12 导出部分失败 | 报告页 | 历史 ZIP/JSON 可下载、PDF 失败不遮挡；新建导出排队→部分完成 |

## 结构

```text
src/
  app.tsx               路由 + 会话门 + 401 全局处理
  api-client/           唯一请求层：types(契约) / client(live) / mock(adapter+fixtures+scenarios) / config
  components/           TopBar / StationBar(装置条) / SideNav / StatusTag / Feedback(错误/空态/骨架)
  features/             auth / projects / papers(解析确认) / runs(执行) / reports(报告)
  formatters/           micros→元、percent/pp、字节、时长、相对时间（null 语义集中于此）
  styles/               tokens.css(原型 :root 唯一色板) + global.css
```

关键实现约束（宪法摘录）：组件只经 api-client 取数；装置条状态只由快照派生；null 显示"未知/未测/不可比较"；mock 计时器只存在于 mock adapter；动效仅 transform/opacity 且支持 reduced-motion；`completed ≠ matched`。

## 已验证（2026-09-25，浏览器实测）

- `pnpm build`（tsc 0 错误）通过；dev server 启动正常。
- 走查通过：登录/退出、M01–M12 全场景、上传→解析→创建实验→保存(409)→确认→运行→报告→导出全链路、M09 会话过期恢复、390px 窄屏三页无横向滚动、取消按钮可达。

## 未完成 / 后续

- SSE 订阅（当前按契约先做 3 秒 polling；F4 接 events 流与断线恢复）。
- 真实 PDF 预览（PDF.js + bbox 高亮跳页；当前 content_available=false 时按契约展示摘录）。
- Playwright 自动化测试与 M01–M12 录制回放。
- live 联调（F2/F3）：live client 已实现同源 `/api/v1` 调用与错误映射，幂等键当前为每次生成新 UUID，F3 联调时按"同一用户操作复用 key"细化到调用方。
- mock 下载为文本占位文件（标注 mock），非真实产物。
