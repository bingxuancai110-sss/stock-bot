(function () {
  'use strict';

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
