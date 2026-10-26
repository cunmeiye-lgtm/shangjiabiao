import sys
import unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from business import sku_specs, fixed_code, money, validate_titles, require_batch_choices, check_unc, price_values, make_rows, SPU_HEADERS, SKU_HEADERS


class BusinessTests(unittest.TestCase):
    def test_supported_tiers(self):
        expected = {1:[1,2,4,6,8],2:[1,2,4,6,8],4:[1,2,2,4,8],
                    6:[1,2,2,2,3,3,6],8:[1,2,2,2,2,4,4,8]}
        for n, tiers in expected.items():
            codes = [f'A{i}' for i in range(n)]
            specs = sku_specs(codes, codes[-1])
            self.assertEqual([sum(x.values()) for x in specs], tiers)
            self.assertEqual(specs[0], {codes[-1]:1})

    def test_codes_are_production_syntax(self):
        self.assertEqual(fixed_code({'A2004':1}), 'A2004')
        self.assertEqual(fixed_code({'A2004':2}), 'A2004*2')
        self.assertEqual(fixed_code({'A2004':1,'A2064':1}), 'A2004+A2064')
        self.assertEqual(fixed_code({'A2004':2,'A2064':2}), '(A2004+A2064)*2')
        with self.assertRaises(ValueError): fixed_code({'A':1,'B':2})

    def test_unknown_combinations_rejected(self):
        for codes, one in [(['A']*2,'A'),(['A','B','C'],'A'),(['A'],'B'),(['A+B'],'A+B')]:
            with self.assertRaises(ValueError): sku_specs(codes,one)

    def test_round_half_up(self):
        self.assertEqual(money('3.865'),Decimal('3.87'))
        for bad in ['NaN','Infinity','-1']:
            with self.assertRaises(ValueError): money(bad)

    def test_titles(self):
        validate_titles(['甲'*27,'乙'*26],2)
        for items,count in [(['甲'*31],1),(['甲'*25],1),(['甲'*27]*2,2),(['甲'*26+'ABCDE'],1),(['甲'*27],2)]:
            with self.assertRaises(ValueError):validate_titles(items,count)

    def test_no_store_price_draft_defaults(self):
        require_batch_choices({'store_uid':'S123','price_sheet':'1.5倍','save_draft':0})
        for missing in ['store_uid','price_sheet','save_draft']:
            config={'store_uid':'S123','price_sheet':'1.5倍','save_draft':1}
            del config[missing]
            with self.assertRaises(ValueError):require_batch_choices(config)

    def test_unc_paths(self):
        check_unc(r'\\server\share\模板',access=False)
        for path in ['O:/套图素材',r'\\server\share\..\别处',r'\\server\\share']:
            with self.assertRaises(ValueError):check_unc(path,access=False)

    def test_price_cells_and_missing_cache(self):
        cells={'B10':'G','M2':1,'N2':2,'M10':3.865,'N10':4.7}
        class Sheet:
            def __getitem__(self,key):return SimpleNamespace(value=cells[key])
        class Book:
            def __getitem__(self,key):return Sheet()
            def close(self):pass
        settings={'size_tiers':['G'],'price_file':'fixture','price_sheet':'1.5倍',
                  'price_row':10,'quantity_header_row':2,'price_columns':{'1':'M','2':'N'}}
        with patch('openpyxl.load_workbook',return_value=Book()):
            self.assertEqual(price_values(settings,[1,2]),{1:Decimal('3.87'),2:Decimal('4.70')})
            for changes in [{'size_tiers':['A','G']},{'price_columns':{'1':'M1'}},{'size_tiers':['A']}]:
                with self.assertRaises(ValueError):price_values({**settings,**changes},[1])
            cells['M2']=2
            with self.assertRaises(ValueError):price_values(settings,[1])
            cells['M2']=1;cells['M10']=None
            with self.assertRaises(ValueError):price_values(settings,[1])

    def test_batch_field_mapping_and_four_style_rows(self):
        class Sheet:
            def __init__(self,headers):self.headers=headers
            def __getitem__(self,row):
                values=self.headers if row==1 else [None]*len(self.headers)
                if row!=1 and len(values)==43:values[22]=1
                return [SimpleNamespace(value=x) for x in values]
        class Book:
            def __getitem__(self,key):return Sheet(SPU_HEADERS if key=='SPU' else SKU_HEADERS)
            def close(self):pass
        codes=['A2004','A2064','A2065','A2066']
        settings={'store_uid':'S123','price_sheet':'1.5倍','save_draft':1,'titles':['甲'*27],
                  'codes':codes,'single_code':'A2004','size_tiers':['A']*4,
                  'sku_positions':[1,2,3,1,1],'sku_names':['随机一张','莲花玫瑰各1张','另两款各1张','四款各1张','四款各2张'],
                  'pages':{'模板1':{'verified':True,'main':[1,2,3,4,5],'detail':[1,2,3,4,5],'sku':[1,2,3]}},
                  'output':'output.xlsx','sku_seed_rows':[2]}
        link={'id':1,'template':'模板1','source_folder':r'\\s\share\素材',
              'output_folder':r'\\s\share\成品','psd_folder':r'\\s\share\PSD'}
        manifest={'links':[link],'templates':[{'name':'模板1','pages':settings['pages']['模板1']}]}
        with patch('allocate.verify_ready',create=True),patch('openpyxl.load_workbook',return_value=Book()),patch('business.check_unc') as unc,patch('business.price_values',return_value={1:Decimal('3.47'),2:Decimal('3.92'),4:Decimal('4.91'),8:Decimal('7.24')}):
            result=make_rows(manifest,settings,'baseline.xlsx')
            rows=result['sheets']['SKU']['rows'];spu=result['sheets']['SPU']['rows'][0]
            self.assertEqual(len(rows),5)
            self.assertEqual([r[1] for r in rows],['A2004','A2004+A2064','A2065+A2066','A2004+A2064+A2065+A2066','(A2004+A2064+A2065+A2066)*2'])
            self.assertEqual([r[9] for r in rows],[1,2,3,1,1])
            self.assertEqual(spu[18],1);self.assertEqual(spu[23],0);self.assertEqual(spu[28],0)
            self.assertEqual(unc.call_count,3)
            self.assertEqual(rows[4][5:8],[8.24,7.24,100])
            many_links=[{**link,'id':i+1} for i in range(17)]
            many_settings={**settings,'titles':['甲'*26+chr(0x4e00+i) for i in range(17)]}
            replay=make_rows({**manifest,'links':many_links},many_settings,'baseline')
            self.assertEqual(len(replay['sheets']['SPU']['rows']),17)
            self.assertEqual(len(replay['sheets']['SKU']['rows']),85)
            for changed in [{'titles':['甲'*31]},{'sku_positions':[1]},{'sku_names':['a']*5},{'sku_positions':[4]*5},{'size_tiers':['A']}]:
                with self.assertRaises(ValueError):make_rows(manifest,{**settings,**changed},'baseline')
            with self.assertRaises(ValueError):make_rows({'links':[{**link,'id':2}]},settings,'baseline')


if __name__ == '__main__':unittest.main()
