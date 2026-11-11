'use strict';
// Estúdio (PLANO_ESTUDIO.md, S1): rascunho assistido do vídeo — fotos, ficha visual, formato e roteiros.
// Usa $, esc, api, post, money, date, toast e view de app.js (scripts clássicos compartilham o escopo global).
let studioData=null,studioDraft=null,studioTab='day',studioPoll=null;
const studioFields=[['hook','Gancho · 0–2 s'],['demo','Demonstração · 2–6 s'],['benefit','Prova · 6–8 s'],['cta','Chamada · 8–10 s']];
const deliveryText={pending:'aguardando',sending:'enviando',sent:'entregue',failed:'não entregue',unknown:'não confirmado',blocked:'bloqueado',refused:'recusado'};
function hm(iso){return iso?new Date(iso).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';}

async function studioLoad(){
 try{studioData=await api('/studio');}catch(e){return toast(e.message);}
 renderStudioLists();
}
function studioTabs(name){studioTab=name;document.querySelectorAll('[data-studio-tab]').forEach(b=>b.classList.toggle('active',b.dataset.studioTab===name));$('#studio-day').hidden=name!=='day';$('#studio-manual').hidden=name!=='manual';}
function studioCard(item,action){
 return '<article class="studio-item">'+(item.image?'<img src="'+esc(item.image)+'" alt="" loading="lazy" referrerpolicy="no-referrer">':'<div class="studio-noimg">sem foto</div>')+
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
 $('#studio-manual-list').innerHTML=d.manual.map(i=>studioCard(Object.assign({},i,{note:'Criado em '+hm(i.created)+(i.scripts?' · '+i.scripts+' roteiros':'')+(i.chosen!==null?' · roteiro escolhido':'')}),
  '<button class="secondary" data-studio-open="'+i.id+'">Abrir</button>')).join('')||'<p class="muted">Cole o link de um produto para começar.</p>';
}
async function studioOpen(id){try{studioDraft=await api('/studio/'+id);showEditor();}catch(e){toast(e.message);}}
async function openStudioDay(runId,key){
 view('studio');
 try{studioDraft=await post('/studio/day',{run_id:runId,key});showEditor();}catch(e){toast(e.message);studioLoad();}
}
async function showEditor(){if(!studioData)await studioLoad();$('#studio-home').hidden=true;$('#studio-editor').hidden=false;renderEditor();watchJob();$('#studio-editor').scrollIntoView();}
function closeEditor(){studioDraft=null;clearTimeout(studioPoll);$('#studio-editor').hidden=true;$('#studio-home').hidden=false;studioLoad();}

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
  '<article class="panel studio-head">'+(p.image?'<img src="'+esc(p.image)+'" alt="" referrerpolicy="no-referrer">':'')+'<div><p class="eyebrow">'+(s.origin==='dia'?'DO DIA':'MANUAL · fora do teto diário')+'</p><h2>'+esc(p.name)+'</h2>'+
  '<p class="muted">'+money(p.price)+' · comissão '+money(p.commission)+' · ★ '+Number(p.rating||0).toFixed(1)+' · '+Number(p.sales||0).toLocaleString('pt-BR')+' vendas · <a href="'+esc(p.link)+'" target="_blank" rel="noopener noreferrer">Ver anúncio ↗</a></p>'+
  (r?'<p class="studio-note'+(r.fallback?' bad':'')+'">'+(r.fallback?'Passou do corte: o produto seguiu para o vídeo automático.':'Reservado para você até '+hm(r.deadline)+'. Sem nenhuma geração até lá, vira vídeo automático.')+'</p>':'')+
  s.warnings.map(w=>'<p class="warning">'+esc(w)+'</p>').join('')+'</div></article><p id="studio-job" hidden></p>';
 const photos='<article class="panel"><p class="eyebrow">1 · FOTOS</p><h3>Referências do produto</h3><p class="hint">Mande fotos de ângulos diferentes: frente, verso, detalhes, o produto na mão (mostra o tamanho). A ★ marca a principal. As fotos ficam só no painel.</p><div class="studio-photos">'+
  s.photos.map(ph=>'<figure class="'+(ph.main?'main':'')+'"><img src="'+esc(ph.url)+'" alt="" referrerpolicy="no-referrer"><figcaption>'+(ph.source==='api'?'Anúncio':'Sua foto')+'</figcaption><div><button type="button" title="Usar como principal" data-photo-main="'+esc(ph.id)+'">'+(ph.main?'★':'☆')+'</button><button type="button" title="Remover" data-photo-remove="'+esc(ph.id)+'">✕</button></div></figure>').join('')+
  '</div><label class="secondary studio-upload'+(s.user_photos>=s.max_photos?' disabled':'')+'">+ Adicionar fotos · '+s.user_photos+' de '+s.max_photos+' suas<input id="studio-files" type="file" accept="image/*" multiple '+(s.user_photos>=s.max_photos?'disabled':'')+'></label></article>';
 const facts='<article class="panel"><p class="eyebrow">2 · FICHA VISUAL</p><h3>O que dá para ver no produto</h3><p class="hint">A IA descreve só o que aparece nas fotos. Corrija o que estiver errado: o roteiro e o prompt só podem usar o que estiver aqui.</p>'+
  '<button type="button" class="secondary" data-studio-run="facts" '+(running?'disabled':'')+'>'+(s.facts?'Descrever de novo':'Descrever as fotos com a IA')+'</button><textarea id="studio-facts" rows="8" maxlength="1500" placeholder="- Pote transparente com tampa azul&#10;- Quatro travas laterais">'+esc(s.facts)+'</textarea><button type="button" class="secondary" data-studio-facts-save>Salvar ficha</button></article>';
 const format='<article class="panel"><p class="eyebrow">3 · FORMATO</p><h3>Como o vídeo conta a história</h3><div class="studio-formats">'+
  studioData.formats.map(f=>'<label class="studio-format'+(s.format===f.key?' active':'')+'" title="'+esc(f.text)+'"><input type="radio" name="studio-format" value="'+esc(f.key)+'" '+(s.format===f.key?'checked':'')+'><b>'+(s.format===f.key?'✓ ':'')+esc(f.label)+'</b><small>'+esc(f.text.split(' — ')[1]||'')+'</small></label>').join('')+'</div></article>';
 const changed=s.scripts.length&&s.scripts_format&&s.scripts_format!==s.format;
 const scripts='<article class="panel"><p class="eyebrow">4 · ROTEIROS</p><h3>Três ângulos diferentes</h3><p class="hint">Cada um com gancho nos 2 primeiros segundos, demonstração, prova e chamada para o produto marcado. Edite à vontade e escolha um.</p>'+
  (changed?'<p class="warning">Os roteiros abaixo foram feitos no formato anterior. Gere de novo para usar o formato escolhido.</p>':'')+
  '<button type="button" class="secondary" data-studio-run="scripts" '+(running||!s.format?'disabled':'')+'>'+(s.scripts.length?'Gerar outros 3 roteiros':'Gerar 3 roteiros')+'</button>'+(s.format?'':'<span class="hint"> Escolha o formato primeiro.</span>')+
  '<div class="studio-scripts">'+s.scripts.map((sc,i)=>'<div class="studio-script'+(s.chosen===i?' chosen':'')+'" data-index="'+i+'"><div class="studio-script-head"><b>'+(i+1)+'</b><input data-sf="angle" value="'+esc(sc.angle)+'" maxlength="60" aria-label="Ângulo do roteiro '+(i+1)+'">'+(sc.edited?'<span class="content-pill">editado</span>':'')+'</div>'+
   studioFields.map(([k,l])=>'<label>'+l+'<textarea data-sf="'+k+'" rows="2" maxlength="220">'+esc(sc[k])+'</textarea></label>').join('')+
   '<label>Narração · '+sc.words+' palavras (18–25)<textarea data-sf="narration" rows="3" maxlength="300">'+esc(sc.narration)+'</textarea></label>'+
   (sc.problems.length?'<ul class="studio-problems">'+sc.problems.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'<p class="studio-ok">✓ Dentro das regras</p>')+
   '<div class="studio-actions"><button type="button" class="secondary" data-script-save="'+i+'">Salvar edição</button><button type="button" class="'+(s.chosen===i?'primary':'secondary')+'" data-script-choose="'+i+'">'+(s.chosen===i?'✓ Roteiro escolhido':'Usar este roteiro')+'</button></div></div>').join('')+'</div>'+
  (s.caption?'<div class="studio-caption"><p class="eyebrow">LEGENDA DO TELEGRAM · '+s.caption.length+'/150</p><label>Descrição<textarea id="studio-desc" rows="2" maxlength="300">'+esc(s.caption.description)+'</textarea></label><label>Hashtags (separadas por espaço)<input id="studio-tags" value="'+esc(s.caption.hashtags.join(' '))+'"></label>'+
   (s.caption.problems.length?'<ul class="studio-problems">'+s.caption.problems.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+'<button type="button" class="secondary" data-caption-save>Salvar legenda</button></div>':'')+'</article>';
 const prompt='<article class="panel"><p class="eyebrow">5 · VÍDEO</p><h3>Prompt para o Flow</h3>'+(s.prompt?'<pre class="studio-prompt">'+esc(s.prompt)+'</pre>':'<p class="hint">Escolha um roteiro para ver o prompt que vai ao Flow, com '+s.photos.length+' foto(s) de referência.</p>')+
  '<button type="button" class="primary" disabled title="A geração no Flow chega na próxima etapa (S2)">Gerar vídeo · em breve</button><p class="hint">Nesta versão o Estúdio prepara tudo, sem gastar créditos. A geração no Flow (com aprovação do custo e prévia antes do Telegram) chega na próxima etapa.</p>'+
  '<button type="button" class="text-button danger" data-studio-delete>Excluir rascunho</button></article>';
 $('#studio-editor').innerHTML=head+photos+facts+format+scripts+prompt;
 renderJob();
}
function readScript(card){const r={};card.querySelectorAll('[data-sf]').forEach(i=>r[i.dataset.sf]=i.value.trim());return r;}
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

document.querySelectorAll('[data-studio-tab]').forEach(b=>b.onclick=()=>studioTabs(b.dataset.studioTab));
document.querySelector('[data-view="studio"]').addEventListener('click',()=>{if(!studioDraft)studioLoad();});
$('#studio-link-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;
 try{studioDraft=await post('/studio/manual',{url:$('#studio-link').value.trim()});$('#studio-link').value='';showEditor();}catch(error){toast(error.message);}finally{b.disabled=false;}};
$('#studio-home').addEventListener('click',e=>{const day=e.target.closest('[data-studio-day]'),open=e.target.closest('[data-studio-open]');
 if(day)openStudioDay(+day.dataset.studioDay,day.dataset.key);else if(open)studioOpen(+open.dataset.studioOpen);});
$('#offers').addEventListener('click',e=>{const b=e.target.closest('[data-studio-card]');if(b)openStudioDay(activeRun.id,b.dataset.studioCard);});
$('#studio-editor').addEventListener('change',e=>{
 if(e.target.id==='studio-files'&&e.target.files.length)uploadPhotos(e.target.files);
 else if(e.target.name==='studio-format')studioAct(post('/studio/'+studioDraft.id+'/format',{format:e.target.value}));
});
$('#studio-editor').addEventListener('click',e=>{
 const t=e.target.closest('button');if(!t||!studioDraft)return;const id=studioDraft.id,d=t.dataset;
 if('studioBack' in d)closeEditor();
 else if(d.photoMain)studioAct(post('/studio/'+id+'/photos/'+d.photoMain+'/main'));
 else if(d.photoRemove){if(confirm('Remover esta foto?'))studioAct(post('/studio/'+id+'/photos/'+d.photoRemove+'/remove'));}
 else if(d.studioRun){if(d.studioRun==='facts'&&studioDraft.facts&&!confirm('A ficha atual será substituída pela nova descrição. Continuar?'))return;
  studioAct(post('/studio/'+id+'/'+d.studioRun),'Pedido enviado ao modelo.');}
 else if('studioFactsSave' in d)studioAct(post('/studio/'+id+'/facts',{text:$('#studio-facts').value}),'Ficha salva.');
 else if(d.scriptSave)studioAct(post('/studio/'+id+'/scripts/'+d.scriptSave,{script:readScript(t.closest('.studio-script'))}),'Roteiro salvo.');
 else if(d.scriptChoose)studioAct(post('/studio/'+id+'/scripts/'+d.scriptChoose,{script:readScript(t.closest('.studio-script')),choose:true}),'Roteiro escolhido.');
 else if('captionSave' in d)studioAct(post('/studio/'+id+'/caption',{description:$('#studio-desc').value,hashtags:$('#studio-tags').value.split(/\s+/).filter(Boolean)}),'Legenda salva.');
 else if('studioDelete' in d){if(confirm('Excluir este rascunho e as fotos enviadas?'))post('/studio/'+id+'/delete').then(()=>{toast('Rascunho excluído.');closeEditor();}).catch(err=>toast(err.message));}
});
