// Run against a fresh recording of the actual integrated system, not invented fixtures.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const W=require('../../src/transitos/panel/console/workspace-model.js');
const {states}=JSON.parse(fs.readFileSync(process.env.TRANSITOS_UI_RECORDING));
const steps=s=>W.allSteps(s),result=(s,action)=>W.actionEvidence(s,steps(s).find(x=>x.action===action));
test('proposal has zero effects; native operators resolve from actual crosswalk',()=>{
 assert.equal(states.proposal.writes.length,0);assert.equal(W.native(states.proposal,'op-2'),'OP-2');
 assert.ok(steps(states.proposal).every(x=>W.actionEvidence(states.proposal,x).label==='Proposed'));
});
test('publication acceptance is separate from actual feed visibility',()=>{
 assert.equal(result(states.approved,'publish_rider_alert').label,'Accepted · not verified');
 assert.equal(result(states.alert_visible,'publish_rider_alert').label,'Verified in product');
 assert.equal(result(states.approved,'assign_operator').label,'Verified in product');
});
test('read-back must match the exact block and expected replacement',()=>{
 const s=structuredClone(states.approved),step=steps(s).find(x=>x.action==='reassign_block');
 s.product_records.scheduling.records['/blocks'][0].vehicle='DIFFERENT';
 assert.equal(W.actionEvidence(s,step).label,'Accepted · not verified');
 const another={...step,idempotency_key:'unexecuted-block-key',params:{...step.params,block_id:'different-block'}};
 assert.equal(W.actionEvidence(states.approved,another).label,'Not started');
});
test('unavailable product response never verifies an accepted action',()=>{
 const s=structuredClone(states.approved);s.product_records.maintenance.records['/workorders']={unavailable:true};
 assert.equal(result(s,'create_work_order').label,'Accepted · not verified');
});
test('compensation supersedes original success',()=>{
 const s=structuredClone(states.approved),step=steps(s)[0];
 s.writes.push({compensation:true,compensates_key:step.idempotency_key,ok:true});
 assert.equal(W.actionEvidence(s,step).label,'Compensated');
 s.writes.at(-1).ok=false;assert.equal(W.actionEvidence(s,step).label,'Compensation failed');
});
test('return approval stays distinct; earlier withdrawal is superseded',()=>{
 assert.equal(W.decision(states.return).group,'decision');assert.equal(W.decision(states.approved).group,'waiting');
 assert.equal(W.decision(states.done).group,'closed');
 assert.equal(result(states.done,'set_vehicle_available').label,'Verified in product');
 assert.equal(result(states.done,'set_vehicle_unavailable').label,'Superseded by return');
});
test('fleet board includes every observed vehicle, not just incident identities',()=>{
 const fleet=W.fleet(states.proposal);
 assert.equal(fleet.length,states.proposal.vehicles.length);
 assert.ok(fleet.some(v=>v.native==='BUS-46'));
 const incident=fleet.find(v=>v.incident);
 assert.equal(incident.availability,'in_service');
 assert.equal(incident.faulted,true); // Fault does not fabricate a withdrawal before approval.
 assert.ok(fleet.filter(v=>!v.faulted).every(v=>v.healthKnown));
});
test('missing fault evidence does not imply healthy buses or fabricate a fault',()=>{
 const s=structuredClone(states.proposal);s.product_records.telematics.records['/faults/current']={unavailable:true};
 assert.ok(W.fleet(s).every(v=>!v.healthKnown&&!v.faulted));
});
test('service list keeps all blocks with native workforce read-back',()=>{
 const rows=W.service(states.approved);assert.equal(rows.length,states.approved.blocks.length);
 assert.ok(rows.every(b=>b.assignmentVerified));assert.equal(rows[0].operator,'OP-2');
});
