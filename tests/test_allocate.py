import copy
import sys
import unittest
import tempfile
import json
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import allocate
import materials


def metadata(count):
    return {f'C:/pool/{i}.png': {'source': f'C:/pool/{i}.png', 'pixel_sha256': str(i), 'sha256': str(i), 'width': 100, 'height': 100, 'format': 'PNG'} for i in range(count)}


def config(names=('one',), count=3, slots=2):
    return {'product_key': 'D06', 'batch_root': 'C:/isolated/batch', 'seed': 42,
            'pools': {'all': [f'C:/pool/{i}.png' for i in range(count)]},
            'templates': [{'name': name, 'psd_dir': 'C:/isolated/psd/' + name,
                           'head_slot': '图案2', 'head_slice': 1,
                           'effect_slots': [{'name': f'图案{i+2}', 'pool': 'all'} for i in range(slots)],
                           'fixed_slots': []} for name in names]}


def bind_profile(cfg, root):
    directory = root / 'psd'
    directory.mkdir()
    documents = []
    for name in ('主图.psd', '详情.psd', 'sku.psd', '白底.psd'):
        path = directory / name
        path.write_bytes(b'Fingerprint sentinel; not parsed or rendered as a PSD')
        stat = path.stat()
        documents.append({'path': str(path), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                          'sha256': materials.file_sha256(path)})
    cfg['templates'][0].update({'psd_dir': str(directory), 'documents': documents,
                               'pages': {'verified': True, 'main': [1], 'detail': [1], 'sku': [1]}})


def copy_fixture(root):
    source = root / 'original.png'
    source.write_bytes(b'opaque bytes: decoder is mocked in this test')
    cfg = config(count=1, slots=1)
    cfg['batch_root'] = str(root / 'batch')
    bind_profile(cfg, root)
    cfg['pools'] = {'all': [str(source)]}
    record = {'source': str(source), 'sha256': materials.file_sha256(source), 'pixel_sha256': 'one'}
    return cfg, {str(source): record}


