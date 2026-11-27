(function () {
  'use strict';

  let latestIds = [];
  let temporarySeen = [];

  function updateBell(ids, markSeen) {
    const bell = document.querySelector('.app-notice-link[data-notice-scope]');
    const dot = bell?.querySelector('.app-notice-dot');
    if (!dot) return;
    latestIds = ids;
    const key = 'stockbot-notices-seen:' + bell.dataset.noticeScope;
    let seen = temporarySeen;
    try {
      const saved = JSON.parse(localStorage.getItem(key) || '[]');
      if (Array.isArray(saved)) seen = saved;
    } catch (_) {}
    if (markSeen) {
      seen = Array.from(new Set([...ids, ...seen])).slice(0, 1000);
      temporarySeen = seen;
      try { localStorage.setItem(key, JSON.stringify(seen)); } catch (_) {}
    }
    const unread = ids.some(id => !seen.includes(id));
    dot.hidden = !unread;
    bell.setAttribute('aria-label', unread ? '通知台，有未讀通知' : '通知台');
  }

  function fromElement(center) {
    return (center.dataset.noticeIds || '').split(' ').filter(Boolean);
  }

  function markOpened() {
    const center = document.querySelector('#app-page-content .notice-center[data-notice-ids]');
    if (center) updateBell(fromElement(center), true);
  }

  window.StockBotNotifications = {
    preview(fragment) {
      const doc = new DOMParser().parseFromString(fragment, 'text/html');
      const center = doc.querySelector('.notice-center[data-notice-ids]');
      if (center) updateBell(fromElement(center), false);
    }
  };

  document.addEventListener('DOMContentLoaded', markOpened);
  document.addEventListener('stockbot:pagecontentready', markOpened);
  window.addEventListener('storage', event => {
    if (event.key?.startsWith('stockbot-notices-seen:')) updateBell(latestIds, false);
  });

  // Delegation also covers notification views inserted by the app shell.
  document.addEventListener('click', function (event) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[data-notice-tab]');
    if (!link) return;
    const center = link.closest('.notice-center');
    const panels = Array.from(center.querySelectorAll('[data-notice-panel]'));
    const selected = panels.find(panel => panel.dataset.noticePanel === link.dataset.noticeTab);
    if (!selected) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    center.querySelectorAll('[data-notice-tab]').forEach(tab => {
      if (tab === link) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    });
    panels.forEach(panel => { panel.hidden = panel !== selected; });
    center.querySelector('[data-notice-count]').textContent = '本分類 ' + selected.dataset.count + ' 筆';

    const url = new URL(link.href);
    if (selected.dataset.page !== '1') url.searchParams.set('page', selected.dataset.page);
    history.replaceState(history.state, '', url.href);
    url.searchParams.delete('page');
    url.searchParams.set('refresh', '1');
    center.querySelector('.notice-refresh').href = url.href;
  }, true);
}());
