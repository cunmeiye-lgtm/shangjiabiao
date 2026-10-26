"""Native XLSX fidelity adapter around Artifact-authored values; never authors business data."""
import argparse
import copy
import hashlib
import io
import json
import math
import os
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path

from lxml import etree as E

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
N = {'s': NS}
CELL = re.compile(r'^([A-Z]{1,3})([1-9][0-9]*)$')


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def colnum(name):
    result = 0
    for c in name: result = result * 26 + ord(c) - 64
    return result


def column(number):
    name = ''
    while number:
        number, rem = divmod(number - 1, 26)
        name = chr(65 + rem) + name
    return name


def address(ref):
    m = CELL.fullmatch(ref)
    if not m or colnum(m[1]) > 16384 or int(m[2]) > 1048576:
        raise ValueError(f'Invalid cell: {ref}')
    return colnum(m[1]), int(m[2])


def package(path):
    with zipfile.ZipFile(path) as z:
        if len(z.namelist()) != len(set(z.namelist())):
            raise ValueError('Duplicate ZIP members')
        parts = {n: z.read(n) for n in z.namelist()}
    if any(n.startswith('_xmlsignatures/') for n in parts):
        raise ValueError('Signed workbooks cannot be preserved after editing')
    links = E.fromstring(parts['xl/_rels/workbook.xml.rels'])
    targets = {r.get('Id'): posixpath.normpath(posixpath.join('xl', r.get('Target'))).lstrip('/')
               for r in links if r.get('TargetMode') != 'External'}
    names = {s.get('name'): targets[s.get(f'{{{REL}}}id')]
             for s in E.fromstring(parts['xl/workbook.xml']).find('s:sheets', N)}
    shared = []
    if 'xl/sharedStrings.xml' in parts:
        shared = [''.join(x.itertext()) for x in E.fromstring(parts['xl/sharedStrings.xml'])]
    return parts, names, shared


def cell_value(cell, strings):
    if cell is None: return None
    if cell.find('s:f', N) is not None:
        return '=' + (cell.findtext('s:f', namespaces=N) or '')
    kind = cell.get('t')
    if kind == 'inlineStr': return ''.join(cell.find('s:is', N).itertext())
    val = cell.findtext('s:v', namespaces=N)
    if val is None: return None
    if kind == 's': return strings[int(val)]
    if kind == 'b': return val == '1'
    if kind in {'str', 'e', 'd'}: return val
    number = float(val)
    return int(number) if number.is_integer() else number


def read_cells(path, sheet, refs):
    parts, names, strings = package(path)
    root = E.fromstring(parts[names[sheet]])
    cells = {c.get('r'): c for c in root.findall('s:sheetData/s:row/s:c', N)}
    return [cell_value(cells.get(ref), strings) for ref in refs]


def scalar(value):
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError('Only scalar spreadsheet values are supported')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Non-finite numeric value')
    if isinstance(value, str) and (value.startswith('=') or len(value) > 32767):
        raise ValueError('Formula authoring and strings over 32767 chars are unsupported')


def changes_for(config):
    if config['operation'] == 'patch': return config['changes']
    return [{'sheet': name, 'cell': f'{column(c)}{r}', 'value': value}
            for name, spec in config['sheets'].items()
            for r, row in enumerate(spec['rows'], 2) for c, value in enumerate(row, 1)]


def validate_shape(config):
    if config.get('operation') not in {'batch', 'patch'}:
        raise ValueError('operation must be batch or patch')
    if not config.get('headers'): raise ValueError('Explicit headers mapping required')
    if config['operation'] == 'batch':
        if not config.get('sheets'): raise ValueError('No batch sheets')
        for name, spec in config['sheets'].items():
            width = len(config['headers'].get(name, []))
            if not width or not spec.get('rows') or any(len(row) != width for row in spec['rows']):
                raise ValueError(f'Full-width nonempty rows required: {name}')
            if not spec.get('seed_rows') or any(type(r) is not int or r < 2 for r in spec['seed_rows']):
                raise ValueError('seed_rows must name existing data rows')
            if len(spec['rows']) > 1048575: raise ValueError('Excel row limit exceeded')
    elif not config.get('changes'):
        raise ValueError('No patches')


