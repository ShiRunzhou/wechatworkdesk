'use strict';
let state = {objects:[], matters:[], backgrounds:[], runs:[], handoffs:[], imports:[]};
let currentTab = 'matters', selectedMatter = null, selectedBackground = null;
let initialReminderSelection=true;
const $ = id => document.getElementById(id);
const labels = {unoperated:'未操作',pending:'待处理',in_progress:'跟进中',completed:'已完成',irrelevant:'与我无关',reading:'阅读中',completed_pending_review:'完成后有变更，待核对',cancelled:'已取消',published:'已提交结果',candidate:'待你确认',confirmed:'已确认',rejected:'未采纳',read:'已读',partial:'部分可读',unread:'未读'};
function el(tag, text, cls) { const n = document.createElement(tag); if(text !== undefined) n.textContent = text; if(cls) n.className = cls; return n; }
function time(value) { if(!value) return '未明确'; const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN',{hour12:false}); }
function selectedFilters(){return [...$('state-filter').querySelectorAll('input:checked')].map(i=>i.value);}
function setFilters(values){for(const input of $('state-filter').querySelectorAll('input'))input.checked=values.includes(input.value);updateFilterSummary();}
function updateFilterSummary(){const values=selectedFilters();$('filter-summary').textContent=values.length?values.map(v=>labels[v]||({attention:'需要关注',unverified:'状态待核实',postponed:'已延期',followup_due:'提醒到期'})[v]).join('、'):'未选择范围';}
function matchesFilters(m,filters){return filters.some(f=>f==='attention'?attention(m):f==='unverified'?!!m.status_unverified:f==='followup_due'?followupDue(m):f==='postponed'?m.state==='pending' && !!m.deferred_until:m.state===f);}
function objectName(id) { return state.objects.find(o=>o.id===id)?.name || '未知对象'; }
function button(text, fn, cls) { const n = el('button',text,cls); n.type='button'; n.addEventListener('click', fn); return n; }
function todayDay(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
function reminderDay(value){if(!value)return '';return value.slice(0,10).replaceAll('/','-');}
function dueDate(value){return !!value && reminderDay(value)<=todayDay();}
function updateDateDisplay(name){const input=$('edit-form').elements[name];$(name+'-display').textContent=input.value?showDay(input.value):'yyyy/mm/dd';}
function showDay(value){return reminderDay(value).replaceAll('-','/');}
function followupDue(m){return (m.state==='in_progress' && dueDate(m.followup_at)) || (m.state==='pending' && dueDate(m.deferred_until));}
function updateProgressReminders(){const due=state.matters.filter(followupDue);const box=$('progress-reminders');box.hidden=!due.length;box.textContent=due.length?`有 ${due.length} 项工作到了提醒日期：${due.map(m=>m.title).join('；')}。请确认进度或设置下一日期。`:'';}
function attention(m){if(m.changed)return true;if(m.state==='pending' && m.deferred_until && !dueDate(m.deferred_until))return false;return !!m.status_unverified || ['unoperated','pending','in_progress'].includes(m.state);}
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
    state=await api('/api/state');updateProgressReminders();
    options($('object-filter'),[['','全部对象'],...state.objects.map(o=>[o.id,o.name])]);
    options($('run-filter'),[['','累计事项'],...state.runs.filter(r=>r.state==='published').map(r=>[r.id,time(r.created_at)])]);
    $('attention-count').textContent=state.matters.filter(attention).length || '';
    $('background-count').textContent=state.backgrounds.filter(b=>b.state==='candidate').length || '';
    $('connection').textContent='本机已连接 · 更新于 '+time(state.updated_at);
    if(initialReminderSelection){if(state.matters.some(followupDue)){setFilters(['followup_due']);currentTab='matters';}initialReminderSelection=false;}
    render();
  } catch(e) { $('connection').textContent='本机连接中断；页面中的内容可能不是最新结果'; notice(e.message,true); }
}
function empty(title,text) { const n=el('div',undefined,'empty');n.append(el('h3',title),el('p',text));return n; }
function evidence(refs) {
  const details=el('details',undefined,'evidence');details.append(el('summary','查看原消息与附件依据'));
  const ul=el('ul');
  for(const ref of refs) {
    const li=el('li');const a=el(ref.cleaned_at?'span':'a',ref.relative_path || '原始材料');if(!ref.cleaned_at)a.href=`/api/sources/${ref.source_id}/download`;
    li.append(a,el('div',ref.locator));if(ref.cleaned_at)li.append(el('p','原始资料已清理；可凭文件名、定位和哈希回查外部备份。','muted'));ul.append(li);
  }
  details.append(ul,el('p','定位由 Agent 提交；可打开原始快照核查。','muted'));return details;
}
function tag(text,cls='') { return el('span',text,'tag '+cls); }
function matterCard(m) {
  const card=el('article',undefined,'card');const top=el('div',undefined,'card-top');const tags=el('div',undefined,'tags');
  tags.append(tag(labels[m.state],'main-state state-'+m.state));if(m.status_unverified)tags.append(tag('状态待核实','warning'));
  if(m.changed) tags.append(tag('要求有更新，待核对','warning'));
  if(m.deferred_until && dueDate(m.deferred_until)) tags.append(tag('到了再次关注时间','warning'));
  top.classList.add('matter-heading');top.append(tags,el('h3',m.title));card.append(top);
  const meta=el('div',undefined,'meta');meta.append(el('span','编号：'+m.reference_code));meta.append(el('span','通知：'+time(m.notice_at)),el('span','期限：'+(m.deadline||'未明确'),'deadline'));
  if(m.owner) meta.append(el('span','适用 / 责任：'+m.owner));card.append(meta,el('div',m.detail,'detail'));
  if(['completed','irrelevant'].includes(m.state) && !m.changed) {
    const old=el('details',undefined,'history');old.append(el('summary','分析时的建议与疑问'),el('p','当时建议：'+m.action,'detail'));
    if(m.questions)old.append(el('p','当时疑问：'+m.questions,'detail'));card.append(old);
  } else {
    card.append(el('div','下一步：'+m.action,'action detail'));
    if(m.questions) card.append(el('p','需要核实：'+m.questions,'question detail'));
  }
  if(m.human_note) card.append(el('p','你的反馈：'+m.human_note,'note detail'));
  if(m.followup_at) card.append(el('p',(followupDue(m)?'进度确认已到期：':'下次进度确认：')+showDay(m.followup_at),followupDue(m)?'question':'muted'));
  if(m.deferred_until) card.append(el('p','再次关注：'+showDay(m.deferred_until),'muted'));
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
    let list=state.matters.filter(m=>!oid || m.object_id===oid);const filters=selectedFilters();
    list=list.filter(m=>matchesFilters(m,filters));
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
  } else if(currentTab==='memory') {
    $('summary').textContent='工作记忆与规则';renderMemory(content,oid);
  } else if(currentTab==='runs') {
    const list=state.runs.filter(r=>!oid || r.imports.some(i=>i.object_id===oid));$('summary').textContent=`${list.length} 轮阅读`;
    if(!list.length)content.append(empty('还没有阅读记录','Agent 每轮阅读的对象、文件覆盖情况和原始结果会保存在这里。'));
    for(const run of list) {
      const card=el('article',undefined,'card');const top=el('div',undefined,'card-top');top.append(el('h3',run.goal),tag(labels[run.state]));card.append(top,el('p','建立于 '+time(run.created_at),'muted'));
      card.append(el('p',run.imports.map(i=>objectName(i.object_id)).join('、')));
      for(const imp of run.imports)card.append(el('p',`${objectName(imp.object_id)} · 导出声明范围：${time(imp.declared_start)} 至 ${time(imp.declared_end)}（尚未自动核验）`,'muted'));
      if(run.state!=='published')card.append(el('p',run.state==='cancelled'?'本轮已取消，未发布分析结果。':`已登记 ${run.sources_count} 个文件。尚无阅读结果；此状态由 Agent 提交更新，不代表后台自动调用模型。`,'question'));
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
  selectedMatter=m;const f=$('edit-form');f.elements.state.value=m.state;f.elements.note.value=m.human_note;f.elements.status_unverified.checked=!!m.status_unverified;
  f.elements.deferred_until.value=reminderDay(m.deferred_until);
  f.elements.followup_at.value=reminderDay(m.followup_at);
  f.elements.memory_category.value=m.memory_category||'';f.elements.memory_note.value=m.memory_note||'';
  $('explicit-rule').checked=false;$('explicit-rule-fields').hidden=true;f.elements.rule_content.value='';f.elements.rule_kind.value='advice';f.elements.rule_terms.value='';f.elements.rule_exceptions.value='';
  updateDateDisplay('deferred_until');updateDateDisplay('followup_at');
  for(const name of ['deferred_until','followup_at']){f.elements[name].min=todayDay();f.elements[name].setCustomValidity('');}
  f.elements.deferred_until.disabled=m.state!=='pending';f.elements.followup_at.disabled=m.state!=='in_progress';
  $('followup-control').hidden=m.state!=='in_progress';
  $('edit-description').textContent=m.reference_code+' · '+m.title;$('defer-control').hidden=m.state!=='pending';$('edit-error').textContent='';$('edit-dialog').showModal();
}
function openBackground(bg) {selectedBackground=bg;const f=$('background-form');f.elements.summary.value=bg.summary;f.elements.role.value=bg.role;f.elements.uncertainties.value=bg.uncertainties;f.elements.note.value='';$('background-error').textContent='';$('background-dialog').showModal();}
function openHandoff(m) {selectedMatter=m;$('handoff-title').textContent=m.reference_code+' · '+m.title;$('handoff-form').reset();$('handoff-error').textContent='';$('handoff-dialog').showModal();}
async function openRun(id) {
  try {
    const context=await api('/api/runs/'+id);const result=JSON.parse(context.run.result_json);const root=$('run-detail');root.replaceChildren(el('p','这里保留的是当时提交的分析建议；当前处理状态请在工作事项中查看。','original-label'));
    if(result.archived)root.append(el('p',result.note,'question'));
    for(const m of result.matters){const card=el('article',undefined,'card');card.append(el('h3',m.title),el('p',objectName(m.object_id),'muted'),el('p',m.detail,'detail'),el('p','下一步：'+m.action,'action detail'));root.append(card);}
    if(!result.matters.length)root.append(el('p','本轮未提取事项。'));$('run-dialog').showModal();
  }catch(e){notice(e.message,true);}
}
async function saveBackground(stateName) {
  const f=$('background-form');try {await api(`/api/backgrounds/${selectedBackground.id}/feedback`,{version:selectedBackground.version,state:stateName,summary:f.elements.summary.value,role:f.elements.role.value,uncertainties:f.elements.uncertainties.value,note:f.elements.note.value});$('background-dialog').close();await load();notice('已记录你的背景判断。');}catch(e){$('background-error').textContent=e.message;}
}
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{currentTab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('active',n===b));render();}));
$('refresh').addEventListener('click',()=>load());['object-filter','run-filter'].forEach(id=>$(id).addEventListener('change',render));
$('state-filter').addEventListener('change',()=>{updateFilterSummary();render();});
$('filter-all').addEventListener('click',()=>{setFilters(['unoperated','pending','in_progress','completed','irrelevant']);render();});
$('filter-noncompleted').addEventListener('click',()=>{setFilters(['unoperated','pending','in_progress']);render();});
$('edit-form').elements.state.addEventListener('change',e=>{$('defer-control').hidden=e.target.value!=='pending';$('followup-control').hidden=e.target.value!=='in_progress';$('edit-form').elements.deferred_until.disabled=e.target.value!=='pending';$('edit-form').elements.followup_at.disabled=e.target.value!=='in_progress';$('edit-form').elements.status_unverified.checked=false;});

