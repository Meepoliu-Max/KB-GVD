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

    // 异常清单（后端 ExceptionList 为 9 个字符串数组字段，逐项平铺展示）
    const excTab = container.querySelector('#tab-exceptions');
    const excList = r.exception_list || {};
    const excLabels = {
      content_conflicts: '内容冲突',
      missing_info: '缺失信息',
      vague_items: '模糊表述',
      expired_items: '过期内容',
      chunk_anomalies: '拆分异常',
      truncated_items: '截断位置',
      sensitive_items: '敏感数据',
      low_confidence_qa: '低置信 QA',
      terminology_pending: '术语待确认',
    };
    const excItems = Object.entries(excList)
      .filter(([, v]) => Array.isArray(v) && v.length)
      .flatMap(([k, v]) => v.map(text => ({ label: excLabels[k] || k, text })));
    excTab.innerHTML = excItems.length ? excItems.map(exc => `
      <div style="border-left:3px solid var(--kb-state-warning);padding:8px 12px;margin:4px 0;background:var(--kb-state-warning-bg);border-radius:0 var(--kb-radius-small) var(--kb-radius-small) 0;font-size:0.8125rem;">
        <strong>${escapeHtml(exc.label)}</strong>
        <div>${escapeHtml(exc.text)}</div>
      </div>
    `).join('') : '<div style="color:var(--kb-neutral-500);padding:20px;text-align:center;">无异常项</div>';

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
    WSClient,
    escapeHtml,
    formatBytes,
    getQueryParam,
    renderPipelineResult,
  };
})();