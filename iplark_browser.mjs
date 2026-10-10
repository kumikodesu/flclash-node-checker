// Read publicly displayed IPLark data; remote browsers use an isolated context.
import readline from 'node:readline';

const ws = new WebSocket(process.argv[2]);
const remote = process.argv.includes('--remote');
let sessionId = null;
let contextId = null;
let nextId = 0;
const pending = new Map();
let documentStatus = null;
let retryAfter = null;
ws.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.method === 'Network.responseReceived' && message.params.type === 'Document' &&
      (!remote || message.sessionId === sessionId)) {
    documentStatus = message.params.response.status;
    retryAfter = Object.entries(message.params.response.headers || {})
      .find(([key]) => key.toLowerCase() === 'retry-after')?.[1] || null;
  }
  const entry = pending.get(message.id);
  if (entry) {
    pending.delete(message.id);
    clearTimeout(entry.timer);
    message.error ? entry.reject(new Error(message.error.message)) : entry.resolve(message.result);
  }
});
function rpc(method, params = {}, browserLevel = false) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout')); }, 12000);
    pending.set(id, {resolve, reject, timer});
    ws.send(JSON.stringify({id, method, params,
      ...(sessionId && !browserLevel ? {sessionId} : {})}));
  });
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, {once:true});
  ws.addEventListener('error', reject, {once:true});
});
if (remote) {
  contextId = (await rpc('Target.createBrowserContext', {disposeOnDetach:true})).browserContextId;
  const target = await rpc('Target.createTarget', {url:'about:blank', browserContextId:contextId});
  sessionId = (await rpc('Target.attachToTarget', {targetId:target.targetId, flatten:true})).sessionId;
}
async function releaseRemoteContext() {
  if (contextId) {
    const owned = contextId;
    contextId = null;
    try { await rpc('Target.disposeBrowserContext', {browserContextId:owned}, true); } catch {}
  }
}
if (remote) process.on('SIGTERM', async () => {
  await releaseRemoteContext();
  ws.close();
  process.exit(0);
});
await rpc('Page.enable');
await rpc('Network.enable');
console.log(JSON.stringify({ready:true}));

const lines = readline.createInterface({input:process.stdin});
for await (const line of lines) {
  const command = JSON.parse(line);
  if (command.close) {
    try {
      if (remote) await releaseRemoteContext();
      else await rpc('Browser.close');
    } catch {}
    break;
  }
  try {
    documentStatus = null;
    retryAfter = null;
    const navigation = await rpc('Page.navigate', {url:`https://iplark.com/${encodeURIComponent(command.ip)}`});
    if (navigation.errorText) throw new Error(navigation.errorText);
    const deadline = Date.now() + 40000;
    let data = null;
    while (Date.now() < deadline) {
      const response = await rpc('Runtime.evaluate', {
        expression:`JSON.stringify((() => {
          const heading = document.querySelector('h1')?.innerText.trim();
          const value = document.querySelector('#score-value')?.innerText.trim();
          const ratio = document.querySelector('.score-ratio')?.innerText.trim();
          const text = document.querySelector('#section-basic')?.innerText || '';
          const region = [...document.querySelectorAll('#section-basic .info-item')]
            .find(e => e.querySelector('label')?.textContent.trim() === '国家/地区')
            ?.querySelector('.value')?.innerText.trim();
          return {heading, value, ratio, text, region, title:document.title,
            page_text:document.body?.innerText.slice(0,300),
            needs_verification:!!document.querySelector('#waf-captcha') ||
              document.title === '安全验证' ||
              [...document.scripts].some(s=>s.src.includes('/sec-cgi/shield/'))};
        })())`, returnByValue:true
      });
      data = JSON.parse(response.result.value || '{}');
      if (data.heading === command.ip && /^\d{1,3}$/.test(data.value || '') &&
          data.ratio?.replace(/\s/g,'') === `${data.value}/100` && Number(data.value) <= 100) {
        console.log(JSON.stringify({ip:command.ip, score:Number(data.value),
          url:`https://iplark.com/${command.ip}`, basic:data.text.slice(0,1800),
          region:data.region || null, region_checked_at:new Date().toISOString(),
          checked_at:new Date().toISOString(), source:'live'}));
        data = null;
        break;
      }
      // A shield script may finish automatically; only classify it after waiting.
      if (documentStatus === 403 || documentStatus === 429) break;
      await delay(500);
    }
    if (data !== null) console.log(JSON.stringify({ip:command.ip, score:null,
      source:'missing', error:'页面未返回有效评分（可能暂无数据或需要网站验证）',
      url:`https://iplark.com/${command.ip}`, page_title:data?.title,
      page_text:data?.page_text, needs_verification:data?.needs_verification,
      http_status:documentStatus, retry_after:retryAfter, checked_at:new Date().toISOString()}));
  } catch (error) {
    console.log(JSON.stringify({ip:command.ip, score:null, source:'missing',
      error:`浏览器读取失败：${error.message}`, url:`https://iplark.com/${command.ip}`,
      checked_at:new Date().toISOString()}));
  }
}
if (remote) await releaseRemoteContext();
ws.close();