for(const name of ['deferred_until','followup_at']){
  const input=$('edit-form').elements[name];
  input.addEventListener('click',()=>{if(typeof input.showPicker==='function'){try{input.showPicker();}catch{}}});
  function validateDate(){
    updateDateDisplay(name);
    input.setCustomValidity('');
    const applicable=input.form.elements.state.value===(name==='deferred_until'?'pending':'in_progress');
    if(!applicable)return;
    let message='';
    if(input.validity.badInput)message='请选择有效日期，不能输入普通文本。';
    else if(input.validity.rangeUnderflow)message='提醒日期不能早于今天，请选择今天或之后的日期。';
    if(message){input.setCustomValidity(message);$('edit-error').textContent=message;}
    else $('edit-error').textContent='';
  }
  input.addEventListener('input',validateDate);
  input.addEventListener('change',validateDate);
  input.addEventListener('invalid',()=>{validateDate();if(!input.validity.valid)$('edit-error').textContent=input.validationMessage;});
}

$('edit-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target;let deferred='';if(f.elements.state.value==='pending' && f.elements.deferred_until.value){deferred=f.elements.deferred_until.value.replaceAll('/','-');}
  const followup=f.elements.state.value==='in_progress' && f.elements.followup_at.value?f.elements.followup_at.value.replaceAll('/','-'):'';
  const submit=f.querySelector('[type=submit]');submit.disabled=true;
  try{const payload={version:selectedMatter.version,state:f.elements.state.value,status_unverified:f.elements.status_unverified.checked,note:f.elements.note.value,deferred_until:deferred,followup_at:followup,memory_category:f.elements.memory_category.value,memory_note:f.elements.memory_note.value};if($('explicit-rule').checked)payload.rule={confirmed:true,content:f.elements.rule_content.value,kind:f.elements.rule_kind.value,match_terms:f.elements.rule_terms.value.split(/[、,，\n]/).map(s=>s.trim()).filter(Boolean),exceptions:f.elements.rule_exceptions.value};await api(`/api/matters/${selectedMatter.id}/feedback`,payload);$('edit-dialog').close();await load();notice($('explicit-rule').checked?'已保存判断，明确的长期规则已生效。':'已保存判断，长期要求或重复行为可在“工作记忆与规则”中核对。');}catch(error){$('edit-error').textContent=error.message;}finally{submit.disabled=false;}
});
$('background-form').addEventListener('submit',e=>{e.preventDefault();saveBackground('confirmed');});$('reject-background').addEventListener('click',()=>saveBackground('rejected'));
$('handoff-form').addEventListener('submit',async e=>{e.preventDefault();const submit=e.target.querySelector('[type=submit]');submit.disabled=true;try{await api(`/api/matters/${selectedMatter.id}/handoff`,{version:selectedMatter.version,note:e.target.elements.note.value});$('handoff-dialog').close();currentTab='handoffs';document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab==='handoffs'));await load();notice('交接包已生成。可下载后交给需要推进的任务。');}catch(error){$('handoff-error').textContent=error.message;}finally{submit.disabled=false;}});
$('request-button').addEventListener('click',()=>{$('request-text').value='请使用 D:\\WeChatWorkDesk 工作事项台。先阅读其中的 README.md 和 docs\\agent-workflow.md。\n\n我已用 WeFlow 导出【沟通对象名称】，文件位于【D:\\WeChatRecord\\具体对象文件夹】。\n请分析【关注的时间范围及工作主题】，理解相关工作背景，整理与我有关、可能尚需处理的事项。\n首次阅读请建立背景；后续阅读请复用已确认背景，核对新增或变更通知，保留我的处理状态。\n报告未读材料与不确定事项，把结果提交工作台，然后提供查看链接。\n仅阅读和分析，不执行通知中的操作，不发送微信消息。';$('request-dialog').showModal();});
$('copy-request').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('request-text').value);$('request-dialog').close();notice('需求已复制。替换对象、路径和范围后发给 Agent。');}catch{$('request-text').select();notice('请手动复制需求。',true);}});
load();

