"""Deterministic PSD-slot allocation; planning is read-only unless --output is given."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath

from materials import copy_one, exclusive_json, file_sha256, image_metadata, inventory_paths, safe_name
from template_cache import check_documents


def absolute(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def joined(root: str, *parts: str) -> str:
    if PureWindowsPath(root).is_absolute():
        return str(PureWindowsPath(root).joinpath(*parts))
    return str(Path(root).joinpath(*parts))


def manifest_digest(manifest: dict) -> str:
    body = {key: value for key, value in manifest.items() if key != 'digest'}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def head_slots(template: dict) -> list[str]:
    """Absent head_slots preserves the original one-head profile and link schema."""
    heads = template.get('head_slots', [template['head_slot']])
    effects = [slot['name'] for slot in template['effect_slots']]
    if (not isinstance(heads, list) or not heads
            or any(not isinstance(name, str) for name in heads)
            or len(set(heads)) != len(heads) or not set(heads) <= set(effects)
            or heads[0] != template['head_slot']):
        raise ValueError('head_slots must name distinct random slots, with head_slot first')
    if len(heads) > 1 and len({slot['pool'] for slot in template['effect_slots']}) != 1:
        raise ValueError('Multi-head profiles require one common pool for all random slots')
    return heads


def validate_config(config: dict) -> None:
    safe_name(config['product_key'])
    if not absolute(config['batch_root']):
        raise ValueError('batch_root must be absolute')
    if not isinstance(config['seed'], int) or isinstance(config['seed'], bool):
        raise ValueError('seed must be an integer')
    templates = config['templates']
    if not templates or len({t['name'].casefold() for t in templates}) != len(templates):
        raise ValueError('Template names must be nonempty and unique ignoring case')
    for template in templates:
        safe_name(template['name'])
        if not absolute(template['psd_dir']):
            raise ValueError('psd_dir must be absolute')
        effects, fixed = template['effect_slots'], template.get('fixed_slots', [])
        names = [safe_name(slot['name']) for slot in effects + fixed]
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError('All replacement slot names must be unique ignoring case')
        head_slots(template)
        if type(template['head_slice']) is not int or template['head_slice'] < 1:
            raise ValueError('head_slice must be a positive integer')
        if 'count' in template and (type(template['count']) is not int or template['count'] < 1):
            raise ValueError('Explicit count must be a positive integer')
        for slot in effects:
            if slot['pool'] not in config['pools']:
                raise ValueError(f'Unknown pool: {slot["pool"]}')
        for slot in fixed:
            if not absolute(slot['source']):
                raise ValueError('Fixed source paths must be absolute')


def pool_records(config: dict, records: dict) -> dict:
    result = {}
    for name, paths in config['pools'].items():
        unique = {}
        for path in sorted(set(paths)):
            record = records[path]
            unique.setdefault(record['pixel_sha256'], record)
        result[name] = list(unique.values())
    return result


def match_slots(effects: list[dict], pools: dict, head_slot: str,
                head: dict, rng: random.Random) -> dict | None:
    """Augmenting paths find a complete distinct-pixel matching, avoiding greedy loss."""
    choices = {}
    by_pixel = {}
    for slot in effects:
        if slot['name'] == head_slot:
            continue
        candidates = [r for r in pools[slot['pool']] if r['pixel_sha256'] != head['pixel_sha256']]
        rng.shuffle(candidates)
        choices[slot['name']] = [r['pixel_sha256'] for r in candidates]
        by_pixel.update({(slot['name'], r['pixel_sha256']): r for r in candidates})
    owners = {}

    def augment(slot: str, seen: set) -> bool:
        for pixel in choices[slot]:
            if pixel in seen:
                continue
            seen.add(pixel)
            if pixel not in owners or augment(owners[pixel], seen):
                owners[pixel] = slot
                return True
        return False

    order = list(choices)
    rng.shuffle(order)
    for slot in order:
        if not augment(slot, set()):
            return None
    return {head_slot: head, **{slot: by_pixel[(slot, pixel)] for pixel, slot in owners.items()}}


def arrangement_signatures(template, assignment):
    """Keep PSD display order and semantic slot mappings as independent checks."""
    ordered = tuple(assignment[slot['name']] for slot in template['effect_slots'])
    named = tuple(sorted((slot['name'].casefold(), assignment[slot['name']])
                         for slot in template['effect_slots']))
    return tuple(sorted(ordered)), ordered, named


def _record_arrangement(template, assignment, used_sets):
    pixels = {slot: record['pixel_sha256'] for slot, record in assignment.items()}
    key, ordered, named = arrangement_signatures(template, pixels)
    if any(other != template['name'] and (old_order == ordered or old_named == named)
           for other, old_order, old_named in used_sets.get(key, [])):
        return False
    used_sets.setdefault(key, []).append((template['name'], ordered, named))
    return True


def template_allocations(template: dict, pools: dict, seed: int, used_sets: dict) -> tuple[list, int]:
    if len(head_slots(template)) > 1:
        return multihead_allocations(template, pools, seed, used_sets)
    rng = random.Random(f'{seed}:{template["name"]}')
    effects = template['effect_slots']
    head_name = template['head_slot']
    head_pool = next(slot['pool'] for slot in effects if slot['name'] == head_name)
    heads = list(pools[head_pool])
    rng.shuffle(heads)
    feasible = []
    for head in heads:
        match = match_slots(effects, pools, head_name, head, rng)
        if match is not None:
            feasible.append((head, match))
    capacity = len(feasible)
    wanted = template.get('count', capacity)
    if not capacity or wanted > capacity:
        raise ValueError(f'{template["name"]}: requested {wanted}; feasible unique-head capacity {capacity}')
    output = []
    for head, initial in feasible[:wanted]:
        match = initial
        for attempt in range(256):
            if _record_arrangement(template, match, used_sets):
                output.append(match)
                break
            match = match_slots(effects, pools, head_name, head, rng)
        else:
            raise ValueError(f'{template["name"]}: cannot produce a distinct cross-template ordering after 256 attempts; change slot/pool assignment or requested count')
    return output, capacity


def multihead_allocations(template: dict, pools: dict, seed: int,
                          used_sets: dict) -> tuple[list, int]:
    """Disjoint head groups attain N//K locally; bounded ordering search may fail."""
    heads = head_slots(template)
    pool = pools[template['effect_slots'][0]['pool']]
    capacity = len(pool) // len(heads)
    wanted = template.get('count', capacity)
    if len(pool) < len(template['effect_slots']) or not capacity or wanted > capacity:
        raise ValueError(f'{template["name"]}: insufficient images or requested count exceeds capacity {capacity}')
    rng = random.Random(f'{seed}:{template["name"]}')
    candidates = list(pool)
    rng.shuffle(candidates)
    remaining_slots = [slot['name'] for slot in template['effect_slots'] if slot['name'] not in heads]
    output = []
    for index in range(wanted):
        group = candidates[index * len(heads):(index + 1) * len(heads)]
        reserved = {record['pixel_sha256'] for record in group}
        choices = [record for record in pool if record['pixel_sha256'] not in reserved]
        output.append(_multihead_arrangement(template, group, choices, remaining_slots,
                                              rng, used_sets))
    return output, capacity


