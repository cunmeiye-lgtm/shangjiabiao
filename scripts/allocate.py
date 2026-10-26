"""Deterministic PSD-slot allocation; planning is read-only unless --output is given."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath

from materials import copy_one, exclusive_json, file_sha256, inventory_paths, safe_name
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
        if not isinstance(template['head_slot'], str) or template['head_slot'] not in [s['name'] for s in effects]:
            raise ValueError('Exactly one head_slot must name a random effect slot; multi-head profiles are unsupported')
        if not isinstance(template['head_slice'], int) or template['head_slice'] < 1:
            raise ValueError('head_slice must be a positive integer')
        if 'count' in template and (not isinstance(template['count'], int) or template['count'] < 1):
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


def template_allocations(template: dict, pools: dict, seed: int, used_sets: dict) -> tuple[list, int]:
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
            ordered = tuple(match[slot['name']]['pixel_sha256'] for slot in effects)
            key = tuple(sorted(ordered))
            previous = used_sets.get(key, [])
            if all(other == template['name'] or arrangement != ordered for other, arrangement in previous):
                used_sets.setdefault(key, []).append((template['name'], ordered))
                output.append(match)
                break
            match = match_slots(effects, pools, head_name, head, rng)
        else:
            raise ValueError(f'{template["name"]}: cannot produce a distinct cross-template ordering after 256 attempts; change slot/pool assignment or requested count')
    return output, capacity


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
              'batch_root': config['batch_root'], 'templates': config['templates'],
              'capacities': capacities, 'links': links, 'copies': copies,
              'fixed_slots_exempt': True}
    return {**result, 'digest': manifest_digest(result)}


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
    links = {row['id']: row for row in manifest['links']}
    if list(links) != list(range(1, len(links) + 1)) or len(links) != len(manifest['links']):
        raise ValueError('Link IDs must be consecutive and unique')
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
    per_link, heads, slots_seen = {}, {}, {}
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
            if row['slot'] == link['head_slot']:
                used = heads.setdefault(link['template'], set())
                if row['pixel_sha256'] in used:
                    raise ValueError('Repeated template head image')
                used.add(row['pixel_sha256'])
    for link in links.values():
        template = templates[link['template']]
        expected = {s['name'] for s in template['effect_slots'] + template.get('fixed_slots', [])}
        if slots_seen.get(link['id'], set()) != expected:
            raise ValueError('Missing replacement slots in manifest')


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
