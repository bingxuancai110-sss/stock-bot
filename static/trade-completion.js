/* A committed trade stays successful even if the subsequent read fails. */
(function(){
  'use strict';
  if(window.StockBotTradeCompletion)return;
  function complete(options){
    var root=document.getElementById('portfolio-v2'),form=options.form;
    if(!root||!form.isConnected)return;
    var originalFocus=document.activeElement;
    var tradeDialog=form.closest('dialog');if(tradeDialog&&tradeDialog.open)tradeDialog.close();
    var card=document.createElement('dialog');card.className='trade-completion-dialog';
    card.setAttribute('aria-labelledby','trade-completion-title');
    var content=document.createElement('section');content.className='p2-trade-result success';
    var mark=document.createElement('span');mark.className='trade-completion-mark';mark.textContent='✓';mark.setAttribute('aria-hidden','true');
    var title=document.createElement('h2');title.id='trade-completion-title';title.textContent=options.isBuy?'買進記帳完成':'賣出記帳完成';
    var message=document.createElement('p');message.textContent=options.message;
    var state=document.createElement('p');state.className='trade-completion-state';state.setAttribute('role','status');state.textContent='正在同步持股資料…';
    var retry=document.createElement('button');retry.type='button';retry.textContent='重新同步持股';retry.hidden=true;
    var close=document.createElement('button');close.type='button';close.className='trade-completion-close';close.textContent='完成';
    content.append(mark,title,message,state,retry,close);card.append(content);document.body.append(card);
    card.addEventListener('close',function(){card.remove();var focus=originalFocus&&originalFocus.isConnected?originalFocus:document.querySelector('[data-p2-add]');if(focus)focus.focus({preventScroll:true});},{once:true});
    close.onclick=function(){card.close();};card.showModal();close.focus({preventScroll:true});
    var busy=false;
    function refresh(){
      if(busy)return;busy=true;retry.disabled=true;state.textContent='正在同步持股資料…';
      var url=new URL(options.url,location.origin);url.searchParams.set('fragment','1');url.searchParams.set('refresh','1');url.searchParams.delete('fast');
      var controller=new AbortController(),deadline=setTimeout(function(){controller.abort();},30000);
      return fetch(url.pathname+url.search,{credentials:'same-origin',cache:'no-store',signal:controller.signal}).then(function(response){
        if(!response.ok)throw new Error('read failed');return response.text();
      }).then(function(markup){
        if(!root.isConnected||document.getElementById('portfolio-v2')!==root)return;
        var doc=new DOMParser().parseFromString(markup,'text/html'),fresh=doc.getElementById('portfolio-v2');
        if(!fresh)throw new Error('missing holdings');
        var y=scrollY,x=scrollX,open=root.querySelector('.position-more[open]'),code=open&&open.closest('[data-position-code]').dataset.positionCode;
        var view=root.querySelector('[data-p2-view][aria-selected="true"]'),viewName=view&&view.dataset.p2View;
        var sort=root.querySelector('[data-p2-sort]'),sortValue=sort&&sort.value;
        var trendChoices=Array.from(root.querySelectorAll('input[id^="trend-period-"],#trend-benchmark')).map(function(e){return {id:e.id,checked:e.checked};});
        var anchor=Array.from(root.querySelectorAll('.position-card')).find(function(e){var r=e.getBoundingClientRect();return r.bottom>80&&r.top<innerHeight;});
        var anchorCode=anchor&&anchor.dataset.positionCode,anchorTop=anchor&&anchor.getBoundingClientRect().top;
        document.dispatchEvent(new CustomEvent('stockbot:pageleaving'));
        if(window.__stockbotHomePrivacyMasked===true)fresh.querySelectorAll('[data-home-sensitive]').forEach(function(e){e.textContent='******';});
        fresh.querySelectorAll('script').forEach(function(s){s.remove();});root.replaceWith(fresh);root=fresh;
        // Rebind the existing portfolio, quote, target-weight and allocation initializers.
        // External shared runtimes remain loaded; no document navigation or loader.
        doc.querySelectorAll('script:not([src])').forEach(function(old){var script=document.createElement('script');script.textContent=old.textContent;fresh.append(script);script.remove();});
        document.dispatchEvent(new CustomEvent('stockbot:page-ready'));
        document.dispatchEvent(new CustomEvent('stockbot:pagecontentready'));
        trendChoices.forEach(function(choice){var input=fresh.querySelector('#'+CSS.escape(choice.id));if(input){input.checked=choice.checked;input.dispatchEvent(new Event('change',{bubbles:true}));}});
        var newSort=fresh.querySelector('[data-p2-sort]');if(newSort&&sortValue){newSort.value=sortValue;newSort.dispatchEvent(new Event('change',{bubbles:true}));}
        if(viewName&&viewName!=='current'){var tab=fresh.querySelector('[data-p2-view="'+viewName+'"]');if(tab)tab.click();}
        if(code){var detail=fresh.querySelector('[data-position-code="'+CSS.escape(code)+'"] .position-more');if(detail)detail.open=true;}
        var newAnchor=anchorCode&&fresh.querySelector('[data-position-code="'+CSS.escape(anchorCode)+'"]');
        var nextY=newAnchor?y+newAnchor.getBoundingClientRect().top-anchorTop:y;
        window.scrollTo({left:x,top:nextY,behavior:'instant'});
        state.textContent='持股與損益已更新';
        if(!retry.hidden)retry.textContent='已同步';
      }).catch(function(){
        state.textContent='記帳已完成，持股畫面尚未同步。請重新同步，不需再次送出交易。';retry.hidden=false;retry.disabled=false;
      }).finally(function(){busy=false;clearTimeout(deadline);});
    }
    retry.onclick=refresh;refresh();
  }
  window.StockBotTradeCompletion={show:complete};
})();
