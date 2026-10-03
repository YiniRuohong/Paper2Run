import test from 'node:test';
import assert from 'node:assert/strict';
import { GuidedController } from '../src/features/guided/controller.ts';
const bootstrap = {project_id:'p', paper_id:'paper', extraction_id:'x'};
const experiment = {experiment_id:'e',project_id:'p',paper_id:'paper',status:'draft',head_revision_id:null,active_run_id:null,latest_run_id:null,repairs_used:0,repairs_limit:0,created_at:'t'};
const revision = {id:'v',experiment_id:'e',revision_no:1,spec_hash:'s',policy_hash:'h',spec:{},validation:{issues:[]},confirmation:null,is_current:true,created_at:'t'};
function setup() {
 const data = new Map(); const calls=[];
 const storage={getItem:k=>data.get(k)??null,setItem:(k,v)=>data.set(k,v)};
 const api={
  async read(path,validate){ calls.push(['GET',path]); const v=path==='/demo'?{actor_id:'demo-user'}:path==='/experiments/e/draft'?{experiment_id:'e',base_revision_id:null,spec:{},issues:[],editable_fields:[]}:path==='/experiments/e'?experiment:path==='/spec-revisions/v'?revision:bootstrap; return validate(v); },
  async write(intent,validate){calls.push(['POST',intent.path,intent]); return validate(intent.path==='/demo/bootstrap'?bootstrap:intent.path.endsWith('/experiments')?experiment:intent.path.endsWith('/spec-revisions')?revision:{confirmation_id:'c',revision_id:'v',spec_hash:'s',confirmed_by:'demo-user',confirmed_at:'t'});},
  async receipt(){throw Object.assign(new Error('missing'),{status:404});},
 };
 return {api,storage,calls,data,create:()=>new GuidedController({api,storage,actor:'demo-user'})};
}
test('subscribe and refresh only GET capabilities, not bootstrap',async()=>{
 const f=setup(),c=f.create();const a=c.subscribe(()=>{}),b=c.subscribe(()=>{});await c.refresh();await c.refresh();a();b();
 assert.deepEqual(f.calls.map(x=>x.slice(0,2)),[['GET','/demo'],['GET','/demo']]);
});
test('consent is required, persistence failure blocks POST',async()=>{
 const f=setup(),c=f.create();await assert.rejects(c.startExample(false),/CONSENT/);assert.equal(f.calls.length,0);
 f.storage.setItem=()=>{throw Error('quota');};await assert.rejects(c.startExample(true),/STORAGE/);assert.equal(f.calls.length,0);
});
test('explicit bootstrap is persisted and review gates confirmation',async()=>{
 const f=setup(),c=f.create();await c.startExample(true);assert.equal(c.getSnapshot().projectId,'p');
 await assert.rejects(c.confirmExperiment(true),/REVIEW/);
 c.reviewExtraction('x');await c.confirmExperiment(true);
 assert.deepEqual(f.calls.filter(x=>x[0]==='POST').map(x=>x[1]),['/demo/bootstrap','/projects/p/experiments','/projects/p/spec-revisions','/spec-revisions/v/confirm']);
 assert.equal(c.getSnapshot().confirmationId,'c');
 assert.equal(f.data.size,1);
});
test('partial revision failure does not repeat successful experiment POST',async()=>{
 const f=setup(),c=f.create();await c.startExample(true);c.reviewExtraction('x');
 const write=f.api.write;let fail=true;f.api.write=async(i,v)=>{if(i.path.endsWith('/spec-revisions')&&fail){fail=false;throw Object.assign(Error('conflict'),{status:409});}return write(i,v);};
 await assert.rejects(c.confirmExperiment(true));await c.confirmExperiment(true);
 assert.equal(f.calls.filter(x=>x[0]==='POST'&&x[1].endsWith('/experiments')).length,1);
});
test('unknown write checks receipt, never automatically replays; explicit replay keeps bytes and key',async()=>{
 const f=setup(),c=f.create(),write=f.api.write;let first;
 f.api.write=async(i,v)=>{first=i;throw Object.assign(Error('network'),{unknown:true});};
 await assert.rejects(c.startExample(true));f.api.write=write;
 await assert.rejects(c.startExample(true),/UNCONFIRMED/);
 assert.equal(f.calls.length,0);
 await c.replayPending();
 assert.equal(f.calls[0][2].key,first.key);assert.equal(f.calls[0][2].body,first.body);
 assert.equal(c.getSnapshot().projectId,'p');
});
test('restore preserves accepted refs and refresh never rewrites',async()=>{
 const f=setup(),c=f.create();await c.startExample(true);f.calls.length=0;
 const restored=f.create();await restored.refresh();assert.equal(restored.getSnapshot().projectId,'p');
 assert.ok(f.calls.every(x=>x[0]==='GET'));
});
test('budget consent blocks writes after review',async()=>{
 const f=setup(),c=f.create();await c.startExample(true);c.reviewExtraction('x');const count=f.calls.length;
 await assert.rejects(c.confirmExperiment(false),/BUDGET/);assert.equal(f.calls.length,count);
});
test('receipt mismatch never accepts or replays an unknown write',async()=>{
 const f=setup(),c=f.create();f.api.write=async()=>{throw Object.assign(Error('network'),{unknown:true});};
 await assert.rejects(c.startExample(true));f.api.receipt=async()=>({state:'completed',target_path:'/api/v1/demo/bootstrap',request_sha256:'0'.repeat(64),response:bootstrap});
 await assert.rejects(c.replayPending(),/RECEIPT_MISMATCH/);assert.equal(c.getSnapshot().projectId,null);
});
test('completed receipts recover without another POST',async()=>{
 const f=setup(),c=f.create();let intent;f.api.write=async i=>{intent=i;throw Object.assign(Error('network'),{unknown:true});};
 await assert.rejects(c.startExample(true));
 const hash=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(intent.body))).toString('hex');
 f.api.receipt=async()=>({state:'completed',target_path:'/api/v1/demo/bootstrap',request_sha256:hash,response:bootstrap});
 await c.replayPending();assert.equal(c.getSnapshot().projectId,'p');assert.equal(f.calls.length,0);
});
test('disposed controller ignores late bootstrap result',async()=>{
 const f=setup(),c=f.create();let resolve;const wait=new Promise(r=>resolve=r);const write=f.api.write;
 f.api.write=async(i,v)=>{await wait;return write(i,v);};const p=c.startExample(true);c.dispose();resolve();await p;
 assert.equal(c.getSnapshot().projectId,null);
});
