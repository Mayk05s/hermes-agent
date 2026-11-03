"""Thin Telegram adapter to Hermes' shared voice/chat template registry.

Module and registry locations are server configuration, never model arguments.
"""
import importlib.util
import os
import sys
from pathlib import Path

_CACHE={}
def shared_templates():
    root=Path(os.environ.get('HERMES_SHARED_HOME_MODULE_DIR',str(Path.home()/'.hermes/quasar-bridge/src/hermes_adapter'))).resolve()
    database=Path(os.environ.get('HERMES_SHARED_HOME_RULES',str(Path.home()/'.hermes/profiles/station-voice/template_rules.sqlite3'))).resolve()
    modules=[]
    for name in ('template_rules','template_bridge'):
        path=root/(name+'.py');key=(str(path),path.stat().st_mtime_ns)
        if key not in _CACHE:
            spec=importlib.util.spec_from_file_location('_hermes_shared_'+name,path)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);_CACHE[key]=module
        modules.append(_CACHE[key])
    return modules[1].SharedTemplates(modules[0].Registry(database))

def unwrap(result):
    if not isinstance(result,dict):raise ValueError('invalid_ha_response')
    value=result.get('response',result)
    if 'service_response' in value:value=value['service_response']
    if value.get('error'):raise ValueError('ha_contract_unavailable')
    return value

async def match(text,call,shared=None):
    inventory=unwrap(await call('inventory',{}))
    result=(shared or shared_templates()).match(text,inventory)
    return {**result,'inventory_revision':inventory['inventory_revision']}

async def learn(proposal,text,request_id,revision,call,shared=None):
    if not request_id or not text:raise ValueError('trusted_original_request_required')
    receipt=unwrap(await call('delivery',{'request_id':request_id})).get('receipt') or {}
    inventory=unwrap(await call('inventory',{}))
    return (shared or shared_templates()).learn(proposal,text,receipt,inventory,request_id,revision)
