import assert from 'node:assert/strict';
import {conversationItems,failureText} from '../../src/bank_v1_web/static/conversation.js';
const job=(id,kind,at,extra={})=>({id,kind,created_at:at,status:'DONE',result:{ok:true,text:id},...extra});
const state={jobs:[job('chat','chat','2026-10-20T10:00:00Z'),job('a','event','2026-10-20T11:00:00Z',{event_key:'event:x:0:0'}),job('b','event','2026-10-20T12:00:00Z',{event_key:'event:x:0:1'}),job('goal','goal','2026-10-20T13:00:00Z'),job('history','chat','2026-10-20T14:00:00Z',{scope:'moneydata'})],operations:[{operation_id:'op',created_at:'2026-10-20T12:30:00Z'}]};
assert.deepEqual(conversationItems(state).map(x=>x.key),['job:chat','event:x','operation:op','job:goal']);
assert.equal(conversationItems(state)[1].job.id,'b');
assert.deepEqual(conversationItems(state,'moneydata').map(x=>x.key),['job:history']);
assert.deepEqual(conversationItems(JSON.parse(JSON.stringify(state))).map(x=>x.key),conversationItems(state).map(x=>x.key));
assert.match(failureText({error:{code:'DSH_VERSION'}}),/没有调用模型/);
const delegated={...state,competition:{goals:{followup:{event_handling:'GOAL_FOLLOWUP'}}},jobs:[job('delegated','event','2026-10-20T11:00:00Z',{result:{ok:false}}),state.jobs[3]]};
assert.deepEqual(conversationItems(delegated).filter(x=>x.type==='job').map(x=>x.job.kind),['goal']);
console.log('PASS: chat/event/goal ordering, event deduplication, scope isolation, restart stable IDs, dependency failure, delegated event filtering');

const pending={jobs:[],events:[{event_id:"pending",state:"PENDING",created_at:"2026-10-20T10:00:00Z"}]};
assert.equal(conversationItems(pending)[0].job.status,"QUEUED");
pending.jobs=[job("arrived","event","2026-10-20T10:00:01Z",{event_key:"event:pending:0:0"})];
assert.equal(conversationItems(pending).length,1);
assert.equal(conversationItems(pending)[0].job.id,"arrived");
console.log("PASS: event appears before worker starts and is replaced without duplication");