def _multihead_arrangement(template, group, choices, remaining_slots, rng, used_sets):
    for _ in range(256):
        ordered_heads = list(group)
        rng.shuffle(ordered_heads)
        assignment = dict(zip(head_slots(template), ordered_heads))
        assignment.update(zip(remaining_slots, rng.sample(choices, len(remaining_slots))))
        if _record_arrangement(template, assignment, used_sets):
            return assignment
    raise ValueError(f'{template["name"]}: no distinct cross-template ordering after 256 attempts; '
                     'bounded search exhausted, not a proof of global infeasibility')


def build_plan(config: dict, records: dict) -> dict:
    """Core accepts inspected metadata for isolated replays without opening raster files."""
    validate_config(config)
    pools = pool_records(config, records)
    batches, capacities, seen = {}, {}, {}
    for template in config['templates']:
        batches[template['name']], capacities[template['name']] = template_allocations(template, pools, config['seed'], seen)
    links, copies = [], []
    for index in range(max(len(batch) for batch in batches.values())):
        for template in config['templates']:
            if index >= len(batches[template['name']]):
                continue
            link = make_link(config, template, index, len(links) + 1)
            links.append(link)
            assignment = batches[template['name']][index]
            for slot in template['effect_slots']:
                copies.append(make_copy(link, slot['name'], assignment[slot['name']], 'effect'))
            for slot in template.get('fixed_slots', []):
                copies.append(make_copy(link, slot['name'], records[slot['source']], 'fixed'))
    result = {'schema_version': 1, 'seed': config['seed'], 'product_key': config['product_key'],
              'batch_root': config['batch_root'], 'templates': copy.deepcopy(config['templates']),
              'capacities': capacities, 'links': links, 'copies': copies,
              'fixed_slots_exempt': True}
    multihead_pools = {template['name']: {
        'pool': template['effect_slots'][0]['pool'],
        'pixels': sorted(record['pixel_sha256'] for record in pools[template['effect_slots'][0]['pool']]),
        'records': [copy.deepcopy(records[path]) for path in sorted(set(
            config['pools'][template['effect_slots'][0]['pool']]))]}
        for template in config['templates'] if len(head_slots(template)) > 1}
    if multihead_pools:
        result['multihead_pools'] = multihead_pools
    manifest = {**result, 'digest': manifest_digest(result)}
    validate_manifest(manifest)
    return manifest


