import {workspaceChooser} from './workspaces.js';
import {localReceiverUnavailable} from './commands.js';
import {GrokConnectionAssistant,grokConnectionView} from './grok-connection.js';
const key=decodeURIComponent(location.hash.slice(1));
const errors={private_office_required:'請從你的私人 OFFICE 入口開啟。',name_in_use:'這個名稱已有人使用，請換一個名字。',invalid_name:'請填寫 1 至 80 字的名稱。',invitation_unavailable:'暫時未能建立邀請，請稍後再試。',invitation_expired:'邀請已過期，請建立新邀請。',already_claimed:'Agent 已登記，不能再取消這張邀請。'};
Object.assign(errors,{agent_membership_changed:'這位 Agent 的身份已改變或已登出，請重新選擇；舊接駁未有套用。',cursor_connection_details_required:'請先填一次 Cursor API key。',cursor_agent_not_available:'這個 Cloud Agent 暫時不可接入，請在 Cursor 確認。',cursor_sdk_required:'這部電腦未安裝 Cursor Python SDK。請先執行 pip install cursor-sdk，再開新對話。',connection_unavailable:'本機暫時未能啟動 Cursor，請再試。',invalid_connection:'這次對話的資料不完整，請再試。',cursor_local_start_failed:'未能開立本機 Cursor session。',cursor_local_agent_invalid:'Cursor 回傳的本機 session 無效，未有接入。',invalid_workspace:'請揀一個已存在的資料夾，或勾選建立新資料夾。',platform_auth_required:'平台憑證未能通過驗證。',platform_access_denied:'這個帳戶未獲准使用平台接口。',platform_target_not_found:'找不到你指定的 Agent。',invalid_webhook_details:'請填寫 Grok Bot routine 的 HTTPS Webhook URL 及 key。',grok_return_route_required:'先讓 Bot 完成接入 Prompt 及首次回報，再啟用指示。',connection_has_active_commands:'仍有指令未結束，暫時不能更換入口。',agent_already_selected:'這個 Agent 已用另一個名稱接入。',platform_connection_failed:'未能連到平台；尚未建立接駁。',agent_has_active_commands:'工作仲未結束，完成後先可以封存。',agent_not_selected:'呢條對話已經不在辦公室。',cursor_repository_invalid:'請填一個 https 儲存庫網址。有分支時要一併填網址。',cursor_cloud_start_failed:'Cursor 未有確認這隻 Cloud Agent，未有接入。',platform_request_failed:'Cursor 未能建立 Cloud Agent，請檢查儲存庫權限後再試。',platform_rate_limited:'Cursor 暫時太忙，請稍後再試。',platform_delivery_unknown:'建立請求的結果未確認。若 Cursor 已開出這隻 Agent，下次對名單時會出現。'});
export const invitationStatus=(state)=>({pending:'等待 Agent 執行接入 Prompt',claimed:'已登記，等待 Agent 首次回報',connected:'已收到 Agent 回報',expired:'邀請已過期',revoked:'邀請已取消'}[state]||'確認接入狀態中');
export function invitationResumeId(search){
  if(typeof search!=='string')return null;
  const values=new URLSearchParams(search).getAll('invitation');
  return values.length===1&&/^[0-9a-f]{32}$/.test(values[0])?values[0]:null;
}
export function invitationPlatform(value){
  return ['codex','cursor_local','grok_bot','crouton','generic'].includes(value)?value:null;
}
export function invitationViewGuard(){
  let intent=0;
  return {
    change(){intent++;},
    capture(context){return {intent,context:{...context}};},
    sameIntent(ticket){return ticket.intent===intent;},
    accepts(ticket,context){return ticket.intent===intent&&context.open===true&&Object.keys(ticket.context).every(name=>ticket.context[name]===context[name]);},
  };
}
export function invitationProgress(data){
  if(data.state!=='connected')return invitationStatus(data.state);
  const capability=data.conversation;
  if(capability?.reason==='dispatch_disabled')return '已入場 · 派工已停用';
  if(data.provider==='grok_bot')return '已入場 · '+grokConnectionView(capability).title;
  if(capability?.persistent)return capability.native_ready===true?'已入場 · 持續收訊已接通':'已入場 · 持續收訊已設定，等待原平台恢復';
  if(capability?.mode==='agent_inbox')return capability.receiver_online?'已入場 · 臨時收訊中，持續接駁待完成':'已入場 · 持續接駁待完成，可先留言';
  return invitationStatus(data.state);
}
export function needsGrokActivation(data){
  return data?.provider==='grok_bot'&&typeof data.agent_id==='string'&&data.agent_id.startsWith('grok_bot:')&&data.state==='connected'&&data.conversation?.mode==='agent_inbox'&&!data.conversation.persistent&&data.conversation.reason!=='dispatch_disabled'&&data.conversation.can_send===true;
}
export function activationFocusPolicy(){
  const seen=new Set();
  return {reset(){seen.clear();},next({identity,enabled,open,editing,urlEmpty,keyEmpty}){
    if(!identity||!enabled||!open||seen.has(identity))return null;
    seen.add(identity);
    if(editing)return null;
    return urlEmpty?'url':keyEmpty?'key':null;
  }};
}
async function request(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',cache:'no-store',credentials:'same-origin',headers:{'X-Office-Key':key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(path.includes('/connections/cursor-local')?120000:path.includes('/connections/cursor-cloud')?60000:path.includes('/connections')?25000:5000)});
  const data=await response.json();if(!response.ok){const error=new Error(errors[data.error]||'暫時未能完成操作，請再試。');error.status=response.status;error.code=data.error;throw error;}return data;
}
if(key){
  const launch=document.createElement('button');launch.className='office-invite-launch';launch.type='button';launch.textContent='＋ 邀請 Agent';
  const dialog=document.createElement('dialog');dialog.className='office-invite-dialog';dialog.setAttribute('aria-labelledby','invite-heading');
  dialog.innerHTML=`<button class="invite-close" type="button" aria-label="關閉邀請視窗">×</button>
    <div class="invite-eyebrow">VIRTUAL OFFICE</div><h2 id="invite-heading">邀請一位 Agent</h2>
    <p class="invite-intro">選擇平台，加入你想共事的 Agent。</p>
    <form class="invite-form"><label>使用平台<select name="provider" required><option value="" disabled selected>請選擇平台</option><option value="codex">Codex App</option><option value="grok_bot">Grok Bot</option><option value="crouton">Crouton</option><option value="generic">其他 Agent</option></select></label>
      <label>Agent 名稱<input name="name" maxlength="80" required placeholder="例如：我的工作助手" autocomplete="off"></label>
      <p class="platform-note invite-footnote"></p>
      <button class="invite-primary" type="submit" disabled>先選擇平台</button>
      <p class="invite-footnote">只邀請你選定的 Agent，不會掃描其他聊天。</p>
      <button class="invite-resume" type="button" hidden>查看之前的邀請</button></form>
    <section class="invite-result" hidden><div class="invite-status" role="status"></div>
      <details class="invite-onboarding-prompt" open><summary hidden>查看原邀請指引</summary>
      <label><span class="invite-prompt-label">入場＋持續收訊 Prompt（貼一次）</span><textarea class="invite-prompt" readonly rows="6" spellcheck="false"></textarea></label>
      <button class="invite-primary invite-copy" type="button">複製 Prompt</button>
      <p class="invite-footnote">貼一次，由 Agent 接續完成入場、公開同步及支援平台的持續收訊設定；平台要求批准時仍需確認。新邀請有效 24 小時。</p>
      </details>
      <div class="grok-assistant-host"></div>
      <div class="invite-actions"><button class="invite-another" type="button">邀請另一位</button><button class="invite-revoke" type="button">取消這張邀請</button></div></section>
    <p class="invite-notice" role="alert"></p>`;
  document.body.append(launch,dialog);
  const chat=document.createElement('dialog');chat.className='office-invite-dialog';chat.setAttribute('aria-labelledby','cursor-chat-heading');
  chat.innerHTML=`<button class="invite-close" type="button" aria-label="關閉新對話">×</button>
    <div class="invite-eyebrow">CURSOR</div><h2 id="cursor-chat-heading">新對話</h2>
    <p class="invite-intro">用 Cursor 之前，先在這部電腦執行 pip install cursor-sdk。然後揀工作資料夾開一條本機對話。API key 只需要填一次。填好之後，未封存的 Cloud Agent 會自動出現。</p>
    <form class="invite-form"><label class="cursor-key-row">Cursor API key<input name="api_key" type="password" autocomplete="off" placeholder="只此一次"></label>
      <label>對話種類<select name="place"><option value="local" selected>本機資料夾</option><option value="cloud">Cloud Agent</option></select></label>
      <div class="cursor-local-fields"><label>Workspace<select name="known"></select></label>
      <label>資料夾絕對路徑<input name="workspace" type="text" placeholder="例如：C:\\projects\\my-project" autocomplete="off"></label>
      <label class="cursor-create-row"><input name="create_folder" type="checkbox"> 這個資料夾還未存在，幫我建立</label></div>
      <div class="cursor-cloud-fields" hidden><label>第一句話<textarea class="cursor-prompt" name="prompt" rows="3" maxlength="4000" placeholder="這句會立即開始工作"></textarea></label>
      <label>儲存庫網址（可留空）<input name="repository" type="text" placeholder="https://github.com/org/repo" autocomplete="off"></label>
      <label>分支（可留空）<input name="branch" type="text" placeholder="main" autocomplete="off"></label>
      <p class="invite-footnote">留空儲存庫會開一隻沒有儲存庫的 Cloud Agent。在這裡封存只藏在這部辦公室，其他裝置仍然看得到。</p></div>
      <button class="invite-primary" type="submit">開新對話</button></form>
    <section class="cursor-archived" hidden><h3>已封存</h3><div class="cursor-archived-list"></div></section>
    <p class="invite-notice" role="alert"></p>`;
  document.body.append(chat);
  const chatForm=chat.querySelector('form'),chatNotice=chat.querySelector('.invite-notice'),chatKey=chatForm.elements.api_key,chatPath=chatForm.elements.workspace,chatKnown=chatForm.elements.known,chatArchived=chat.querySelector('.cursor-archived');
  async function openChat(){
    chatNotice.textContent='';
    const home=await request('/api/office/connections/cursor-local');
    chatKey.parentElement.hidden=!!home.has_key;chatKey.required=!home.has_key;
    chatKnown.replaceChildren(new Option('自訂路徑',''));
    for(const row of home.workspaces||[])chatKnown.append(new Option(row.name,row.path));
    chatPath.value=home.workspace||'';
    if(home.workspace){const match=[...chatKnown.options].find(option=>option.value===home.workspace);if(match)chatKnown.value=home.workspace;}
    const parked=home.archived||[];chatArchived.hidden=!parked.length;const list=chat.querySelector('.cursor-archived-list');list.replaceChildren();
    for(const item of parked){
      const button=document.createElement('button');button.type='button';button.textContent='再開 · '+item.name;button.addEventListener('click',async()=>{
        chatNotice.textContent='';button.disabled=true;
        try{await request('/api/office/connections/cursor-local',{action:'reopen',agent_id:item.agent_id});window.dispatchEvent(new CustomEvent('office:refresh'));chat.close();}
        catch(error){chatNotice.textContent=error.message;button.disabled=false;}
      });
      list.append(button);
    }
  }
  chatKnown.addEventListener('change',()=>{if(chatKnown.value)chatPath.value=chatKnown.value;});
  function place(){
    const cloud=chatForm.elements.place.value==='cloud';
    chat.querySelector('.cursor-local-fields').hidden=cloud;
    chat.querySelector('.cursor-cloud-fields').hidden=!cloud;
    chatForm.elements.prompt.required=cloud;
    chat.querySelector('.invite-intro').textContent=cloud?'開一隻 Cursor Cloud Agent。第一句話會立即開始工作，儲存庫可以留空。API key 只填一次。在這裡封存只會藏在這部辦公室。':'用 Cursor 之前，先在這部電腦執行 pip install cursor-sdk。然後揀工作資料夾開一條本機對話。API key 只需要填一次。填好之後，未封存的 Cloud Agent 會自動出現。';
  }
  chatForm.elements.place.addEventListener('change',place);
  addEventListener('office:new-conversation',async()=>{try{await openChat();chat.showModal();}catch(error){chat.showModal();chatNotice.textContent=error.message;}});
  chat.querySelector('.invite-close').addEventListener('click',()=>chat.close());
  chatForm.addEventListener('submit',async event=>{
    event.preventDefault();chatNotice.textContent='';const submit=chatForm.querySelector('button[type="submit"]');submit.disabled=true;
    const cloud=chatForm.elements.place.value==='cloud';
    const body=cloud?{prompt:chatForm.elements.prompt.value.trim()}:{workspace:chatPath.value,create_folder:chatForm.elements.create_folder.checked};
    if(cloud){const repository=chatForm.elements.repository.value.trim(),branch=chatForm.elements.branch.value.trim();if(repository)body.repository=repository;if(branch)body.branch=branch;}
    if(!chatKey.parentElement.hidden)body.api_key=chatKey.value;
    try{await request(cloud?'/api/office/connections/cursor-cloud':'/api/office/connections/cursor-local',body);chatKey.value='';if(cloud)chatForm.elements.prompt.value='';window.dispatchEvent(new CustomEvent('office:refresh'));chat.close();}
    catch(error){chatNotice.textContent=error.message;}
    finally{submit.disabled=false;}
  });
  const form=dialog.querySelector('form'),result=dialog.querySelector('.invite-result'),notice=dialog.querySelector('.invite-notice'),status=dialog.querySelector('.invite-status'),prompt=dialog.querySelector('textarea'),copy=dialog.querySelector('.invite-copy'),revoke=dialog.querySelector('.invite-revoke'),submit=form.querySelector('button[type="submit"]'),resumeButton=form.querySelector('.invite-resume');
  const workspace=workspaceChooser();submit.before(workspace.root);
  let current=null,timer=null,busy=false,requestId=null;
  let previousInvitation=invitationResumeId(location.search);
  function platform(){const chosen=invitationPlatform(form.elements.provider.value);workspace.root.hidden=!chosen;submit.disabled=busy||!chosen;submit.textContent=!chosen?'先選擇平台':'產生入場＋持續收訊 Prompt';dialog.querySelector('.platform-note').textContent=!chosen?'先選擇 Agent 所用的平台，再產生適合的接入指引。':'只需貼一次 Prompt，Agent 會接續處理入場及原生收訊設定；缺少平台入口或批准時會如實回報。';}
  form.elements.provider.addEventListener('change',platform);platform();
  const viewGuard=invitationViewGuard();let desiredInvitation=null,desiredAgent=null;
  let onboardingIdentity=null,onboardingComplete=false;
  const connectionAssistant=new GrokConnectionAssistant({
    transport:{read:agent=>request('/api/office/connections/check?agent='+encodeURIComponent(agent)),save:(agent,details)=>request('/api/office/connections/grok',{agent_id:agent,...details}),start:(agent,id)=>request('/api/office/connections/check',{agent_id:agent,request_id:id})},
    isVisible:()=>dialog.open&&!result.hidden,
    onDone:()=>dialog.close(),
    onSnapshot:(snapshot,agent)=>{if(current?.agent_id!==agent)return;if(snapshot.capability)current={...current,conversation:snapshot.capability};status.textContent=invitationProgress(current);status.dataset.state=grokConnectionView(current.conversation).state;},
  });
  dialog.querySelector('.grok-assistant-host').append(connectionAssistant.element);
  function viewContext(){return {open:dialog.open,invitation:current?.id||null,agent:current?.agent_id||null,desiredInvitation,desiredAgent,epoch:connectionAssistant.controller.epoch,revision:connectionAssistant.controller.revision};}
  function captureView(){return viewGuard.capture(viewContext());}
  function currentView(ticket){return viewGuard.accepts(ticket,viewContext());}
  function beginView(invitation=null,agent=null){viewGuard.change();desiredInvitation=invitation;desiredAgent=agent;return captureView();}
  function stop(){clearInterval(timer);timer=null;}
  function renderActivation(data){
    connectionAssistant.bind(data);
    return connectionAssistant.controller.capability;
  }
  function render(data){const accepted=renderActivation(data);if(data.provider==='grok_bot')data={...data,conversation:accepted};current=data;if(data.id)previousInvitation=data.id;desiredInvitation=data.id||null;desiredAgent=data.agent_id||null;const identity=data.agent_id||data.id,completedGrok=data.provider==='grok_bot'&&data.state==='connected',onboarding=result.querySelector('.invite-onboarding-prompt');onboarding.querySelector('summary').hidden=!completedGrok;if(identity!==onboardingIdentity||completedGrok!==onboardingComplete)onboarding.open=!completedGrok;onboardingIdentity=identity;onboardingComplete=completedGrok;const unavailable=data.setup&&localReceiverUnavailable(data.conversation);dialog.querySelector('#invite-heading').textContent=completedGrok?'完成 Grok 連接':unavailable?'收訊能力／限制':data.setup?'啟用持續收訊':'邀請一位 Agent';dialog.querySelector('.invite-intro').textContent=completedGrok?(data.name||'這位 Bot')+' · 跟住四步，完成連接及測試。':data.setup?'沿用目前 Agent 的身份及歷史。':'選擇平台，加入你想共事的 Agent。';result.querySelector('.invite-prompt-label').textContent=unavailable?'目前平台收訊能力說明':data.setup?'現有 Agent 收訊補接 Prompt':'入場＋持續收訊 Prompt（貼一次）';result.querySelector('.invite-footnote').textContent=unavailable?'這個原聊天目前只能保存留言，不能自動喚醒；重貼 Prompt 或再撳收訊圖示都不會啟用背景入口。':data.setup?'這是現有 Agent 的補接指引；新邀請已包含收訊設定，未接通時留言仍會保存。':'貼一次，由 Agent 接續完成入場、首次回報、公開同步及支援平台的持續收訊設定；平台要求批准時仍需確認。新邀請有效 24 小時。';form.hidden=true;result.hidden=false;prompt.value=data.prompt||(data.provider==='grok_bot'?'請沿用此 Bot 的 OFFICE 回報程式建立 Webhook routine；完整步驟見安裝包 docs/GROK-BOT.md。':'暫時未有可用的接入指引，請重新建立邀請或查看這位 Agent 的收訊能力。');status.textContent=invitationProgress(data);status.dataset.state=data.provider==='grok_bot'&&data.state==='connected'?grokConnectionView(data.conversation).state:data.state;revoke.hidden=data.state!=='pending';copy.disabled=!data.prompt||['expired','revoked'].includes(data.state);connectionAssistant.syncVisibility();if(!shouldFollow(data))stop();}
  async function refresh(){if(!current?.id||!dialog.open||busy)return;const ticket=captureView(),id=current.id;try{const data=await request('/api/office/invitations?id='+encodeURIComponent(id));if(!currentView(ticket))return;render(data);notice.textContent='';}catch(error){if(currentView(ticket))notice.textContent=error.message;}}
  function shouldFollow(data){
    if(!data||data.setup)return false;
    if(['pending','claimed'].includes(data.state))return true;
    if(data.state!=='connected'||data.conversation?.reason==='dispatch_disabled')return false;
    return ['codex','grok_bot'].includes(data.provider)&&!data.conversation?.persistent||data.conversation?.persistent&&data.conversation.native_ready!==true;
  }
  function follow(){stop();if(shouldFollow(current))timer=setInterval(refresh,2000);}
  async function newInvitation(){
    stop();current=null;connectionAssistant.bind(null);const ticket=beginView();requestId=null;
    form.reset();workspace.reset();form.elements.provider.value='';platform();form.hidden=false;result.hidden=true;notice.textContent='';
    resumeButton.hidden=!previousInvitation;
    dialog.querySelector('#invite-heading').textContent='邀請一位 Agent';dialog.querySelector('.invite-intro').textContent='選擇平台，加入你想共事的 Agent。';
    if(!dialog.open)dialog.showModal();form.elements.provider.focus();
    const openTicket=captureView();
    try{await workspace.load(undefined,{accept:()=>currentView(openTicket)});}catch(error){if(viewGuard.sameIntent(ticket)&&currentView(openTicket))notice.textContent=error.message;}
  }
  launch.addEventListener('click',newInvitation);
  dialog.querySelector('.invite-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{viewGuard.change();stop();connectionAssistant.syncVisibility();});
  form.addEventListener('input',()=>requestId=null);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;const provider=invitationPlatform(form.elements.provider.value);
    if(!provider){notice.textContent='請先選擇 Agent 所用的平台。';form.elements.provider.focus();return;}
    const ticket=beginView();busy=true;submit.disabled=true;notice.textContent='';requestId||=crypto.randomUUID();
    const name=form.elements.name.value,id=requestId;
    try{
        const chosen=await workspace.value();if(!currentView(ticket))return;
        const data=await request('/api/office/invitations',{name,provider,request_id:id,...(chosen.mode==='manual'?{workspace_id:chosen.workspace_id}:{})});
        if(!currentView(ticket))return;render(data);follow();copy.focus();
    }catch(error){if(currentView(ticket))notice.textContent=error.message;}
    finally{busy=false;platform();if(viewGuard.sameIntent(ticket)&&dialog.open&&current)renderActivation(current);}
  });
  copy.addEventListener('click',async()=>{
    const ticket=captureView();
    try{await navigator.clipboard.writeText(prompt.value);if(!currentView(ticket))return;notice.textContent=current?.setup&&localReceiverUnavailable(current.conversation)?'已複製收訊能力說明；目前沒有背景入口，毋須重貼或重新啟用。':current?.setup?'已複製，貼回這位 Agent 的原聊天。':'已複製合併指引；貼給所選 Agent 一次即可，之後按平台需要完成批准。';}
    catch{if(currentView(ticket)){prompt.focus();prompt.select();notice.textContent='請手動複製已選取的 Prompt。';}}
  });
  dialog.querySelector('.invite-another').addEventListener('click',newInvitation);
  window.addEventListener('office-connect-agent',async event=>{
    const id=event.detail?.agent_id;if(!id?.startsWith('grok_bot:'))return;stop();if(!dialog.open)dialog.showModal();
    const inv=id.startsWith('grok_bot:inv-')?id.slice('grok_bot:inv-'.length):null,ticket=beginView(inv,id);
    try{
      const data=inv?await request('/api/office/invitations?id='+encodeURIComponent(inv)):await request('/api/office/conversation',{agent_id:id});
      if(!currentView(ticket))return;render(inv?data:{agent_id:id,provider:'grok_bot',state:'connected',setup:true,prompt:data.prompt,conversation:data.capability,grok_setup:data.grok_setup});
    }catch(error){if(currentView(ticket))notice.textContent=error.message;}
  });
  window.addEventListener('office-inbox-setup',async event=>{
    const id=event.detail?.agent_id;if(!id)return;stop();if(!dialog.open)dialog.showModal();const ticket=beginView(null,id);notice.textContent='準備這位 Agent 的收訊指引…';
    try{
      const data=await request('/api/office/conversation',{agent_id:id});if(!currentView(ticket))return;
      render({agent_id:id,provider:id.split(':')[0],state:'connected',setup:true,prompt:data.prompt,conversation:data.capability,grok_setup:data.grok_setup});
      notice.textContent=localReceiverUnavailable(data.capability)?'目前只能保存留言，未能喚醒這個 Cursor 原聊天；不需要反覆啟用或重貼 Prompt。':id.startsWith('grok_bot:')?'按連接助手完成步驟；收到實際測試回覆先算連接成功。':'將這段指引貼回這位 Agent 原本的聊天即可。身份及歷史會保留。';
    }catch(error){if(currentView(ticket))notice.textContent=error.message;}
  });
  revoke.addEventListener('click',async()=>{
    if(!current||busy)return;const id=current.id,ticket=beginView(id,current.agent_id);busy=true;revoke.disabled=true;
    try{const data=await request('/api/office/invitations/revoke',{id});if(!currentView(ticket))return;render(data);notice.textContent='';}
    catch(error){if(currentView(ticket))notice.textContent=error.message;}
    finally{busy=false;if(viewGuard.sameIntent(ticket))revoke.disabled=false;}
  });
  resumeButton.addEventListener('click',async()=>{
    if(!previousInvitation||busy)return;
    const resume=previousInvitation;
    form.hidden=true;result.hidden=true;notice.textContent='正在讀取這份現有邀請…';
    dialog.querySelector('#invite-heading').textContent='查看之前的邀請';dialog.querySelector('.invite-intro').textContent='只會讀取你選定的邀請。';
    const ticket=beginView(resume);
    try{const data=await request('/api/office/invitations?id='+encodeURIComponent(resume));if(!currentView(ticket))return;render(data);notice.textContent='';follow();}
    catch(error){if(currentView(ticket)){form.hidden=false;notice.textContent='未能開啟這份現有邀請。'+error.message;}}
  });
}
