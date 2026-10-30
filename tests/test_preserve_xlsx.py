"""Preservation-layer fault tests; small XML packages are isolated test fixtures."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("preserve_xlsx", Path(__file__).parents[1] / "scripts/preserve_xlsx.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture(path, value=0, merged=False):
    parts = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="SPU" sheetId="1" r:id="r1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:C3"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>编号</t></is></c><c r="B1" t="inlineStr"><is><t>保存草稿箱</t></is></c><c r="C1" t="inlineStr"><is><t>说明</t></is></c></row><row r="2" ht="20" customHeight="1"><c r="A2"><v>1</v></c><c r="B2"><v>{value}</v></c></row><row r="3"><c r="A3"><f>A2+1</f><v>2</v></c></row></sheetData>{"<mergeCells><mergeCell ref=\"B2:C2\"/></mergeCells>" if merged else ""}<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>',
        'customXml/item1.xml': '<notes>keep verbatim</notes>',
    }
    with zipfile.ZipFile(path, 'w') as z:
        for key, val in parts.items():
            z.writestr(key, val)


class PreserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.xlsx'
        fixture(self.source)
        self.config = {'source': str(self.source), 'output': str(self.root / 'out.xlsx'),
                       'operation': 'patch', 'headers': {'SPU': ['编号', '保存草稿箱', '说明']},
                       'changes': [{'sheet': 'SPU', 'cell': 'B2', 'expected': 0, 'value': 1}]}

    def prepared(self, config=None):
        return M.prepare(config or self.config, self.root / 'work')

    def authored(self, prepared, value=1):
        fixture(Path(prepared['authored']), value)

    def test_patch_only_requested_cell_and_other_parts(self):
        prepared = self.prepared()
        self.authored(prepared)
        result = M.finish(prepared)
        self.assertEqual(M.read_cells(result['output'], 'SPU', ['B2']), [1])
        with zipfile.ZipFile(self.source) as a, zipfile.ZipFile(result['output']) as b:
            for name in a.namelist():
                if name != 'xl/worksheets/sheet1.xml':
                    self.assertEqual(a.read(name), b.read(name))
            self.assertIn(b'<f>A2+1</f>', b.read('xl/worksheets/sheet1.xml'))

    def test_expected_mismatch(self):
        self.config['changes'][0]['expected'] = 8
        with self.assertRaisesRegex(ValueError, 'expected'):
            self.prepared()

    def test_duplicate_patch(self):
        self.config['changes'] *= 2
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.prepared()

    def test_header_shift_unknown_sheet_and_cell(self):
        for change in [{'sheet': 'Other'}, {'cell': 'B0'}, {'cell': 'Z2'}]:
            cfg = copy.deepcopy(self.config)
            cfg['changes'][0].update(change)
            with self.assertRaises(ValueError):
                self.prepared(cfg)
        self.config['headers']['SPU'][1] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'Header'):
            self.prepared()

    def test_merged_data_rejected(self):
        fixture(self.source, merged=True)
        with self.assertRaisesRegex(ValueError, 'merged'):
            self.prepared()

    def test_source_mutation_and_authored_mismatch_rejected(self):
        p = self.prepared()
        self.authored(p, 8)
        with self.assertRaisesRegex(ValueError, 'Artifact'):
            M.finish(p)
        self.authored(p)
        fixture(self.source, 9)
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            M.finish(p)

    def test_existing_output_not_overwritten(self):
        Path(self.config['output']).write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            self.prepared()
        self.assertEqual(Path(self.config['output']).read_bytes(), b'keep')

    def test_batch_fills_missing_seed_cell_and_removes_stale_rows(self):
        cfg = {**self.config, 'operation': 'batch', 'sheets': {'SPU': {'rows': [[7, 1, '新增']], 'seed_rows': [2]}}}
        cfg.pop('changes')
        p = self.prepared(cfg)
        fixture(Path(p['authored']), 1)
        # Simulate Artifact's authored cell payload for the unit-layer fixture.
        with zipfile.ZipFile(p['authored']) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        parts['xl/worksheets/sheet1.xml'] = parts['xl/worksheets/sheet1.xml'].replace(b'<v>1</v></c><c r="B2">', b'<v>7</v></c><c r="B2">').replace(b'</row><row r="3">', '<c r="C2" t="inlineStr"><is><t>新增</t></is></c></row><row r="3">'.encode())
        with zipfile.ZipFile(p['authored'], 'w') as z:
            for n, v in parts.items(): z.writestr(n, v)
        out = M.finish(p)['output']
        self.assertEqual(M.read_cells(out, 'SPU', ['A2', 'B2', 'C2', 'A3']), [7, 1, '新增', None])

    def test_explicit_replace_requires_hash_and_backup(self):
        self.config.update(output=str(self.source), replace_input=True)
        with self.assertRaisesRegex(ValueError, 'source_sha256'):
            self.prepared()
        self.config.update(source_sha256=M.sha256(self.source), backup=str(self.root / 'backup.xlsx'))
        p = self.prepared()
        self.authored(p)
        M.finish(p)
        self.assertEqual(M.read_cells(self.source, 'SPU', ['B2']), [1])
        self.assertEqual(M.read_cells(self.config['backup'], 'SPU', ['B2']), [0])

    def test_locked_replace_only_uses_opted_in_fallback(self):
        self.config.update(output=str(self.source), replace_input=True,
                           source_sha256=M.sha256(self.source), backup=str(self.root / 'backup.xlsx'),
                           fallback_output=str(self.root / 'fallback.xlsx'))
        p = self.prepared()
        self.authored(p)
        with patch.object(M.os, 'replace', side_effect=PermissionError('locked')):
            result = M.finish(p)
        self.assertEqual(Path(result['output']), self.root / 'fallback.xlsx')
        self.assertEqual(M.read_cells(self.source, 'SPU', ['B2']), [0])
        self.assertEqual(M.read_cells(result['output'], 'SPU', ['B2']), [1])

    def test_reject_non_scalar_and_formula_inputs(self):
        for value in [float('nan'), {'bad': 1}, '=SUM(A1:A2)']:
            cfg = copy.deepcopy(self.config)
            cfg['changes'][0]['value'] = value
            with self.assertRaises(ValueError): self.prepared(cfg)

    def test_shared_strings_and_column_conversion(self):
        from lxml import etree as E
        self.assertEqual(M.cell_value(E.fromstring(f'<c xmlns="{M.NS}" t="s"><v>0</v></c>'), ['文字']), '文字')
        self.assertEqual(M.column(43), 'AQ')
        self.assertEqual(M.address('XFD1048576'), (16384, 1048576))
        with self.assertRaises(ValueError): M.address('XFE1')

    def test_detect_rebuilt_style_or_unrelated_content_change(self):
        p = self.prepared()
        parts, names, strings = M.package(p['snapshot'])
        from lxml import etree as E
        root = E.fromstring(parts[names['SPU']])
        edited = M.edit_sheet(p, 'SPU', copy.deepcopy(root), {'B2': 1})
        altered = edited.replace(b'r="B2"', b'r="B2" s="2"')
        with self.assertRaisesRegex(ValueError, 'formatting'):
            M.verify_rebuilt(p, 'SPU', root, altered, {'B2': 1})
        altered = edited.replace(b'<f>A2+1</f>', b'<f>A2+9</f>')
        with self.assertRaisesRegex(ValueError, 'Unrelated'):
            M.verify_rebuilt(p, 'SPU', root, altered, {'B2': 1})


@unittest.skipUnless(all(os.environ.get(k) for k in ['JINGZHI_INTEGRATION_SOURCE', 'JINGZHI_NODE', 'JINGZHI_INTEGRATION_WORKDIR']),
                     'Set integration environment to opt into bundled Artifact test')
class ArtifactIntegrationTests(unittest.TestCase):
    def test_real_template_patch_and_small_batch(self):
        """The caller supplies a read-only source and scratch parent with runtime junction."""
        import openpyxl  # Read only: Artifact Tool authors all test output values.
        source = Path(os.environ['JINGZHI_INTEGRATION_SOURCE']).resolve()
        digest = M.sha256(source)
        wb = openpyxl.load_workbook(source)
        headers = {name: [c.value for c in wb[name][1]] for name in ['SPU', 'SKU']}
        draft = next(c.coordinate for c in wb['SPU'][1] if c.value == '保存草稿箱').replace('1', '2')
        script = Path(__file__).parents[1] / 'scripts/workbook.mjs'
        with tempfile.TemporaryDirectory(dir=os.environ['JINGZHI_INTEGRATION_WORKDIR']) as tmp:
            base = Path(tmp)
            for operation in ['patch', 'batch']:
                work = base / operation
                config = {'source': str(source), 'output': str(base / f'{operation}.xlsx'),
                          'operation': operation, 'headers': headers,
                          'previews': [{'sheet': 'SPU', 'range': 'R1:T4'}]}
                if operation == 'patch':
                    config['changes'] = [{'sheet': 'SPU', 'cell': draft, 'expected': wb['SPU'][draft].value,
                                          'value': 0 if wb['SPU'][draft].value == 1 else 1}]
                else:
                    config['sheets'] = {name: {'rows': [[c.value for c in wb[name][r]] for r in range(2, 4 if name == 'SPU' else 12)],
                                              'seed_rows': [2] if name == 'SPU' else [2, 3, 4, 5, 6]} for name in headers}
                cfg = base / f'{operation}.json'
                cfg.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
                cmd = [os.environ['JINGZHI_NODE'], str(script), str(cfg), '--work-dir', str(work), '--python', sys.executable]
                subprocess.run(cmd, check=True, capture_output=True)
                prepared = json.loads((work / 'prepared.json').read_text('utf-8'))
                result = M.finish(prepared)
                self.assertNotIn('artifact_verified', result)
                subprocess.run(cmd + ['--verify'], check=True, capture_output=True)
                receipt = json.loads((work / 'verification.json').read_text('utf-8'))
                self.assertTrue(receipt.get('artifact_verified'))
                self.assertEqual(receipt.get('artifact_sha256'), M.sha256(result['output']))
                self.assertEqual(result['changed_cells'], 1 if operation == 'patch' else 186)
                self.assertTrue((work / 'final_1.png').is_file())
                with zipfile.ZipFile(source) as a, zipfile.ZipFile(result['output']) as b:
                    changed = set(prepared['sheet_members'].values())
                    for member in a.namelist():
                        if member not in changed: self.assertEqual(a.read(member), b.read(member))
                if operation == 'patch':
                    output = Path(result['output'])
                    output.write_bytes(output.read_bytes() + b'changed-after-verification')
                    failed = subprocess.run(cmd + ['--verify'], capture_output=True, text=True)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn('Output hash changed', failed.stderr)
        self.assertEqual(M.sha256(source), digest)


if __name__ == '__main__':
    unittest.main()