def make_link(config: dict, template: dict, index: int, identifier: int) -> dict:
    combination = f'组合{index + 1:03d}'
    return {'id': identifier, 'template': template['name'], 'combination': index + 1,
            'source_folder': joined(config['batch_root'], template['name'], combination, config['product_key']),
            'output_folder': joined(config['batch_root'], '成品', template['name'], combination),
            'psd_folder': template['psd_dir'], 'head_slot': template['head_slot'], 'head_slice': template['head_slice']}


def make_copy(link: dict, slot: str, record: dict, role: str) -> dict:
    extension = PureWindowsPath(record['source']).suffix
    if not extension:
        raise ValueError('Source image extension is missing')
    return {'link_id': link['id'], 'slot': slot, 'role': role, 'source': record['source'],
            'destination': joined(link['source_folder'], slot + extension),
            'sha256': record['sha256'], 'pixel_sha256': record['pixel_sha256']}


def validate_manifest(manifest: dict) -> None:
    if manifest.get('schema_version') != 1 or manifest.get('digest') != manifest_digest(manifest):
        raise ValueError('Manifest digest/schema invalid; re-plan rather than manually editing')
    if any(type(row.get(field)) is not int or row[field] < 1
           for row in manifest['links'] for field in ('id', 'combination', 'head_slice')):
        raise ValueError('Link IDs, combinations and slices must be positive integers')
    if any(type(row.get('link_id')) is not int or row['link_id'] < 1 for row in manifest['copies']):
        raise ValueError('Copy link IDs must be positive integers')
    _validate_record_bindings(manifest['copies'])
    links = {row['id']: row for row in manifest['links']}
    if list(links) != list(range(1, len(links) + 1)) or len(links) != len(manifest['links']):
        raise ValueError('Link IDs must be consecutive and unique')
    pools = {slot['pool']: [] for template in manifest['templates'] for slot in template['effect_slots']}
    validate_config({**manifest, 'pools': pools})
    templates = {row['name']: row for row in manifest['templates']}
    safe_name(manifest['product_key'])
    if not absolute(manifest['batch_root']):
        raise ValueError('Manifest batch_root must be absolute')
    for link in links.values():
        template = templates[link['template']]
        safe_name(template['name'])
        if link != make_link(manifest, template, link['combination'] - 1, link['id']):
            raise ValueError('Link paths/positions differ from their template and batch root')
    destinations = set()
    per_link, heads, slots_seen, assignments = {}, {}, {}, {}
    for row in manifest['copies']:
        link = links[row['link_id']]
        safe_name(row['slot'])
        template = templates[link['template']]
        roles = {s['name']: 'effect' for s in template['effect_slots']}
        roles.update({s['name']: 'fixed' for s in template.get('fixed_slots', [])})
        if roles.get(row['slot']) != row['role']:
            raise ValueError('Manifest slot role is inconsistent with template')
        if row['slot'] in slots_seen.setdefault(link['id'], set()):
            raise ValueError('Duplicate replacement slot within link')
        slots_seen[link['id']].add(row['slot'])
        expected = joined(link['source_folder'], row['slot'] + PureWindowsPath(row['source']).suffix)
        if expected != row['destination'] or expected.casefold() in destinations:
            raise ValueError('Invalid or duplicate copy destination')
        destinations.add(expected.casefold())
        if row['role'] == 'effect':
            pixels = per_link.setdefault(row['link_id'], set())
            if row['pixel_sha256'] in pixels:
                raise ValueError('Repeated random image within link')
            pixels.add(row['pixel_sha256'])
            assignments.setdefault(link['id'], {})[row['slot']] = row['pixel_sha256']
            if row['slot'] in head_slots(template):
                used = heads.setdefault(link['template'], set())
                if row['pixel_sha256'] in used:
                    raise ValueError('Repeated template head image')
                used.add(row['pixel_sha256'])
    for link in links.values():
        template = templates[link['template']]
        expected = {s['name'] for s in template['effect_slots'] + template.get('fixed_slots', [])}
        if slots_seen.get(link['id'], set()) != expected:
            raise ValueError('Missing replacement slots in manifest')
    _validate_multihead_pools(manifest, templates, links, assignments)
    _validate_record_bindings(_material_records(manifest))
    _validate_round_robin(manifest, templates, links)
    _validate_cross_template(templates, links, assignments)


