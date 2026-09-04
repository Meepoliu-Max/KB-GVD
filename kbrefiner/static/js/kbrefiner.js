/**
 * KBRefiner 前端应用核心逻辑
 * 提供 API 客户端、WebSocket 管理、UI 工具函数
 */
const KBRefiner = (() => {
  'use strict';

  // ===== API 客户端 =====
  const API = {
    /** 上传文件 */
    async upload(file) {
      const form = new FormData();
      form.append('file', file);
      const resp = await fetch('/api/upload', { method: 'POST', body: form });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || `上传失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 发起处理（同步模式） */
    async processSync(fileId) {
      const resp = await fetch(`/api/process?file_id=${fileId}`, { method: 'POST' });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || `处理失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 发起处理（异步模式，配合 WebSocket） */
    async processAsync(fileId) {
      const resp = await fetch(`/api/process?file_id=${fileId}&async_mode=true`, { method: 'POST' });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || `启动失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 查询任务状态 */
    async getStatus(taskId) {
      const resp = await fetch(`/api/status/${taskId}`);
      if (!resp.ok) throw new Error(`查询状态失败 (${resp.status})`);
      return resp.json();
    },

    /** 获取最终结果 */
    async getResult(taskId) {
      const resp = await fetch(`/api/result/${taskId}`);
      if (!resp.ok) throw new Error(`获取结果失败 (${resp.status})`);
      return resp.json();
    },

    /** 获取任务列表 */
    async listTasks() {
      const resp = await fetch('/api/tasks');
      if (!resp.ok) throw new Error(`获取任务列表失败 (${resp.status})`);
      return resp.json();
    },

    /** 取消任务 */
    async cancelTask(taskId) {
      const resp = await fetch(`/api/task/${taskId}/cancel`, { method: 'POST' });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || `取消任务失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 导出下载地址（浏览器直接访问即触发下载） */
    exportUrl(taskId, format) {
      return `/api/export/${taskId}?format=${encodeURIComponent(format)}`;
    },
  };

  // ===== 认证与管理后台 API =====
  const Auth = {
    /** 登录（成功后服务端设置 HttpOnly Cookie） */
    async login(email, password) {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `登录失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 登出 */
    async logout() {
      const resp = await fetch('/api/auth/logout', { method: 'POST' });
      if (!resp.ok) throw new Error(`登出失败 (${resp.status})`);
      return resp.json();
    },

    /** 当前用户（开放模式未登录返回 {user: null}） */
    async me() {
      const resp = await fetch('/api/auth/me');
      if (!resp.ok) {
        if (resp.status === 401) return { user: null, require_login: true };
        throw new Error(`获取用户信息失败 (${resp.status})`);
      }
      return resp.json();
    },
  };

  const Admin = {
    async _json(resp, action) {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `${action}失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 仪表盘统计 */
    async stats() {
      return this._json(await fetch('/api/admin/stats'), '获取统计');
    },

    /** 用户列表（支持 search/role/status 筛选） */
    async listUsers(params = {}) {
      const qs = new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== '' && v != null)
      ).toString();
      return this._json(await fetch(`/api/admin/users${qs ? `?${qs}` : ''}`), '获取用户列表');
    },

    /** 创建用户 */
    async createUser(payload) {
      return this._json(await fetch('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }), '创建用户');
    },

    /** 用户详情（含统计与最近任务） */
    async getUser(userId) {
      return this._json(await fetch(`/api/admin/users/${userId}`), '获取用户详情');
    },

    /** 更新用户（用户名/邮箱/角色/状态） */
    async updateUser(userId, payload) {
      return this._json(await fetch(`/api/admin/users/${userId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }), '更新用户');
    },

    /** 重置密码 */
    async resetPassword(userId, newPassword) {
      return this._json(await fetch(`/api/admin/users/${userId}/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: newPassword }),
      }), '重置密码');
    },

    /** 删除用户 */
    async deleteUser(userId) {
      return this._json(await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' }), '删除用户');
    },

    /** 全部任务（管理视角） */
    async listTasks() {
      return this._json(await fetch('/api/admin/tasks'), '获取任务列表');
    },
  };

  // ===== 消息管理 =====
  const Messages = {
    async _json(resp, action) {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `${action}失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 管理端消息列表（type/status/search 筛选） */
    async list(params = {}) {
      const qs = new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== '' && v != null)
      ).toString();
      return this._json(await fetch(`/api/admin/messages${qs ? `?${qs}` : ''}`), '获取消息列表');
    },

    /** 创建消息（send_now 立即发送 / scheduled_at 定时 / 否则草稿） */
    async create(payload) {
      return this._json(await fetch('/api/admin/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }), '创建消息');
    },

    /** 编辑草稿 */
    async update(id, payload) {
      return this._json(await fetch(`/api/admin/messages/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }), '编辑消息');
    },

    async send(id) {
      return this._json(await fetch(`/api/admin/messages/${id}/send`, { method: 'POST' }), '发送消息');
    },

    async revoke(id) {
      return this._json(await fetch(`/api/admin/messages/${id}/revoke`, { method: 'POST' }), '撤回消息');
    },

    async cancelSchedule(id) {
      return this._json(await fetch(`/api/admin/messages/${id}/cancel`, { method: 'POST' }), '取消定时');
    },

    async remove(id) {
      return this._json(await fetch(`/api/admin/messages/${id}`, { method: 'DELETE' }), '删除消息');
    },

    /** 用户端：当前可见的已发送消息 */
    async mine(limit = 10) {
      return this._json(await fetch(`/api/messages?limit=${limit}`), '获取消息');
    },
  };

  // ===== 黑白名单 =====
  const Access = {
    async _json(resp, action) {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `${action}失败 (${resp.status})`);
      }
      return resp.json();
    },

    /** 全量规则 + 开关状态 */
    async get() {
      return this._json(await fetch('/api/admin/access'), '获取访问规则');
    },

    /** 启用/停用某类校验（仅超级管理员） */
    async toggle(type, enabled) {
      return this._json(await fetch('/api/admin/access/toggle', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, enabled }),
      }), '切换开关');
    },

    /** 添加规则（type: ip_whitelist / domain_whitelist / user_blacklist） */
    async addRule(type, value, note = '') {
      return this._json(await fetch('/api/admin/access/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, value, note }),
      }), '添加规则');
    },

    async removeRule(ruleId) {
      return this._json(await fetch(`/api/admin/access/rules/${ruleId}`, { method: 'DELETE' }), '删除规则');
    },
  };

  // ===== 系统设置 =====
  const Settings = {
    async _json(resp, action) {
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `${action}失败 (${resp.status})`);
      }
      return resp.json();
    },

    async get() {
      return this._json(await fetch('/api/admin/settings'), '获取系统设置');
    },

    /** 部分更新：仅提交需要变更的分组 */
    async update(payload) {
      return this._json(await fetch('/api/admin/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }), '保存设置');
    },
  };

  // ===== WebSocket 管理 =====
  class WSClient {
    constructor(taskId) {
      this.taskId = taskId;
      this.ws = null;
      this._heartbeat = null;
      this._callbacks = {};
    }

    connect(callbacks = {}) {
      this._callbacks = callbacks;
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const url = `${protocol}//${window.location.host}/api/ws/${this.taskId}`;

      this.ws = new WebSocket(url);

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          switch (msg.type) {
            case 'progress':
              this._callbacks.onProgress?.(msg);
              break;
            case 'result':
              this._callbacks.onResult?.(msg.result);
              this.close();
              break;
            case 'error':
              this._callbacks.onError?.(msg.error);
              this.close();
              break;
            case 'pong':
              break;
          }
        } catch (e) {
          console.error('WS message parse error:', e);
        }
      };

      this.ws.onclose = () => {
        this._stopHeartbeat();
        this._callbacks.onClose?.();
      };

      this.ws.onerror = (e) => {
        console.error('WebSocket error:', e);
        this._callbacks.onError?.('连接异常');
      };

      // 心跳
      this._startHeartbeat();
    }

    _startHeartbeat() {
      this._stopHeartbeat();
      this._heartbeat = setInterval(() => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send('ping');
        }
      }, 30000);
    }

    _stopHeartbeat() {
      if (this._heartbeat) {
        clearInterval(this._heartbeat);
        this._heartbeat = null;
      }
    }

    close() {
      this._stopHeartbeat();
      if (this.ws) {
        this.ws.close();
        this.ws = null;
      }
    }
  }

  // ===== 工具函数 =====
  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    let size = bytes;
    while (size >= 1024 && i < units.length - 1) {
      size /= 1024;
      i++;
    }
    return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
  }

  /** 将 Pipeline 结果渲染到页面 */
  function renderPipelineResult(result, container) {
    if (!result || !container) return;

    const r = result;
    const docInfo = r.document_info || {};
    const quality = r.quality_summary || {};
    const atoms = r.knowledge_atoms || [];

    // 汇总统计
    container.innerHTML = `
      <div class="kb-summary-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px;">
        <div class="kb-summary-item" style="text-align:center;padding:16px;background:var(--kb-neutral-50);border-radius:var(--kb-radius-medium);">
          <div class="kb-summary-value" style="font-size:1.5rem;font-weight:700;color:var(--kb-neutral-900);">${docInfo.total_chunks || 0}</div>
          <div class="kb-summary-label" style="font-size:0.8125rem;color:var(--kb-neutral-500);margin-top:4px;">知识原子</div>
        </div>
        <div class="kb-summary-item" style="text-align:center;padding:16px;background:var(--kb-neutral-50);border-radius:var(--kb-radius-medium);">
          <div class="kb-summary-value" style="font-size:1.5rem;font-weight:700;color:var(--kb-neutral-900);">${docInfo.total_qa_pairs || 0}</div>
          <div class="kb-summary-label" style="font-size:0.8125rem;color:var(--kb-neutral-500);margin-top:4px;">QA 对</div>
        </div>
        <div class="kb-summary-item" style="text-align:center;padding:16px;background:var(--kb-neutral-50);border-radius:var(--kb-radius-medium);">
          <div class="kb-summary-value" style="font-size:1.5rem;font-weight:700;color:${quality.needs_review ? 'var(--kb-state-warning)' : 'var(--kb-state-success)'};">${quality.avg_confidence || 0}%</div>
          <div class="kb-summary-label" style="font-size:0.8125rem;color:var(--kb-neutral-500);margin-top:4px;">平均置信度</div>
        </div>
        <div class="kb-summary-item" style="text-align:center;padding:16px;background:var(--kb-neutral-50);border-radius:var(--kb-radius-medium);">
          <div class="kb-summary-value" style="font-size:1.5rem;font-weight:700;color:${(quality.exception_count || 0) > 0 ? 'var(--kb-state-error)' : 'var(--kb-state-success)'};">${quality.exception_count || 0}</div>
          <div class="kb-summary-label" style="font-size:0.8125rem;color:var(--kb-neutral-500);margin-top:4px;">异常项</div>
        </div>
      </div>
      ${quality.needs_review ? `<div style="background:var(--kb-state-warning-bg);border:1px solid var(--kb-state-warning);border-radius:var(--kb-radius-medium);padding:12px;margin-bottom:16px;font-size:0.875rem;color:var(--kb-neutral-800);">该文档需要人工复核，请查看异常清单和备注项。</div>` : ''}
      <div class="kb-tab-bar" style="display:flex;gap:0;border-bottom:1px solid var(--kb-border);margin-bottom:16px;">
        <button class="kb-tab-btn active" data-tab="atoms" style="padding:8px 16px;border:none;background:none;cursor:pointer;font-size:0.875rem;color:var(--kb-primary);border-bottom:2px solid var(--kb-primary);font-weight:500;">知识原子</button>
        <button class="kb-tab-btn" data-tab="qa" style="padding:8px 16px;border:none;background:none;cursor:pointer;font-size:0.875rem;color:var(--kb-neutral-500);">QA 对</button>
        <button class="kb-tab-btn" data-tab="exceptions" style="padding:8px 16px;border:none;background:none;cursor:pointer;font-size:0.875rem;color:var(--kb-neutral-500);">异常清单</button>
        <button class="kb-tab-btn" data-tab="raw" style="padding:8px 16px;border:none;background:none;cursor:pointer;font-size:0.875rem;color:var(--kb-neutral-500);">原始 JSON</button>
      </div>
      <div class="kb-tab-content active" id="tab-atoms" style="max-height:500px;overflow-y:auto;"></div>
      <div class="kb-tab-content" id="tab-qa" style="display:none;max-height:500px;overflow-y:auto;"></div>
      <div class="kb-tab-content" id="tab-exceptions" style="display:none;max-height:500px;overflow-y:auto;"></div>
      <div class="kb-tab-content" id="tab-raw" style="display:none;"><pre style="font-size:0.75rem;overflow:auto;max-height:500px;background:var(--kb-neutral-50);padding:12px;border-radius:var(--kb-radius-medium);"></pre></div>
    `;

    // 知识原子
    const atomsTab = container.querySelector('#tab-atoms');
    atomsTab.innerHTML = atoms.map(a => `
      <div style="border:1px solid var(--kb-border);border-radius:var(--kb-radius-medium);padding:12px;margin-bottom:8px;background:var(--kb-card);">
        <div style="font-weight:600;font-size:0.875rem;margin-bottom:4px;">${escapeHtml(a.title || a.chunk_id || '未命名')}</div>
        <div style="font-size:0.75rem;color:var(--kb-neutral-500);margin-bottom:8px;">ID: ${a.chunk_id || '-'} | 类型: ${a.doc_type || '-'} | 模块: ${a.metadata?.business_module || '-'}</div>
        <div style="font-size:0.8125rem;line-height:1.6;">${escapeHtml((a.content || '').slice(0, 500))}${(a.content || '').length > 500 ? '...' : ''}</div>
        ${a.qa_pairs && a.qa_pairs.length ? `
          <div style="margin-top:8px;font-size:0.75rem;color:var(--kb-neutral-500);font-weight:500;">QA 对 (${a.qa_pairs.length})</div>
          ${a.qa_pairs.slice(0, 3).map(qa => `
            <div style="border-left:3px solid var(--kb-primary);padding:6px 10px;margin:4px 0;background:var(--kb-neutral-50);border-radius:0 var(--kb-radius-small) var(--kb-radius-small) 0;">
              <div style="font-weight:500;font-size:0.8125rem;">Q: ${escapeHtml(qa.question || '')}</div>
              <div style="font-size:0.8125rem;color:var(--kb-neutral-600);margin-top:2px;">A: ${escapeHtml(qa.answer || '')}</div>
            </div>
          `).join('')}
          ${a.qa_pairs.length > 3 ? `<div style="font-size:0.75rem;color:var(--kb-neutral-400);margin-top:4px;">还有 ${a.qa_pairs.length - 3} 个 QA 对...</div>` : ''}
        ` : ''}
        ${a.remark ? `<div style="font-size:0.75rem;color:var(--kb-state-warning);margin-top:4px;">备注: ${escapeHtml(a.remark)}</div>` : ''}
      </div>
    `).join('') || '<div style="color:var(--kb-neutral-500);padding:20px;text-align:center;">暂无知识原子</div>';

    // QA 对（平铺）
    const qaTab = container.querySelector('#tab-qa');
    const allQa = atoms.flatMap(a => (a.qa_pairs || []).map(qa => ({ ...qa, chunk: a.title || a.chunk_id })));
    qaTab.innerHTML = allQa.length ? allQa.map(qa => `
      <div style="border-left:3px solid var(--kb-primary);padding:8px 12px;margin:6px 0;background:var(--kb-neutral-50);border-radius:0 var(--kb-radius-small) var(--kb-radius-small) 0;">
        <div style="font-weight:500;font-size:0.8125rem;">Q: ${escapeHtml(qa.question || '')}</div>
        <div style="font-size:0.8125rem;color:var(--kb-neutral-600);margin-top:2px;">A: ${escapeHtml(qa.answer || '')}</div>
        <div style="font-size:0.7rem;color:var(--kb-neutral-500);margin-top:2px;">来源: ${qa.chunk || '-'} | 置信度: ${qa.confidence_score || 0}% | 关键词: ${(qa.keywords || []).join(', ')}</div>
      </div>
    `).join('') : '<div style="color:var(--kb-neutral-500);padding:20px;text-align:center;">暂无 QA 对</div>';

    // 异常清单（后端 ExceptionList 为 9 个字符串数组字段，逐项平铺展示 + 操作指引）
    const excTab = container.querySelector('#tab-exceptions');
    const excList = r.exception_list || {};
    const excMeta = {
      content_conflicts: { label: '内容冲突', desc: '同一知识点在不同位置存在矛盾或不一致', action: '查看对应知识原子，核对并修正冲突内容', tab: 'atoms' },
      missing_info: { label: '缺失信息', desc: '原文中缺少必要的关键信息', action: '补充缺失的前提条件、参数或步骤说明', tab: 'atoms' },
      vague_items: { label: '模糊表述', desc: '存在「适当」「相关」等模糊用语，缺乏量化标准', action: '将模糊表述替换为具体数值或明确范围', tab: 'atoms' },
      expired_items: { label: '过期内容', desc: '引用了已过期的版本号、日期或政策', action: '更新为最新版本信息或标注「以最新文档为准」', tab: 'atoms' },
      chunk_anomalies: { label: '拆分异常', desc: '知识原子切分边界不理想，上下文断裂', action: '重新审视分块边界，合并或重新拆分', tab: 'atoms' },
      truncated_items: { label: '截断位置', desc: '内容在关键位置被截断，语义不完整', action: '扩展该知识原子的取文本长度或手动拼接', tab: 'atoms' },
      sensitive_items: { label: '敏感数据', desc: '检测到可能的敏感信息（手机号/身份证等）', action: '脱敏处理或删除敏感字段后再上线', tab: 'atoms' },
      low_confidence_qa: { label: '低置信 QA', desc: 'QA 对的置信度低于阈值，答案可能不完整', action: '逐条复核低置信 QA，人工校验答案', tab: 'qa' },
      terminology_pending: { label: '术语待确认', desc: '出现未经术语库确认的新名词', action: '确认术语后更新术语库或添加同义词', tab: 'atoms' },
    };
    const excItems = Object.entries(excList)
      .filter(([, v]) => Array.isArray(v) && v.length)
      .flatMap(([k, v]) => v.map(text => ({ ...(excMeta[k] || { label: k, desc: '', action: '', tab: 'atoms' }), key: k, text })));
    excTab.innerHTML = excItems.length ? `
      <div style="margin-bottom:12px;padding:10px 12px;background:var(--kb-primary-50);border-radius:var(--kb-radius-medium);font-size:0.8125rem;color:var(--kb-neutral-700);">
        共 <strong>${excItems.length}</strong> 项异常需要处理。点击任意异常项可跳转到对应内容进行修正。
      </div>
      ${excItems.map((exc, i) => `
      <div class="kb-exc-item" data-exc-idx="${i}" style="border:1px solid var(--kb-state-warning);border-left:3px solid var(--kb-state-warning);padding:10px 14px;margin:6px 0;background:var(--kb-state-warning-bg);border-radius:0 var(--kb-radius-medium) var(--kb-radius-medium) 0;font-size:0.8125rem;cursor:pointer;transition:box-shadow 0.15s;" onmouseover="this.style.boxShadow='0 2px 8px rgba(245,158,11,0.15)'" onmouseout="this.style.boxShadow='none'">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
          <span style="background:var(--kb-state-warning);color:#fff;font-size:0.6875rem;padding:1px 6px;border-radius:8px;font-weight:600;">${escapeHtml(exc.label)}</span>
          <span style="color:var(--kb-neutral-500);font-size:0.75rem;">${escapeHtml(exc.desc)}</span>
        </div>
        <div style="color:var(--kb-neutral-800);margin-bottom:6px;">${escapeHtml(exc.text)}</div>
        <div style="display:flex;align-items:center;gap:4px;color:var(--kb-primary-600);font-size:0.75rem;font-weight:500;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M9 18l6-6-6-6"/></svg>
          ${escapeHtml(exc.action)} · 点击跳转
        </div>
      </div>
    `).join('')}
    ` : '<div style="color:var(--kb-neutral-500);padding:20px;text-align:center;">无异常项，文档质量合格</div>';

    // 异常项点击 → 切换到对应 Tab 并高亮
    excTab.querySelectorAll('.kb-exc-item').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.excIdx);
        const exc = excItems[idx];
        if (!exc) return;
        // 切换到目标 Tab
        const targetBtn = container.querySelector(`.kb-tab-btn[data-tab="${exc.tab}"]`);
        if (targetBtn) targetBtn.click();
      });
    });

    // 原始 JSON
    const rawEl = container.querySelector('#tab-raw pre');
    if (rawEl) rawEl.textContent = JSON.stringify(r, null, 2);

    // Tab 切换
    container.querySelectorAll('.kb-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        container.querySelectorAll('.kb-tab-btn').forEach(b => {
          b.style.color = 'var(--kb-neutral-500)';
          b.style.borderBottomColor = 'transparent';
          b.style.fontWeight = '400';
        });
        btn.style.color = 'var(--kb-primary)';
        btn.style.borderBottomColor = 'var(--kb-primary)';
        btn.style.fontWeight = '500';
        container.querySelectorAll('.kb-tab-content').forEach(t => t.style.display = 'none');
        const tab = container.querySelector(`#tab-${btn.dataset.tab}`);
        if (tab) tab.style.display = 'block';
      });
    });
  }

  return {
    API,
    Auth,
    Admin,
    Messages,
    Access,
    Settings,
    WSClient,
    escapeHtml,
    formatBytes,
    getQueryParam,
    renderPipelineResult,
  };
})();