def validate_sheet(config, name, parts, member, strings):
    root = E.fromstring(parts[member])
    cells = {c.get('r'): c for c in root.findall('s:sheetData/s:row/s:c', N)}
    headers = config['headers'].get(name)
    if not headers: raise ValueError(f'Missing header binding: {name}')
    got = [cell_value(cells.get(f'{column(c)}1'), strings) for c in range(1, len(headers) + 1)]
    if got != headers: raise ValueError(f'Header mismatch: {name}')
    if any(address(ref)[0] > len(headers) for ref in cells):
        raise ValueError(f'Header binding omits columns: {name}')
    if config['operation'] == 'batch':
        rows = {int(r.get('r')) for r in root.findall('s:sheetData/s:row', N)}
        if any(r not in rows for r in config['sheets'][name]['seed_rows']):
            raise ValueError(f'Missing seed row: {name}')
        for tag in ['tableParts', 'autoFilter', 'drawing', 'hyperlinks']:
            if root.find(f's:{tag}', N) is not None:
                raise ValueError(f'Batch cannot preserve row-bound {tag}: {name}')
        if any(address(m.get('ref').split(':')[-1])[1] > 1 for m in root.findall('s:mergeCells/s:mergeCell', N)):
            raise ValueError(f'Batch cannot preserve merged data cells: {name}')
    return root, cells


def validate_changes(config, parts, names, strings):
    seen, sheets = set(), {}
    for change in changes_for(config):
        name, ref = change['sheet'], change['cell']
        if name not in names: raise ValueError(f'Unknown sheet: {name}')
        if name not in sheets: sheets[name] = validate_sheet(config, name, parts, names[name], strings)
        root, cells = sheets[name]
        c, r = address(ref)
        if r < 2 or c > len(config['headers'][name]): raise ValueError(f'Unknown data cell: {name}!{ref}')
        if (name, ref) in seen: raise ValueError(f'Duplicate patch: {name}!{ref}')
        seen.add((name, ref))
        scalar(change['value'])
        if 'expected' in change and cell_value(cells.get(ref), strings) != change['expected']:
            raise ValueError(f'expected value mismatch: {name}!{ref}')
        for merge in root.findall('s:mergeCells/s:mergeCell', N):
            a, b = (address(x) for x in merge.get('ref').split(':'))
            if a[0] <= c <= b[0] and a[1] <= r <= b[1]:
                raise ValueError(f'Cannot edit merged data cells: {name}!{ref}')
        if config['operation'] == 'patch' and root.find(f's:sheetData/s:row[@r="{r}"]', N) is None:
            raise ValueError('Patch cannot append rows; use batch')
    return sheets


def prepare(config, workdir):
    validate_shape(config)
    source, output = Path(config['source']).resolve(), Path(config['output']).resolve()
    digest = sha256(source)
    replacing = config.get('replace_input', False)
    if replacing:
        if config.get('source_sha256') != digest or not config.get('backup'):
            raise ValueError('replace_input requires source_sha256 and exclusive backup path')
        if output != source: raise ValueError('replace_input output must equal source')
        if Path(config['backup']).exists(): raise FileExistsError(config['backup'])
    elif source == output or output.exists():
        raise FileExistsError(f'Existing output protected: {output}')
    fallback = config.get('fallback_output')
    if fallback and (Path(fallback).exists() or Path(fallback).resolve() in {source, output}):
        raise ValueError('fallback_output must be a new distinct path')
    parts, names, strings = package(source)
    sheets = validate_changes(config, parts, names, strings)
    if config['operation'] == 'batch':
        for member, payload in parts.items():
            if member.startswith('xl/') and 'comment' in member.lower() and member.endswith('.xml'):
                if any(address(c.get('ref'))[1] > 1 for c in E.fromstring(payload).findall('.//s:comment', N)):
                    raise ValueError('Batch with data-row comments requires manual migration')
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = workdir / 'source.xlsx'
    with snapshot.open('xb') as f: f.write(source.read_bytes())
    if sha256(snapshot) != digest: raise ValueError('Source changed during snapshot')
    result = {**config, 'source': str(source), 'output': str(output), 'source_sha256': digest,
              'snapshot': str(snapshot), 'authored': str(workdir / 'authored.xlsx'),
              'workdir': str(workdir), 'sheet_members': {name: names[name] for name in sheets}}
    with (workdir / 'prepared.json').open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def set_value(cell, value):
    cell.attrib.pop('t', None)
    for child in list(cell):
        if child.tag in {f'{{{NS}}}{tag}' for tag in ['v', 'is', 'f']}: cell.remove(child)
    if value is None: return
    if isinstance(value, str):
        cell.set('t', 'inlineStr')
        text = E.SubElement(E.SubElement(cell, f'{{{NS}}}is'), f'{{{NS}}}t')
        text.text = value
        if value != value.strip(): text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    else:
        if isinstance(value, bool): cell.set('t', 'b')
        E.SubElement(cell, f'{{{NS}}}v').text = str(int(value) if isinstance(value, bool) else value)


