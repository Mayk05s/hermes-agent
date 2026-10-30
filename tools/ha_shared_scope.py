"""HA-owned shared zones for an exact canonical entity and verified members.

Only namespaced labels carrying this versioned marker define membership. Area
labels describe the zone; the entity remains in its real physical HA area.
"""
import json

PREFIX = 'hermes_shared_zone_'
KIND = 'hermes.shared_zone.v1'


def area_names(entity):
    return set(entity.get('logical_areas') or []) | {a for a in
        (entity.get('physical_area'), entity.get('area')) if a}


def in_areas(entity, areas):
    return bool(area_names(entity) & set(areas))


async def label_registry(ws, identifier=10):
    await ws.send_json({'id': identifier, 'type': 'config/label_registry/list'})
    while True:
        result = await ws.receive_json()
        if result.get('id') == identifier:
            if not result.get('success'):
                raise ValueError('HA label registry unavailable')
            return result['result']


def resolve(areas, registry, devices, states, labels, exposure=None):
    """Return canonical metadata; changed/hidden/unresolved groups fail closed."""
    rows = {r['entity_id']: r for r in registry}
    live = {s['entity_id']: s for s in states}
    names = {a['area_id']: a['name'] for a in areas}
    device_areas = {d['id']: d.get('area_id') for d in devices}
    result = {}
    for label in labels:
        label_id = label.get('label_id', '')
        if not label_id.startswith(PREFIX):
            continue
        try:
            marker = json.loads(label.get('description') or '')
        except (ValueError, TypeError):
            continue
        if not isinstance(marker, dict) or marker.get('kind') != KIND:
            continue
        group = marker.get('entity_id')
        members = marker.get('members')
        if not isinstance(group, str) or not isinstance(members, list) or not members or any(not isinstance(m, str) for m in members):
            continue
        expected = set(members)
        zone = {a['name'] for a in areas if label_id in (a.get('labels') or [])}
        actual = live.get(group, {}).get('attributes', {}).get('entity_id')
        valid = (len(zone) >= 2 and group in rows and label_id in (rows[group].get('labels') or [])
                 and isinstance(actual, list) and set(actual) == expected
                 and len(expected) == len(members) and group not in expected
                 and all(m in rows and m in live and m.split('.')[0] == group.split('.')[0]
                         and not live[m].get('attributes', {}).get('entity_id') for m in expected))
        if exposure is not None:
            valid = valid and all(exposure.get(e, {}).get('conversation') for e in expected | {group})
        affected = expected | {group} | ({m for m in actual if isinstance(m,str)} if isinstance(actual,list) and not valid else set())
        if any(e in result for e in affected):
            valid = False
        for eid in affected:
            if eid not in rows:
                continue
            if not valid:
                result[eid] = {'scope_blocked': True, 'canonical_group': group}
                continue
            row = rows[eid]
            physical = names.get(row.get('area_id') or device_areas.get(row.get('device_id')))
            result[eid] = {'physical_area': physical, 'logical_areas': sorted(zone | ({physical} if physical else set())),
                           'canonical_group': group, 'shared_scope_source': 'ha_label:' + label_id}
    return result
