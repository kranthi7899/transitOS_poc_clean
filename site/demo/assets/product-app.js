"use strict";
const productKey=new URLSearchParams(location.search).get("product")||location.pathname.split("/").pop();
let productStamp="", productReplay;
async function updateProduct(){
  const status=document.getElementById("connection");
  try{
    let data;
    if(document.documentElement.dataset.mode==="replay"){
      if(!productReplay){const res=await fetch("./replay.json");if(!res.ok)throw new Error();productReplay=await res.json();}
      const key=localStorage.getItem("transitos-replay-node")||"ready";
      data=productReplay.states[key]||productReplay.states.ready;
      status.textContent="Recorded replay · follows dispatcher replay in this browser";
    }else{const res=await fetch("/api/demo/state");if(!res.ok)throw new Error();data=await res.json();status.textContent=data.busy?"Updating · actual workflow":"Connected · local simulated products";}
    const stamp=JSON.stringify(data);if(stamp===productStamp)return;productStamp=stamp;
    document.title=`${TransitProducts.meta[productKey]?.[1]||"Unknown product"} · simulated workspace`;
    document.getElementById("product-workspace").innerHTML=TransitProducts.workspace(data,productKey,true)||"Unknown simulated product.";
    document.getElementById("product-updates").innerHTML=TransitProducts.updates({...data,notifications:(data.notifications||[]).filter(n=>n.product===productKey||n.product==="workflow")});
  }catch{status.textContent="Disconnected · displayed records may be stale";}
}
updateProduct();setInterval(updateProduct,800);