class AllocationTest(unittest.TestCase):
    def test_capacity_is_unique_heads_not_division(self):
        plan = allocate.build_plan(config(count=5, slots=4), metadata(5))
        self.assertEqual(len(plan['links']), 5)
        heads = []
        for link in plan['links']:
            entries = [x for x in plan['copies'] if x['link_id'] == link['id']]
            self.assertEqual(len({x['pixel_sha256'] for x in entries}), 4)
            heads.append(next(x['pixel_sha256'] for x in entries if x['slot'] == '图案2'))
        self.assertEqual(len(set(heads)), 5)

    def test_matching_avoids_greedy_starvation(self):
        cfg = config(count=3, slots=3)
        cfg['pools'].update({'head': ['C:/pool/0.png'], 'narrow': ['C:/pool/1.png'], 'wide': ['C:/pool/1.png', 'C:/pool/2.png']})
        cfg['templates'][0]['effect_slots'] = [{'name': '图案2', 'pool': 'head'}, {'name': '图案3', 'pool': 'wide'}, {'name': '图案4', 'pool': 'narrow'}]
        plan = allocate.build_plan(cfg, metadata(3))
        self.assertEqual(len(plan['links']), 1)
        self.assertEqual([x['pixel_sha256'] for x in plan['copies']], ['0', '2', '1'])

    def test_round_robin_and_deterministic(self):
        cfg = config(names=('one', 'two'), count=6, slots=2)
        cfg['templates'][0]['count'] = 2
        cfg['templates'][1]['count'] = 3
        first = allocate.build_plan(cfg, metadata(6))
        self.assertEqual(first, allocate.build_plan(cfg, metadata(6)))
        self.assertEqual([x['template'] for x in first['links']], ['one', 'two', 'one', 'two', 'two'])
        altered = copy.deepcopy(first)
        altered['links'][0]['id'] = 999
        with self.assertRaises(ValueError):
            allocate.validate_manifest(altered)

    def test_duplicate_pixels_reduce_capacity(self):
        records = metadata(3)
        records['C:/pool/2.png']['pixel_sha256'] = '1'
        result = allocate.build_plan(config(count=3, slots=2), records)
        self.assertEqual(len(result['links']), 2)

    def test_bad_profiles_and_capacity(self):
        cfg = config(count=2, slots=3)
        with self.assertRaises(ValueError):
            allocate.build_plan(cfg, metadata(2))
        cfg = config()
        cfg['templates'][0]['effect_slots'][1]['name'] = '图案2'
        with self.assertRaises(ValueError):
            allocate.build_plan(cfg, metadata(3))

    def test_d06_replay(self):
        cfg = config(names=('怪仙', '凯文', '男1', '男2', '男3', '男4', '男5'), count=140, slots=4)
        records = metadata(143)
        for template in cfg['templates']:
            fixed = [{'name': '图案1', 'source': 'C:/pool/140.png'}]
            if template['name'] in ('怪仙', '凯文', '男1', '男3'):
                fixed += [{'name': '图案6', 'source': 'C:/pool/141.png'}, {'name': '图案7', 'source': 'C:/pool/142.png'}]
            else:
                template['effect_slots'].append({'name': '图案6', 'pool': 'all'})
            template['fixed_slots'] = fixed
            if template['name'] == '男5':
                template['head_slice'] = 2
        plan = allocate.build_plan(cfg, records)
        self.assertEqual(len(plan['links']), 980)
        self.assertEqual(len(plan['copies']), 6440)

    def test_four_style_replay(self):
        cfg = config(count=102, slots=6)
        cfg['pools'] = {'A2004': [f'C:/pool/{i}.png' for i in range(34)],
                        'A2064': [f'C:/pool/{i}.png' for i in range(34, 60)],
                        'A2065': [f'C:/pool/{i}.png' for i in range(60, 77)],
                        'A2066': [f'C:/pool/{i}.png' for i in range(77, 102)]}
        kinds = ['A2004', 'A2064', 'A2065', 'A2066', 'A2004', 'A2064']
        cfg['templates'][0]['effect_slots'] = [{'name': f'图案{i+2}', 'pool': kind} for i, kind in enumerate(kinds)]
        cfg['templates'][0]['count'] = 17
        cfg['templates'][0]['fixed_slots'] = [{'name': f'产品{i}', 'source': f'C:/pool/{102+i}.png'} for i in range(4)]
        plan = allocate.build_plan(cfg, metadata(106))
        self.assertEqual(len(plan['links']), 17)
        self.assertEqual(len(plan['copies']), 170)
        for link in plan['links']:
            effects = [row for row in plan['copies'] if row['link_id'] == link['id'] and row['role'] == 'effect']
            for row, kind in zip(effects, kinds):
                self.assertIn(row['source'], cfg['pools'][kind])

    def test_fixed_exempt_and_cross_template_arrangement(self):
        cfg = config(names=('one', 'two'), count=2, slots=2)
        cfg['templates'][0]['count'] = 1
        cfg['templates'][1]['count'] = 1
        # Find a seed with distinct heads: exactly two permutations are possible.
        for seed in range(20):
            cfg['seed'] = seed
            try:
                plan = allocate.build_plan(cfg, metadata(2))
                break
            except ValueError:
                continue
        else:
            self.fail('No feasible cross-template ordering found')
        arranged = [[x['pixel_sha256'] for x in plan['copies'] if x['link_id'] == i] for i in (1, 2)]
        self.assertEqual(set(arranged[0]), set(arranged[1]))
        self.assertNotEqual(arranged[0], arranged[1])
        cfg = config(count=2, slots=2)
        cfg['templates'][0]['fixed_slots'] = [{'name': '显色后', 'source': 'C:/pool/0.png'}]
        plan = allocate.build_plan(cfg, metadata(2))
        allocate.validate_manifest(plan)
        self.assertEqual(len(plan['copies']), 6)

    def test_impossible_cross_template_and_tampered_paths(self):
        with self.assertRaisesRegex(ValueError, '256 attempts'):
            allocate.build_plan(config(names=('one', 'two'), count=1, slots=1), metadata(1))
        plan = allocate.build_plan(config(count=2, slots=1), metadata(2))
        plan['links'][0]['source_folder'] = 'C:/elsewhere'
        plan['digest'] = allocate.manifest_digest(plan)
        with self.assertRaises(ValueError):
            allocate.validate_manifest(plan)

    def test_execute_end_to_end_and_resume(self):
        with tempfile.TemporaryDirectory() as folder, patch('allocate.image_metadata', return_value={'pixel_sha256': 'one'}):
            root = Path(folder)
            cfg, records = copy_fixture(root)
            plan = allocate.build_plan(cfg, records)
            self.assertFalse((root / 'batch').exists())
            first = allocate.execute_copy(plan)
            self.assertEqual(first['copied'], 1)
            second = allocate.execute_copy(plan)
            self.assertEqual(second['resumed'], 1)
            self.assertTrue((root / 'batch' / '素材分配清单.csv').is_file())
            self.assertTrue(Path(plan['links'][0]['output_folder']).is_dir())
            self.assertTrue(allocate.verify_ready(plan)['verified'])
            with self.assertRaises(ValueError):
                allocate.execute_copy(plan, workers=32)
            cfg['seed'] = 200
            other = allocate.build_plan(cfg, records)
            with self.assertRaises(FileExistsError):
                allocate.execute_copy(other)
            (root / 'psd').rename(root / 'moved_psd')
            with self.assertRaises(FileNotFoundError):
                allocate.execute_copy(plan)

    def test_missing_stale_and_unverified_profile_blocks_before_writes(self):
        with tempfile.TemporaryDirectory() as folder, patch('allocate.image_metadata', return_value={'pixel_sha256': 'one'}):
            root = Path(folder)
            cfg, records = copy_fixture(root)
            missing = copy.deepcopy(cfg)
            del missing['templates'][0]['documents']
            with self.assertRaisesRegex(ValueError, 'four inspected'):
                allocate.execute_copy(allocate.build_plan(missing, records))
            unverified = copy.deepcopy(cfg)
            unverified['templates'][0]['pages']['verified'] = False
            with self.assertRaisesRegex(ValueError, 'verified page'):
                allocate.execute_copy(allocate.build_plan(unverified, records))
            plan = allocate.build_plan(cfg, records)
            Path(cfg['templates'][0]['documents'][0]['path']).write_bytes(b'changed document')
            with self.assertRaisesRegex(ValueError, 'reinspect'):
                allocate.execute_copy(plan)
            self.assertFalse((root / 'batch').exists())

    def test_extras_and_unowned_batch_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch('allocate.image_metadata', return_value={'pixel_sha256': 'one'}):
            root = Path(folder)
            cfg, records = copy_fixture(root)
            plan = allocate.build_plan(cfg, records)
            batch = root / 'batch'
            batch.mkdir()
            occupied = batch / 'user.txt'
            occupied.write_text('user data', encoding='utf-8')
            with self.assertRaisesRegex(FileExistsError, 'ownership'):
                allocate.execute_copy(plan)
            self.assertEqual(occupied.read_text(encoding='utf-8'), 'user data')
            # A separate empty batch can be owned and resumed; extra replacement files block it.
            cfg['batch_root'] = str(root / 'owned')
            plan = allocate.build_plan(cfg, records)
            allocate.execute_copy(plan)
            extra = Path(plan['links'][0]['source_folder']) / '图案2.jpg'
            extra.write_bytes(b'conflicting extension')
            with self.assertRaisesRegex(FileExistsError, 'ambiguous'):
                allocate.execute_copy(plan)
            with self.assertRaisesRegex(FileExistsError, 'ambiguous'):
                allocate.verify_ready(plan)
            self.assertTrue(extra.is_file())

    def test_stale_receipt_and_changed_target_block_ready(self):
        with tempfile.TemporaryDirectory() as folder, patch('allocate.image_metadata', return_value={'pixel_sha256': 'one'}):
            root = Path(folder)
            cfg, records = copy_fixture(root)
            plan = allocate.build_plan(cfg, records)
            allocate.execute_copy(plan)
            receipt_file = root / 'batch' / '素材复制核验.json'
            receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
            receipt_file.write_text(json.dumps({**receipt, 'manifest_digest': 'old'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'receipt'):
                allocate.verify_ready(plan)
            with self.assertRaisesRegex(ValueError, 'receipt'):
                allocate.execute_copy(plan)
            receipt_file.write_text(json.dumps(receipt), encoding='utf-8')
            Path(plan['copies'][0]['destination']).write_bytes(b'changed copied material')
            with self.assertRaisesRegex(ValueError, 'changed after receipt'):
                allocate.verify_ready(plan)

    def test_cli_read_only_and_explicit_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = config(count=1, slots=1)
            cfg['batch_root'] = str(root / 'batch')
            config_file = root / 'config.json'
            config_file.write_text(json.dumps(cfg), encoding='utf-8')
            records = metadata(1)
            records['C:/pool/0.png']['source'] = str(Path('C:/pool/0.png'))
            with patch('allocate.inventory_paths', return_value={'images': list(records.values())}):
                with patch.object(sys, 'argv', ['allocate.py', 'plan', str(config_file)]), redirect_stdout(StringIO()) as stdout:
                    allocate.main()
                self.assertEqual(len(json.loads(stdout.getvalue())['links']), 1)
                self.assertFalse((root / 'batch').exists())
                target = root / 'manifest.json'
                with patch.object(sys, 'argv', ['allocate.py', 'inventory', str(config_file), '--output', str(target)]):
                    allocate.main()
                self.assertTrue(target.is_file())


if __name__ == '__main__':
    unittest.main()
