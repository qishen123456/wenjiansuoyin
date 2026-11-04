'use strict';
const guideSearch = document.getElementById('guideSearch');
const guideSections = [...document.querySelectorAll('[data-guide-section]')];
const guideLinks = [...document.querySelectorAll('#guideNavigation a')];
const guideStatus = document.getElementById('guideSearchStatus');
function filterGuide() {
  const query=guideSearch.value.trim().toLocaleLowerCase();
  const terms=query.split(/\s+/).filter(Boolean);
  document.body.classList.toggle('guide-filtering',!!query);
  let matched=0;
  guideSections.forEach(section=>{
    const text=section.textContent.toLocaleLowerCase();
    const visible=terms.every(term=>text.includes(term));
    section.hidden=!visible;
    if (visible) matched++;
    // Show relevant answers immediately while searching.
    section.querySelectorAll('details').forEach(detail=>{if(query)detail.open=terms.every(term=>detail.textContent.toLocaleLowerCase().includes(term));});
  });
  document.getElementById('guideNoResults').hidden=matched>0;
  guideStatus.textContent=query ? `找到 ${matched} 个相关章节` : '按你要完成的事情查找';
  guideSections.forEach(section=>{if(!query)section.querySelectorAll('details').forEach(detail=>detail.open=false);});
}
guideSearch.addEventListener('input',filterGuide);
document.getElementById('guideClearSearch').addEventListener('click',()=>{guideSearch.value='';filterGuide();guideSearch.focus();});
document.querySelectorAll('a[href^="#"]').forEach(link=>link.addEventListener('click',()=>{if(guideSearch.value){guideSearch.value='';filterGuide();}}));
document.addEventListener('keydown',event=>{
  if(event.key==='/' && !event.metaKey && !event.ctrlKey && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) {event.preventDefault();guideSearch.focus();}
  if(event.key==='Escape' && document.activeElement===guideSearch){guideSearch.value='';filterGuide();guideSearch.blur();}
});
const guideObserver=new IntersectionObserver(entries=>{
  const active=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top)[0];
  if(!active)return;
  guideLinks.forEach(link=>{
    const selected=link.hash==='#'+active.target.id;
    link.classList.toggle('active',selected);
    if(selected)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');
  });
},{rootMargin:'-12% 0px -65% 0px'});
guideSections.forEach(section=>guideObserver.observe(section));
