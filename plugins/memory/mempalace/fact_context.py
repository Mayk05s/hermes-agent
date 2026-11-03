"""Source-scoped archive retrieval for fact extraction; no recency retention window.

Pages bound individual responses, never the age or position of eligible evidence.
Every returned original is seal-checked and authorized again before use.
"""
import hmac
import json
import re

from .fact_pipeline import canonical, detected_secret, digest


def initialize_index(db):
    db.execute('CREATE TABLE IF NOT EXISTS fact_context_migrations(version INTEGER PRIMARY KEY)')
    initialized=db.execute('SELECT 1 FROM fact_context_migrations WHERE version=1').fetchone()
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fact_message_search USING fts5(text, tokenize='unicode61')")
    db.execute("""CREATE TRIGGER IF NOT EXISTS fact_message_search_insert AFTER INSERT ON jobs BEGIN
        INSERT INTO fact_message_search(rowid,text) VALUES(new.rowid,json_extract(new.payload,'$.text'));
        END""")
    if not initialized:
        db.execute("INSERT OR IGNORE INTO fact_message_search(rowid,text) SELECT rowid,json_extract(payload,'$.text') FROM jobs")
        db.execute('INSERT OR IGNORE INTO fact_context_migrations VALUES(1)')
    db.execute("CREATE INDEX IF NOT EXISTS fact_message_scope ON jobs(json_extract(payload,'$.context.write_wing'),json_extract(payload,'$.received_at'))")


