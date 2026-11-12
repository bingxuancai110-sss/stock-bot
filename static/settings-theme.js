/* Theme preference only: do not intercept holdings, trading, or other forms. */
(function () {
  'use strict';
  if (window.StockBotThemeSettings) return;
  window.StockBotThemeSettings = true;
  var valid = ['mist', 'warm', 'stone'];
  var timeoutMs = 22000;

  function status(form, state, text) {
    if (window.StockBotUI) window.StockBotUI.formState(form, state, text);
    else {
      var box = form.querySelector('[data-theme-status]');
      if (!box) { box = document.createElement('p'); box.dataset.themeStatus = '1'; box.setAttribute('role', 'status'); form.appendChild(box); }
      box.textContent = text;
    }
  }
  function apply(theme) {
    if (valid.indexOf(theme) !== -1) {
      document.documentElement.dataset.uiTheme = theme;
      var option = document.querySelector('[data-settings-theme-form] input[value="' + theme + '"]');
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta && option && /^#[0-9a-f]{6}$/i.test(option.dataset.themeColor || '')) meta.content = option.dataset.themeColor;
    }
  }
  function apiURL(form) {
    var url = new URL('/web/api/settings/theme', location.origin);
    var token = new URL(form.action, location.href).searchParams.get('t') || new URLSearchParams(location.search).get('t');
    if (token) url.searchParams.set('t', token);
    return url;
  }
  async function request(form, method, body) {
    var controller = new AbortController(), timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    try {
      var response = await fetch(apiURL(form), {method: method, body: body, credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.status === 401) { var auth = new Error('登入已失效，請重新登入後再儲存。'); auth.rejected = true; throw auth; }
      if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
        var unreadable = new Error(response.status === 403 ? '無法使用此設定，請重新整理並確認登入狀態。' : '未收到可確認的設定結果。');
        unreadable.rejected = response.status === 403;
        throw unreadable;
      }
      var data = await response.json();
      if (!response.ok || !data.ok || valid.indexOf(data.theme) === -1) {
        var error = new Error(data.message || '暫時無法確認配色。');
        error.rejected = data.state === 'rejected'; throw error;
      }
      return data;
    } finally { clearTimeout(timer); }
  }
  window.addEventListener('submit', async function (event) {
    var form = event.target;
    if (!form.matches('form[data-settings-theme-form]')) return;
    event.preventDefault();
    // Own only this form before legacy document handlers can disable it.
    event.stopImmediatePropagation();
    if (form.dataset.themeBusy === '1') return;
    var selected = form.querySelector('[name="ui_theme"]:checked');
    if (!selected) { status(form, 'error', '請先選擇配色。'); return; }
    var desired = selected.value, body = new FormData(form);
    var button = form.querySelector('button[type="submit"]'), checking = form.dataset.themeCheck === '1';
    var controls = Array.from(form.querySelectorAll('[name="ui_theme"],button[type="submit"]'));
    var disabled = controls.map(function (control) { return control.disabled; });
    form.dataset.themeBusy = '1';
    controls.forEach(function (control) { control.disabled = true; });
    if (button) button.textContent = checking ? '正在確認…' : '儲存中…';
    status(form, 'loading', checking ? '正在讀取已保存的配色…' : '正在儲存配色…');
    var unknown = false;
    function finish(data) {
      if (!form.isConnected) return;
      apply(data.theme);
      if (data.theme === desired) status(form, 'success', '已儲存：' + data.label + '。');
      else status(form, 'error', '目前保存的是「' + data.label + '」，尚未確認你選擇的配色；可重新套用。');
    }
    try {
      finish(await request(form, checking ? 'GET' : 'POST', checking ? undefined : body));
    } catch (error) {
      if (!form.isConnected) return;
      if (error.rejected) status(form, 'error', error.message);
      else {
        // A dropped write response can still mean the server committed it.
        // Read the saved state; never automatically resend the POST.
        status(form, 'loading', '正在核對伺服器已保存的配色…');
        if (button) button.textContent = '正在確認…';
        try { finish(await request(form, 'GET')); }
        catch (readError) {
          if (readError.rejected) status(form, 'error', readError.message);
          else {
            unknown = true;
            status(form, 'waiting', '儲存結果待確認。按「確認目前配色」只會讀取設定，不會重複送出。');
          }
        }
      }
    } finally {
      delete form.dataset.themeBusy;
      delete form.dataset.submitted;
      form.classList.remove('tap-loading');
      form.dataset.themeCheck = unknown ? '1' : '0';
      controls.forEach(function (control, index) { control.disabled = disabled[index]; });
      if (button) button.textContent = unknown ? '確認目前配色' : '套用並儲存配色';
    }
  }, true);
})();
