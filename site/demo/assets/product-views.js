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
  function workspace(s,key,standalone=false){const m=meta[key],p=s.product_records?.[key];if(!m)return "";
    return `<article class="native-product ${key}"><div class="native-product-head"><span class="native-logo">${m[0]}</span><div><small>SIMULATED ${e(p?.role||key)} APPLICATION</small><h2>${m[1]}</h2><p>${m[2]}</p></div><span class="native-status">${s.busy?"Updating":"Snapshot"}</span></div><div class="native-product-body"><p class="native-summary">${e(summary(s,key))}</p>${body(s,key)}<div class="native-source"><span>Native interface</span><strong>${e(p?.format)}</strong><span>Identifiers</span><strong>${e(p?.identity)}</strong></div><details class="native-payload"><summary>Inspect native records & format</summary>${Object.entries(p?.records||{}).map(([path,data])=>`<h4>GET ${e(path)}</h4><pre>${data?.format==="text/csv"?e(data.raw):json(data)}</pre>`).join("")}</details><p class="product-note">Read from this mock application's own API and store · simulated time ${e(p?.captured_at)}.</p>${standalone?"":`<a class="native-open" href="${window.document.documentElement.dataset.mode==="replay"?`./product.html?product=${key}`:`/products/${key}`}" target="_blank" rel="noopener">Open ${m[1]} in its own tab ↗</a>`}</div></article>`;
  }
  function board(s,selected="all"){
    const keys=selected==="all"?Object.keys(meta):[selected];
    return `<div class="estate-boundary"><strong>Five independent mock applications. One coordinated response.</strong><p>Each FastAPI application owns a separate product store and native interface. These original demo workspaces are read-only views of those records, hosted together locally; they are not real vendor screens or separately hosted production platforms.</p></div><div class="estate-summary">${Object.entries(meta).map(([k,m])=>`<button data-workspace="${k}"><span>${m[0]} ${m[1]}</span><strong>${e(summary(s,k))}</strong></button>`).join("")}</div><div class="native-grid ${selected==="all"?"":"single"}">${keys.map(k=>workspace(s,k)).join("")}</div>`;
  }
  function updates(s){return (s.notifications||[]).slice().reverse().map(n=>`<article class="dispatcher-update ${n.severity}"><span class="update-dot"></span><div><strong>${e(n.title)}</strong><p>${e(n.detail)}</p><small>${e(n.product)} · ${e(n.time)} · #${n.id}</small></div></article>`).join("")||'<div class="empty-attention"><h3>No incident updates yet</h3><p>Inject a fault in Operations. Actual workflow milestones and adapter outcomes appear here.</p></div>';}
  return {meta,summary,workspace,board,updates};
})();
