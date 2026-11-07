'use strict';
const $ = id => document.getElementById(id);
let state = {articles: [], keywords: [], runs: []}, source = 'all', page = 1;
const names = {google: 'Google', naver: 'Naver', daum: 'Daum'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const stamp = value => value ? new Date(value).toLocaleString('ko-KR', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '날짜 미제공';
function notice(text) {$('notice').textContent = text;}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-News-Radar':'1'},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '요청에 실패했습니다.');
  return result;
}
function renderFeed() {
  const query = $('search').value.toLocaleLowerCase(), keyword = $('keyword-filter').value, days = Number($('period').value);
  const rows = state.articles.filter(row => (!days || (row.published && new Date(row.published).getTime() >= Date.now() - days * 86400000)) && (row.title + ' ' + row.summary).toLocaleLowerCase().includes(query) && row.matches.some(m => (source === 'all' || m.provider === source) && (!keyword || String(m.keyword) === keyword)));
  const pages = Math.max(1, Math.ceil(rows.length / 15)); page = Math.min(page, pages);
  $('result-count').textContent = rows.length;
  $('articles').innerHTML = rows.slice((page-1)*15,page*15).map(row => `<article class="news"><div class="meta">${[...new Set(row.matches.map(m=>m.method))].map(m=>`<span class="badge">${esc(m)}</span>`).join('')}<span>${esc(row.publisher)}</span><span>· ${esc(stamp(row.published))}</span></div><h3><a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a></h3><p>${esc(row.summary)}</p><div class="news-bottom"><span>${[...new Set(row.matches.map(m=>m.name))].map(n=>'# '+esc(n)).join(' &nbsp; ')}</span><a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">기사 보기 ↗</a></div></article>`).join('') || `<div class="empty"><strong>${state.collecting ? '새로운 소식을 찾고 있습니다' : '표시할 기사가 없습니다'}</strong>${state.collecting ? '포털별 검색이 끝나면 기사가 여기에 표시됩니다.' : '키워드를 등록하고 지금 수집하기를 누르거나, 검색 조건과 수집 상태를 확인하세요.'}</div>`;
  $('page-label').textContent = `${page} / ${pages}`; $('previous').disabled = page <= 1; $('next').disabled = page >= pages;
}
async function refresh() {
  try {
    state = await api('/api/state');
    $('total').textContent = state.articles.length.toLocaleString(); $('kw-count').textContent = state.keywords.length;
    $('auto-info').textContent = `● ${state.interval}분 간격 자동 수집`;
    $('collect').disabled = state.collecting; $('collect').textContent = state.collecting ? '↻ 수집 중…' : '↻ 지금 수집하기';
    $('keywords').innerHTML = state.keywords.map(k => `<span class="chip">${esc(k.name)}<button data-delete="${k.id}" aria-label="${esc(k.name)} 키워드 삭제">×</button></span>`).join('');
    const previous = $('keyword-filter').value;
    $('keyword-filter').innerHTML = '<option value="">모든 키워드</option>' + state.keywords.map(k=>`<option value="${k.id}">${esc(k.name)}</option>`).join('');
    if (state.keywords.some(k=>String(k.id)===previous)) $('keyword-filter').value = previous;
    const latest = state.runs.map(r=>r.finished).sort().at(-1);
    $('last-run').textContent = latest ? stamp(latest) : '—';
    const errors = state.runs.filter(r=>r.status==='error').length;
    $('run-summary').textContent = errors ? `${errors}개 검색 오류 · 아래 수집 상태 확인` : state.runs.length ? '각 키워드의 최근 수집 결과' : '첫 수집을 기다리고 있습니다';
    $('providers').innerHTML = Object.entries(state.providers).map(([key,value])=>`<div class="provider"><strong>${names[key]}</strong>${esc(value)}</div>`).join('');
    $('runs').innerHTML = state.runs.map(r=>`<div class="run ${r.status==='error'?'error':''}">${esc(names[r.provider])} · ${esc(r.keyword)} · ${esc(r.method)} · ${r.status==='error' ? esc(r.detail) : `${r.count}건 일치${r.detail ? " · " + esc(r.detail) : ""}`} · ${esc(stamp(r.finished))}</div>`).join('');
    renderFeed();
  } catch (error) {notice('서버 연결 실패: ' + error.message);}
}
$('collect').onclick = async () => {try {await api('/api/collect',{}); notice('수집을 요청했습니다. 결과는 자동으로 갱신됩니다.'); await refresh();} catch(e){notice(e.message);}};
$('keyword-form').onsubmit = async event => {event.preventDefault(); try {await api('/api/keywords',{name:$('keyword-input').value}); $('keyword-input').value=''; notice('키워드를 추가했습니다. 지금 수집하기를 누르면 바로 검색합니다.'); await refresh();} catch(e){notice(e.message);}};
$('keywords').onclick = async event => {const button=event.target.closest('[data-delete]'); if(!button)return; try {await api('/api/keywords/delete',{id:Number(button.dataset.delete)}); notice('키워드를 삭제했습니다.'); await refresh();} catch(e){notice(e.message);}};
$('sources').onclick = event => {const button = event.target.closest('[data-source]'); if(!button)return; source=button.dataset.source; page=1; document.querySelectorAll('[data-source]').forEach(b=>b.classList.toggle('active',b===button)); renderFeed();};
['search','keyword-filter','period'].forEach(id=>$(id).addEventListener('input',()=>{page=1;renderFeed();}));
$('previous').onclick=()=>{page--;renderFeed();}; $('next').onclick=()=>{page++;renderFeed();};
refresh(); setInterval(refresh,5000);
