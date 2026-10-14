"use strict";
window.TransitProducts=(()=>{
  const meta={telematics:["⌁","Signal Desk","Vehicle health","telematics"],maintenance:["▤","Shop Ledger","Repair operations","maintenance"],scheduling:["▱","Service Board","Vehicle roster & blocks","scheduling"],workforce:["♙","Crew Roster","Operators & coverage","workforce"],alerts:["◉","Rider Bulletin","Published service messages","alerts"]};
  const e=v=>String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const json=v=>e(JSON.stringify(v,null,2));
  const list=v=>Array.isArray(v)?v:[];
  const record=(s,key,path,base=false)=>(base?s.product_baseline:s.product_records)?.[key]?.records?.[path];
  const chip=(v,changed=false)=>`<span class="record-value ${changed?"record-changed":""}">${e(v)}${changed?" <small>CHANGED</small>":""}</span>`;
  const table=(heads,rows)=>`<div class="record-table-wrap"><table class="record-table"><thead><tr>${heads.map(h=>`<th>${e(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")||`<tr><td colspan="${heads.length}">No record available yet.</td></tr>`}</tbody></table></div>`;
  const row=cells=>`<tr>${cells.map(c=>`<td>${c}</td>`).join("")}</tr>`;
  const unavailable=(s,key)=>Object.values(s.product_records?.[key]?.records||{}).some(v=>v?.unavailable);
  function summary(s,key){
    if(unavailable(s,key))return "Some native evidence is unavailable";
    if(key==="telematics")return list(record(s,key,"/faults/current")).length?"Fault recorded · T-77":"Monitoring · no fault recorded";
    if(key==="maintenance"){const w=list(record(s,key,"/workorders"))[0];return w?`${w.wo_no} · ${w.status}`:"No incident work order";}
    if(key==="scheduling"){const b=list(record(s,key,"/blocks"))[0],v=list(record(s,key,"/vehicles")).find(v=>v.id==="BUS-44");return `${b?.vehicle||"—"} covers ${b?.id||"—"} · BUS-44 ${v?.status||"unknown"}`;}
    if(key==="workforce"){const a=record(s,key,"/assignments/blk-1");return `${a?.operator||"—"} assigned to ${a?.block||"—"}`;}
    return `${s.alerts.length} published · ${list(record(s,key,"/alerts")).length} visible`;
  }
  function body(s,key){
    if(!s.product_records?.[key])return '<p class="muted">Native product evidence is not included in this recording.</p>';
    let html="";
    if(key==="telematics"){
      const vehicles=list(record(s,key,"/vehicles")).filter(v=>v.id==="T-77"||v.name==="BUS-45"),faults=list(record(s,key,"/faults/current"));
      html=table(["Device ID","Product name","Current fault"],vehicles.map(v=>row([e(v.id),e(v.name),chip(faults.some(f=>f.vehicle.id===v.id)?faults.filter(f=>f.vehicle.id===v.id).map(f=>`${f.fault.lamps.stop?"Stop lamp · ":""}SPN ${f.fault.spn} / FMI ${f.fault.fmi}`).join("; "):"No recorded fault",faults.some(f=>f.vehicle.id===v.id))])));
      html+='<p class="product-note">No GPS or arrival event is modeled here. The fault remains recorded until explicitly cleared; repair closure does not prove a telematics clear signal.</p>';
    }
    if(key==="maintenance"){
      const wos=list(record(s,key,"/workorders"));
      html=table(["Work order","Asset","Shop status","Opened by"],wos.map(w=>row([e(w.wo_no),e(w.asset),chip(w.status,true),e(w.opened_by)])));
      html+='<h3 class="record-subhead">Native asset register</h3>'+table(["Asset ID","Canonical vehicle","Description"],list(record(s,key,"/assets")).filter(a=>s.identity_bridge?.groups.some(g=>g.aliases.maintenance===a.asset)).map(a=>row([e(a.asset),e(canonicalFor(s,key,a.asset)),e(a.description)])));
      html+='<p class="product-note">Maintenance owns the repair record. A closed order resumes the workflow, then a person decides return to service.</p>';
    }
    if(key==="scheduling"){
      const vehicles=list(record(s,key,"/vehicles")).filter(v=>["BUS-44","BUS-45"].includes(v.id)),before=list(record(s,key,"/vehicles",true));
      html=table(["Vehicle roster","Before incident","Current availability"],vehicles.map(v=>{const b=before.find(b=>b.id===v.id);return row([e(v.id),e(b?.status),chip(v.status,v.status!==b?.status)]);}));
      const blocks=list(record(s,key,"/blocks")),baseline=list(record(s,key,"/blocks",true));
      html+='<h3 class="record-subhead">Service block assignment</h3>'+table(["Block","Before incident","Current vehicle"],blocks.map(b=>{const old=baseline.find(o=>o.id===b.id);return row([e(b.id),e(old?.vehicle),chip(b.vehicle,b.vehicle!==old?.vehicle)]);}));
      html+='<p class="product-note">“Ready” means available in the roster, not reassigned to this block. A changed block confirms assignment; physical arrival is not tracked.</p>';
    }
    if(key==="workforce"){
      const a=record(s,key,"/assignments/blk-1"),old=record(s,key,"/assignments/blk-1",true);
      html=table(["Block","Before incident","Current operator"],[row([e(a?.block),e(old?.operator),chip(a?.operator,a?.operator!==old?.operator)])]);
      html+='<h3 class="record-subhead">Native CSV roster</h3>'+table(["Operator","Hours today","Licence","Extra board"],list(record(s,key,"/export/operators.csv")?.rows).filter(o=>["OP-1","OP-2"].includes(o.operator)).map(o=>row([e(o.operator),e(o.hours_today),e(o.licence_class),e(o.on_extra_board)])));
      html+='<p class="product-note">The adapter parses a CSV export for qualifications and hours; operator reassignment is a separate API action.</p>';
    }
    if(key==="alerts"){
      const visible=list(record(s,key,"/alerts"));
      html=table(["Alert","Publication","Rider feed"],s.alerts.map(a=>row([e(a.id),e(a.header_text||"Service change"),chip(visible.some(v=>v.id===a.id)?"Visible":"Waiting for visibility",visible.some(v=>v.id===a.id))])));
      html+=visible.map(a=>`<div class="rider-message"><strong>${e(a.header_text)}</strong><p>${e(a.description_text)}</p><small>Block ${e(a.block)} · ${e(a.id)}</small></div>`).join("");
      html+='<p class="product-note">This is a simulated GTFS-style JSON surface, not a deployed GTFS-Realtime protobuf feed. The visibility delay belongs to this product.</p>';
    }
    if(unavailable(s,key))html='<p class="notice error">A native read was unavailable. Inspect the response below; missing evidence is not a confirmed state.</p>'+html;
    return html;
  }
  function canonicalFor(s,key,id){return s.identity_bridge?.groups.find(g=>g.aliases[key]===id)?.canonical||"Unresolved";}
  function identityLinks(s,key){
    const bridge=s.identity_bridge;if(!bridge)return "";
    const groups=bridge.groups.filter(g=>g.aliases[key]);
    if(key==="alerts")return '<div class="identity-local"><strong>Linked through the service block</strong><p>Rider alerts use a block ID, not their own bus or operator ID. The scheduling block supplies the vehicle link.</p></div>';
    return `<div class="identity-local"><strong>Identity in this product</strong>${groups.map(g=>`<div><code>${e(g.aliases[key])}</code><span>→</span><code>${e(g.canonical)}</code><small>${e(g.label)}</small></div>`).join("")||'<p>No usable mapping for these incident entities. A person must resolve the missing or conflicting identity.</p>'}<p>Native IDs stay in this application. Adapters resolve them to the shared canonical identity.</p></div>`;
  }
  function identityBridge(s){
    const b=s.identity_bridge;if(!b)return "";
    const fault=b.events.find(ev=>ev.type==="VehicleFaultDetected"),refs=fault?.refs;
    return `<section class="identity-bridge"><div class="section-heading"><div><div class="eyebrow">IDENTITY RESOLUTION</div><h2>Different IDs. The same entity.</h2></div><span class="subtle-tag">${e(b.agency)} · TIME-VALID MAPPINGS</span></div><p class="product-note">${e(b.method)}</p><div class="identity-entities">${b.groups.map(g=>`<article class="identity-entity"><div class="identity-entity-head"><span>${e(g.label)}</span><code>${e(g.canonical)}</code><small>${g.status==="mapped"?"Mapping available":g.status==="review_required"?"Human review required":"Unmapped"}</small></div><div class="identity-aliases">${Object.entries(g.aliases).map(([k,id])=>`<div><small>${e(meta[k]?.[1]||k)}</small><code>${e(id)}</code></div>`).join("")||'<p>No usable native IDs.</p>'}</div></article>`).join("")}</div><p class="product-note">Operators have a workforce ID and a canonical ID in this scenario. Scheduling and rider communications join through the service block; separate operator IDs are not invented for them.</p>${fault?`<div class="identity-event-path"><strong>Observed fault-event translation</strong><div><span>Telematics <code>${e(fault.native)}</code></span><b>→</b><span>Resolved event <code>${e(fault.canonical)}</code></span><b>→</b><span>Maintenance <code>${e(refs?.maintenance||"Unresolved")}</code></span><span>Scheduling <code>${e(refs?.scheduling||"Unresolved")}</code></span></div><small>Event ${e(fault.id)} · ${e(fault.time)}. Adapters use destination-native IDs for product calls.</small></div>`:'<p class="identity-wait">Inject the fault to see the actual resolved event and its destination references.</p>'}<details class="native-payload"><summary>Inspect mapping validity and resolved-event evidence</summary><pre>${json(b)}</pre></details>${b.conflicts.length?'<p class="notice error">Conflicting mappings require human review. No alias is selected for an ambiguous entity.</p>':""}</section>`;
  }
  function workspace(s,key,standalone=false){const m=meta[key],p=s.product_records?.[key];if(!m)return "";
    return `<article class="native-product ${key}"><div class="native-product-head"><span class="native-logo">${m[0]}</span><div><small>SIMULATED ${e(p?.role||key)} APPLICATION</small><h2>${m[1]}</h2><p>${m[2]}</p></div><span class="native-status">${s.busy?"Updating":"Snapshot"}</span></div><div class="native-product-body"><p class="native-summary">${e(summary(s,key))}</p>${identityLinks(s,key)}${body(s,key)}<div class="native-source"><span>Native interface</span><strong>${e(p?.format)}</strong><span>Identifiers</span><strong>${e(p?.identity)}</strong></div><details class="native-payload"><summary>Inspect native records & format</summary>${Object.entries(p?.records||{}).map(([path,data])=>`<h4>GET ${e(path)}</h4><pre>${data?.format==="text/csv"?e(data.raw):json(data)}</pre>`).join("")}</details><p class="product-note">Read from this mock application's own API and store · simulated time ${e(p?.captured_at)}.</p>${standalone?"":`<a class="native-open" href="${window.document.documentElement.dataset.mode==="replay"?`./product.html?product=${key}`:`/products/${key}`}" target="_blank" rel="noopener">Open ${m[1]} in its own tab ↗</a>`}</div></article>`;
  }
  function board(s,selected="all"){
    const keys=selected==="all"?Object.keys(meta):[selected];
    return `<div class="estate-boundary"><strong>Five independent mock applications. One coordinated response.</strong><p>Each FastAPI application owns a separate product store and native interface. These original demo workspaces are read-only views of those records, hosted together locally; they are not real vendor screens or separately hosted production platforms.</p></div>${identityBridge(s)}<div class="estate-summary">${Object.entries(meta).map(([k,m])=>`<button data-workspace="${k}"><span>${m[0]} ${m[1]}</span><strong>${e(summary(s,k))}</strong></button>`).join("")}</div><div class="native-grid ${selected==="all"?"":"single"}">${keys.map(k=>workspace(s,k)).join("")}</div>`;
  }
  function updates(s){return (s.notifications||[]).slice().reverse().map(n=>`<article class="dispatcher-update ${n.severity}"><span class="update-dot"></span><div><strong>${e(n.title)}</strong><p>${e(n.detail)}</p><small>${e(n.product)} · ${e(n.time)} · #${n.id}</small></div></article>`).join("")||'<div class="empty-attention"><h3>No incident updates yet</h3><p>Inject a fault in Operations. Actual workflow milestones and adapter outcomes appear here.</p></div>';}
  return {meta,summary,workspace,board,updates,identityBridge};
})();
