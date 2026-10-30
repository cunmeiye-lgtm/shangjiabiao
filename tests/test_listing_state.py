"""Isolated title/state tests. XLSX fixtures are authored as OOXML zip text."""
import hashlib
import io
from contextlib import redirect_stdout,redirect_stderr
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from xml.sax.saxutils import escape

SPEC=importlib.util.spec_from_file_location('listing_state',Path(__file__).parents[1]/'scripts/listing_state.py')
M=importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name]=M
SPEC.loader.exec_module(M)


def xlsx(path,rows,headers=('店铺Uid*','标题*','价格','链接序号')):
    def cells(values,r):
        result=[]
        for i,v in enumerate(values):
            if v is None:continue
            address=f'{chr(65+i)}{r}'
            if type(v) is int:result.append(f'<c r="{address}"><v>{v}</v></c>')
            elif type(v) is bool:result.append(f'<c r="{address}" t="b"><v>{int(v)}</v></c>')
            else:result.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
        return ''.join(result)
    data=''.join(f'<row r="{r}">{cells(values,r)}</row>' for r,values in enumerate([headers,*rows],1))
    parts={
      '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
      '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="SPU" sheetId="1" r:id="r1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
      'xl/worksheets/sheet1.xml':f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:{chr(64+len(headers))}{len(rows)+1}"/><sheetData>{data}</sheetData></worksheet>'}
    with zipfile.ZipFile(path,'w') as z:
        for name,value in parts.items():z.writestr(name,value)


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ListingStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.state=self.root/'ledger'/'state.json';self.uid='S100'

    def workbook(self,name='v1',titles=('甲标题',),uid=None,price='1'):
        p=self.root/f'{name}.xlsx';xlsx(p,[(uid or self.uid,t,price,i) for i,t in enumerate(titles,1)]);return p

    def verification(self,book,name=None,**changes):
        p=self.root/(name or (book.stem+'.verification.json'))
        d={'output':str(book),'sha256':digest(book),'changed_cells':1,'preserved_other_zip_parts':True,
           'operation':'patch','uploaded':False,'artifact_verified':True,'artifact_sha256':digest(book)}
        d.update(changes);p.write_text(json.dumps(d),encoding='utf-8');return p

    def register(self,book,batch='batch-a',parent=None,**kw):
        return M.register_delivery(self.state,book,self.verification(book),self.uid,batch,
                                   parent_sha256=parent,**kw)

    def test_readonly_scan_uid_nfkc_and_lockfile(self):
        p=self.root/'old.xlsx';xlsx(p,[('S100','Ａ款',1),('S100','A款',2),('S200','其他',1)])
        lock=self.root/'~$locked.xlsx';lock.write_bytes(b'not Excel')
        before=p.read_bytes();r=M.scan_titles([p,lock],self.uid)
        self.assertEqual(r['titles'],['A款']);self.assertEqual(len(r['records']),2)
        self.assertEqual(p.read_bytes(),before);self.assertEqual(len(r['skipped_lock_files']),1)

    def test_format_characters_in_history_cannot_hide_title_collisions(self):
        p=self.workbook(titles=('Ａ\u200b款\ufeff',))
        self.assertEqual(M.scan_titles([p],self.uid)['titles'],['A款'])
        with self.assertRaises(ValueError):M.check_titles(['A款'],self.uid,history_paths=[p])
        self.assertEqual(M.normalize_title('Ａ\u2060款'),'A款')

    def test_new_titles_reject_invisible_format_and_control_characters(self):
        for char in ('\u200b','\ufeff','\u200d','\u2060','\u202e','\t'):
            with self.assertRaises(ValueError):M.check_titles(['甲'+char+'标题'],self.uid)
        p=self.workbook(titles=('甲\u200b标题',))
        with self.assertRaises(ValueError):self.register(p)
        self.assertFalse(self.state.exists())

    def test_history_normalization_preserves_visible_punctuation_and_internal_spaces(self):
        for title in ('甲 乙','甲·乙','甲-乙','甲，乙'):
            self.assertNotEqual(M.normalize_title(title),M.normalize_title('甲乙'))
        self.assertEqual(M.normalize_title('Ａ，Ｂ'),'A,B')
        with self.assertRaises(ValueError):M.normalize_title('\ufeff\u200b')

    def test_glob_is_current_directory_only(self):
        self.workbook();(self.root/'sub').mkdir();xlsx(self.root/'sub'/'hidden.xlsx',[('S100','隐藏',1)])
        r=M.scan_titles([],self.uid,directory=self.root,pattern='*.xlsx')
        self.assertEqual(len(r['files']),1)
        with self.assertRaises(ValueError):M.scan_titles([],self.uid,directory=self.root,pattern='**/*.xlsx')

    def test_bad_excel_and_unknown_header_fail(self):
        p=self.root/'bad.xlsx';p.write_bytes(b'bad')
        with self.assertRaises(ValueError):M.scan_titles([p],self.uid)
        xlsx(p,[('S100','标题',1)],headers=('店铺','标题*','价格'))
        with self.assertRaises(ValueError):M.scan_titles([p],self.uid)

    def test_missing_title_or_invalid_uid_fails(self):
        p=self.root/'bad.xlsx'
        for rows in [[('S100',None,1)],[('not-a-uid','标题',1)]]:
            xlsx(p,rows)
            with self.assertRaises(ValueError):M.scan_titles([p],self.uid)

    def test_duplicate_required_headers_fail(self):
        p=self.root/'duplicate.xlsx';xlsx(p,[('S100','标题',1)],headers=('店铺Uid*','标题*','标题*'))
        with self.assertRaises(ValueError):M.scan_titles([p],self.uid)

    def test_check_same_store_conflicts_other_store_does_not(self):
        p=self.workbook(titles=('甲标题',));q=self.workbook('other',('乙标题',),uid='S200')
        with self.assertRaises(ValueError):M.check_titles(['甲标题'],self.uid,history_paths=[p])
        self.assertTrue(M.check_titles(['乙标题'],self.uid,history_paths=[q])['checked'])
        with self.assertRaises(ValueError):M.check_titles(['Ａ','A'],self.uid)

    def test_history_config_unknown_keys_fail(self):
        with self.assertRaises(ValueError):M.validate_title_history(['甲'],self.uid,{'typo':[]})
        self.assertTrue(M.validate_title_history(['甲'],self.uid,{'workbooks':[]})['checked'])

    def test_first_delivery_and_resolve(self):
        p=self.workbook();r=self.register(p)
        self.assertEqual(r['latest']['sha256'],digest(p))
        latest=M.resolve_latest(self.state,self.uid,'batch-a')
        self.assertEqual(Path(latest['path']),p);self.assertEqual(latest['sha256'],digest(p))

    def test_unverified_or_mismatched_report_cannot_register(self):
        p=self.workbook()
        for changes in [{'artifact_verified':False},{'artifact_sha256':'0'*64},{'sha256':'0'*64},
                        {'uploaded':True},{'preserved_other_zip_parts':False},{'changed_cells':True}]:
            with self.assertRaises(ValueError):M.register_delivery(self.state,p,self.verification(p,**changes),self.uid,'batch-a')
        self.assertFalse(self.state.exists())
        v=self.verification(p);d=json.loads(v.read_text());del d['artifact_verified'];v.write_text(json.dumps(d))
        with self.assertRaises(ValueError):M.register_delivery(self.state,p,v,self.uid,'batch-a')

    def test_mixed_or_wrong_output_uid_cannot_register(self):
        p=self.workbook(uid='S200')
        with self.assertRaises(ValueError):self.register(p)
        xlsx(p,[('S100','甲',1),('S200','乙',1)])
        with self.assertRaises(ValueError):self.register(p)

    def test_resolve_fails_closed_on_workbook_drift(self):
        p=self.workbook();self.register(p);p.write_bytes(p.read_bytes()+b'changed')
        with self.assertRaises(ValueError):M.resolve_latest(self.state,self.uid)

    def test_resolve_rejects_stale_request_missing_and_report_drift(self):
        p=self.workbook();self.register(p)
        with self.assertRaises(ValueError):M.resolve_latest(self.state,self.uid,expected_sha256='0'*64)
        v=self.root/'v1.verification.json';v.write_text(v.read_text()+' ')
        with self.assertRaises(ValueError):M.resolve_latest(self.state,self.uid)
        with self.assertRaises(ValueError):M.resolve_latest(self.root/'absent.json',self.uid)

    def test_same_batch_sku_edit_preserves_titles_and_history_parent(self):
        a=self.workbook();self.register(a);parent=digest(a);b=self.workbook('v2',price='2')
        self.assertTrue(M.check_titles(['甲标题'],self.uid,state_path=self.state,history_paths=[a],
                                     batch_id='batch-a',parent_sha256=parent,link_ids=[1])['checked'])
        self.register(b,parent=parent,history_paths=[a])
        self.assertEqual(M.resolve_latest(self.state,self.uid)['sha256'],digest(b))
        with self.assertRaises(ValueError):self.register(self.workbook('v3'),parent=parent)

    def test_cross_batch_ledger_conflict_and_title_change(self):
        a=self.workbook();self.register(a)
        with self.assertRaises(ValueError):self.register(self.workbook('b'),batch='batch-b')
        b=self.workbook('b',('乙标题',));self.register(b,batch='batch-b')
        with self.assertRaises(ValueError):self.register(self.workbook('a2',('乙标题',)),parent=digest(a))
        c=self.workbook('a3',('丙标题',));self.register(c,parent=digest(a))
        self.assertEqual(M.resolve_latest(self.state,self.uid,'batch-a')['sha256'],digest(c))

    def test_retired_same_batch_title_cannot_be_recycled_or_swapped(self):
        a=self.workbook(titles=('甲标题','乙标题'));self.register(a)
        b=self.workbook('v2',('丙标题','乙标题'));self.register(b,parent=digest(a))
        for titles in [('甲标题','乙标题'),('乙标题','丙标题'),('丙标题','甲标题')]:
            with self.assertRaises(ValueError):
                M.check_titles(list(titles),self.uid,state_path=self.state,batch_id='batch-a',parent_sha256=digest(b),link_ids=[1,2])
        self.assertTrue(M.check_titles(['丙标题','乙标题'],self.uid,state_path=self.state,
                                      batch_id='batch-a',parent_sha256=digest(b),history_paths=[a,b],link_ids=[1,2])['checked'])

    def test_current_title_exemption_requires_real_link_ids(self):
        a=self.workbook();self.register(a)
        with self.assertRaises(ValueError):
            M.check_titles(['甲标题'],self.uid,state_path=self.state,batch_id='batch-a',parent_sha256=digest(a))
        b=self.root/'id-changed.xlsx';xlsx(b,[('S100','甲标题','1',2)])
        with self.assertRaises(ValueError):self.register(b,parent=digest(a),history_paths=[a])

    def test_link_ids_are_required_unique_positive_integers(self):
        for values in ([0],[-1],[True],['1'],[1,1]):
            p=self.root/'bad-ids.xlsx';xlsx(p,[('S100',f'标题{i}','1',value) for i,value in enumerate(values)])
            with self.assertRaises(ValueError):self.register(p)
        p=self.root/'missing-id.xlsx';xlsx(p,[('S100','标题',1)],headers=('店铺Uid*','标题*','价格'))
        self.assertEqual(M.scan_titles([p],self.uid)['titles'],['标题'])
        with self.assertRaises(ValueError):self.register(p)

    def test_same_rows_with_swapped_ids_do_not_gain_exemptions(self):
        a=self.workbook(titles=('甲标题','乙标题'));self.register(a)
        b=self.root/'ids-swapped.xlsx';xlsx(b,[('S100','甲标题','1',2),('S100','乙标题','1',1)])
        with self.assertRaises(ValueError):self.register(b,parent=digest(a))

    def test_formula_link_identity_cannot_use_a_cached_integer(self):
        p=self.workbook()
        with zipfile.ZipFile(p) as z:parts={name:z.read(name) for name in z.namelist()}
        parts['xl/worksheets/sheet1.xml']=parts['xl/worksheets/sheet1.xml'].replace(
            b'<c r="D2"><v>1</v></c>',b'<c r="D2"><f>1+0</f><v>1</v></c>')
        with zipfile.ZipFile(p,'w') as z:
            for name,value in parts.items():z.writestr(name,value)
        with self.assertRaises(ValueError):self.register(p)

    def test_resolve_compares_actual_link_ids_not_only_hash_declarations(self):
        p=self.workbook();self.register(p)
        xlsx(p,[('S100','甲标题','1',2)]);verification=self.verification(p)
        state=json.loads(self.state.read_text(encoding='utf-8'))
        entry=state['stores'][self.uid]['batches']['batch-a'][-1]
        entry['sha256']=digest(p);entry['verification']['sha256']=digest(verification)
        state['stores'][self.uid]['latest']=dict(entry)
        self.state.write_text(json.dumps(state),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'link IDs'):M.resolve_latest(self.state,self.uid)

    def test_state_cannot_be_written_inside_skill_tree(self):
        p=self.workbook();forbidden=Path(M.__file__).parents[1]/'unwanted-state.json'
        with self.assertRaises(ValueError):M.register_delivery(forbidden,p,self.verification(p),self.uid,'batch-a')
        self.assertFalse(forbidden.exists())

    def test_unknown_schema_uid_or_tampered_state_fails(self):
        with self.assertRaises(ValueError):M.check_titles(['甲'],'bad')
        self.state.parent.mkdir();self.state.write_text('{"schema_version":99}')
        with self.assertRaises(ValueError):M.resolve_latest(self.state,self.uid)
        self.state.write_text('[]')
        with self.assertRaises(ValueError):M.check_titles(['甲'],self.uid,state_path=self.state)

    def test_exclusive_lock_and_atomic_failure_preserve_old_state(self):
        p=self.workbook();self.register(p);old=self.state.read_bytes();b=self.workbook('v2',('乙',))
        lock=Path(str(self.state)+'.lock');lock.write_text('other process')
        with self.assertRaises(ValueError):self.register(b,parent=digest(p))
        self.assertEqual(self.state.read_bytes(),old);lock.unlink()
        with patch.object(M.os,'replace',side_effect=PermissionError('busy')):
            with self.assertRaises((OSError,ValueError)):self.register(b,parent=digest(p))
        self.assertEqual(self.state.read_bytes(),old);self.assertFalse(lock.exists())

    def test_lock_write_failure_cleans_only_our_lock(self):
        p=self.workbook()
        with patch.object(M.os,'write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.register(p)
        self.assertFalse(Path(str(self.state)+'.lock').exists())
        self.assertFalse(self.state.exists())

    def test_state_compare_before_replace_rejects_uncooperative_drift(self):
        p=self.workbook();self.register(p);old_sha=digest(self.state)
        old_state=json.loads(self.state.read_text(encoding='utf-8'))
        changed=self.state.read_bytes()+b' ';self.state.write_bytes(changed)
        with self.assertRaises(ValueError):M._atomic_state(self.state,old_state,old_sha)
        self.assertEqual(self.state.read_bytes(),changed)

    def test_cli_register_and_check_are_local_only(self):
        p=self.workbook();v=self.verification(p);out=io.StringIO()
        with redirect_stdout(out):
            status=M.main(['register','--state',str(self.state),'--store-uid',self.uid,
                           '--batch-id','batch-a','--workbook',str(p),'--verification',str(v)])
        self.assertEqual(status,0);self.assertTrue(json.loads(out.getvalue())['registered'])
        titles=self.root/'titles.json';titles.write_text('["新标题"]',encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(['check','--store-uid',self.uid,'--titles',str(titles),'--state',str(self.state)]),0)
        titles.write_text('["甲标题"]',encoding='utf-8')
        with redirect_stderr(io.StringIO()):
            self.assertEqual(M.main(['check','--store-uid',self.uid,'--titles',str(titles),'--state',str(self.state)]),1)

    def test_cli_scan_and_resolve(self):
        p=self.workbook();self.register(p);script=Path(M.__file__)
        cmd=[sys.executable,str(script),'scan','--store-uid',self.uid,'--workbook',str(p)]
        r=subprocess.run(cmd,check=True,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(json.loads(r.stdout)['titles'],['甲标题'])
        cmd=[sys.executable,str(script),'resolve','--state',str(self.state),'--store-uid',self.uid]
        r=subprocess.run(cmd,check=True,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(json.loads(r.stdout)['sha256'],digest(p))


if __name__=='__main__':unittest.main()
