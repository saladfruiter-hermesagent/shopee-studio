'use strict';
// Estúdio (PLANO_ESTUDIO.md, S1 e S2): rascunho assistido do vídeo — fotos, ficha visual, formato, roteiros,
// geração no Flow com aprovação do custo, prévia na moldura da Shopee Vídeo e envio ao Telegram só depois da aprovação.
// Usa $, esc, api, post, money, date, toast e view de app.js (scripts clássicos compartilham o escopo global).
let studioData=null,studioDraft=null,studioTab='day',studioPoll=null,studioStyle='problema';
const studioFields=[['hook','Gancho · 0–2 s'],['demo','Demonstração · 2–6 s'],['benefit','Prova · 6–8 s'],['cta','Chamada · 8–10 s']];
const problemFields=[['hook','Problema real + câmera · 0–3 s'],['demo','O produto resolve · 3–6 s'],['benefit','Rotina melhor · 6–8 s'],['cta','Toque no produto · 8–10 s']];
function styleLabel(key){const st=(studioData?.styles||[]).find(x=>x.key===key);return st?st.label:key;}
const deliveryText={pending:'aguardando',sending:'enviando',sent:'entregue',failed:'não entregue',unknown:'não confirmado',blocked:'bloqueado',refused:'recusado'};
function hm(iso){return iso?new Date(iso).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';}

async function studioLoad(){
 try{studioData=await api('/studio');}catch(e){return toast(e.message);}
 renderStudioLists();
}
function studioTabs(name){
 studioTab=name;
 document.querySelectorAll('[data-studio-tab]').forEach(b=>{const active=b.dataset.studioTab===name;b.classList.toggle('active',active);b.setAttribute('aria-selected',active?'true':'false');b.tabIndex=active?0:-1;});
 $('#studio-day').hidden=name!=='day';$('#studio-manual').hidden=name!=='manual';
}
/* Um tablist de verdade responde às setas: seleção acompanha o foco (Home/End nas pontas). */
function studioTabKeys(event){
 const tabs=Array.from(document.querySelectorAll('[data-studio-tab]'));
 const index=tabs.indexOf(event.currentTarget);if(index<0)return;
 const last=tabs.length-1;
 const next={ArrowRight:(index+1)%tabs.length,ArrowLeft:(index+last)%tabs.length,Home:0,End:last}[event.key];
 if(next===undefined)return;
 event.preventDefault();tabs[next].focus();studioTabs(tabs[next].dataset.studioTab);
}
function studioCard(item,action){
 return '<article class="studio-item">'+(item.image?'<img src="'+esc(item.image)+'" alt="" width="84" height="84" decoding="async" loading="lazy" referrerpolicy="no-referrer">':'<div class="studio-noimg">sem foto</div>')+
  '<div><h3 title="'+esc(item.name)+'">'+esc(item.name)+'</h3><p class="muted">'+money(item.price)+' · comissão '+money(item.commission)+'</p>'+(item.note?'<p class="studio-note'+(item.bad?' bad':'')+'">'+esc(item.note)+'</p>':'')+action+'</div></article>';
}
function renderStudioLists(){
 const d=studioData;if(!d)return;
 $('#studio-day-hint').textContent=d.slots?'Os '+d.slots+' melhores de cada envio esperam por você aqui. Sem nenhuma geração até '+d.cutoff+', viram vídeo automático.':'Reserva desligada: em Preferências → Vídeo, defina quantos produtos de cada envio vêm para o Estúdio.';
 $('#studio-day-list').innerHTML=d.today.map(i=>{
  const waiting=i.delivery==='pending'&&!i.video&&!i.fallback;
  const note=i.fallback?'Passou do corte: seguiu para o vídeo automático'+(i.video?' ('+i.video.label+')':'')+'.':waiting?'Reservado até '+hm(i.deadline)+'.':'Telegram: '+(deliveryText[i.delivery]||i.delivery)+(i.video?' · vídeo: '+i.video.label:'')+'.';
  return studioCard(Object.assign({},i,{note,bad:i.fallback||['failed','blocked','refused'].includes(i.delivery)}),
   '<button class="secondary" data-studio-day="'+i.run_id+'" data-key="'+esc(i.key)+'">'+(i.session_id?'Continuar rascunho':'Abrir no Estúdio')+'</button>');
 }).join('')||'<p class="muted">Nenhum produto reservado ainda. Eles aparecem aqui depois do envio da consulta do dia.</p>';
 $('#studio-manual-list').innerHTML=d.manual.map(i=>studioCard(Object.assign({},i,{note:'Criado em '+hm(i.created)+(i.video?' · vídeo: '+i.video.label:(i.scripts?' · '+i.scripts+' roteiros':'')+(i.chosen!==null?' · roteiro escolhido':''))}),
  '<button class="secondary" data-studio-open="'+i.id+'">Abrir</button>')).join('')||'<p class="muted">Cole o link de um produto para começar.</p>';
}
async function studioOpen(id){try{studioDraft=await api('/studio/'+id);studioStyle=studioDraft.scripts_style||'problema';showEditor();}catch(e){toast(e.message);}}
async function openStudioDay(runId,key){
 view('studio');
 try{studioDraft=await post('/studio/day',{run_id:runId,key});studioStyle=studioDraft.scripts_style||'problema';showEditor();}catch(e){toast(e.message);studioLoad();}
}
async function showEditor(){if(!studioData)await studioLoad();$('#studio-home').hidden=true;$('#studio-editor').hidden=false;renderEditor();watchJob();$('#studio-editor').scrollIntoView();}
function closeEditor(){studioDraft=null;clearTimeout(studioPoll);clearTimeout(videoPoll);$('#studio-editor').hidden=true;$('#studio-home').hidden=false;studioLoad();}

function watchJob(){
 clearTimeout(studioPoll);
 if(studioDraft?.job?.status!=='running')return;
 studioPoll=setTimeout(async()=>{
  if(!studioDraft)return;
  try{const fresh=await api('/studio/'+studioDraft.id);const done=fresh.job?.status!=='running';studioDraft=fresh;
   if(done){renderEditor();toast(fresh.job.status==='ok'?'Pronto: '+fresh.job.message:fresh.job.message);}else renderJob();}
  catch(e){toast(e.message);}
  watchJob();
 },3000);
}
function renderJob(){const j=studioDraft.job,box=$('#studio-job');if(!box)return;
 box.hidden=!j;if(!j)return;box.className='studio-job '+j.status;
 box.textContent=j.status==='running'?j.message+' (pode levar alguns minutos; pode sair desta tela)':j.status==='ok'?'✓ IA: '+j.message:j.message;
 document.querySelectorAll('[data-studio-run]').forEach(b=>b.disabled=j.status==='running');}

function renderEditor(){
 const s=studioDraft,p=s.product,r=s.reservation,running=s.job?.status==='running';
 const head='<button type="button" class="text-button studio-back" data-studio-back>← Voltar à lista</button>'+
  '<article class="panel studio-head">'+(p.image?'<img src="'+esc(p.image)+'" alt="" width="120" height="120" decoding="async" referrerpolicy="no-referrer">':'')+'<div><p class="eyebrow">'+(s.origin==='dia'?'DO DIA':'MANUAL · fora do teto diário')+'</p><h2>'+esc(p.name)+'</h2>'+
  '<p class="muted">'+money(p.price)+' · comissão '+money(p.commission)+' · ★ '+Number(p.rating||0).toFixed(1)+' · '+Number(p.sales||0).toLocaleString('pt-BR')+' vendas · <a href="'+esc(p.link)+'" target="_blank" rel="noopener noreferrer">Ver anúncio ↗</a></p>'+
  (r?'<p class="studio-note'+(r.fallback?' bad':'')+'">'+(r.fallback?'Passou do corte: o produto seguiu para o vídeo automático.':'Reservado para você até '+hm(r.deadline)+'. Sem nenhuma geração até lá, vira vídeo automático.')+'</p>':'')+
  s.warnings.map(w=>'<p class="warning">'+esc(w)+'</p>').join('')+'</div></article><p id="studio-job" hidden></p>';
 const photos='<article class="panel"><p class="eyebrow">1 · FOTOS</p><h3>Referências do produto</h3><p class="hint">Mande fotos de ângulos diferentes: frente, verso, detalhes, o produto na mão (mostra o tamanho). A ★ marca a principal. As fotos ficam só no painel.</p><div class="studio-photos">'+
  s.photos.map(ph=>'<figure class="'+(ph.main?'main':'')+'"><img src="'+esc(ph.url)+'" alt="" width="110" height="110" decoding="async" referrerpolicy="no-referrer"><figcaption>'+(ph.source==='api'?'Anúncio':'Sua foto')+'</figcaption><div><button type="button" title="Usar como principal" data-photo-main="'+esc(ph.id)+'">'+(ph.main?'★':'☆')+'</button><button type="button" title="Remover" data-photo-remove="'+esc(ph.id)+'">✕</button></div></figure>').join('')+
  '</div><label class="secondary studio-upload'+(s.user_photos>=s.max_photos?' disabled':'')+'">+ Adicionar fotos · '+s.user_photos+' de '+s.max_photos+' suas<input id="studio-files" type="file" accept="image/*" multiple '+(s.user_photos>=s.max_photos?'disabled':'')+'></label></article>';
 const facts='<article class="panel"><p class="eyebrow">2 · FICHA VISUAL</p><h3>O que dá para ver no produto</h3><p class="hint">A IA descreve só o que aparece nas fotos. Corrija o que estiver errado: o roteiro e o prompt só podem usar o que estiver aqui.</p>'+
  '<button type="button" class="secondary" data-studio-run="facts" '+(running?'disabled':'')+'>'+(s.facts?'Descrever de novo':'Descrever as fotos com a IA')+'</button><textarea id="studio-facts" rows="8" maxlength="1500" placeholder="- Pote transparente com tampa azul&#10;- Quatro travas laterais">'+esc(s.facts)+'</textarea><button type="button" class="secondary" data-studio-facts-save>Salvar ficha</button></article>';
 const format='<article class="panel"><p class="eyebrow">3 · FORMATO</p><h3>Como a câmera filma e quem aparece</h3><p class="hint">A abordagem (problema e solução, antes e depois, teste…) vem nos roteiros.</p><div class="studio-formats">'+
  studioData.formats.map(f=>'<label class="studio-format'+(s.format===f.key?' active':'')+'" title="'+esc(f.text)+'"><input type="radio" name="studio-format" value="'+esc(f.key)+'" '+(s.format===f.key?'checked':'')+'><b>'+(s.format===f.key?'✓ ':'')+esc(f.label)+'</b><small>'+esc(f.text.split(' — ')[1]||'')+'</small><i>'+(f.person?'com pessoa':'só mãos')+' · '+(f.speaks?'fala para a câmera':'voz em off')+'</i></label>').join('')+'</div></article>';
 const changed=s.scripts.length&&s.scripts_format&&s.scripts_format!==s.format;
 const problem=s.scripts_style==='problema';
 const styles='<div class="studio-styles" role="radiogroup" aria-label="Estilo do roteiro">'+(studioData.styles||[]).map(st=>'<label class="studio-format'+(studioStyle===st.key?' active':'')+'"><input type="radio" name="studio-style" value="'+esc(st.key)+'" '+(studioStyle===st.key?'checked':'')+'><b>'+(studioStyle===st.key?'✓ ':'')+esc(st.label)+'</b><small>'+esc(st.text)+'</small></label>').join('')+'</div>';
 const scripts='<article class="panel"><p class="eyebrow">4 · ROTEIROS</p><h3>Três abordagens diferentes</h3><p class="hint">Escolha o estilo, gere 3 roteiros, edite à vontade e escolha um.</p>'+styles+
  (changed?'<p class="warning">Os roteiros abaixo foram feitos no formato anterior. Gere de novo para usar o formato escolhido.</p>':'')+
  (s.scripts.length&&s.scripts_style&&s.scripts_style!==studioStyle?'<p class="warning">Os roteiros abaixo são do estilo “'+esc(styleLabel(s.scripts_style))+'”. Gere de novo para usar o estilo escolhido.</p>':'')+
  '<button type="button" class="secondary" data-studio-run="scripts" '+(running||!s.format?'disabled':'')+'>'+(s.scripts.length?'Gerar outros 3 roteiros':'Gerar 3 roteiros')+'</button>'+(s.format?'':'<span class="hint"> Escolha o formato primeiro.</span>')+
  '<div class="studio-scripts">'+s.scripts.map((sc,i)=>'<div class="studio-script'+(s.chosen===i?' chosen':'')+'" data-index="'+i+'"><div class="studio-script-head"><b>'+(i+1)+'</b><input data-sf="angle" value="'+esc(sc.angle)+'" maxlength="60" aria-label="Abordagem do roteiro '+(i+1)+'">'+(sc.edited?'<span class="content-pill">editado</span>':'')+'</div>'+
   (problem?problemFields:studioFields).map(([k,l])=>'<label>'+l+'<textarea data-sf="'+k+'" rows="2" maxlength="220">'+esc(sc[k])+'</textarea></label>').join('')+
   (problem?'<fieldset class="studio-callouts"><legend>Chamadas na tela</legend>'+(studioData.callout_roles||[]).map((r,n)=>'<label class="callout-'+(n+1)+'">'+esc(r)+'<input data-callout="'+n+'" maxlength="26" value="'+esc((sc.callouts||[])[n]||'')+'"></label>').join('')+'</fieldset>':'')+
   '<label>Narração · '+sc.words+' palavras ('+(s.narration_words||[18,25]).join('–')+')'+(problem?' · falada, sem legenda na tela':'')+'<textarea data-sf="narration" rows="3" maxlength="300">'+esc(sc.narration)+'</textarea></label>'+
   (sc.problems.length?'<ul class="studio-problems">'+sc.problems.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'<p class="studio-ok">✓ Dentro das regras</p>')+
   '<div class="studio-actions"><button type="button" class="secondary" data-script-save="'+i+'">Salvar edição</button><button type="button" class="'+(s.chosen===i?'primary':'secondary')+'" data-script-choose="'+i+'">'+(s.chosen===i?'✓ Roteiro escolhido':'Usar este roteiro')+'</button></div></div>').join('')+'</div>'+
  (s.caption?'<div class="studio-caption"><p class="eyebrow">LEGENDA DO TELEGRAM · '+s.caption.length+'/150</p><label>Descrição<textarea id="studio-desc" rows="2" maxlength="300">'+esc(s.caption.description)+'</textarea></label><label>Hashtags (separadas por espaço)<input id="studio-tags" value="'+esc(s.caption.hashtags.join(' '))+'"></label>'+
   (s.caption.problems.length?'<ul class="studio-problems">'+s.caption.problems.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+'<button type="button" class="secondary" data-caption-save>Salvar legenda</button></div>':'')+'</article>';
 const prompt='<article class="panel" id="studio-video"></article>';
 $('#studio-editor').innerHTML=head+photos+facts+format+scripts+prompt;
 renderJob();
 renderVideoPanel();
}

/* ---------- 5 · vídeo no Flow (S2): aprovar custo, acompanhar, prévia na moldura, enviar ou descartar ---------- */
const videoActive=['queued','working','preparing','approving','submitted','downloading','subtitling','uploading','sending'];
const videoRetry=['failed','unknown','discarded','skipped'];
let videoPoll=null,videoPolls=0;
function videoBlocker(){
 const s=studioDraft,g=s.generation,r=s.reservation;
 if(!s.prompt)return 'Escolha um roteiro (passo 4) para montar o prompt.';
 if(s.scripts_format&&s.scripts_format!==s.format)return 'Os roteiros são de outro formato: gere de novo ou volte o formato.';
 if(!g.refs.length)return 'Sem fotos de referência: mantenha a do anúncio ou envie fotos suas.';
 if(r&&r.fallback)return 'Passou do corte: o produto já seguiu para o vídeo automático.';
 if(!g.enabled)return 'Geração de vídeo desligada em Preferências → Vídeo.';
 if(g.paused)return 'Vídeos pausados: '+g.paused.reason;
 if(g.worker_problem)return g.worker_problem;
 if(s.origin==='dia'&&g.used+g.cost>g.cap)return 'Teto diário atingido: '+g.used+' de '+g.cap+' créditos usados hoje.';
 return '';
}
function videoBudget(){const g=studioDraft.generation;
 return studioDraft.origin==='dia'?'Do dia: conta no teto diário ('+g.used+' de '+g.cap+' créditos usados hoje).':'Manual: fora do teto diário. Em vídeos manuais: '+g.manual.today+' créditos hoje, '+g.manual.month+' no mês.';}
function shopeeFrame(v){
 const s=studioDraft,p=s.product,c=s.caption;
 return '<div class="shopee-frame"><video src="'+esc(v.url)+'" controls playsinline preload="metadata" width="270" height="480"></video>'+
  '<div class="sv-side" aria-hidden="true"><span>♥</span><span>✉</span><span>↗</span></div>'+
  '<div class="sv-bottom" aria-hidden="true"><b class="sv-user">@sua_loja</b>'+(c?'<p class="sv-caption">'+esc(c.text)+'</p>':'')+
  '<div class="sv-product">'+(p.image?'<img src="'+esc(p.image)+'" alt="" width="34" height="34" referrerpolicy="no-referrer">':'')+'<span><b>'+esc(p.name)+'</b><small>'+money(p.price)+'</small></span></div></div></div>';
}
function videoFacts(v){const m=v.meta||{},f=m.fidelity;
 return '<ul class="studio-video-facts">'+(m.duration?'<li>'+m.width+'×'+m.height+' · '+Number(m.duration).toFixed(1)+' s · '+(m.callouts?m.callouts+' chamadas na tela · fala sem legenda':(m.subtitles||0)+' legendas')+'</li>':'')+
  (f?(f.score!=null?'<li class="'+(f.score<7?'bad':'ok')+'">Fidelidade ao produto: '+f.score+'/10 · '+esc(f.reason)+'</li>':'<li>Fidelidade não avaliada ('+esc(f.error)+')</li>'):(v.state==='review'?'<li>Conferindo a fidelidade ao produto…</li>':''))+
  '<li>'+v.credits+' créditos · gerado em '+hm(v.updated||v.created)+'</li></ul>';
}
function videoPreview(v){
 const s=studioDraft,c=s.caption;
 let actions='';
 if(v.state==='review')actions='<button type="button" class="primary" data-video-send="'+v.id+'">Aprovar e enviar ao Telegram</button><button type="button" class="secondary" data-video-regenerate="'+v.id+'">Descartar e gerar outro · '+s.generation.cost+' créditos</button><button type="button" class="text-button danger" data-video-discard="'+v.id+'">Só descartar</button>';
 else if(v.state==='delivery_failed')actions='<p class="warning">'+esc(v.error||'O Telegram recusou o vídeo.')+'</p><button type="button" class="primary" data-video-send="'+v.id+'">Reenviar ao Telegram</button><button type="button" class="text-button danger" data-video-discard="'+v.id+'">Descartar</button>';
 else if(v.state==='sent')actions='<p class="studio-ok">✓ Enviado ao Telegram com a legenda e os links. Poste o vídeo na Shopee Vídeo com o produto marcado.</p>';
 else if(v.state==='delivery_unknown')actions='<p class="warning">Entrega não confirmada: confira o Telegram. Não há reenvio automático.</p>';
 const message=c?'<div class="studio-message"><p class="eyebrow">LEGENDA QUE VAI AO TELEGRAM</p><p>'+esc(c.text)+'</p>'+(c.problems.length?'<ul class="studio-problems">'+c.problems.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+'<p class="hint">Mais os links do produto. Para mudar, edite a legenda no passo 4 antes de enviar.</p></div>':'';
 return '<div class="studio-preview">'+(v.url?shopeeFrame(v):'<p class="hint">O arquivo do vídeo não está mais no painel.</p>')+
  '<div class="studio-preview-side"><p class="studio-video-state '+(v.state==='review'?'running':v.state==='sent'?'ok':'')+'">Vídeo #'+v.id+': '+esc(v.label)+'</p>'+videoFacts(v)+(v.state!=='sent'?message:'')+
  '<div class="studio-actions">'+actions+'</div>'+(v.url?'<a class="text-button" href="'+esc(v.url)+'?download=1" download>Baixar o vídeo (MP4)</a>':'')+'</div></div>';
}
function renderVideoPanel(){
 const box=$('#studio-video');if(!box||!studioDraft)return;
 const s=studioDraft,g=s.generation,v=s.video;
 let h='<p class="eyebrow">5 · VÍDEO</p><h3>Gerar no Flow e revisar antes de enviar</h3>'+
  '<p class="hint">Vão para o Flow '+g.refs.length+' foto(s) de referência (até '+g.max_refs+', a principal primeiro). Nada vai ao Telegram antes da sua aprovação.</p>'+
  '<div class="studio-refs">'+g.refs.map((u,i)=>'<img src="'+esc(u)+'" alt="Referência '+(i+1)+'" width="64" height="64" decoding="async" referrerpolicy="no-referrer">').join('')+'</div>'+
  (s.prompt?'<details class="studio-details"><summary>Prompt que vai ao Flow</summary><pre class="studio-prompt">'+esc(s.prompt)+'</pre></details>':'');
 if(v&&videoActive.includes(v.state))
  h+='<p class="studio-video-state running">Vídeo #'+v.id+': '+esc(v.label)+' · '+v.credits+' créditos. A fila do Flow pode levar até 1 h; pode sair desta tela.'+(g.worker_online?'':' O worker de vídeo está offline agora: a geração espera na fila.')+'</p>';
 else if(v&&['review','sent','delivery_failed','delivery_unknown'].includes(v.state))h+=videoPreview(v);
 else if(v&&['failed','unknown'].includes(v.state))
  h+='<p class="studio-video-state failed">Vídeo #'+v.id+': '+esc(v.label)+'. '+esc(v.error||'')+(v.state==='unknown'?' Confira o projeto no Flow antes de gerar de novo: o vídeo pode já existir lá.':'')+'</p>';
 if(!v||videoRetry.includes(v.state)){
  const blocker=videoBlocker();
  h+='<div class="studio-generate"><button type="button" class="primary" data-video-generate '+(blocker?'disabled':'')+'>'+(v?'Gerar de novo':'Gerar vídeo')+' · '+g.cost+' créditos</button><p class="hint">'+esc(blocker||videoBudget())+'</p></div>';
 }
 if(s.videos.length>1)h+='<details class="studio-details"><summary>Gerações deste rascunho ('+s.videos.length+')</summary><ul class="studio-history">'+s.videos.map(x=>'<li>#'+x.id+' · '+hm(x.created)+' · '+esc(x.label)+' · '+x.credits+' créditos</li>').join('')+'</ul></details>';
 h+='<button type="button" class="text-button danger" data-studio-delete>Excluir rascunho</button>';
 box.innerHTML=h;
 watchVideo();
}
function watchVideo(){
 clearTimeout(videoPoll);const v=studioDraft?.video;
 const waiting=v&&(videoActive.includes(v.state)||(v.state==='review'&&!(v.meta||{}).fidelity&&videoPolls<60));
 if(!waiting){videoPolls=0;return;}
 videoPoll=setTimeout(async()=>{
  if(!studioDraft)return;videoPolls++;
  try{const fresh=await api('/studio/'+studioDraft.id),before=studioDraft.video,after=fresh.video;
   const changed=!before||!after||before.id!==after.id||before.state!==after.state||JSON.stringify(before.meta)!==JSON.stringify(after.meta);
   studioDraft=Object.assign(studioDraft,{video:fresh.video,videos:fresh.videos,generation:fresh.generation});
   if(changed){renderVideoPanel();if(after&&after.state==='review'&&before&&before.state!=='review')toast('Vídeo pronto: confira a prévia antes de enviar.');return;}
  }catch(e){toast(e.message);}
  watchVideo();
 },6000);
}
async function studioGenerate(regenerate){
 const s=studioDraft,g=s.generation,v=s.video,id=s.id;
 const warn=v&&v.state==='unknown'?'\n\nATENÇÃO: a geração anterior ficou incerta e pode já ter gastado créditos. Confira o projeto no Flow antes.':'';
 if(!confirm((regenerate?'Descartar o vídeo atual e gerar outro':'Gerar 1 vídeo no Flow')+' com '+g.refs.length+' foto(s) de referência?\n\nCusto: '+g.cost+' créditos (se o Flow pedir outro valor, nada é gerado).\n'+videoBudget()+warn))return;
 try{
  if(regenerate)studioDraft=await post('/studio/'+id+'/videos/'+v.id+'/discard');
  studioDraft=await post('/studio/'+id+'/generate',{cost:g.cost});toast('Vídeo na fila do worker.');
 }catch(e){toast(e.message);try{studioDraft=await api('/studio/'+id);}catch(_){}}
 renderVideoPanel();
}
async function studioVideoAct(path,message,question){
 if(question&&!confirm(question))return;
 try{studioDraft=await post('/studio/'+studioDraft.id+'/videos/'+path);toast(message(studioDraft));}catch(e){toast(e.message);}
 renderVideoPanel();
}
function readScript(card){const r={};card.querySelectorAll('[data-sf]').forEach(i=>r[i.dataset.sf]=i.value.trim());const c=[...card.querySelectorAll('[data-callout]')];if(c.length)r.callouts=c.map(i=>i.value.trim());return r;}
async function studioAct(promise,message){try{studioDraft=await promise;renderEditor();watchJob();if(message)toast(message);}catch(e){toast(e.message);}}

function readAsDataUrl(blob){return new Promise((ok,bad)=>{const f=new FileReader();f.onload=()=>ok(f.result);f.onerror=()=>bad(Error('Não consegui ler a foto.'));f.readAsDataURL(blob);});}
async function shrink(file){
 // Reduz no celular antes de enviar: lado maior 1280 px, JPEG 85%. O painel aceita até 2,5 MB por foto.
 const source=await readAsDataUrl(file);
 const img=await new Promise((ok,bad)=>{const i=new Image();i.onload=()=>ok(i);i.onerror=()=>bad(Error('Formato de foto não suportado: '+file.name));i.src=source;});
 const scale=Math.min(1,1280/Math.max(img.naturalWidth,img.naturalHeight)),canvas=document.createElement('canvas');
 canvas.width=Math.round(img.naturalWidth*scale);canvas.height=Math.round(img.naturalHeight*scale);
 canvas.getContext('2d').drawImage(img,0,0,canvas.width,canvas.height);
 const blob=await new Promise(ok=>canvas.toBlob(ok,'image/jpeg',0.85));
 return (await readAsDataUrl(blob)).split(',')[1];
}
async function uploadPhotos(files){
 const list=[...files].slice(0,studioDraft.max_photos-studioDraft.user_photos);
 for(const [n,file] of list.entries()){
  toast('Enviando foto '+(n+1)+' de '+list.length+'…');
  try{studioDraft=await post('/studio/'+studioDraft.id+'/photos',{data:await shrink(file)});renderEditor();}
  catch(e){toast(e.message);break;}
 }
 if(list.length<files.length)toast('Só cabem '+studioDraft.max_photos+' fotos suas por produto.');else toast('Fotos enviadas.');
}

document.querySelectorAll('[data-studio-tab]').forEach(b=>{b.onclick=()=>studioTabs(b.dataset.studioTab);b.addEventListener('keydown',studioTabKeys);});
document.querySelector('[data-view="studio"]').addEventListener('click',()=>{if(!studioDraft)studioLoad();});
$('#studio-link-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;
 try{studioDraft=await post('/studio/manual',{url:$('#studio-link').value.trim()});$('#studio-link').value='';showEditor();}catch(error){toast(error.message);}finally{b.disabled=false;}};
$('#studio-home').addEventListener('click',e=>{const day=e.target.closest('[data-studio-day]'),open=e.target.closest('[data-studio-open]');
 if(day)openStudioDay(+day.dataset.studioDay,day.dataset.key);else if(open)studioOpen(+open.dataset.studioOpen);});
$('#offers').addEventListener('click',e=>{const b=e.target.closest('[data-studio-card]');if(b)openStudioDay(activeRun.id,b.dataset.studioCard);});
$('#studio-editor').addEventListener('change',e=>{
 if(e.target.id==='studio-files'&&e.target.files.length)uploadPhotos(e.target.files);
 else if(e.target.name==='studio-format')studioAct(post('/studio/'+studioDraft.id+'/format',{format:e.target.value}));
 else if(e.target.name==='studio-style'){studioStyle=e.target.value;renderEditor();}
});
$('#studio-editor').addEventListener('click',e=>{
 const t=e.target.closest('button');if(!t||!studioDraft)return;const id=studioDraft.id,d=t.dataset;
 if('studioBack' in d)closeEditor();
 else if(d.photoMain)studioAct(post('/studio/'+id+'/photos/'+d.photoMain+'/main'));
 else if(d.photoRemove){if(confirm('Remover esta foto?'))studioAct(post('/studio/'+id+'/photos/'+d.photoRemove+'/remove'));}
 else if(d.studioRun){if(d.studioRun==='facts'&&studioDraft.facts&&!confirm('A ficha atual será substituída pela nova descrição. Continuar?'))return;
  studioAct(post('/studio/'+id+'/'+d.studioRun,d.studioRun==='scripts'?{style:studioStyle}:{}),'Pedido enviado ao modelo.');}
 else if('studioFactsSave' in d)studioAct(post('/studio/'+id+'/facts',{text:$('#studio-facts').value}),'Ficha salva.');
 else if(d.scriptSave)studioAct(post('/studio/'+id+'/scripts/'+d.scriptSave,{script:readScript(t.closest('.studio-script'))}),'Roteiro salvo.');
 else if(d.scriptChoose)studioAct(post('/studio/'+id+'/scripts/'+d.scriptChoose,{script:readScript(t.closest('.studio-script')),choose:true}),'Roteiro escolhido.');
 else if('captionSave' in d)studioAct(post('/studio/'+id+'/caption',{description:$('#studio-desc').value,hashtags:$('#studio-tags').value.split(/\s+/).filter(Boolean)}),'Legenda salva.');
 else if('videoGenerate' in d)studioGenerate(false);
 else if(d.videoRegenerate)studioGenerate(true);
 else if(d.videoSend)studioVideoAct(d.videoSend+'/send',r=>r.video&&r.video.state==='sent'?'Vídeo enviado ao Telegram.':'Envio não confirmado: '+(r.video?r.video.label:''),'Enviar este vídeo ao Telegram com a legenda e os links do produto?');
 else if(d.videoDiscard)studioVideoAct(d.videoDiscard+'/discard',()=>'Vídeo descartado. Os créditos continuam contados.','Descartar este vídeo? Os créditos já gastos não voltam.');
 else if('studioDelete' in d){if(confirm('Excluir este rascunho e as fotos enviadas?'))post('/studio/'+id+'/delete').then(()=>{toast('Rascunho excluído.');closeEditor();}).catch(err=>toast(err.message));}
});
