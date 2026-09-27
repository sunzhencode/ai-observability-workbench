#!/usr/bin/env node
/** UI regressions against a local server; all commands are intercepted, no outbound actions. */
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
const require = createRequire(new URL('../operations-console/package.json', import.meta.url));
const { chromium } = require('playwright');
const origin = process.env.ACCEPTANCE_URL ?? 'http://127.0.0.1:5174';
assert(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname));
const browser = await chromium.launch({ headless: true });
const api = await browser.newContext();
const queue = await (await api.request.get(`${origin}/api/v1/occurrences?limit=50`)).json();
const item = queue.items.find(item => item.title.includes('Checkout')) ?? queue.items[0];
assert(item, 'Requires seeded local mock occurrence');
const incident = await (await api.request.get(`${origin}/api/v1/incidents/${item.incident_id}`)).json();
const run = (id, summary, created) => ({
  schema_version:'V2', id, occurrence_id:item.id, status:'COMPLETED', job_id:null,
  provider_profile_id:null, model_channel_id:null, model_revision:1,
  alert_evidence:[{evidence_id:'alert-proof',alert_ref:'frozen-ref',alertname:'FrozenAlert',summary:'Frozen alert text',severity:'warning',source_state:'FIRING',labels:{}}],
  metric_evidence:[{evidence_id:'metric-proof',metric_id:'error_ratio',status:'DATA',summary:{latest:0.2,minimum:0.1,maximum:0.2,point_count:2},sample:[]}],
  degraded_domains:[],available_metric_count:1,activities:[],
  report:{summary_zh:summary,verdict:'LIKELY_INCIDENT',confidence:0.7,findings:[{title_zh:'需要核验',analysis_zh:'依据持久指标',evidence_ids:['metric-proof']}],recommended_actions:[],missing_evidence_zh:[],degraded_domains:[],evidence_gain:1},
  request_count:1,tool_call_count:1,input_tokens:10,output_tokens:10,safe_error_code:null,feedback:null,created_at:created,updated_at:created,
});
let failures = 0;
async function check(name, exercise) {
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  const state = { occurrence:{...item}, incident:structuredClone(incident), runs:[], incidentError:null, notificationError:false, command:null, metricReads:0, deliveryReads:[] };
  await page.route('**/api/v1/**', async route => {
    const request=route.request(); const path=new URL(request.url()).pathname;
    if(request.method()!=='GET' && state.command) return state.command(route);
    if(request.method()!=='GET') return route.fulfill({status:409,json:{error:{code:'TEST_READ_ONLY',message:'No command allowed'}}});
    if(path===`/api/v1/occurrences/${item.id}`) return route.fulfill({json:state.occurrence});
    if(path===`/api/v1/incidents/${item.incident_id}`) return state.incidentError
      ? route.fulfill({status:state.incidentError,json:{error:{code:'READ_UNAVAILABLE',message:'Read unavailable'}}})
      : route.fulfill({json:state.incident});
    if(path===`/api/v1/occurrences/${item.id}/investigator-runs`) return state.runsError ? route.fulfill({status:503,json:{error:{code:'READ_UNAVAILABLE',message:'Read unavailable'}}}) : route.fulfill({json:state.runs});
    if(path===`/api/v1/occurrences/${item.id}/tasks`) return route.fulfill({json:[]});
    if(path===`/api/v1/incidents/${item.incident_id}/notification` && state.notificationError)
      return route.fulfill({status:503,json:{error:{code:'READ_UNAVAILABLE',message:'Read unavailable'}}});
    if(path.includes('/metric-evidence')) state.metricReads++;
    if(path==='/api/v1/notification-deliveries' && state.deliveries) {
      const query=new URL(request.url()).searchParams;
      state.deliveryReads.push(Object.fromEntries(query));
      return route.fulfill({json:query.has('before_id') ? [] : state.deliveries});
    }
    return route.continue();
  });
  try { await exercise(page,state); console.log(`PASS ${name}`); }
  catch(error) { failures++; console.log(`FAIL ${name}: ${error.message.split('\n')[0]}`); }
  finally { await context.close(); }
}
await check('mapping label retains keyboard focus',async page=>{
  await page.goto(`${origin}/automation/mapping`);
  const input=page.getByRole('textbox',{name:'条件 1 标签',exact:true});
  await input.fill(''); await input.focus(); await page.keyboard.type('namespace');
  assert.equal(await input.inputValue(),'namespace');
  assert(await input.evaluate(el=>el===document.activeElement));
});
await check('signal recovery preserves resolution text',async(page,state)=>{
  state.occurrence.response_state='IN_PROGRESS'; state.occurrence.signal_state='FIRING';
  await page.clock.install();
  await page.goto(`${origin}/incidents/${item.id}#actions`);
  const input=page.getByPlaceholder('说明核验结果，以及为什么可以结束本次处理');
  await input.fill('保留正在填写的核验结论');
  state.occurrence.signal_state='RECOVERED';
  await page.clock.fastForward(16000);
  await page.waitForFunction(()=>document.body.innerText.includes('上游已恢复'));
  assert.equal(await input.inputValue(),'保留正在填写的核验结论');
});
await check('historical occurrence excludes present-day alert members',async(page,state)=>{
  state.incident.occurrence_no=item.occurrence_no+1;
  state.incident.members[0].annotations.summary='WRONG_CURRENT_MEMBER';
  state.runs=[run('old-run','旧报告','2026-09-10T00:00:00Z')];
  await page.goto(`${origin}/incidents/${item.id}#alerts`);
  await page.getByRole('region',{name:'Occurrence Alerts',exact:true}).waitFor();
  assert(!await page.getByText('WRONG_CURRENT_MEMBER',{exact:true}).count());
  await page.getByText('Frozen alert text',{exact:true}).waitFor();
});
await check('older investigation report remains selectable and evidence clickable',async(page,state)=>{
  state.runs=[run('new-run','新报告','2026-09-14T00:00:00Z'),run('old-run','旧报告','2026-09-10T00:00:00Z')];
  await page.goto(`${origin}/incidents/${item.id}#ai`);
  await page.getByLabel('查看哪次调查').selectOption('old-run');
  await page.getByText('旧报告',{exact:true}).waitFor();
  const link=page.getByRole('button',{name:'查看证据 metric-proof',exact:true});
  await link.click();
  assert(await page.locator('[data-evidence-id="metric-proof"]').evaluate(el=>el===document.activeElement));
});
await check('network error is not a missing incident',async(page,state)=>{
  state.incidentError=503;
  await page.clock.install();
  await page.goto(`${origin}/alerts?incident=${item.incident_id}`);
  await page.clock.fastForward(16000);
  await page.getByText(/事件详情暂时无法读取/).waitFor();
  assert(!await page.getByText(`找不到事件 #${item.incident_id}。`,{exact:true}).count());
});
await check('notification read failure is not an unmatched policy',async(page,state)=>{
  state.notificationError=true;
  await page.clock.install();
  await page.goto(`${origin}/alerts?incident=${item.incident_id}`);
  await page.clock.fastForward(16000);
  await page.getByText(/通知状态暂时无法读取/).waitFor();
  assert(!await page.getByText(/当前 occurrence 没有匹配通知策略/).count());
});
await check('current occurrence evidence offers deterministic metrics',async(page)=>{
  await page.goto(`${origin}/incidents/${item.id}#evidence`);
  await page.getByTestId('metric-evidence').waitFor();
  assert(!await page.getByText(/详细的类型化指标证据尚未接入/).count());
});
await check('start handling is available without entering actions',async(page)=>{
  await page.goto(`${origin}/incidents/${item.id}`);
  await page.getByRole('button',{name:'确认并开始处理',exact:true}).waitFor();
});
await check('resolved occurrence never queries live metrics',async(page,state)=>{
  state.occurrence.response_state='RESOLVED';
  state.runs=[run('frozen-run','结束前的报告','2026-09-10T00:00:00Z')];
  await page.goto(`${origin}/incidents/${item.id}#evidence`);
  await page.getByText('Frozen alert text',{exact:true}).waitFor();
  assert.equal(state.metricReads,0);
  assert.equal(await page.getByTestId('metric-evidence').count(),0);
});
await check('viewing an old report cancels only the active investigation',async(page,state)=>{
  const active=run('active-run','进行中','2026-09-14T00:00:00Z');
  active.status='RUNNING'; active.report=null;
  state.runs=[active,run('old-run','旧报告','2026-09-10T00:00:00Z')];
  let canceled=null;
  state.command=async route=>{
    canceled=new URL(route.request().url()).pathname;
    active.status='CANCELED';
    await route.fulfill({json:active});
  };
  await page.goto(`${origin}/incidents/${item.id}#ai`);
  await page.getByLabel('查看哪次调查').selectOption('old-run');
  assert(await page.getByRole('button',{name:'重新调查当前事件',exact:true}).isDisabled());
  await page.getByRole('button',{name:'停止调查',exact:true}).click();
  await page.getByText('旧报告',{exact:true}).waitFor();
  assert.equal(canceled,'/api/v1/investigator-runs/active-run/cancel');
});
await check('delivery filters and older-page cursor reach the API',async(page,state)=>{
  state.deliveries=Array.from({length:100},(_,index)=>({
    id:200-index, event_key:`delivery-${index}`, incident_id:42,route_id:1,route_target_id:1,
    channel_id:'test',channel_name:'Test channel',event_type:'FIRING_OPENED',state:'SUCCEEDED',
    attempt_count:1,scheduled_at:'2026-09-14T00:00:00Z',next_attempt_at:'2026-09-14T00:00:00Z',
    succeeded_at:'2026-09-14T00:00:00Z',suppression_reason:null,payload_snapshot:{source_id:'test'},attempts:[],
  }));
  await page.goto(`${origin}/notifications/deliveries`);
  await page.getByLabel('Incident ID',{exact:true}).fill('42');
  const older=page.getByRole('button',{name:'查看更早记录',exact:true});
  await older.click();
  await page.getByText('当前筛选没有更早的投递记录。',{exact:true}).waitFor();
  assert(state.deliveryReads.some(query=>query.incident_id==='42' && query.before_id==='101'));
  await page.getByRole('button',{name:'返回最新记录',exact:true}).click();
  await page.locator('.delivery-row').first().waitFor();
  assert(!Object.hasOwn(state.deliveryReads.at(-1),'before_id'));
});
await check('invalid incident ID cannot refresh an unfiltered query',async(page,state)=>{
  state.deliveries=[];
  await page.goto(`${origin}/notifications/deliveries`);
  await page.getByText('当前筛选没有投递记录。',{exact:true}).waitFor();
  await page.getByLabel('Incident ID',{exact:true}).fill('0');
  await page.getByText('请输入有效的正整数 Incident ID。',{exact:true}).waitFor();
  assert(await page.getByRole('button',{name:'刷新',exact:true}).isDisabled());
  state.deliveryReads.length=0;
  await page.getByRole('button',{name:'刷新',exact:true}).evaluate(el=>el.click());
  assert.equal(state.deliveryReads.length,0);
  await page.getByLabel('Incident ID',{exact:true}).fill('42');
  await page.getByText('当前筛选没有投递记录。',{exact:true}).waitFor();
  await page.getByRole('button',{name:'刷新',exact:true}).click();
  assert(state.deliveryReads.every(query=>query.incident_id==='42'));
});
for (const tab of ['evidence','alerts','ai']) await check(`initial investigation read failure can retry on ${tab}`,async(page,state)=>{
  state.occurrence.response_state='RESOLVED'; state.runsError=true;
  state.runs=[run('old-run','历史报告','2026-09-10T00:00:00Z')];
  await page.goto(`${origin}/incidents/${item.id}#${tab}`);
  await page.getByText('暂时无法确认是否有调查快照，请重新读取。',{exact:true}).waitFor();
  state.runsError=false;
  await page.getByRole('button',{name:'重新读取调查记录',exact:true}).click();
  await page.getByText('Frozen alert text',{exact:true}).waitFor();
  assert.equal(await page.getByText('暂时无法确认是否有调查快照，请重新读取。',{exact:true}).count(),0);
});
await api.close(); await browser.close();
process.exitCode=failures?1:0;
