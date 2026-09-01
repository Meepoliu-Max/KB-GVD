# Changelog

本项目的所有重要变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-08-21

### 新增

- **用户认证体系**：pbkdf2 密码哈希 + HMAC 签名令牌（零第三方依赖），Cookie / Bearer 双通道认证。
  - 前台登录页 `/login`、后台登录页 `/admin/login`。
  - `REQUIRE_LOGIN=false`（默认）开放模式直接可用；开启后前台页面与业务 API 未登录跳转或返回 401。
- **管理后台**（`/admin/*`，需 super_admin / admin 角色）：
  - 仪表盘：用户/任务/Token 统计卡片、每日任务趋势折线图、Token 消耗柱状图（SVG 动态渲染）、用户消耗排行。
  - 用户管理：列表/搜索/筛选、添加用户、启禁用、重置密码、用户详情（消耗统计 + 近 7 天 Token 趋势 + 最近任务）。
  - 权限保护：普通管理员不能操作超级管理员账号；最后一个超级管理员不可禁用/降级/删除；不能对自己执行禁用/删除。
- **任务归属与 Token 统计**：上传自动关联 user_id；任务完成后回写 LLM Token 消耗（按任务持久化）；任务列表按用户隔离（普通用户仅见自己的任务）。
- **CLI 命令** `kbrefiner createsuperuser`：交互式创建首个超级管理员账号。
- 测试：新增 61 项认证/权限/页面守卫/CLI 测试，全量 316 项通过。

### 修复

#### BUG-1: 升级后浏览器命中旧版静态 JS，登录页报 `Cannot read properties of undefined (reading 'login')`

- **现象**：管理后台浏览器实测中，首次登录点击按钮无响应，控制台报
  `TypeError: Cannot read properties of undefined (reading 'login')`（源自 `KBRefiner.Auth.login`）。
- **根因**：服务端 `/static/js/kbrefiner.js` 已更新（新增导出 `Auth` / `Admin` 模块，14329 → 16671 字节），
  但浏览器命中旧版 HTTP 强缓存（DevTools 中 `transferSize: 0` 证实未走网络）。
  旧版未导出 `Auth`，页面调用 `KBRefiner.Auth.login` 即抛 undefined。
  **所有从旧版本升级的用户都会踩到**，且表现为"登录按钮坏了个功能"而非缓存问题，排查成本高。
- **修复方案**（[main.py](kbrefiner/main.py)）：新增 HTTP 中间件，对 `/static/*` 与所有 `text/html` 响应
  返回 `Cache-Control: no-cache`——允许浏览器缓存但每次发起协商请求（304 校验），
  兼顾加载性能与版本一致性。相比 `no-store` 避免了每次全量下载。
- **验证**：curl 确认 `/static/js/kbrefiner.js` 与 `/admin/login` 响应头均含 `cache-control: no-cache`；
  浏览器强制刷新后登录流程正常。

#### BUG-2: 用户列表页禁用用户后，"启用"按钮同样被禁用，无法就地恢复

- **现象**：管理后台用户列表点击"禁用"成功后，该行的"启用"按钮渲染为 `disabled`，
  只能进入用户详情页才能重新启用，列表页操作形成"单行死锁"。
- **根因**：[admin-users.html](kbrefiner/static/admin-users.html) 渲染行按钮时的权限判断写反了保护意图：
  ```js
  const canToggle = u.status === 'active' && !isSelf;  // 错误
  ```
  该条件在用户处于 `active` 时才允许操作（本意是防重复禁用），但"启用"操作恰恰发生在
  `disabled` 状态下，导致需要操作的按钮反而永远不可用。
- **修复方案**：操作保护只需排除操作者自身（防止管理员误禁用自己），状态翻转双向放开：
  ```js
  const canToggle = !isSelf;  // 仅自身禁改
  ```
  后端已有兜底：`PATCH /api/admin/users/{id}` 对"禁用自己"返回 400，
  前端补渲染保护即可，无需重复校验状态。
- **验证**：浏览器实测列表页禁用 → 启用完整往返成功；自身行的启停按钮仍为置灰。

### 性能

- Stage 1/2 分段并行：800KB PDF 全流水线 2m13s → 75s（详见 `docs/opensource/README.md` 权衡说明）。

## [1.0.0] - 2026-08-20

- 首个正式版本：RAG 四阶流水线（Clean → Chunk → QA → Tag）、Web UI、CLI、SDK、
  导出格式（coze_qa / coze_text / dify_qa / dify_text / dify_jsonl / json）、断点续跑。