def _validate_multihead_pools(manifest, templates, links, assignments):
    expected = {name for name, template in templates.items() if len(head_slots(template)) > 1}
    metadata = manifest.get('multihead_pools', {})
    if not isinstance(metadata, dict) or set(metadata) != expected:
        raise ValueError('Multi-head pool metadata must bind exactly the multi-head templates')
    for name in expected:
        template, entry = templates[name], metadata[name]
        if not isinstance(entry, dict):
            raise ValueError('Invalid multi-head pool metadata')
        pixels = entry.get('pixels')
        if (entry.get('pool') != template['effect_slots'][0]['pool']
                or not isinstance(pixels, list) or not pixels
                or any(not isinstance(pixel, str) or not pixel for pixel in pixels)
                or len(set(pixels)) != len(pixels)):
            raise ValueError('Invalid deduplicated multi-head pool metadata')
        if (len(pixels) < len(template['effect_slots'])
                or manifest['capacities'].get(name) != len(pixels) // len(head_slots(template))):
            raise ValueError('Multi-head capacity differs from floor(unique pixels / head slots)')
        pool = set(pixels)
        records = entry.get('records')
        if not isinstance(records, list) or not records:
            raise ValueError('Multi-head pool metadata requires all source records')
        _validate_record_bindings(records)
        if {record['pixel_sha256'] for record in records} != pool:
            raise ValueError('Multi-head pool pixels differ from source record bindings')
        members = {(record['source'], record['sha256'], record['pixel_sha256']) for record in records}
        link_ids = {link['id'] for link in links.values() if link['template'] == name}
        if any((row['source'], row['sha256'], row['pixel_sha256']) not in members
               for row in manifest['copies'] if row['role'] == 'effect' and row['link_id'] in link_ids):
            raise ValueError('Random image does not match its inspected pool record binding')
        if any(not set(assignments[link['id']].values()) <= pool
               for link in links.values() if link['template'] == name):
            raise ValueError('Random image is outside its inspected multi-head pool')


def _validate_round_robin(manifest, templates, links):
    capacities = manifest.get('capacities', {})
    if set(capacities) != set(templates):
        raise ValueError('Manifest capacity keys differ from templates')
    counts = {}
    for name, template in templates.items():
        capacity = capacities[name]
        count = template.get('count', capacity)
        if type(capacity) is not int or capacity < 1 or count > capacity:
            raise ValueError('Invalid manifest capacity or requested count')
        counts[name] = count
    if dict(Counter(link['template'] for link in links.values())) != counts:
        raise ValueError('Link counts differ from requested count/capacity')
    expected = [(name, index + 1) for index in range(max(counts.values()))
                for name in templates if index < counts[name]]
    actual = [(link['template'], link['combination']) for link in links.values()]
    if actual != expected:
        raise ValueError('Links do not follow template round-robin order')


def _validate_cross_template(templates, links, assignments):
    previous = {}
    for link in links.values():
        template = templates[link['template']]
        key, ordered, named = arrangement_signatures(template, assignments[link['id']])
        if any(other != template['name'] and (old_order == ordered or old_named == named)
               for other, old_order, old_named in previous.get(key, [])):
            raise ValueError('Repeated cross-template photo set and display ordering or named-slot mapping')
        previous.setdefault(key, []).append((template['name'], ordered, named))


def _material_records(manifest):
    return list(manifest['copies']) + [record for entry in manifest.get('multihead_pools', {}).values()
                                       for record in entry['records']]


def _validate_record_bindings(records):
    """One byte SHA can decode to only one pixel SHA, including fixed inputs."""
    byte_pixels, source_claims = {}, {}
    for record in records:
        if (not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key]
                                               for key in ('source', 'sha256', 'pixel_sha256'))
                or not absolute(record['source'])):
            raise ValueError('Invalid source byte/pixel metadata binding')
        digest, pixel = record['sha256'], record['pixel_sha256']
        source = str(PureWindowsPath(record['source'])).casefold()
        if digest in byte_pixels and byte_pixels[digest] != pixel:
            raise ValueError('Same source byte SHA claims different pixel metadata')
        claim = (digest, pixel)
        if source in source_claims and source_claims[source] != claim:
            raise ValueError('Same source path claims inconsistent byte/pixel metadata')
        byte_pixels[digest] = pixel
        source_claims[source] = claim


