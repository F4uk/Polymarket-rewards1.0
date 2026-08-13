/* web/static/app.js — Shared utilities */

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// 钱包短地址 + 备注标签(备注纯展示,各页复用)。
function shortAddr(a) {
  return a && a.length > 12 ? a.slice(0, 6) + '...' + a.slice(-4) : (a || '');
}
// 钱包标签:有备注显示备注,否则短地址;完整地址请放到 title。
function walletLabel(remark, addr) {
  return (remark && String(remark).trim()) ? String(remark) : shortAddr(addr);
}

// 备注只在 /api/wallets 上,而挂单/持仓/动作等接口只回地址 —— 这里把钱包列表
// 缓存成一次全页面共享的请求,地址一律小写做 key(各接口回传的大小写来源不同,
// 不归一会查不到备注而静默退回短地址)。
let _walletsPromise = null;
const _remarkCache = {};

function walletList() {
  if (!_walletsPromise) {
    _walletsPromise = fetch('/api/wallets').then(r => r.json()).then(ws => {
      const list = Array.isArray(ws) ? ws : [];
      list.forEach(w => {
        _remarkCache[String(w.address || '').toLowerCase()] = w.remark || '';
      });
      return list;
    }).catch(() => []);
  }
  return _walletsPromise;
}

// 只拿得到地址时的钱包标签(备注优先)。缓存未就绪就退回短地址,下一轮刷新补上。
function walletLabelOf(addr) {
  return walletLabel(_remarkCache[String(addr || '').toLowerCase()] || '', addr);
}

// 往钱包下拉里追加选项(备注优先,完整地址进 title)。页面自带的「全部」选项写在
// HTML 里,这里只负责追加钱包。返回 promise,便于首屏等备注就绪再渲染表格。
function fillWalletSelect(sel) {
  return walletList().then(ws => {
    ws.forEach(w => {
      const opt = document.createElement('option');
      opt.value = w.address;
      opt.textContent = walletLabel(w.remark, w.address);
      opt.title = w.address;
      sel.appendChild(opt);
    });
    return ws;
  });
}

// 相对时间(秒级时间戳)。0/空 = 从没发生过。
function relTime(ts) {
  const t = Number(ts) || 0;
  if (t <= 0) return '—';
  const d = Math.floor(Date.now() / 1000 - t);
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return Math.floor(d / 86400) + ' 天前';
}
// 配套的绝对时间,放 title 悬浮。
function absTime(ts) {
  const t = Number(ts) || 0;
  return t > 0 ? new Date(t * 1000).toLocaleString('zh-CN') : '从未活跃';
}

let _toastTimer = null;
function showToast(msg) {
  let el = document.getElementById('app-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'app-toast';
    el.className = 'app-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 1500);
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); if (done) done(); }
  catch (e) { alert('复制失败，请从悬浮提示手动复制'); }
  document.body.removeChild(ta);
}

function copyCid(cid) {
  if (!cid) return;
  const ok = () => showToast('已复制 condition ID');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cid).then(ok).catch(() => fallbackCopy(cid, ok));
  } else {
    fallbackCopy(cid, ok);
  }
}

// 市场单元格:名称(有链接则 <a>,无则纯文本;名称缺失显示截断 condition_id)
// + 📋 复制完整 condition_id 按钮。
function marketCell(name, conditionId, url) {
  const cid = conditionId || '';
  const label = name || (cid ? cid.slice(0, 8) + '...' + cid.slice(-6) : '');
  const safe = escapeHtml(label);
  const inner = url
    ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${safe}</a>`
    : safe;
  const copyBtn = cid
    ? ` <button class="btn-xs" title="复制完整 condition ID"` +
      ` onclick="copyCid('${escapeHtml(cid)}')">📋</button>`
    : '';
  return `<span title="${escapeHtml(cid)}">${inner}${copyBtn}</span>`;
}

// 全局扫描指示器:每个页面都轮询后端扫描状态,显示在侧边栏 —— 扫描是后端后台线程,
// 切页不会停;这里让进度在任意页面都可见、切页也不丢(点击回到市场发现页)。
(function () {
  function tick() {
    fetch('/api/engine/scan-status').then(r => r.json()).then(d => {
      const el = document.getElementById('scan-indicator');
      if (!el) return;
      if (d && d.scan_status === 'scanning') {
        const c = d.scan_checked || 0, t = d.scan_total || 0;
        el.style.display = 'block';
        el.textContent = t > 0
          ? `⟳ 扫描中 ${c}/${t} · 已找到 ${d.found || 0}`
          : `⟳ 扫描中 · 已找到 ${d.found || 0}`;
      } else {
        el.style.display = 'none';
      }
    }).catch(() => {});
  }
  setInterval(tick, 3000);
  tick();
})();

// 全局拦截:任一 fetch 被重定向到登录/设置页(会话失效,典型场景=服务重启后
// 旧标签页仍开着)时,直接跳登录,避免页面静默停更 + .json() 解析登录 HTML 报错(F12)。
(function () {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (...args) {
    return _origFetch(...args).then((resp) => {
      if (resp && resp.redirected && /\/(login|setup)(\?|$)/.test(resp.url)) {
        window.location.href = '/login';
      }
      return resp;
    });
  };
})();