class ContextArchive:
    def __init__(self, box, row, evidence, resolver=None, client_factory=None):
        self.box, self.row, self.evidence = box, row, evidence
        self.resolver = resolver or box._current_route
        self.client_factory = client_factory
        self.selected = []
        self.entries = []
        self.last_request = None
        if row['context_jobs'] is not None:
            selected = json.loads(row['context_jobs'])
            expected = box.seal({'job':row['id'],'context_jobs':selected})
            if not hmac.compare_digest(row['context_mac'] or '', expected):
                raise ValueError('tampered_context_selection')
            for item in selected:
                self._load(item)

    def _original(self, row):
        if row is None: raise ValueError('tampered_context_evidence')
        original = json.loads(row['payload'])
        if not hmac.compare_digest(row['mac'], self.box.seal(original)):
            raise ValueError('tampered_context_evidence')
        ctx, anchor = original['context'], self.evidence['context']
        # Shared conversation participants may supply object context. Private
        # memory and specialist workflows remain pinned to the original actor.
        private = anchor['destination']['kind'] != 'group' or self.evidence['purpose'] != 'normal'
        if (original['purpose'] != self.evidence['purpose'] or original['config_path'] != self.evidence['config_path']
                or any(ctx[k] != anchor[k] for k in ('write_wing','destination','profile_name'))
                or private and ctx['actor_ref'] != anchor['actor_ref']
                or ctx['source_ref'] == anchor['source_ref']
                or original['received_at'] >= self.evidence['received_at']
                or detected_secret(original['text'])):
            raise ValueError('revoked_context_scope')
        self.resolver(original)
        entry={'source_ref':ctx['source_ref'],'text':original['text'],
               'actor_ref':ctx['actor_ref'],'received_at':original['received_at']}
        if row['output'] is not None:
            output=json.loads(row['output'])
            if not hmac.compare_digest(row['output_mac'] or '',self.box.seal({'job':row['id'],'output':output})):
                raise ValueError('tampered_context_evidence')
            entry['fact_keys']=[f['key'] for f in output if f.get('action')=='set']
        return entry

    def _remember(self, selection, entry):
        if any(x['source_ref'] == entry['source_ref'] for x in self.entries): return
        # A provider request has a resource budget, not a history horizon. A
        # budget failure retries visibly instead of claiming "no useful fact".
        if sum(len(x['text'].encode()) for x in self.entries) + len(entry['text'].encode()) > 128000:
            raise ValueError('context_budget_exhausted')
        self.selected.append(selection)
        self.entries.append(entry)

    def _load(self, item):
        if isinstance(item,str):
            row = self.box.db.execute('SELECT * FROM jobs WHERE id=?',(item,)).fetchone()
            self._remember(item,self._original(row))
        elif isinstance(item,dict) and set(item) == {'memory','scope'}:
            if item['scope'] != self._scope(): raise ValueError('revoked_context_scope')
            self.resolver(self.evidence)
            self._remember(item,item['memory'])
        else: raise ValueError('tampered_context_selection')

    def inherit(self, candidate):
        """Reuse a cached output's authenticated sources, rechecking current scope."""
        if candidate['context_jobs'] is None:return
        selected=json.loads(candidate['context_jobs'])
        expected=self.box.seal({'job':candidate['id'],'context_jobs':selected})
        if not hmac.compare_digest(candidate['context_mac'] or '',expected):
            raise ValueError('tampered_context_selection')
        for item in selected:self._load(item)
        self._save()

    def _scope(self):
        ctx = self.evidence['context']
        return digest({k:ctx[k] for k in ('read_wings','write_wing','destination','profile_name','policy_fingerprint')})

    def _save(self):
        with self.box.db:
            self.box.db.execute('UPDATE jobs SET context_jobs=?,context_mac=? WHERE id=?',
                (canonical(self.selected).decode(), self.box.seal({'job':self.row['id'],'context_jobs':self.selected}),self.row['id']))

    def lookup(self, request):
        if not isinstance(request,dict) or set(request)-{'kind','query','offset'} or request.get('kind') not in {'history_search','history_page','memory_search','fact_catalog'}:
            raise ValueError('invalid_context_lookup')
        kind, query, offset = request['kind'], request.get('query',''), request.get('offset',0)
        if not isinstance(query,str) or len(query.encode())>2000 or type(offset) is not int or offset<0:
            raise ValueError('invalid_context_lookup')
        if kind not in {'history_page','fact_catalog'} and not query.strip(): raise ValueError('invalid_context_lookup')
        if kind == 'fact_catalog':
            self.resolver(self.evidence)
            ctx=self.evidence['context']
            ledgers=self.box.db.execute('SELECT * FROM facts WHERE wing=? AND actor=? ORDER BY fact_key LIMIT 51 OFFSET ?',
                (ctx['write_wing'],ctx['actor_ref'],offset)).fetchall()
            found=[];future_keys=[]
            for ledger in ledgers[:50]:
                value={k:ledger[k] for k in ledger.keys() if k!='mac'}
                if not hmac.compare_digest(ledger['mac'],self.box.seal(value)):raise ValueError('tampered_fact_ledger')
                if ledger['received_at']>=self.evidence['received_at']:
                    future_keys.append(ledger['fact_key']);continue
                original=self.box.db.execute("""SELECT * FROM jobs WHERE json_extract(payload,'$.context.source_ref')=?
                    AND json_extract(payload,'$.context.write_wing')=? AND json_extract(payload,'$.received_at')=?
                    AND output IS NOT NULL ORDER BY created_at DESC LIMIT 1""",
                    (ledger['source_ref'],ctx['write_wing'],ledger['received_at'])).fetchone()
                if original is None:continue
                payload=json.loads(original['payload']);output=json.loads(original['output'])
                if not hmac.compare_digest(original['mac'],self.box.seal(payload)) or not hmac.compare_digest(
                        original['output_mac'] or '',self.box.seal({'job':original['id'],'output':output})):
                    raise ValueError('tampered_context_evidence')
                fact=next((f for f in output if f['key']==ledger['fact_key']),None)
                if fact is None or digest([fact['text'],fact['action']])!=ledger['content_hash']:raise ValueError('tampered_fact_ledger')
                text=fact['text'] if ledger['operation']=='set' else 'This property was retracted; its previous value must not be reused.'
                entry={'source_ref':'storedfact:'+ledger['id']+':'+ledger['content_hash'],'text':text,
                    'fact_key':ledger['fact_key'],'actor_ref':ledger['actor'],'received_at':ledger['received_at']}
                self._remember({'memory':entry,'scope':self._scope()},entry);found.append(entry)
            self._save()
            return {'entries':found,'future_property_keys':future_keys,'kind':kind,'has_more':len(ledgers)>50,'next_offset':offset+50}
        if kind == 'memory_search':
            if offset: raise ValueError('invalid_context_lookup')
            route = self.resolver(self.evidence)
            if self.client_factory: client = self.client_factory(route)
            else:
                from . import MemPalaceMemoryProvider
                client = MemPalaceMemoryProvider().for_access_context(route.context,options=route.provider_options)._client
            from .capability_provider import _result_text
            payload = json.loads(_result_text(client.mcp_call(route.context,'mempalace_search',{'query':query,'limit':20},operation='read')))
            if isinstance(payload,dict) and (payload.get('isError') or payload.get('error') or payload.get('success') is False):
                raise ValueError('context_memory_unavailable')
            rows = payload if isinstance(payload,list) else payload.get('results',[])
            if not isinstance(rows,list): raise ValueError('context_memory_unavailable')
            found = []
            for row in rows:
                text = row.get('text',row.get('content',row.get('chunk_text'))) if isinstance(row,dict) else None
                if not isinstance(text,str) or not text or detected_secret(text): continue
                if row.get('wing') and row['wing'] not in route.context.read_wings: raise ValueError('revoked_context_scope')
                entry={'source_ref':'memory:'+digest([self._scope(),row.get('drawer_id'),text]),'text':text,'kind':'stored_memory'}
                slug=row.get('source_file',row.get('title',''))
                ledger=self.box.db.execute('SELECT * FROM facts WHERE wing=? AND slug=?',
                    (self.evidence['context']['write_wing'],slug)).fetchone()
                if ledger:
                    value={k:ledger[k] for k in ledger.keys() if k!='mac'}
                    if not hmac.compare_digest(ledger['mac'],self.box.seal(value)):raise ValueError('tampered_fact_ledger')
                    entry['fact_key']=ledger['fact_key'];entry['actor_ref']=ledger['actor']
                self._remember({'memory':entry,'scope':self._scope()},entry);found.append(entry)
            self._save()
            return {'entries':found,'has_more':False,'query':query,'kind':kind}
        ctx = self.evidence['context']
        params = [ctx['write_wing'],self.evidence['received_at']]
        sql = """SELECT j.* FROM jobs j WHERE json_extract(j.payload,'$.context.write_wing')=?
            AND json_extract(j.payload,'$.received_at')<?"""
        if kind == 'history_search':
            tokens=re.findall(r'[^\W_]+',query.casefold(),re.UNICODE)
            if not tokens:return {'entries':[],'has_more':False,'kind':kind}
            terms=[]
            for token in dict.fromkeys(tokens):
                # FTS5 prefix lookup finds inflected Russian nouns as well as
                # exact labels. Terms are escaped, never raw FTS expressions.
                stem=token[:-2] if len(token)>6 else token
                terms.append('"'+stem.replace('"','""')+'"'+('*' if len(stem)>=3 else ''))
            sql += ' AND j.rowid IN (SELECT rowid FROM fact_message_search WHERE fact_message_search MATCH ?)'
            params.append(' OR '.join(terms))
            sql += " ORDER BY json_extract(j.payload,'$.received_at') DESC,j.id DESC"
        else:sql += " ORDER BY json_extract(j.payload,'$.received_at') DESC,j.id DESC"
        page_size=50
        sql += ' LIMIT ? OFFSET ?';params += [page_size+1,offset]
        candidates=self.box.db.execute(sql,params).fetchall()
        found=[]
        for row in candidates[:page_size]:
            try:entry=self._original(row)
            except ValueError:continue
            self._remember(row['id'],entry);found.append(entry)
        self._save()
        return {'entries':found,'has_more':len(candidates)>page_size,'next_offset':offset+page_size,
                'kind':kind,'query':query,'history_has_no_age_limit':True}