def verify_material_metadata(manifest: dict, workers: int = 4) -> dict:
    """Read real input bytes and decoded pixels before copy or readiness approval.

    Includes every bound multi-head pool member, even if not assigned to a link.
    Decode once per distinct byte SHA/extension; verify all aliases' bytes.
    """
    records = _material_records(manifest)
    _validate_record_bindings(records)
    sources = {record['source']: record for record in records}
    groups = {}
    for record in sources.values():
        key = (record['sha256'], Path(record['source']).suffix.lower())
        groups.setdefault(key, []).append(record)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_verify_material_group, groups.values()))
    return {'sources': len(sources), 'decoded_groups': len(groups), 'verified': True}


def _verify_material_group(records):
    snapshots = {}
    for record in records:
        path = Path(record['source'])
        stat = path.stat()
        snapshots[record['source']] = (stat.st_size, stat.st_mtime_ns)
        if file_sha256(path) != record['sha256']:
            raise ValueError(f'Source hash changed: {path}')
    decoded = image_metadata(records[0]['source'])
    if any(record['pixel_sha256'] != decoded['pixel_sha256'] for record in records):
        raise ValueError(f'Source pixel metadata mismatch: {records[0]["source"]}')
    for record in records:
        path = Path(record['source'])
        stat = path.stat()
        if (snapshots[record['source']] != (stat.st_size, stat.st_mtime_ns)
                or file_sha256(path) != record['sha256']):
            raise ValueError(f'Source changed during metadata verification: {path}')


def verify_profiles(manifest: dict) -> None:
    """A plan is executable only while its inspected four-document profile is current."""
    for template in manifest['templates']:
        directory = Path(template['psd_dir'])
        if not directory.is_dir():
            raise FileNotFoundError(f'PSD directory is unavailable: {template["psd_dir"]}')
        documents = template.get('documents', [])
        if len(documents) != 4:
            raise ValueError('Copy requires four inspected PSD document versions per template')
        names = {Path(document['path']).name.casefold() for document in documents}
        if names != {'主图.psd', '详情.psd', 'sku.psd', '白底.psd'}:
            raise ValueError('Profile must bind 主图.psd, 详情.psd, sku.psd and 白底.psd')
        for document in documents:
            if Path(document['path']).resolve().parent != directory.resolve():
                raise ValueError('Bound PSD document is outside its declared PSD directory')
            if (not {'bytes', 'mtime_ns', 'sha256'} <= set(document)
                    or not isinstance(document['sha256'], str) or len(document['sha256']) != 64):
                raise ValueError('PSD document fingerprint is incomplete')
        if any(result['reinspect'] for result in check_documents(documents)):
            raise ValueError(f'PSD profile changed or unavailable; reinspect: {template["name"]}')
        pages = template.get('pages', {})
        if pages.get('verified') is not True:
            raise ValueError('Copy requires verified page mappings bound to this profile')
        for kind in ('main', 'detail', 'sku'):
            values = pages.get(kind)
            if (not isinstance(values, list) or not values
                    or any(type(value) is not int or value < 1 for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f'Invalid verified page mapping: {kind}')
        if template['head_slice'] not in pages['main']:
            raise ValueError('Head slice is not in the bound main-image page mapping')


def check_material_folders(manifest: dict, complete: bool = False) -> None:
    """Reject ambiguous replacement inputs; partial expected files are resumable."""
    expected = {}
    for row in manifest['copies']:
        path = Path(row['destination'])
        expected.setdefault(path.parent, set()).add(path.name.casefold())
    for folder, names in expected.items():
        if not folder.exists():
            if complete:
                raise FileNotFoundError(f'Material folder missing: {folder}')
            continue
        if not folder.is_dir():
            raise FileExistsError(f'Material directory path is occupied: {folder}')
        children = list(folder.iterdir())
        actual = [child.name.casefold() for child in children]
        if any(not child.is_file() for child in children) or set(actual) - names or len(set(actual)) != len(actual):
            raise FileExistsError(f'Unexpected/ambiguous material files preserved: {folder}')
        if complete and set(actual) != names:
            raise FileNotFoundError(f'Material folder is incomplete: {folder}')


def verify_ready(manifest: dict, workers: int = 4) -> dict:
    """Read-only publication gate: current PSD profile, receipt and copied bytes."""
    validate_manifest(manifest)
    verify_profiles(manifest)
    if not 1 <= workers <= 16:
        raise ValueError('workers must be between 1 and 16')
    verify_material_metadata(manifest, workers)
    root = Path(manifest['batch_root'])
    saved = json.loads((root / '素材分配清单.json').read_text(encoding='utf-8'))
    if saved != manifest:
        raise ValueError('Saved batch manifest differs from publication input')
    check_receipt(manifest)
    check_material_folders(manifest, complete=True)
    def verify_destination(row):
        if file_sha256(row['destination']) != row['sha256']:
            raise ValueError(f'Copied material changed after receipt: {row["destination"]}')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(verify_destination, manifest['copies']))
    return {'manifest_digest': manifest['digest'], 'files': len(manifest['copies']), 'verified': True}