def ensure_cell(row, ref):
    cell = row.find(f's:c[@r="{ref}"]', N)
    if cell is None:
        cell = E.Element(f'{{{NS}}}c', r=ref)
        at = next((i for i, other in enumerate(row) if address(other.get('r'))[0] > address(ref)[0]), len(row))
        row.insert(at, cell)
    return cell


def edit_sheet(config, name, root, values):
    body = root.find('s:sheetData', N)
    if config['operation'] == 'batch':
        spec = config['sheets'][name]
        seeds = {int(r.get('r')): copy.deepcopy(r) for r in body}
        for row in list(body):
            if int(row.get('r')) > 1: body.remove(row)
        for r, row_values in enumerate(spec['rows'], 2):
            row = copy.deepcopy(seeds[spec['seed_rows'][(r - 2) % len(spec['seed_rows'])]])
            row.set('r', str(r))
            for cell in row: cell.set('r', f'{column(address(cell.get("r"))[0])}{r}')
            for c in range(1, len(row_values) + 1):
                ref = f'{column(c)}{r}'
                set_value(ensure_cell(row, ref), values[ref])
            body.append(row)
        dim = root.find('s:dimension', N)
        if dim is None:
            dim = E.Element(f'{{{NS}}}dimension')
            root.insert(0, dim)
        dim.set('ref', f'A1:{column(len(spec["rows"][0]))}{len(spec["rows"]) + 1}')
    else:
        for ref, value in values.items():
            row = body.find(f's:row[@r="{address(ref)[1]}"]', N)
            set_value(ensure_cell(row, ref), value)
    return E.tostring(root, encoding='UTF-8', xml_declaration=True, standalone=True)


def publish(config, payload):
    output = Path(config['output'])
    output.parent.mkdir(parents=True, exist_ok=True)
    if not config.get('replace_input'):
        with output.open('xb') as f: f.write(payload)
        return output
    backup = Path(config['backup'])
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open('xb') as f: f.write(Path(config['snapshot']).read_bytes())
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.jingzhi-', suffix='.xlsx', delete=False) as f:
        temp = Path(f.name)
        f.write(payload)
    try:
        if sha256(config['source']) != config['source_sha256']: raise ValueError('Source changed before replace')
        os.replace(temp, output)
    except PermissionError:
        if not config.get('fallback_output'): raise
        output = Path(config['fallback_output'])
        with output.open('xb') as f: f.write(payload)
    finally:
        if temp.exists(): temp.unlink()
    return output


