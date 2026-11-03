"""Ingress -> source retrieval -> extraction -> durable write -> recall contracts."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plugins.memory.mempalace.fact_context import ContextArchive
from plugins.memory.mempalace.fact_pipeline import FactOutbox,canonical,digest
from tests.plugins.memory.test_mempalace_fact_pipeline import pipeline,context,evidence,fact,Client


def model_reply(value):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value),tool_calls=[]))])


class MemoryClient(Client):
    def mcp_call(self,ctx,name,args,operation):
        if operation=='read':
            assert name=='mempalace_search'
            rows=[{'text':v,'source_file':slug,'wing':wing} for (wing,slug),v in self.pages.items()
                  if wing in ctx.read_wings and all(term.casefold() in v.casefold() for term in args['query'].split())]
            return {'content':[{'type':'text','text':json.dumps({'results':rows})}]}
        return super().mcp_call(ctx,name,args,operation)


@pytest.mark.parametrize('subject,query,current,key',[
    ('Наш запасной роутер — модель Archer AX20.','роутер','Он теперь стоит на верхней полке шкафа.','router.location'),
    ('У Тёмы есть синий велосипед.','велосипед','Он теперь хранится у бабушки в гараже.','blue_bike.location'),
    ('Договорились обсуждать семейный бюджет по воскресеньям.','бюджет','Теперь делаем это по понедельникам.','budget.review_day'),
])
def test_remote_fact_survives_200_messages_and_months(pipeline,monkeypatch,subject,query,current,key):
    pipeline.config['extractor']={'provider':'fixture','model':'fixture'}
    pipeline.enqueue(evidence(context('m_subject'),subject,at=100))
    for i in range(250):pipeline.enqueue(evidence(context('m_chat_'+str(i)),'Обсуждаем другой вопрос номер '+str(i),at=200+i))
    ctx=context('m_current');item=evidence(ctx,current,at=10000000)
    receipt=pipeline.enqueue(item);remote=MemoryClient();seen=[]
    def llm(**kwargs):
        packet=json.loads(kwargs['messages'][-1]['content'])
        if 'context_lookup_result' not in packet:
            return model_reply({'context_lookup':{'kind':'history_search','query':query,'offset':0}})
        rows=packet['context_lookup_result']['entries'];seen.extend(rows)
        selected=next(x for x in rows if x['source_ref']=='m_subject')
        return model_reply({'facts':[dict(fact(current,key=key),context_evidence=[{'source_ref':selected['source_ref'],'evidence':subject}])]})
    monkeypatch.setattr('agent.auxiliary_client.call_llm',llm)
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    assert result['committed'] and result['written']==1
    read=json.loads(remote.mcp_call(ctx,'mempalace_search',{'query':query},operation='read')['content'][0]['text'])
    assert len(read['results'])==1 and current in read['results'][0]['text']
    assert subject in read['results'][0]['text'] and seen
    # Process restart/re-delivery cannot call the model or duplicate a write.
    reopened=FactOutbox(pipeline.config_path)
    try:
        replay=reopened.process_one(receipt['job_id'],extractor=lambda _:pytest.fail('duplicate extraction'),
            route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
        assert replay['committed'] and remote.calls==1
    finally:reopened.close()


def test_history_pages_reach_original_beyond_200_entries(pipeline):
    for i in range(251):pipeline.enqueue(evidence(context('m_'+str(i)),'Исходное сообщение номер '+str(i),at=100+i))
    current=evidence(context('m_current'),'Теперь он стоит на полке.',at=9000000)
    receipt=pipeline.enqueue(current);row=pipeline.db.execute('SELECT * FROM jobs WHERE id=?',(receipt['job_id'],)).fetchone()
    archive=ContextArchive(pipeline,row,current,lambda _:None)
    offset=0;found=[]
    while True:
        page=archive.lookup({'kind':'history_page','offset':offset})
        found.extend(page['entries'])
        if not page['has_more']:break
        offset=page['next_offset']
    assert len(found)==251 and found[-1]['source_ref']=='m_0'


def test_memory_lookup_is_scoped_and_can_supply_old_subject(pipeline,monkeypatch):
    pipeline.config['extractor']={'provider':'fixture','model':'fixture'}
    ctx=context('m_current');item=evidence(ctx,'Он теперь на даче в кладовке.',at=9000000)
    receipt=pipeline.enqueue(item);remote=MemoryClient()
    remote.pages[(ctx.write_wing,'old_router')]='Запасной роутер Archer AX20.'
    remote.pages[('private.somebody_else.main','private')]='PRIVATE_ROUTER'
    def llm(**kwargs):
        packet=json.loads(kwargs['messages'][-1]['content'])
        if 'context_lookup_result' not in packet:return model_reply({'context_lookup':{'kind':'memory_search','query':'роутер'}})
        rows=packet['context_lookup_result']['entries']
        assert len(rows)==1 and 'PRIVATE' not in json.dumps(rows)
        return model_reply({'facts':[dict(fact(item['text'],key='router.location'),context_evidence=[{'source_ref':rows[0]['source_ref'],'evidence':rows[0]['text']}])]})
    monkeypatch.setattr('agent.auxiliary_client.call_llm',llm)
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    assert result['committed']
    assert any('Он теперь на даче' in value and 'Archer AX20' in value for value in remote.pages.values())


def test_all_five_facts_commit_and_correction_supersedes_one(pipeline,monkeypatch):
    pipeline.config['extractor']={'provider':'fixture','model':'fixture'}
    parts=['Ключи лежат в синей коробке.','Запасной роутер стоит на полке.','Велосипед хранится в гараже.','Обсуждаем бюджет по понедельникам.','Я предпочитаю чай без сахара.']
    keys=['keys.location','router.location','bicycle.location','budget.review_day','self.tea_preference']
    item=evidence(context('m_many'),' '.join(parts),at=100);receipt=pipeline.enqueue(item);remote=MemoryClient()
    monkeypatch.setattr('agent.auxiliary_client.call_llm',lambda **kw:model_reply({'facts':[fact(p,key=k) for p,k in zip(parts,keys)]}))
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=context('m_many')),client_factory=lambda _:remote)
    assert result['facts']==5 and result['written']==5 and len(remote.pages)==5
    updated=evidence(context('m_correction'),'Ключи теперь лежат в красной коробке.',at=200)
    receipt=pipeline.enqueue(updated)
    monkeypatch.setattr('agent.auxiliary_client.call_llm',lambda **kw:model_reply({'facts':[fact(updated['text'],key='keys.location')]}))
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=context('m_correction')),client_factory=lambda _:remote)
    assert result['committed'] and len(remote.pages)==5
    assert not any('синей коробке' in x for x in remote.pages.values())
    assert any('красной коробке' in x for x in remote.pages.values())


def test_unresolved_reference_is_not_silently_completed_without_facts(pipeline,monkeypatch):
    pipeline.config['extractor']={'provider':'fixture','model':'fixture'}
    ctx=context();item=evidence(ctx,'Они теперь там, как договорились.')
    receipt=pipeline.enqueue(item)
    monkeypatch.setattr('agent.auxiliary_client.call_llm',lambda **kw:model_reply({'unresolved':'subject and location are unknown'}))
    remote=MemoryClient()
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    assert result['status']=='retry' and result['code']=='context_unresolved'
    assert remote.calls==0


def test_catalog_supplies_stable_identity_for_correction_without_recent_history(pipeline):
    remote=MemoryClient();ctx=context('m_base')
    original=evidence(ctx,'Семейный бюджет обсуждаем по воскресеньям.',at=100)
    receipt=pipeline.enqueue(original)
    pipeline.process_one(receipt['job_id'],extractor=lambda text:[fact(text,key='budget.review_day')],
        route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    current=evidence(context('m_now'),'Теперь делаем это по понедельникам.',at=9000000)
    receipt=pipeline.enqueue(current);row=pipeline.db.execute('SELECT * FROM jobs WHERE id=?',(receipt['job_id'],)).fetchone()
    archive=ContextArchive(pipeline,row,current,lambda _:SimpleNamespace(context=context('m_now')))
    result=archive.lookup({'kind':'fact_catalog','offset':0})
    assert len(result['entries'])==1
    assert result['entries'][0]['fact_key']=='budget.review_day'
    assert 'по воскресеньям' in result['entries'][0]['text']
    assert result['entries'][0]['actor_ref']==ctx.actor_ref


def test_search_index_backfill_recovers_after_incomplete_migration(pipeline):
    from plugins.memory.mempalace.fact_context import initialize_index
    receipt=pipeline.enqueue(evidence(context('m_original'),'Запасной роутер Archer AX20.',at=100))
    with pipeline.db:
        pipeline.db.execute('DELETE FROM fact_message_search')
        pipeline.db.execute('DELETE FROM fact_context_migrations')
    initialize_index(pipeline.db)
    assert pipeline.db.execute('SELECT count(*) FROM fact_message_search').fetchone()[0]==1
    initialize_index(pipeline.db)
    assert pipeline.db.execute('SELECT count(*) FROM fact_message_search').fetchone()[0]==1


def test_memory_context_failure_retries_instead_of_claiming_no_fact(pipeline,monkeypatch):
    pipeline.config['extractor']={'provider':'fixture','model':'fixture'}
    class Unavailable(MemoryClient):
        def mcp_call(self,ctx,name,args,operation):
            if operation=='read':raise ConnectionError('isolated unavailable transport')
            return super().mcp_call(ctx,name,args,operation)
    ctx=context('m_current');receipt=pipeline.enqueue(evidence(ctx,'Он теперь лежит на полке.',at=100))
    monkeypatch.setattr('agent.auxiliary_client.call_llm',lambda **kw:model_reply({'context_lookup':{'kind':'memory_search','query':'роутер'}}))
    remote=Unavailable()
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    assert result['status']=='retry' and not result['committed'] and remote.calls==0
    assert pipeline.db.execute('SELECT result FROM jobs WHERE id=?',(receipt['job_id'],)).fetchone()[0] is None


def test_context_dependent_output_repairs_missing_subject_quote_before_write(pipeline,monkeypatch):
    pipeline.config['extractor']={'provider':'fixture','model':'fixture'}
    prior='Наш запасной роутер Archer AX20.'
    pipeline.enqueue(evidence(context('m_prior'),prior,at=100))
    ctx=context('m_now');current='Он теперь стоит на полке.'
    receipt=pipeline.enqueue(evidence(ctx,current,at=200));remote=MemoryClient();calls=[]
    def llm(**kw):
        calls.append(kw['messages'][-1])
        if len(calls)==1:return model_reply({'context_lookup':{'kind':'history_search','query':'роутер'}})
        item=fact(current,key='router.location')
        if len(calls)>2:item['context_evidence']=[{'source_ref':'m_prior','evidence':prior}]
        return model_reply({'facts':[item]})
    monkeypatch.setattr('agent.auxiliary_client.call_llm',llm)
    result=pipeline.process_one(receipt['job_id'],route_resolver=lambda _:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    assert result['committed'] and remote.calls==1 and len(calls)==3
    assert 'Archer AX20' in next(iter(remote.pages.values()))


def test_fact_catalog_is_owned_by_author_even_inside_shared_conversation(pipeline):
    remote=MemoryClient()
    for actor,text,key in [('a_alice','Я предпочитаю овсяную кашу.','self.breakfast'),('a_bob','Я предпочитаю гречневую кашу.','self.breakfast')]:
        ctx=context('m_'+actor,actor=actor);receipt=pipeline.enqueue(evidence(ctx,text,at=100))
        pipeline.process_one(receipt['job_id'],extractor=lambda text:[fact(text,key=key)],route_resolver=lambda _,ctx=ctx:SimpleNamespace(context=ctx),client_factory=lambda _:remote)
    ctx=context('m_now',actor='a_alice');current=evidence(ctx,'Теперь предпочитаю её с яблоком.',at=200)
    receipt=pipeline.enqueue(current);row=pipeline.db.execute('SELECT * FROM jobs WHERE id=?',(receipt['job_id'],)).fetchone()
    archive=ContextArchive(pipeline,row,current,lambda _:SimpleNamespace(context=ctx))
    result=archive.lookup({'kind':'fact_catalog'})
    assert len(result['entries'])==1 and result['entries'][0]['actor_ref']=='a_alice'
    assert 'гречневую' not in json.dumps(result,ensure_ascii=False)
