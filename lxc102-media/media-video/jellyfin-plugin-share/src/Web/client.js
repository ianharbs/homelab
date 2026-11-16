/* Compartilhar — botão no jellyfin-web (injetado pelo File Transformation). */
(function () {
  'use strict';
  if (window.__jfShareLoaded) return;
  window.__jfShareLoaded = true;

  var SUPPORTED = { Series: 1, Season: 1, Episode: 1, Movie: 1 };
  var MENU_IDS = ['addtoplaylist', 'addtocollection', 'edit', 'editimages', 'identify', 'refresh', 'delete',
    'markplayed', 'markunplayed', 'moremediainfo', 'multiSelect', 'download', 'copy-stream', 'queue', 'instantmix'];
  var cfgCache = {};
  var itemCache = {};
  var lastMenu = null; // { id, type, t }
  var scanT = 0;

  function client() { return window.ApiClient; }

  function api(path, opts) {
    var c = client();
    opts = opts || {};
    return fetch(c.getUrl(path), {
      method: opts.method || 'GET',
      headers: { 'Authorization': 'MediaBrowser Token="' + c.accessToken() + '"', 'Content-Type': 'application/json' },
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || ('HTTP ' + r.status)); });
      return r.status === 204 ? null : r.json();
    });
  }

  function getCfg() {
    var c = client();
    if (!c || !c.getCurrentUserId || !c.getCurrentUserId()) return Promise.resolve({ canShare: false });
    var uid = c.getCurrentUserId();
    if (!cfgCache[uid]) {
      cfgCache[uid] = api('Share/Config').catch(function () { return { canShare: false }; });
    }
    return cfgCache[uid];
  }

  function getItem(id) {
    var c = client();
    if (!itemCache[id]) {
      itemCache[id] = c.getItem(c.getCurrentUserId(), id).catch(function () { delete itemCache[id]; return null; });
    }
    return itemCache[id];
  }

  function hashItemId() {
    var m = /[?&]id=([0-9a-fA-F-]{32,36})/.exec(location.hash || location.search || '');
    return m ? m[1].replace(/-/g, '') : null;
  }

  // ------------------------------------------------------------ botão na página de detalhes
  function scanDetailPage() {
    if (!/details/.test(location.hash)) return;
    var id = hashItemId();
    if (!id) return;
    var page = document.querySelector('.itemDetailPage:not(.hide)');
    if (!page) return;
    var row = page.querySelector('.mainDetailButtons');
    if (!row) return;
    var existing = row.querySelector('.btnJfShare');
    if (existing && existing.getAttribute('data-item') === id) return;
    if (existing) existing.remove();
    if (row.__jfSharePending === id) return;
    row.__jfSharePending = id;
    Promise.all([getCfg(), getItem(id)]).then(function (r) {
      row.__jfSharePending = null;
      var cfg = r[0], item = r[1];
      if (!cfg.canShare || !item || !SUPPORTED[item.Type] || hashItemId() !== id) return;
      if (row.querySelector('.btnJfShare')) return;
      var b = document.createElement('button');
      b.setAttribute('is', 'emby-button');
      b.type = 'button';
      b.className = 'button-flat detailButton emby-button btnJfShare';
      b.title = 'Compartilhar';
      b.setAttribute('data-item', id);
      b.innerHTML = '<div class="detailButton-content"><span class="material-icons detailButton-icon share" aria-hidden="true"></span></div>';
      b.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); openDialog(id); });
      var more = row.querySelector('.btnMoreCommands');
      if (more) row.insertBefore(b, more); else row.appendChild(b);
    });
  }

  // ------------------------------------------------------------ opção no menu "⋮" / clique direito
  function rememberMenuTarget(e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var trigger = e.type === 'contextmenu' ? t : t.closest('[data-action="menu"], .btnCardOptions, .btnMoreCommands, .listItemButton[data-action="menu"]');
    if (!trigger) return;
    if (trigger.classList && trigger.classList.contains('btnMoreCommands')) {
      var hid = hashItemId();
      if (hid) lastMenu = { id: hid, type: null, t: Date.now() };
      return;
    }
    var holder = trigger.closest('[data-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-id');
    if (!/^[0-9a-fA-F]{32}$/.test(id || '')) return;
    lastMenu = { id: id, type: holder.getAttribute('data-type'), t: Date.now() };
  }
  document.addEventListener('click', rememberMenuTarget, true);
  document.addEventListener('contextmenu', rememberMenuTarget, true);

  function decorateActionSheet(sheet) {
    if (!lastMenu || Date.now() - lastMenu.t > 4000) return;
    if (sheet.querySelector('[data-id="jfshare"]')) return;
    var items = sheet.querySelectorAll('.actionSheetMenuItem');
    var isItemMenu = Array.prototype.some.call(items, function (b) { return MENU_IDS.indexOf(b.getAttribute('data-id')) !== -1; });
    if (!isItemMenu) return;
    var target = lastMenu;
    lastMenu = null;
    if (target.type && !SUPPORTED[target.type]) return;
    Promise.all([getCfg(), target.type ? Promise.resolve({ Type: target.type }) : getItem(target.id)]).then(function (r) {
      if (!r[0].canShare || !r[1] || !SUPPORTED[r[1].Type]) return;
      var model = sheet.querySelector('.actionSheetMenuItem[data-id="addtoplaylist"]') || items[0];
      if (!model || !document.body.contains(sheet)) return;
      var b = model.cloneNode(true);
      b.setAttribute('data-id', 'jfshare');
      b.removeAttribute('autofocus');
      var icon = b.querySelector('.actionsheetMenuItemIcon');
      if (icon) icon.className = 'actionsheetMenuItemIcon listItemIcon listItemIcon-transparent material-icons share';
      var txt = b.querySelector('.actionSheetItemText') || b.querySelector('.listItemBodyText');
      if (txt) txt.textContent = 'Compartilhar';
      var aside = b.querySelector('.actionSheetItemAsideText');
      if (aside) aside.remove();
      // o próprio actionsheet fecha ao clicar (classe actionSheetMenuItem); depois abrimos o diálogo
      b.addEventListener('click', function () { setTimeout(function () { openDialog(target.id); }, 150); });
      model.parentNode.insertBefore(b, model.nextSibling);
    });
  }

  new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var added = muts[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.nodeType !== 1) continue;
        var sheet = n.classList.contains('actionSheet') ? n : n.querySelector && n.querySelector('.actionSheet');
        if (sheet) decorateActionSheet(sheet);
      }
    }
    if (!scanT) scanT = setTimeout(function () { scanT = 0; scanDetailPage(); }, 250);
  }).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('hashchange', function () { setTimeout(scanDetailPage, 300); });

  // ------------------------------------------------------------ diálogo
  var css = '' +
    '.jfs-back{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px}' +
    '.jfs-dlg{background:#1c1c1e;color:#eee;border-radius:12px;max-width:460px;width:100%;padding:22px 22px 18px;box-shadow:0 20px 60px rgba(0,0,0,.6);font-size:15px;line-height:1.4}' +
    '.jfs-dlg h2{margin:0 0 4px;font-size:20px;font-weight:600;display:flex;align-items:center;gap:8px}' +
    '.jfs-name{color:#fff;font-weight:500;margin:10px 0 2px}.jfs-scope{color:#9aa0a6;font-size:13px;margin-bottom:16px}' +
    '.jfs-row{display:flex;align-items:center;gap:10px;margin:10px 0;flex-wrap:wrap}' +
    '.jfs-row label{cursor:pointer;display:flex;align-items:center;gap:6px}' +
    '.jfs-days{width:80px;padding:8px 10px;border-radius:8px;border:1px solid #3a3a3c;background:#2c2c2e;color:#fff;font-size:15px}' +
    '.jfs-days:disabled{opacity:.4}' +
    '.jfs-url{flex:1;min-width:0;padding:9px 10px;border-radius:8px;border:1px solid #3a3a3c;background:#111;color:#8fd3ff;font-size:13px;font-family:monospace}' +
    '.jfs-btn{border:0;border-radius:8px;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer;background:#3a3a3c;color:#fff}' +
    '.jfs-btn.pri{background:#00a4dc}.jfs-btn:disabled{opacity:.5;cursor:default}' +
    '.jfs-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}' +
    '.jfs-note{color:#9aa0a6;font-size:12.5px;margin-top:12px}.jfs-err{color:#ff8a80;font-size:13px;margin-top:8px}';

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  }

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  function scopeText(item) {
    switch (item.Type) {
      case 'Series': return 'Série completa — todas as temporadas';
      case 'Season': return 'Somente esta temporada';
      case 'Episode': return 'Somente este episódio';
      default: return 'Filme';
    }
  }

  function itemTitle(item) {
    if (item.Type === 'Season') return (item.SeriesName ? item.SeriesName + ' — ' : '') + item.Name;
    if (item.Type === 'Episode') {
      var code = 'T' + (item.ParentIndexNumber || 0) + ':E' + (item.IndexNumber || 0);
      return (item.SeriesName ? item.SeriesName + ' — ' : '') + code + ' — ' + item.Name;
    }
    return item.Name + (item.ProductionYear ? ' (' + item.ProductionYear + ')' : '');
  }

  function openDialog(id) {
    if (!document.getElementById('jfs-style')) {
      var st = el('style', { id: 'jfs-style' }); st.textContent = css; document.head.appendChild(st);
    }
    Promise.all([getCfg(), getItem(id)]).then(function (r) {
      var cfg = r[0], item = r[1];
      if (!item) return;
      var days = cfg.defaultDays > 0 ? cfg.defaultDays : 7;
      var back = el('div', { 'class': 'jfs-back' });
      var dlg = el('div', { 'class': 'jfs-dlg', role: 'dialog', 'aria-modal': 'true' },
        '<h2><span class="material-icons share" aria-hidden="true"></span>Compartilhar</h2>' +
        '<div class="jfs-name">' + esc(itemTitle(item)) + '</div>' +
        '<div class="jfs-scope">' + esc(scopeText(item)) + '</div>' +
        '<div class="jfs-row">Validade: <input class="jfs-days" type="number" min="1" max="3650" value="' + days + '"> dias' +
        '<label><input type="checkbox" class="jfs-never"' + (cfg.defaultDays === 0 ? ' checked' : '') + '> Sem validade</label></div>' +
        '<div class="jfs-result" hidden><div class="jfs-row"><input class="jfs-url" readonly><button class="jfs-btn pri jfs-copy">Copiar</button></div>' +
        '<div class="jfs-row" style="justify-content:flex-end"><button class="jfs-btn jfs-open">Abrir</button><button class="jfs-btn jfs-send" hidden>Enviar…</button></div></div>' +
        '<div class="jfs-err" hidden></div>' +
        '<div class="jfs-note">Quem tiver o link assiste e baixa sem conta. Para revogar: Painel › Plugins › Compartilhar.</div>' +
        '<div class="jfs-foot"><button class="jfs-btn jfs-close">Fechar</button><button class="jfs-btn pri jfs-make">Gerar link</button></div>');
      back.appendChild(dlg);
      document.body.appendChild(back);

      var q = function (s) { return dlg.querySelector(s); };
      var daysIn = q('.jfs-days'), never = q('.jfs-never');
      var sync = function () { daysIn.disabled = never.checked; };
      never.addEventListener('change', sync); sync();
      var close = function () { back.remove(); document.removeEventListener('keydown', onKey, true); };
      var onKey = function (e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
      document.addEventListener('keydown', onKey, true);
      back.addEventListener('click', function (e) { if (e.target === back) close(); });
      q('.jfs-close').addEventListener('click', close);

      q('.jfs-make').addEventListener('click', function () {
        var d = never.checked ? 0 : Math.max(1, parseInt(daysIn.value, 10) || 1);
        var btn = this; btn.disabled = true; q('.jfs-err').hidden = true;
        api('Share/Links', { method: 'POST', body: { itemId: id, days: d } }).then(function (link) {
          q('.jfs-result').hidden = false;
          q('.jfs-url').value = link.url;
          q('.jfs-url').select();
          btn.textContent = 'Gerar outro';
          q('.jfs-open').onclick = function () { window.open(link.url, '_blank', 'noopener'); };
          if (navigator.share) {
            q('.jfs-send').hidden = false;
            q('.jfs-send').onclick = function () { navigator.share({ title: itemTitle(item), url: link.url }).catch(function () {}); };
          }
        }).catch(function (e) {
          q('.jfs-err').textContent = 'Erro: ' + e.message; q('.jfs-err').hidden = false;
        }).then(function () { btn.disabled = false; });
      });

      q('.jfs-copy').addEventListener('click', function () {
        var input = q('.jfs-url'), b = this;
        var done = function () { b.textContent = 'Copiado!'; setTimeout(function () { b.textContent = 'Copiar'; }, 1500); };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(input.value).then(done, function () { input.select(); document.execCommand('copy'); done(); });
        } else { input.select(); document.execCommand('copy'); done(); }
      });
    });
  }

  window.jfShareOpen = openDialog;
})();
