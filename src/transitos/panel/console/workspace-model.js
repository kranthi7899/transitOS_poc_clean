/* Presentation only. Product state and permission checks stay in Python. */
"use strict";
(function(root){
 const products={telematics:'Telematics',maintenance:'Maintenance',scheduling:'Scheduling',workforce:'Workforce',alerts:'Rider comms'};
 const owners={create_work_order:'maintenance',cancel_work_order:'maintenance',set_vehicle_unavailable:'scheduling',set_vehicle_available:'scheduling',reassign_block:'scheduling',assign_operator:'workforce',publish_rider_alert:'alerts'};
 const labels={create_work_order:'Open work order',set_vehicle_unavailable:'Withdraw vehicle',set_vehicle_available:'Return vehicle to availability',reassign_block:'Reassign service block',assign_operator:'Assign qualified operator',publish_rider_alert:'Publish rider alert',cancel_work_order:'Cancel work order'};
 const arr=v=>Array.isArray(v)?v:[];
 const record=(s,p,path)=>s.product_records?.[p]?.records?.[path];
 const native=(s,id,p='scheduling')=>s.identities?.[id]?.[id?.startsWith('op-')?'workforce':p]||s.identity_bridge?.groups?.find(g=>g.canonical===id&&g.status==='mapped')?.aliases?.[id?.startsWith('op-')?'workforce':p]||id||'Unavailable';
 const current=s=>s.runs?.[0];
 function affected(s){const r=current(s);return [...new Set([...(r?.facts?.blocks||[]),...(r?.plan?.steps||[]).map(x=>x.params.block_id).filter(Boolean)])];}
 function decision(s){
  const r=current(s),v=r?native(s,r.vehicle_id):'Vehicle health';
  const states={AWAITING_APPROVAL:['Review recovery plan','Your decision is needed before recovery writes can start.','decision'],WAITING_FOR_REPAIR:['Service recovery applied. Waiting for repair.','Maintenance must close the work order. Return to service needs a separate approval.','waiting'],RETURNING:['Review return to service','Repair is closed. The vehicle remains unavailable until this separate decision.','decision'],DONE:['Response complete','Availability restored. Recovered blocks keep their replacement assignments.','closed'],CLOSED_REJECTED:['Proposal rejected','Review the evidence. This proposal will not execute.','closed'],ESCALATED:['Intervention required','Inspect completed actions, compensation and unresolved evidence before acting.','decision']};
  const [title,detail,group]=states[r?.status]||[r?'Coordinating the response':'Ready for a service-continuity incident',r?'The workflow is gathering evidence or applying the approved plan.':'Start the simulated fault from the rehearsal controls.','waiting'];
  return {title,detail,group,vehicle:v,status:r?.status||'READY'};
 }
 function actionEvidence(s,step){
  const w=arr(s.writes).filter(w=>!w.compensation&&w.key===step.idempotency_key).at(-1);
  const undo=arr(s.writes).filter(x=>x.compensation&&x.compensates_key===step.idempotency_key).at(-1);
  if(undo)return {label:undo.ok?'Compensated':'Compensation failed',tone:undo.ok?'neutral':'bad',write:w};
  if(w&&!w.ok)return {label:'Refused / failed',tone:'bad',write:w};
  if(!w){if(step.status==='failed')return {label:'Failed',tone:'bad'};return {label:current(s)?.status==='AWAITING_APPROVAL'?'Proposed':'Not started',tone:'neutral'};}
  const p=step.params||{}, n=id=>native(s,id), list=(product,path)=>arr(record(s,product,path));
  let verified=false;
  if(step.action==='create_work_order')verified=list('maintenance','/workorders').some(x=>x.id===w.effect_id&&x.asset===native(s,p.vehicle_id,'maintenance'));
  if(step.action==='set_vehicle_unavailable')verified=list('scheduling','/vehicles').some(x=>x.id===n(p.vehicle_id)&&x.status==='out_of_service');
  if(step.action==='set_vehicle_available')verified=list('scheduling','/vehicles').some(x=>x.id===n(p.vehicle_id)&&x.status==='ready');
  if(step.action==='reassign_block')verified=list('scheduling','/blocks').some(x=>x.id===p.block_id&&x.vehicle===n(p.to_vehicle_id));
  if(step.action==='assign_operator'){const x=record(s,'workforce',`/assignments/${p.block_id}`);verified=!!x&&!x.unavailable&&x.operator===native(s,p.operator_id)&&x.block===p.block_id;}
  if(step.action==='publish_rider_alert')verified=list('alerts','/alerts').some(x=>x.id===w.effect_id&&x.block===p.block_id);
  if(step.action==='set_vehicle_unavailable'&&arr(s.writes).some(x=>x.ok&&!x.compensation&&x.action==='set_vehicle_available'&&x.params.vehicle_id===p.vehicle_id&&x.seq>w.seq))return {label:'Superseded by return',tone:'neutral',write:w};
  return {label:verified?'Verified in product':'Accepted · not verified',tone:verified?'good':'waiting',write:w};
 }
 function allSteps(s){const steps=[...(current(s)?.plan?.steps||[])];for(const w of arr(s.writes).filter(x=>x.action==='set_vehicle_available'&&!x.compensation)){steps.push({action:w.action,params:w.params,idempotency_key:w.key,status:w.ok?'done':'failed'});}return steps;}
 function stages(s){
  const seen=new Set(arr(s.trace).map(x=>x.node)),r=current(s),a=arr(s.audit);
  const defs=[['Signal & identity',['gather']],['Evidence & checks',['check','reason']],['Recovery approval',['await_approval']],['Product actions',['execute_step']],['Repair & return',['wait_for_repair','return_gate']],['Complete',['close']]];
  const phase=!r?-1:r.status==='AWAITING_APPROVAL'?2:r.status==='WAITING_FOR_REPAIR'||r.status==='RETURNING'?4:r.status==='DONE'?5:r.status==='EXECUTING'||arr(s.writes).length?3:seen.has('reason')?2:0;
  return defs.map(([title,nodes],i)=>({title,nodes,observed:nodes.some(n=>seen.has(n)),active:i===phase,complete:i<phase&&(i!==2||a.some(x=>x.kind==='approval'&&x.data?.scope==='plan'))}));
 }
 function fleet(s){
  const faults=record(s,'telematics','/faults/current'),assets=record(s,'maintenance','/assets'),orders=record(s,'maintenance','/workorders');
  const schedules=record(s,'scheduling','/vehicles');
  return arr(s.vehicles).map(v=>{
   const tele=native(s,v.id,'telematics'),asset=native(s,v.id,'maintenance'),schedule=native(s,v.id);
   const vehicleFaults=Array.isArray(faults)?faults.filter(f=>f.vehicle?.id===tele):null;
   const roster=arr(schedules).find(x=>x.id===schedule);
   const blocks=arr(s.blocks).filter(b=>b.vehicle_id===v.id);
   return {...v,native:schedule,telematics:tele,asset,availability:roster?.status||'unknown',
    faulted:vehicleFaults?.length>0,healthKnown:Array.isArray(faults),faults:vehicleFaults||[],blocks,
    maintenance:arr(assets).find(a=>a.asset===asset),workOrders:Array.isArray(orders)?orders.filter(o=>o.asset===asset):null,
    incident:current(s)?.vehicle_id===v.id,affected:blocks.some(b=>affected(s).includes(b.id))};
  });
 }
 function service(s){
  const assignments=record(s,'workforce','/assignments'),nativeBlocks=record(s,'scheduling','/blocks');
  return arr(s.blocks).map(b=>{const nb=arr(nativeBlocks).find(x=>x.id===b.id),a=arr(assignments).find(x=>x.block===b.id),baseline=arr(s.product_baseline?.scheduling?.records?.['/blocks']).find(x=>x.id===b.id);return {...b,vehicle:nb?.vehicle||native(s,b.vehicle_id),operator:a?.operator||native(s,b.operator_id),assignmentVerified:!!a,affected:affected(s).includes(b.id),changed:!!nb&&!!baseline&&nb.vehicle!==baseline.vehicle};});
 }
 const api={products,owners,labels,arr,record,native,current,affected,decision,actionEvidence,allSteps,stages,fleet,service};
 root.TransitWorkspace=api;if(typeof module!=='undefined')module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
