'use strict';
let state = {objects:[], matters:[], backgrounds:[], runs:[], handoffs:[], imports:[]};
let currentTab = 'matters', selectedMatter = null, selectedBackground = null;
const $ = id => document.getElementById(id);
const labels = {pending:'待处理',needs_confirmation:'待确认',completed:'已完成',irrelevant:'与我无关',deferred:'稍后处理',reading:'阅读中',published:'已提交结果',candidate:'待你确认',confirmed:'已确认',rejected:'未采纳',read:'已读',partial:'部分可读',unread:'未读'};
function el(tag, text, cls) { const n = document.createElement(tag); if(text !== undefined) n.textContent = text; if(cls) n.className = cls; return n; }
function time(value) { if(!value) return '未明确'; const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN',{hour12:false}); }
function objectName(id) { return state.objects.find(o=>o.id===id)?.name || '未知对象'; }
function button(text, fn, cls) { const n = el('button',text,cls); n.type='button'; n.addEventListener('click', fn); return n; }
function attention(m) { return !!m.changed || ['pending','needs_confirmation'].includes(m.state) || (m.state==='deferred' && new Date(m.deferred_until)<=new Date()); }
async function api(path, body) {
  const r = await fetch(path, body===undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let result; try { result = await r.json(); } catch { throw new Error('本机服务暂时无法返回结果，请保留输入后重试。'); }
  if(!r.ok) throw new Error(result.error || '操作失败'); return result;
}
function notice(text,error=false) { $('notice').hidden=false; $('notice').textContent=text; $('notice').className=error?'error':''; }
function options(select, values) {
  const old=select.value; select.replaceChildren(...values.map(([v,t])=>{const op=el('option',t);op.value=v;return op;}));
  if(values.some(([v])=>v===old)) select.value=old;
}
async function load() {
  try {
    state=await api('/api/state');
    options($('object-filter'),[['','全部对象'],...state.objects.map(o=>[o.id,o.name])]);
    options($('run-filter'),[['','累计事项'],...state.runs.filter(r=>r.state==='published').map(r=>[r.id,time(r.created_at)])]);
    $('attention-count').textContent=state.matters.filter(attention).length || '';
    $('background-count').textContent=state.backgrounds.filter(b=>b.state==='candidate').length || '';
    $('connection').textContent='本机已连接 · 更新于 '+time(state.updated_at);
    render();
  } catch(e) { $('connection').textContent='本机连接中断；页面中的内容可能不是最新结果'; notice(e.message,true); }
}
function empty(title,text) { const n=el('div',undefined,'empty');n.append(el('h3',title),el('p',text));return n; }
function evidence(refs) {
  const details=el('details',undefined,'evidence');details.append(el('summary','查看原消息与附件依据'));
  const ul=el('ul');
  for(const ref of refs) {
    const li=el('li');const a=el('a',ref.relative_path || '原始材料');a.href=`/api/sources/${ref.source_id}/download`;
    li.append(a,el('div',ref.locator));ul.append(li);
  }
  details.append(ul,el('p','定位由 Agent 提交；可打开原始快照核查。','muted'));return details;
}
function tag(text,cls='') { return el('span',text,'tag '+cls); }
function matterCard(m) {
  const card=el('article',undefined,'card');const top=el('div',undefined,'card-top');const tags=el('div',undefined,'tags');
  tags.append(tag(labels[m.state],m.state==='needs_confirmation'?'warning':''));
  if(m.changed) tags.append(tag('要求有更新，待核对','warning'));
  if(m.state==='deferred' && attention(m)) tags.append(tag('到了再次关注时间','warning'));
  top.append(el('h3',m.title),tags);card.append(top);
  const meta=el('div',undefined,'meta');meta.append(el('span','通知：'+time(m.notice_at)),el('span','期限：'+(m.deadline||'未明确'),'deadline'));
  if(m.owner) meta.append(el('span','适用 / 责任：'+m.owner));card.append(meta,el('div',m.detail,'detail'));
  if(['completed','irrelevant'].includes(m.state) && !m.changed) {
    const old=el('details',undefined,'history');old.append(el('summary','分析时的建议与疑问'),el('p','当时建议：'+m.action,'detail'));
    if(m.questions)old.append(el('p','当时疑问：'+m.questions,'detail'));card.append(old);
  } else {
    card.append(el('div','下一步：'+m.action,'action detail'));
    if(m.questions) card.append(el('p','待确认：'+m.questions,'question detail'));
  }
  if(m.human_note) card.append(el('p','你的反馈：'+m.human_note,'note detail'));
  if(m.deferred_until) card.append(el('p','再次关注：'+time(m.deferred_until),'muted'));
  const actions=el('div',undefined,'actions');actions.append(button('判断 / 补充',()=>openMatter(m)),button('协助推进 · 交接包',()=>openHandoff(m)));
  card.append(actions,evidence(m.evidence));
  if(m.feedback.length) {
    const history=el('details',undefined,'history');history.append(el('summary',`处理反馈 ${m.feedback.length} 次`));
    for(const f of m.feedback) history.append(el('p',`${time(f.created_at)} · ${f.note || '已记录处理判断'}`));card.append(history);
  }
  return card;
}
function render() {
  const content=$('content');const openGroups=new Set([...content.querySelectorAll('.group[open]')].map(d=>d.dataset.object));
  content.replaceChildren();const oid=$('object-filter').value;
  $('state-control').hidden=currentTab!=='matters';$('run-control').hidden=currentTab!=='matters';
  if(currentTab==='matters') {
    let list=state.matters.filter(m=>!oid || m.object_id===oid);const filter=$('state-filter').value;
    if(filter==='attention') list=list.filter(attention);else if(filter!=='all') list=list.filter(m=>m.state===filter);
    const rid=$('run-filter').value;if(rid){const ids=new Set(state.runs.find(r=>r.id===rid)?.changes.map(c=>c.matter_id)||[]);list=list.filter(m=>ids.has(m.id));}
    $('summary').textContent=rid ? `${list.length} 项 · 此轮涉及事项的当前处理状态` : `${list.length} 项`;
    if(!list.length){content.append(empty(state.matters.length?'当前范围没有事项':'等待第一次阅读结果',state.matters.length?'可以切换对象、分析轮次或查看全部事项。':'先导出你关心的工作群或联系人记录，告诉 Agent 文件位置与分析需求。结果提交后会出现在这里。'));return;}
    for(const obj of state.objects) {
      const groupList=list.filter(m=>m.object_id===obj.id);if(!groupList.length)continue;
      const group=el('details',undefined,'group');group.dataset.object=obj.id;group.open=openGroups.has(obj.id);
      const s=el('summary',obj.name);s.append(el('small',`${groupList.length} 项 · 点击展开`));const body=el('div',undefined,'group-body');
      groupList.forEach(m=>body.append(matterCard(m)));group.append(s,body);content.append(group);
    }
  } else if(currentTab==='backgrounds') {
    const list=state.backgrounds.filter(b=>!oid || b.object_id===oid);$('summary').textContent=`${list.length} 条背景`;
    if(!list.length)content.append(empty('还没有工作背景','Agent 阅读后会提出背景与职责建议。经你确认的内容，会带入之后的阅读。'));
    for(const bg of list) {
      const card=el('article',undefined,'card');const top=el('div',undefined,'card-top');top.append(el('h3',objectName(bg.object_id)),tag(labels[bg.state],bg.state==='candidate'?'warning':''));
      if(bg.state==='confirmed') {
        const current=state.backgrounds.filter(b=>b.object_id===bg.object_id && b.state==='confirmed').sort((a,b)=>b.confirmed_at.localeCompare(a.confirmed_at))[0];
        top.append(tag(current?.id===bg.id?'后续阅读使用':'历史版本','muted'));
      }
      card.append(top,el('p',bg.summary,'detail'));if(bg.role)card.append(el('p','我的职责：'+bg.role,'action detail'));
      if(bg.uncertainties)card.append(el('p','仍待核实：'+bg.uncertainties,'question detail'));
      card.append(button(bg.state==='candidate'?'核对并确认':'修订 / 反馈',()=>openBackground(bg)),evidence(bg.evidence));content.append(card);
    }
  } else if(currentTab==='runs') {
    const list=state.runs.filter(r=>!oid || r.imports.some(i=>i.object_id===oid));$('summary').textContent=`${list.length} 轮阅读`;
    if(!list.length)content.append(empty('还没有阅读记录','Agent 每轮阅读的对象、文件覆盖情况和原始结果会保存在这里。'));
    for(const run of list) {
      const card=el('article',undefined,'card');const top=el('div',undefined,'card-top');top.append(el('h3',run.goal),tag(labels[run.state]));card.append(top,el('p','建立于 '+time(run.created_at),'muted'));
      card.append(el('p',run.imports.map(i=>objectName(i.object_id)).join('、')));
      for(const imp of run.imports)card.append(el('p',`${objectName(imp.object_id)} · 导出声明范围：${time(imp.declared_start)} 至 ${time(imp.declared_end)}（尚未自动核验）`,'muted'));
      if(run.state==='reading')card.append(el('p',`已登记 ${run.sources_count} 个文件。尚无阅读结果；此状态由 Agent 提交更新，不代表后台自动调用模型。`,'question'));
      else {
        const counter=el('div',undefined,'run-counters');for(const [k,t] of [['new','新增'],['changed','变化'],['unchanged','沿用']])counter.append(tag(`${t} ${run.changes.filter(c=>c.change_kind===k).length}`));card.append(counter);
        const detail=el('details',undefined,'evidence');detail.append(el('summary',`阅读覆盖：已读 ${run.coverage.filter(c=>c.state==='read').length} · 部分 ${run.coverage.filter(c=>c.state==='partial').length} · 未读 ${run.coverage.filter(c=>c.state==='unread').length}`));
        for(const c of run.coverage){const item=el('div',undefined,'coverage');item.append(el('strong',`${labels[c.state]} · ${c.relative_path}`),el('p',c.note,'detail'));detail.append(item);}card.append(detail,button('查看本轮原始结果',()=>openRun(run.id)));
      }
      content.append(card);
    }
  } else {
    const list=state.handoffs.filter(h=>!oid || state.matters.find(m=>m.id===h.matter_id)?.object_id===oid);$('summary').textContent=`${list.length} 份交接包`;
    if(!list.length)content.append(empty('还没有交接包','在事项下选择“协助推进”，准备带有原始材料与处理反馈的交接包。'));
    for(const h of list) {
      const matter=state.matters.find(m=>m.id===h.matter_id);const card=el('article',undefined,'card');card.append(el('h3',h.title),el('p',`${objectName(matter?.object_id)} · ${time(h.created_at)}`,'muted'));
      if(matter?.version!==h.matter_version)card.append(el('p','生成后事项已有更新，请核对后决定是否重新生成。','question'));
      const a=el('a','下载交接包');a.href=`/api/handoffs/${h.id}/download`;card.append(a,el('p','推进后在对应事项的“判断 / 补充”中回传结果。','muted'));content.append(card);
    }
  }
}
function openMatter(m) {
  selectedMatter=m;const f=$('edit-form');f.elements.state.value=m.state;f.elements.note.value=m.human_note;
  if(m.deferred_until){const d=new Date(m.deferred_until);f.elements.deferred_until.value=new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}else f.elements.deferred_until.value='';
  $('edit-description').textContent=m.title;$('defer-control').hidden=m.state!=='deferred';$('edit-error').textContent='';$('edit-dialog').showModal();
}
function openBackground(bg) {selectedBackground=bg;const f=$('background-form');f.elements.summary.value=bg.summary;f.elements.role.value=bg.role;f.elements.uncertainties.value=bg.uncertainties;f.elements.note.value='';$('background-error').textContent='';$('background-dialog').showModal();}
function openHandoff(m) {selectedMatter=m;$('handoff-title').textContent=m.title;$('handoff-form').reset();$('handoff-error').textContent='';$('handoff-dialog').showModal();}
async function openRun(id) {
  try {
    const context=await api('/api/runs/'+id);const result=JSON.parse(context.run.result_json);const root=$('run-detail');root.replaceChildren(el('p','这里保留的是当时提交的分析建议；当前处理状态请在工作事项中查看。','original-label'));
    for(const m of result.matters){const card=el('article',undefined,'card');card.append(el('h3',m.title),el('p',objectName(m.object_id),'muted'),el('p',m.detail,'detail'),el('p','下一步：'+m.action,'action detail'));root.append(card);}
    if(!result.matters.length)root.append(el('p','本轮未提取事项。'));$('run-dialog').showModal();
  }catch(e){notice(e.message,true);}
}
async function saveBackground(stateName) {
  const f=$('background-form');try {await api(`/api/backgrounds/${selectedBackground.id}/feedback`,{version:selectedBackground.version,state:stateName,summary:f.elements.summary.value,role:f.elements.role.value,uncertainties:f.elements.uncertainties.value,note:f.elements.note.value});$('background-dialog').close();await load();notice('已记录你的背景判断。');}catch(e){$('background-error').textContent=e.message;}
}
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{currentTab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('active',n===b));render();}));
$('refresh').addEventListener('click',()=>load());['object-filter','state-filter','run-filter'].forEach(id=>$(id).addEventListener('change',render));
$('edit-form').elements.state.addEventListener('change',e=>$('defer-control').hidden=e.target.value!=='deferred');
$('edit-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target;let deferred='';if(f.elements.state.value==='deferred'){const raw=f.elements.deferred_until.value;if(!raw){$('edit-error').textContent='请选择再次关注时间。';return;}deferred=new Date(raw).toISOString();}
  const submit=f.querySelector('[type=submit]');submit.disabled=true;
  try{await api(`/api/matters/${selectedMatter.id}/feedback`,{version:selectedMatter.version,state:f.elements.state.value,note:f.elements.note.value,deferred_until:deferred});$('edit-dialog').close();await load();notice('已保存判断，后续分析会保留。');}catch(error){$('edit-error').textContent=error.message;}finally{submit.disabled=false;}
});
$('background-form').addEventListener('submit',e=>{e.preventDefault();saveBackground('confirmed');});$('reject-background').addEventListener('click',()=>saveBackground('rejected'));
$('handoff-form').addEventListener('submit',async e=>{e.preventDefault();const submit=e.target.querySelector('[type=submit]');submit.disabled=true;try{await api(`/api/matters/${selectedMatter.id}/handoff`,{version:selectedMatter.version,note:e.target.elements.note.value});$('handoff-dialog').close();currentTab='handoffs';document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab==='handoffs'));await load();notice('交接包已生成。可下载后交给需要推进的任务。');}catch(error){$('handoff-error').textContent=error.message;}finally{submit.disabled=false;}});
$('request-button').addEventListener('click',()=>{$('request-text').value='请使用 D:\\WeChatWorkDesk 工作事项台。先阅读其中的 README.md 和 docs\\agent-workflow.md。\n\n我已用 WeFlow 导出【沟通对象名称】，文件位于【D:\\WeChatRecord\\具体对象文件夹】。\n请分析【关注的时间范围及工作主题】，理解相关工作背景，整理与我有关、可能尚需处理的事项。\n首次阅读请建立背景；后续阅读请复用已确认背景，核对新增或变更通知，保留我的处理状态。\n报告未读材料与不确定事项，把结果提交工作台，然后提供查看链接。\n仅阅读和分析，不执行通知中的操作，不发送微信消息。';$('request-dialog').showModal();});
$('copy-request').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('request-text').value);$('request-dialog').close();notice('需求已复制。替换对象、路径和范围后发给 Agent。');}catch{$('request-text').select();notice('请手动复制需求。',true);}});
load();