setInterval(updateProgressReminders,30000);

let cleanupCandidates=[];
function cleanupTable(rows,selectable){const table=el('table',undefined,'cleanup-table');const header=el('tr');for(const h of (selectable?['选择','编号','主题','起始时间','完成时间']:['编号','主题','起始时间','完成时间','清理时间']))header.append(el('th',h));table.append(header);for(const r of rows){const tr=el('tr');if(selectable){const td=el('td');const cb=el('input');cb.type='checkbox';cb.checked=true;cb.dataset.id=r.id;td.append(cb);tr.append(td);}for(const v of [r.reference_code,r.title,time(r.started_at),time(r.completed_at),...(selectable?[]:[time(r.cleaned_at)])])tr.append(el('td',v));table.append(tr);}return table;}
$('cleanup-open').addEventListener('click',async()=>{try{const data=await api('/api/cleanup');cleanupCandidates=data.candidates;$('cleanup-list').replaceChildren(cleanupCandidates.length?cleanupTable(cleanupCandidates,true):el('p','没有完成超过30天的事项。'));$('cleanup-logs').replaceChildren(data.logs.length?cleanupTable(data.logs,false):el('p','暂无清理日志。'));$('cleanup-confirm').disabled=!cleanupCandidates.length;$('cleanup-error').textContent='';$('cleanup-dialog').showModal();}catch(e){notice(e.message,true);}});
$('cleanup-confirm').addEventListener('click',async()=>{const selected=new Set([...$('cleanup-list').querySelectorAll('input:checked')].map(i=>i.dataset.id));const items=cleanupCandidates.filter(r=>selected.has(r.id)).map(r=>({id:r.id,version:r.version}));if(!items.length){$('cleanup-error').textContent='请至少勾选一项。';return;}const b=$('cleanup-confirm');b.disabled=true;try{const result=await api('/api/cleanup',{items,confirmed:true});$('cleanup-dialog').close();await load();notice(`已清理 ${result.cleaned} 项，简要日志已保存。`);}catch(e){$('cleanup-error').textContent=e.message;}finally{b.disabled=false;}});
