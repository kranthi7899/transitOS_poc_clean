"use strict";
const W = TransitWorkspace;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const asTime = value => value ? new Date(value).toLocaleString('en-GB', {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'UTC'})+' UTC' : 'Not recorded';
const pill = (label, tone='neutral') => `<span class="pill ${tone}">${esc(label)}</span>`;
const json = value => `<pre>${esc(JSON.stringify(value,null,2))}</pre>`;
const nat = (id, product) => W.native(state,id,product);
const human = value => String(value ?? 'unknown').replaceAll('_',' ');
const table = (headers, rows, empty='No matching records in this snapshot.') => `<div class="table-scroll"><table><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${headers.length}" class="empty-row">${esc(empty)}</td></tr>`}</tbody></table></div>`;
const cell = value => `<td>${esc(value)}</td>`;
let state = null, selectedVehicle = null, selectedCase = 'live', filter = 'all', query = '', affectedOnly = false;
let pending = false, refreshing = false, failed = false, localError = '', stamp = '', generation = null;
let dispatcher = 'Dispatcher', acknowledged = new Set(), approvalKey = null, approvalBinding = null;
let technicalKey = null, technicalOpener = null, replay = null, replayNode = 'ready', autoSync = false, lastSync = Date.now();
const isReplay = document.documentElement.dataset.mode === 'replay';
const busIcon = '<svg viewBox="0 0 40 22" aria-hidden="true"><rect x="2" y="2" width="36" height="15" rx="3"/><path d="M7 6h5v5H7zm9 0h5v5h-5zm9 0h5v5h-5z"/><circle cx="10" cy="18" r="3"/><circle cx="31" cy="18" r="3"/></svg>';
const rules = {last_spare:'Minimum spare reserve',double_booking:'No overlapping assignment',vehicle_ready:'Replacement ready',asset_active:'Active maintenance asset',vehicle_seats:'Passenger capacity',vehicle_wheelchair:'Wheelchair positions',vehicle_lift:'Lift / ramp suitability',vehicle_fit:'Vehicle type fit',operator_hours_day:'Daily operator hours',operator_hours_week:'Weekly operator hours',operator_licence_class:'Licence class',operator_endorsement:'Passenger endorsement',operator_availability:'Operator availability'};
const ruleName = value => rules[value] || human(value);
function technicalButton(key, label='Inspect raw evidence') { return `<button class="text-button" data-inspect="${esc(key)}">${esc(label)} ↗</button>`; }
function summaryItem(label, value, tone='') { return `<div class="fleet-stat ${tone}"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`; }
function matches(value) { return !query || JSON.stringify(value).toLowerCase().includes(query.toLowerCase()); }
function selectedRow() { return W.fleet(state).find(v=>v.id===selectedVehicle); }
function fleetView() {
 const all = W.fleet(state);
 const counts = {all:all.length,in_service:all.filter(v=>v.availability==='in_service').length,ready:all.filter(v=>v.availability==='ready').length,out_of_service:all.filter(v=>v.availability==='out_of_service').length,fault:all.filter(v=>v.faulted).length};
 $('fleet-summary').innerHTML = summaryItem('Vehicles',all.length)+summaryItem('In service',counts.in_service,'good')+summaryItem('Ready',counts.ready)+summaryItem('Unavailable',counts.out_of_service,'bad')+summaryItem('Fault reported',counts.fault,'warning');
 $('data-source').innerHTML = `<span class="source-led ${failed?'offline':''}"></span><strong>${isReplay?'Recorded product snapshots':state.storage_profile==='databricks'?'Databricks · '+esc(state.connected_instance):'Local scenario · not Databricks'}</strong><span>${all.length} vehicles · ${(state.blocks||[]).length} blocks · ${W.arr(W.record(state,'workforce','/export/operators.csv')?.rows).length} operators</span>`;
 document.querySelectorAll('[data-filter]').forEach(b=>{b.classList.toggle('active',b.dataset.filter===filter);b.setAttribute('aria-pressed',String(b.dataset.filter===filter));});
 const visible = all.filter(v=>(filter==='all'||filter==='fault'&&v.faulted||filter===v.availability)&&matches({...v,operators:v.blocks.map(b=>nat(b.operator_id))}));
 $('fleet-board').innerHTML = visible.map(v=>`<button class="vehicle-card ${v.faulted?'faulted':''} ${v.id===selectedVehicle?'selected':''} ${v.incident?'incident':''}" data-vehicle="${esc(v.id)}" aria-pressed="${v.id===selectedVehicle}"><span class="vehicle-card-top">${busIcon}<strong>${esc(v.native)}</strong>${v.faulted?'<span class="fault-marker" aria-label="Fault reported">!</span>':''}</span><span class="vehicle-status"><i class="status-dot ${esc(v.availability)}"></i>${esc(human(v.availability))}</span><span class="vehicle-sub">${esc(v.depot)} · ${v.blocks.length} blocks${v.faulted?' · fault reported':!v.healthKnown?' · fault evidence unavailable':''}</span></button>`).join('')||'<p class="empty-row">No vehicles match this filter. Other fleet records remain available.</p>';
 const v = selectedRow();
 if(!v){$('vehicle-detail').innerHTML='<p class="empty-row">Select a vehicle to see its connected records alongside the response.</p>';return;}
 const faults = v.faults.map(f=>`SPN ${f.fault?.spn} / FMI ${f.fault?.fmi}${f.fault?.lamps?.stop?' · Stop lamp':''}`).join('; ');
 const nativeOps = W.arr(W.record(state,'workforce','/export/operators.csv')?.rows);
 const links = W.service(state).filter(b=>b.vehicle_id===v.id);
 const operatorIds = [...new Set(links.map(b=>b.operator))];
 $('vehicle-detail').innerHTML = `<div class="section-heading"><div><span class="eyebrow">SELECTED VEHICLE · CONNECTED RECORDS</span><h2>${esc(v.native)} <span class="heading-muted">${esc(v.type)} / ${esc(v.depot)}</span></h2></div>${pill(v.incident?'Incident vehicle':'Fleet vehicle',v.incident?'warning':'neutral')}</div><div class="identity-line"><span>Telematics <b>${esc(v.telematics)}</b></span><span>Maintenance <b>${esc(v.asset)}</b></span><span>Shared ID <b>${esc(v.id)}</b></span></div><div class="vehicle-evidence"><article><span class="eyebrow">TELEMATICS</span><strong class="${v.faulted?'text-warning':''}">${esc(v.healthKnown?faults||'No recorded fault':'Evidence unavailable')}</strong><small>${v.faults[0]?.time?'Event '+asTime(v.faults[0].time):'A missing report is not a health certification.'}</small></article><article><span class="eyebrow">MAINTENANCE</span><strong>${esc(v.workOrders===null?'Evidence unavailable':v.workOrders.length?v.workOrders.map(o=>`${o.wo_no} · ${o.status}`).join(', '):'No work order for this asset')}</strong><small>${esc(v.maintenance?.description||'Asset details unavailable')}${v.maintenance?.meter_mi!=null?' · '+Number(v.maintenance.meter_mi).toLocaleString()+' mi':''}</small></article><article><span class="eyebrow">SCHEDULING × WORKFORCE</span><strong>${esc(human(v.availability))} · ${links.length} assigned blocks</strong><small>${esc(operatorIds.length?operatorIds.join(', '):'No operator assignment in this snapshot')}</small></article></div>${operatorIds.length?`<div class="linked-operators">${operatorIds.map(id=>{const op=nativeOps.find(o=>o.operator===id);return `<span><b>${esc(id)}</b> ${op?`${esc(op.hours_today)}h today · licence ${esc(op.licence_class||'unrecorded')}`:'Roster evidence unavailable'}</span>`;}).join('')}</div>`:''}<div class="section-foot"><span>Read through native product APIs · ${asTime(state.product_records?.scheduling?.captured_at)} simulation time</span>${technicalButton('vehicle:'+v.id,'Source records')}</div>`;
}
function serviceView() {
 const blocks = W.service(state).filter(b=>(!affectedOnly||b.affected)&&matches(b));
 $('service-table').innerHTML = table(['Block','Vehicle','Operator','Service window · UTC'],blocks.map(b=>`<tr class="${b.affected?'affected':''} ${b.vehicle_id===selectedVehicle?'linked':''}"><td><button data-block="${esc(b.id)}" class="table-link">${esc(b.id)}</button>${b.affected?'<small class="row-note">Affected by incident</small>':''}</td><td><button class="table-link" data-vehicle="${esc(b.vehicle_id)}">${esc(b.vehicle)}</button>${b.changed?'<small class="row-note changed">Reassigned</small>':''}</td><td>${esc(b.operator)}${!b.assignmentVerified?'<small class="row-note">Assignment read-back unavailable</small>':''}</td>${cell(asTime(b.start)+' – '+asTime(b.end))}</tr>`));
 const ops = W.arr(W.record(state,'workforce','/export/operators.csv')?.rows);
 $('operator-count').textContent=ops.length+' native roster records';
 $('operators-table').innerHTML=table(['Operator','Hours today / week','Licence','Roster'],ops.filter(matches).map(o=>`<tr class="${W.service(state).some(b=>b.vehicle_id===selectedVehicle&&b.operator===o.operator)?'linked':''}">${cell(o.operator)}${cell(`${o.hours_today||'Unrecorded'} / ${o.hours_week||'Unrecorded'}`)}${cell(o.licence_class||'Unrecorded')}${cell(o.on_extra_board==='true'?'Extra board':'Scheduled')}</tr>`),'Operator roster unavailable or no matching records.');
}
function attentionView() {
 const r=W.current(state),d=W.decision(state),needs=r&&d.group==='decision';
 $('attention-count').textContent=needs?'1':'0';
 $('attention-queue').innerHTML=r?`<button data-case="live" class="attention-item ${selectedCase==='live'?'active':''}"><span class="attention-icon ${d.group}">${needs?'!':d.group==='closed'?'✓':'◷'}</span><span><strong>${esc(d.vehicle)} · Vehicle fault</strong><small>${esc(d.title)}</small></span>${pill(d.group==='decision'?'Decision required':d.group==='closed'?'Completed':'Waiting',d.group==='decision'?'warning':d.group==='closed'?'good':'neutral')}</button>`:'<div class="attention-empty"><span>✓</span><div><strong>No incident awaiting action</strong><small>Monitor the fleet, or inject a simulated fault below.</small></div></div>';
}
function actionTarget(step) {
 const p=step.params||{};
 if(step.action==='reassign_block')return `${p.block_id}: ${nat(p.from_vehicle_id)} → ${nat(p.to_vehicle_id)}`;
 if(step.action==='assign_operator')return `${p.block_id}: ${nat(p.from_operator_id)} → ${nat(p.operator_id)}`;
 if(step.action==='publish_rider_alert')return p.block_id+' · '+(p.message||'Service change');
 return nat(p.vehicle_id);
}
function coverageComparison(plan) {
 const before=W.arr(state.product_baseline?.scheduling?.records?.['/blocks']);
 const assignments=W.arr(state.product_baseline?.workforce?.records?.['/assignments']);
 const rows=(plan?.steps||[]).filter(s=>s.action==='reassign_block');
 return table(['Affected block','Vehicle · current → proposed','Operator · current → proposed'],rows.map(s=>{const p=s.params,cur=W.service(state).find(b=>b.id===p.block_id),op=(plan.steps||[]).find(x=>x.action==='assign_operator'&&x.params.block_id===p.block_id);return `<tr>${cell(p.block_id)}<td>${esc(cur?.vehicle||'Unavailable')} <span class="arrow">→</span> <button class="table-link" data-vehicle="${esc(p.to_vehicle_id)}">${esc(nat(p.to_vehicle_id))}</button><small class="row-note">Before incident: ${esc(before.find(b=>b.id===p.block_id)?.vehicle||nat(p.from_vehicle_id))}</small></td><td>${esc(cur?.operator||'Unavailable')} <span class="arrow">→</span> ${esc(op?nat(op.params.operator_id):cur?.operator||'No change proposed')}<small class="row-note">Before incident: ${esc(assignments.find(b=>b.block===p.block_id)?.operator||'Not recorded')}</small></td></tr>`;}));
}
function checksView(plan) {
 const checks=state.assessment?.items||plan?.clearance||[],flagged=checks.filter(i=>i.status!=='pass');
 const key=['last_spare','vehicle_fit','operator_hours_day','double_booking'];
 return `<div class="checks-summary"><strong>Code-enforced checks</strong><span>${checks.filter(i=>i.status==='pass').length} passed · ${flagged.length} require context</span></div><div class="checks-grid">${checks.filter(i=>key.includes(i.rule)||i.requires_ack).map(i=>`<div class="check ${i.status==='pass'?'pass':'warn'}"><span>${i.status==='pass'?'✓':'!'}</span><div><strong>${esc(ruleName(i.rule))}</strong><small>${esc(i.detail||human(i.status))}</small></div></div>`).join('')}</div>${flagged.length?`<div class="evidence-gaps"><strong>Evidence gaps / checks not evaluated</strong>${flagged.map(i=>`<p>${esc(ruleName(i.rule))}: ${esc(i.detail||human(i.status))}</p>`).join('')}</div>`:''}<details id="all-checks"><summary>All ${checks.length} recorded checks</summary>${checks.map(i=>`<p class="check-line">${pill(human(i.status),i.status==='pass'?'good':'warning')} <strong>${esc(ruleName(i.rule))}</strong> ${esc(i.detail||i.basis)}</p>`).join('')}</details>`;
}
function responseView() {
 if(selectedCase!=='live'){renderPreview();return;}
 const r=W.current(state),d=W.decision(state),plan=r?.plan;
 if(!r){$('case-content').innerHTML=`<section class="empty-case"><span class="eyebrow">VEHICLE HEALTH → SERVICE CONTINUITY</span><h2>Monitor the fleet.<br>Act when it matters.</h2><p>Select any vehicle on the left to inspect its linked records. An event creates a concrete response here, with evidence, checks and a decision.</p><div class="foundation-path"><span>Detect</span><b>→</b><span>Assess</span><b>→</b><span>Approve</span><b>→</b><span>Recover</span></div><button class="button primary" data-command="trigger">Inject simulated vehicle fault →</button></section>`;renderApproval(null);return;}
 let html=`<section class="response-heading"><div><span class="eyebrow">VEHICLE HEALTH / SERVICE CONTINUITY</span><h2>${esc(nat(r.vehicle_id))} · Vehicle fault</h2><p>${W.affected(state).length} affected blocks · plan v${r.plan_version} · ${esc(r.trigger_event?.type)}</p></div>${pill(human(r.status),d.group==='decision'?'warning':d.group==='closed'?'good':'neutral')}</section><div class="decision-banner ${d.group}"><strong>${esc(d.title)}</strong><p>${esc(d.detail)}</p></div><section class="orchestration"><span class="eyebrow">ORCHESTRATION · OBSERVED WORKFLOW</span><div class="stage-strip">${W.stages(state).map(x=>`<span class="stage ${x.active?'active':''} ${x.complete?'complete':''}"><i>${x.complete?'✓':x.active?'●':'○'}</i>${esc(x.title)}</span>`).join('')}</div></section>`;
 if(plan){
  html+=`<section class="response-section"><div class="section-heading"><div><span class="eyebrow">${state.reasoner==='stub'||plan.fallback_used?'CODE-RANKED RECOMMENDATION':'MODEL RECOMMENDATION · CODE VALIDATED'}</span><h3>Service recovery plan</h3></div><span class="small-label">${r.status==='AWAITING_APPROVAL'?'Nothing executed':'Current product state below'}</span></div><p class="rationale">${esc(plan.reasoning)}</p>${coverageComparison(plan)}<details id="alternatives"><summary>${plan.alternatives?.length||0} recorded alternative${plan.alternatives?.length===1?'':'s'} · compare in place</summary>${(plan.alternatives||[]).map((a,i)=>`<div class="alternative"><strong>Alternative ${i+2} · ${esc(a.tier)} fit</strong>${table(['Block','Vehicle','Operator'],(a.assignments||[]).map(x=>`<tr>${cell(x.block_id)}<td><button class="table-link" data-vehicle="${esc(x.spare_id)}">${esc(nat(x.spare_id))}</button></td>${cell(nat(x.operator_id))}</tr>`))}<p>${esc(a.warnings)} warnings · ${esc(a.unverifiable)} unverified</p></div>`).join('')}<p class="section-note">${esc(plan.rank_reason||'No additional ranking explanation recorded.')} Alternatives are not executable selections here.</p></details></section>`;
  html+=`<section class="response-section">${checksView(plan)}</section><section class="response-section"><div class="section-heading"><h3>Product actions & outcomes</h3><span class="small-label">${state.writes.length} adapter calls</span></div><ol class="action-list">${W.allSteps(state).map((step,i)=>{const result=W.actionEvidence(state,step);return `<li><span class="action-number ${result.tone}">${result.tone==='good'?'✓':i+1}</span><div><strong>${esc(W.labels[step.action]||step.action)}</strong><p>${esc(actionTarget(step))}</p><small>${esc(W.products[W.owners[step.action]])}</small></div>${pill(result.label,result.tone==='waiting'?'warning':result.tone)}${technicalButton('action:'+i,'Record')}</li>`;}).join('')}</ol></section>`;
 }
 if(r.status==='ESCALATED'||r.status==='CLOSED_REJECTED')html+=`<section class="exception"><strong>${r.status==='ESCALATED'?'Human intervention needed':'Proposal rejected'}</strong><p>${esc((r.violations||[]).join(' · ')||'No further automatic execution of this proposal.')}</p></section>`;
 const alerts=W.arr(state.alerts);
 if(alerts.length)html+=`<section class="response-section"><h3>Rider communication</h3>${alerts.map(a=>`<div class="rider-alert"><strong>${esc(a.header_text||'Service change')}</strong><p>${esc(a.description_text||'')}</p>${pill(W.arr(state.visible_alerts).some(x=>x.id===a.id)?'Visible in rider feed':'Accepted · feed pending',W.arr(state.visible_alerts).some(x=>x.id===a.id)?'good':'warning')}</div>`).join('')}<p class="section-note">Feed visibility is observed; physical arrival and individual delivery receipts are not modelled.</p></section>`;
 html+=`<section class="response-section"><div class="section-heading"><h3>Operational timeline</h3>${technicalButton('audit','Full audit')}</div><ol class="timeline">${W.arr(state.notifications).slice().reverse().slice(0,8).map(n=>`<li><span class="timeline-dot ${esc(n.severity)}"></span><div><strong>${esc(n.title)}</strong><p>${esc(n.detail)}</p><small>${asTime(n.time)} · ${esc(n.product)}</small></div></li>`).join('')||'<li>No observed activity yet.</li>'}</ol>${technicalButton('trace','Technical workflow trace')}</section>`;
 const openIds=[...document.querySelectorAll('#case-content details[open]')].map(x=>x.id),scroll=$('case-content').parentElement.scrollTop;
 $('case-content').innerHTML=html;
 openIds.forEach(id=>{if($(id))$(id).open=true;});$('case-content').parentElement.scrollTop=scroll;
 renderApproval(r);
}
function renderApproval(r) {
 if(!r||selectedCase!=='live'||!['AWAITING_APPROVAL','RETURNING'].includes(r.status)){$('approval-slot').innerHTML=r&&selectedCase==='live'?`<div class="approval-idle"><span>${r.status==='WAITING_FOR_REPAIR'?'◷ Waiting for maintenance closure':r.status==='DONE'?'✓ Response complete':'No decision available in this state'}</span>${r.status==='WAITING_FOR_REPAIR'?'<button class="button secondary" data-command="repair">Simulate repair closure</button>':''}</div>`:'';return;}
 const returnScope=r.status==='RETURNING',key=`${state.generation}:${r.plan_version}:${returnScope?'return_to_service':'plan'}`;
 if(approvalKey!==key){approvalKey=key;acknowledged.clear();}
 approvalBinding={generation:state.generation,plan_version:r.plan_version,scope:returnScope?'return_to_service':'plan'};
 const needed=[...new Set((r.plan?.clearance||[]).filter(i=>i.requires_ack).map(i=>i.rule))];
 const disabled=state.busy||pending||failed||state.policy.kill_switch||!dispatcher.trim()||needed.some(x=>!acknowledged.has(x));
 $('approval-slot').innerHTML=`<section class="approval-panel" aria-label="Current approval"><div class="approval-heading"><strong>${returnScope?'Separate return-to-service decision':'Review complete? Authorise plan v'+r.plan_version}</strong><span>${state.policy.kill_switch?'Agency automation paused':returnScope?'Repair alone does not restore availability':'Version-bound decision · current facts rechecked before writes'}</span></div>${needed.length?`<div class="required-acks">${needed.map(rule=>`<label><input type="checkbox" name="ack" value="${esc(rule)}" ${acknowledged.has(rule)?'checked':''}>I acknowledge: ${esc(ruleName(rule))}<small>${esc(r.plan.clearance.filter(i=>i.rule===rule).map(i=>i.detail).join(' · '))}</small></label>`).join('')}</div>`:''}<div class="approval-controls"><label>Decision recorded by<input id="dispatcher" maxlength="80" value="${esc(dispatcher)}" autocomplete="name" aria-label="Dispatcher name"></label><button class="button primary" data-command="approve" ${disabled?'disabled':''}>${isReplay?'Replay approval':returnScope?'Approve return':'Approve plan v'+r.plan_version} →</button><button class="button secondary" data-command="reject" ${state.busy||pending||failed||!dispatcher.trim()?'disabled':''}>${returnScope?'Keep unavailable':'Reject'}</button></div><small>${isReplay?'Recorded transition only; no live approval or product write.':needed.some(x=>!acknowledged.has(x))?'Required evidence acknowledgement is incomplete.':'Hard safety constraints cannot be overridden by this decision.'}</small></section>`;
}
const previews={
 'no-show':{name:'Operator no-show',description:'Compare qualified cover without changing the workspace structure.',headers:['Example operator','Hours left','Evidence','Outcome'],rows:[['Example A','3 h','Example qualifications recorded','Eligible example'],['Example B','45 min','Example qualifications recorded','Insufficient hours']],note:'Workforce check-in → eligible cover → run assignment → confirmation'},
 payroll:{name:'Payroll exceptions',description:'Review related scheduling and workforce records in a line-item view.',headers:['Example record','Scheduling','Workforce','Difference'],rows:[['Example 01','8 h','8.5 h','+0.5 h'],['Example 02','7 h','Missing','Evidence needed']],note:'Timecard → recorded duty → source comparison → sign-off'}
};
function renderPreview(){const p=previews[selectedCase];$('case-content').innerHTML=`<section class="response-section"><span class="eyebrow">ILLUSTRATIVE WORKFLOW · READ ONLY</span><h2>${p.name}</h2><p>${p.description}</p><div class="preview-notice">All values below are fictional examples. This workflow is not connected and cannot submit product actions.</div>${table(p.headers,p.rows.map(row=>`<tr>${row.map(cell).join('')}</tr>`))}<p>${p.note}</p><button class="button secondary" data-case="live">← Return to current response</button></section>`;$('approval-slot').innerHTML='<div class="approval-idle">Approval unavailable · illustrative workflow only</div>';}
function render() {
 if(!state)return;
 if(generation!==state.generation){generation=state.generation;selectedCase='live';selectedVehicle=W.current(state)?.vehicle_id||state.vehicles?.[0]?.id;acknowledged.clear();approvalKey=null;stamp='';}
 if(!W.fleet(state).some(v=>v.id===selectedVehicle))selectedVehicle=state.vehicles?.[0]?.id;
 $('agency').textContent=state.agency;
 $('profile').textContent=isReplay?'Recorded replay · simulated products':state.storage_profile==='databricks'?'Databricks-backed product APIs':'Local scenario · not connected to Databricks';
 $('clock').textContent='Simulation clock · '+asTime(state.clock);
 $('observed').textContent='Product read · '+asTime(state.observed_at||Object.values(state.product_records||{})[0]?.observed_at);
 $('spend').textContent=isReplay?'No live API calls':state.spend?`Model $${Number(state.spend.spent_usd).toFixed(4)} / $${Number(state.spend.cap_usd).toFixed(2)}`:state.reasoner==='stub'?'Code-ranked · no paid model calls':state.reasoner;
 const message=failed?'Connection unavailable. Last observed state is shown; actions are disabled.':localError||state.error||pending&&'Request submitted; waiting for the product workflow…'||state.busy&&'Coordinating product evidence and actions…'||state.policy.kill_switch&&'Agency automation paused. Approvals cannot execute product actions.'||isReplay&&'Recorded replay: controls select captured states, not live products.'||'';
 $('notice').hidden=!message;$('notice').textContent=message;$('notice').classList.toggle('error',failed||!!localError||!!state.error);
 $('pause').textContent=state.policy.kill_switch?'Resume agency automation':'Pause agency automation';$('pause').disabled=state.busy||pending||failed||isReplay;$('refresh').disabled=state.busy||pending||failed||isReplay;
 $('auto-sync').disabled=isReplay;$('auto-sync').checked=autoSync;
 const signature=JSON.stringify(state)+selectedVehicle+selectedCase+filter+query+affectedOnly+pending+failed+localError;
 if(signature!==stamp){const focus=document.activeElement,focusId=focus?.id,focusStart=focus?.selectionStart,focusEnd=focus?.selectionEnd;const leftScroll=document.querySelector('.fleet-panel').scrollTop;
  if(focusId==='dispatcher')dispatcher=focus.value;
  stamp=signature;fleetView();serviceView();attentionView();responseView();document.querySelector('.fleet-panel').scrollTop=leftScroll;
  if(focusId&&$(focusId)){const target=$(focusId);target.focus({preventScroll:true});if(focusStart!=null&&target.setSelectionRange)target.setSelectionRange(focusStart,focusEnd);}
 }
 for(const action of ['trigger','repair','advance','reset'])$(action).disabled=state.busy||pending||failed;
 $('trigger').disabled ||=!!W.current(state);$('repair').disabled ||=W.current(state)?.status!=='WAITING_FOR_REPAIR';$('advance').disabled ||=!state.alerts?.length;$('reset').disabled ||=state.storage_profile==='databricks';
 $('profile-note').textContent=isReplay?'Recorded transitions only.':state.storage_profile==='databricks'?'Synthetic data in Databricks. Cloud reset is disabled; use a new named instance. Sync requests use trial compute.':'Small local scenario. Use the connected profile for the full imported Databricks fleet.';
 document.querySelectorAll('[data-command]').forEach(b=>b.disabled ||=state.busy||pending||failed);
}
function technicalData(key) {
 if(key==='system')return ['Data connection',{profile:state.storage_profile||'local',instance:state.connected_instance,reasoner:state.reasoner,observed_at:state.observed_at,simulation_time:state.clock,products:state.product_records,notice:'Simulation time and wall-clock observation time differ. Snapshots are not GPS feeds. Product reads do not certify continuous availability.'}];
 if(key==='audit')return ['Decision audit',{audit:state.audit,latest_revision:state.history?.at(-1),invariant_violations:state.invariant_violations}];
 if(key==='trace')return ['Observed workflow trace',state.trace];
 if(key.startsWith('vehicle:')){const v=W.fleet(state).find(x=>x.id===key.split(':')[1]);return ['Native vehicle evidence',{vehicle:v,identity:state.identities?.[v?.id],source_times:Object.fromEntries(Object.entries(state.product_records||{}).map(([k,v])=>[k,{observed_at:v.observed_at,simulation_time:v.captured_at}]))}];}
 const step=W.allSteps(state)[Number(key.split(':')[1])];return ['Exact action record',step?{step,result:W.actionEvidence(state,step),retry_key:step.idempotency_key}:{}];
}
function inspect(key,button){if(!state)return;technicalKey=key;technicalOpener=button;const [title,data]=technicalData(key);$('technical-title').textContent=title;$('technical-content').innerHTML=json(data);$('technical-dialog').showModal();}
function closeTechnical(){if($('technical-dialog').open)$('technical-dialog').close();technicalKey=null;if(technicalOpener?.isConnected)technicalOpener.focus();}
async function command(action) {
 if(!state||pending||state.busy||failed)return;
 if(['approve','reject'].includes(action)&&(selectedCase!=='live'||!approvalBinding))return;
 const bound=approvalBinding?{...approvalBinding}:null;
 if(isReplay){const next=action==='reset'?'ready':replay.edges[replayNode]?.[action];if(!next){localError='This transition was not recorded. Use the local live workspace for this action.';render();return;}replayNode=next;localStorage.setItem('transitos-replay-node',next);stamp='';await refresh();return;}
 pending=true;localError='';render();
 const payload={action,generation:state.generation,...(['approve','reject'].includes(action)?bound:{}),user:dispatcher.trim(),acknowledged:[...acknowledged]};
 try{const response=await fetch('/api/demo/command',{method:'POST',headers:{'Content-Type':'application/json','X-TransitOS-Demo':'1'},body:JSON.stringify(payload)});if(!response.ok){const error=await response.json();throw new Error(typeof error.detail==='string'?error.detail:'Action refused. Review current product evidence.');}lastSync=Date.now();stamp='';await refresh();}catch(error){localError=error.message;}finally{pending=false;render();}
}
async function refresh(){if(refreshing)return;refreshing=true;try{if(isReplay){if(!replay){const response=await fetch('./replay.json');if(!response.ok)throw new Error();replay=await response.json();}state=structuredClone(replay.states[replayNode]);}else{const response=await fetch('/api/demo/state');if(!response.ok)throw new Error();state=await response.json();}if(failed)stamp='';failed=false;render();}catch{failed=true;if(state)render();else{$('notice').hidden=false;$('notice').textContent='Cannot reach the operations workspace. Check the local server.';}}finally{refreshing=false;}}
document.addEventListener('click',event=>{
 const vehicle=event.target.closest('[data-vehicle]');if(vehicle){selectedVehicle=vehicle.dataset.vehicle;stamp='';render();}
 const block=event.target.closest('[data-block]');if(block){const row=W.service(state).find(b=>b.id===block.dataset.block);if(row){selectedVehicle=row.vehicle_id;stamp='';render();}}
 const filterButton=event.target.closest('[data-filter]');if(filterButton){filter=filterButton.dataset.filter;stamp='';render();}
 const caseButton=event.target.closest('[data-case]');if(caseButton){selectedCase=caseButton.dataset.case;if(selectedCase==='live'&&W.current(state))selectedVehicle=W.current(state).vehicle_id;stamp='';render();}
 const inspectButton=event.target.closest('[data-inspect]');if(inspectButton)inspect(inspectButton.dataset.inspect,inspectButton);
 const action=event.target.closest('[data-command]');if(action)command(action.dataset.command);
});
$('fleet-search').addEventListener('input',event=>{query=event.target.value;stamp='';render();});
$('affected-only').addEventListener('change',event=>{affectedOnly=event.target.checked;stamp='';render();});
$('auto-sync').addEventListener('change',event=>{autoSync=event.target.checked;lastSync=Date.now();});
document.addEventListener('input',event=>{if(event.target.id==='dispatcher'){dispatcher=event.target.value;const r=W.current(state),required=(r?.plan?.clearance||[]).filter(i=>i.requires_ack);const approve=document.querySelector('[data-command="approve"]');if(approve)approve.disabled=pending||state.busy||failed||state.policy.kill_switch||!dispatcher.trim()||required.some(i=>!acknowledged.has(i.rule));const reject=document.querySelector('[data-command="reject"]');if(reject)reject.disabled=pending||state.busy||failed||!dispatcher.trim();}});
document.addEventListener('change',event=>{if(event.target.name==='ack'){event.target.checked?acknowledged.add(event.target.value):acknowledged.delete(event.target.value);stamp='';render();}});
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName)&&!$('technical-dialog').open){event.preventDefault();$('fleet-search').focus();}});
$('close-technical').addEventListener('click',closeTechnical);$('technical-dialog').addEventListener('cancel',event=>{event.preventDefault();closeTechnical();});
$('refresh').addEventListener('click',()=>command('refresh'));$('pause').addEventListener('click',()=>command(state.policy.kill_switch?'resume':'pause'));
for(const action of ['trigger','repair','advance'])$(action).addEventListener('click',()=>{selectedCase='live';command(action);});
$('reset').addEventListener('click',()=>$('confirm-dialog').showModal());$('cancel-reset').addEventListener('click',()=>$('confirm-dialog').close());$('confirm-reset').addEventListener('click',()=>{$('confirm-dialog').close();command('reset');});
if(isReplay)localStorage.setItem('transitos-replay-node','ready');
refresh();setInterval(()=>{refresh();if(autoSync&&!isReplay&&!document.hidden&&!pending&&!state?.busy&&!failed&&Date.now()-lastSync>=60000){lastSync=Date.now();command('refresh');}},1500);
