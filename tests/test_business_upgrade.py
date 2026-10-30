"""Business handoffs for history-aware batches; no raster or workbook authoring."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import business


class Sheet:
    def __init__(self, headers):
        self.headers = headers

    def __getitem__(self, row):
        if row == 1:
            return [SimpleNamespace(value=value) for value in self.headers]
        values = [None] * len(self.headers)
        if len(values) == len(business.SPU_HEADERS):
            values[22] = 1
        return [SimpleNamespace(value=value) for value in values]


class Book:
    def __getitem__(self, name):
        return Sheet(business.SPU_HEADERS if name == 'SPU' else business.SKU_HEADERS)

    def close(self):
        pass


class BusinessUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.pages = {'verified': True, 'main': [1, 2, 3], 'detail': [1, 2], 'sku': [1]}
        self.names = ['【试贴体验】1张', '【日常换贴】2张', '【热卖推荐】4张',
                      '【超值囤货】6张', '【囤货优选】8张']
        self.settings = {
            'store_uid': 'S123', 'price_sheet': '1.5倍', 'save_draft': 0,
            'titles': ['甲' * 27], 'codes': ['TEST'], 'single_code': 'TEST',
            'size_tiers': ['D'], 'sku_names': self.names, 'sku_positions': [1] * 5,
            'pages': {'sample': self.pages}, 'output': 'new.xlsx', 'sku_seed_rows': [2],
        }
        link = {'id': 1, 'template': 'sample', 'source_folder': r'\\server\share\raw',
                'output_folder': r'\\server\share\output', 'psd_folder': r'\\server\share\psd'}
        self.manifest = {'links': [link], 'templates': [{'name': 'sample', 'pages': self.pages}]}
        for target, options in [
            ('allocate.verify_ready', {}),
            ('openpyxl.load_workbook', {'return_value': Book()}),
            ('business.check_unc', {}),
            ('business.price_values', {'return_value': {q: Decimal(str(q)) for q in (1, 2, 4, 6, 8)}}),
        ]:
            self.enterContext(patch(target, **options))

    def make(self, **changes):
        return business.make_rows(self.manifest, {**self.settings, **changes}, 'baseline.xlsx')

    def test_page_positions_use_chinese_comma(self):
        spu = self.make()['sheets']['SPU']['rows'][0]
        self.assertEqual(spu[9:11], ['1，2，3', '1，2'])

    def test_invisible_formatting_cannot_make_a_new_title(self):
        for hidden in ['\u200b', '\ufeff', '\t']:
            with self.subTest(hidden=repr(hidden)), self.assertRaises(ValueError):
                business.validate_titles(['甲' * 13 + hidden + '乙' * 13], 1)

    def test_marketing_labels_preserve_fulfillment_and_prices(self):
        rows = self.make()['sheets']['SKU']['rows']
        self.assertEqual([row[3] for row in rows], self.names)
        self.assertEqual([row[1] for row in rows], ['TEST', 'TEST*2', 'TEST*4', 'TEST*6', 'TEST*8'])
        self.assertEqual([row[5:8] for row in rows], [[q + 1, q, 100] for q in (1, 2, 4, 6, 8)])
        self.assertTrue(all(row[2] is None and row[4] is None and row[9] == 1 for row in rows))

    def test_history_gate_receives_explicit_store_and_parent(self):
        checker = Mock()
        history = {'state_path': 'state.json', 'workbooks': ['older.xlsx'],
                   'batch_id': 'batch-one', 'parent_sha256': 'a' * 64}
        with patch.dict(sys.modules, {'listing_state': SimpleNamespace(check_titles=checker)}):
            self.make(title_history=history)
        checker.assert_called_once_with(
            self.settings['titles'], 'S123', state_path='state.json',
            history_paths=['older.xlsx'], batch_id='batch-one', parent_sha256='a' * 64,
            link_ids=[1])

    def test_boolean_link_id_is_not_a_real_association(self):
        altered = {**self.manifest, 'links': [{**self.manifest['links'][0], 'id': True}]}
        with self.assertRaises(ValueError):
            business.make_rows(altered, self.settings, 'baseline.xlsx')

    def test_history_conflict_blocks_workbook_job(self):
        checker = Mock(side_effect=ValueError('Historical title collision'))
        with patch.dict(sys.modules, {'listing_state': SimpleNamespace(check_titles=checker)}):
            with self.assertRaisesRegex(ValueError, 'Historical title collision'):
                self.make(title_history={'workbooks': ['older.xlsx']})

    def test_unknown_history_options_are_not_silently_ignored(self):
        for history in [True, [], {}, {'workbook': ['typo.xlsx']}, {'workbooks': 'older.xlsx'}]:
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.make(title_history=history)


if __name__ == '__main__':
    unittest.main()