def check_receipt(manifest: dict) -> None:
    receipt_file = Path(manifest['batch_root']) / '素材复制核验.json'
    receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
    if (receipt.get('manifest_digest') != manifest['digest'] or receipt.get('verified') is not True
            or receipt.get('files') != len(manifest['copies'])):
        raise ValueError('Copy receipt is missing, stale or inconsistent')


def execute_copy(manifest: dict, workers: int = 4) -> dict:
    validate_manifest(manifest)
    verify_profiles(manifest)
    if not 1 <= workers <= 16:
        raise ValueError('workers must be between 1 and 16')
    verify_material_metadata(manifest, workers)
    root = Path(manifest['batch_root'])
    saved = root / '素材分配清单.json'
    if saved.exists():
        if json.loads(saved.read_text(encoding='utf-8')) != manifest:
            raise FileExistsError(f'Existing batch manifest differs: {saved}')
    else:
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise FileExistsError(f'Populated batch has no matching ownership manifest: {root}')
    if (root / '素材复制核验.json').exists():
        check_receipt(manifest)
    check_material_folders(manifest)
    if not saved.exists():
        exclusive_json(saved, manifest)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(copy_one, manifest['copies']))
    for link in manifest['links']:
        Path(link['output_folder']).mkdir(parents=True, exist_ok=True)
    csv_path = root / '素材分配清单.csv'
    if not csv_path.exists():
        with csv_path.open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(manifest['copies'][0]))
            writer.writeheader()
            writer.writerows(manifest['copies'])
    receipt = {'manifest_digest': manifest['digest'], 'files': len(results), 'verified': True,
               'copied': sum(r['status'] == 'copied' for r in results),
               'resumed': sum(r['status'] == 'resumed' for r in results)}
    receipt_path = root / '素材复制核验.json'
    if not receipt_path.exists():
        exclusive_json(receipt_path, receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inventory', 'plan', 'copy'])
    parser.add_argument('input', help='Config JSON for inventory/plan; saved manifest JSON for copy')
    parser.add_argument('--output', help='Explicit JSON output (exclusive create); otherwise stdout')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    supplied = json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
    if args.command == 'copy':
        result = execute_copy(supplied, args.workers)
    else:
        validate_config(supplied)
        paths = [p for pool in supplied['pools'].values() for p in pool]
        paths += [s['source'] for t in supplied['templates'] for s in t.get('fixed_slots', [])]
        inventory = inventory_paths(paths)
        records = {r['source']: r for r in inventory['images']}
        # Match equivalent Windows separator spellings in config without renaming sources.
        mapped = {p: records[str(Path(p))] for p in paths}
        result = inventory if args.command == 'inventory' else build_plan(supplied, mapped)
    if args.output:
        exclusive_json(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