def verify_rebuilt(config, name, old, new, expected):
    """Compare untouched worksheet XML and seed styles before publishing bytes."""
    old, new = copy.deepcopy(old), E.fromstring(new)
    cells = {c.get('r'): c for c in new.findall('s:sheetData/s:row/s:c', N)}
    if any(cell_value(cells.get(ref), []) != value for ref, value in expected.items()):
        raise ValueError(f'Rebuilt values differ: {name}')
    if config['operation'] == 'batch':
        spec = config['sheets'][name]
        seeds = {int(r.get('r')): r for r in old.findall('s:sheetData/s:row', N)}
        rows = new.findall('s:sheetData/s:row', N)
        if len(rows) != len(spec['rows']) + 1: raise ValueError('Rebuilt row count differs')
        if E.tostring(rows[0]) != E.tostring(seeds[1]): raise ValueError('Header changed')
        for index, row in enumerate(rows[1:]):
            seed = seeds[spec['seed_rows'][index % len(spec['seed_rows'])]]
            if {k: v for k, v in row.attrib.items() if k != 'r'} != {k: v for k, v in seed.attrib.items() if k != 'r'}:
                raise ValueError('Seed row formatting changed')
            seed_styles = {address(c.get('r'))[0]: c.get('s') for c in seed}
            if any(c.get('s') != seed_styles.get(address(c.get('r'))[0]) for c in row):
                raise ValueError('Seed cell style changed')
        for tree in [old, new]:
            for tag in ['sheetData', 'dimension']:
                elem = tree.find(f's:{tag}', N)
                if elem is not None: tree.remove(elem)
    else:
        for ref in expected:
            before = old.find(f's:sheetData/s:row/s:c[@r="{ref}"]', N)
            after = new.find(f's:sheetData/s:row/s:c[@r="{ref}"]', N)
            if before is not None and {k: v for k, v in before.attrib.items() if k != 't'} != {k: v for k, v in after.attrib.items() if k != 't'}:
                raise ValueError('Patched cell formatting changed')
            for cell in [before, after]:
                if cell is not None: cell.getparent().remove(cell)
    if E.tostring(old) != E.tostring(new): raise ValueError('Unrelated worksheet content changed')


def finish(config):
    for key in ['source', 'snapshot']:
        if sha256(config[key]) != config['source_sha256']: raise ValueError('Source changed')
    original, names, strings = package(config['snapshot'])
    sheets = validate_changes(config, original, names, strings)
    authored, authored_names, authored_strings = package(config['authored'])
    values = {name: {} for name in sheets}
    authored_cells = {name: {c.get('r'): c for c in E.fromstring(authored[authored_names[name]]).findall('s:sheetData/s:row/s:c', N)} for name in sheets}
    for change in changes_for(config):
        name, ref = change['sheet'], change['cell']
        actual = cell_value(authored_cells[name].get(ref), authored_strings)
        if actual != change['value'] or isinstance(actual, bool) != isinstance(change['value'], bool):
            raise ValueError(f'Artifact value mismatch: {name}!{ref}: {actual!r} != {change["value"]!r}')
        values[name][ref] = actual
    edits = {names[name]: edit_sheet(config, name, copy.deepcopy(root), values[name]) for name, (root, _) in sheets.items()}
    for name, (root, _) in sheets.items(): verify_rebuilt(config, name, root, edits[names[name]], values[name])
    memory = io.BytesIO()
    with zipfile.ZipFile(config['snapshot']) as source, zipfile.ZipFile(memory, 'w') as out:
        for info in source.infolist(): out.writestr(copy.copy(info), edits.get(info.filename, original[info.filename]))
    payload = memory.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as final:
        for member, content in original.items():
            if member not in edits and final.read(member) != content: raise ValueError('Unrelated ZIP part changed')
    if sha256(config['source']) != config['source_sha256']: raise ValueError('Source changed before publication')
    output = publish(config, payload)
    for name, entries in values.items():
        if read_cells(output, name, list(entries)) != list(entries.values()): raise ValueError('Published readback failed')
    result = {'output': str(output), 'sha256': sha256(output), 'changed_cells': sum(map(len, values.values())),
              'preserved_other_zip_parts': True, 'operation': config['operation'], 'uploaded': False}
    with (Path(config['workdir']) / 'verification.json').open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--finish', type=Path)
    args = parser.parse_args()
    if bool(args.prepare) == bool(args.finish): parser.error('Choose exactly --prepare or --finish')
    path = args.prepare or args.finish
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if args.prepare and not args.work_dir: parser.error('--work-dir required')
    result = prepare(config, args.work_dir) if args.prepare else finish(config)
    print(json.dumps({'prepared': str(args.work_dir / 'prepared.json')} if args.prepare else result, ensure_ascii=False))


if __name__ == '__main__': main()
