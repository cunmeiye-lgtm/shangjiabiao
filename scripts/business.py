"""Business rules and read-only price extraction; no workbook authoring here."""
import argparse
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path, PureWindowsPath

SPU_HEADERS = ['店铺Uid*','所属平台','标题*','前缀词','二级关键词','三级关键词','重复词过滤','品牌*','包含型号*','主图位置','详情图位置','图片路径','计划上架时间','链接序号','类目序号','原图路径','PSD路径','镜头路径','保存草稿箱','两级SKU图','是否单规格','规格顺序','SKU来源于表格','上架模式','统一单买价格','统一价格','统一库存数量','统一折扣','上架状态','店铺品牌','运费模板','保修服务','是否假一赔十','发货时效*','预售时间','单次限购','累计限购','商品描述','类目名称','类目id*','视频id','视频url','视频路径']
SKU_HEADERS = ['链接序号','固定编码','图案编码','SKU名称','SKU编码','单买价格','价格','数量','折扣','sku图位置']


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError('Price must be finite and nonnegative')
        return amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError('Missing or invalid price') from exc


def sku_specs(codes, single_code):
    if len(codes) not in (1,2,4,6,8) or len(set(codes)) != len(codes):
        raise ValueError('Only distinct 1/2/4/6/8 styles are supported')
    if single_code not in codes or any(not re.fullmatch(r'[A-Za-z0-9_-]+',c) for c in codes):
        raise ValueError('Specify a real fixed single style and valid product codes')
    n = len(codes)
    first = [{single_code:1}]
    if n == 1:
        return [{codes[0]:q} for q in (1,2,4,6,8)]
    if n == 2:
        return first + [dict.fromkeys(codes,q) for q in (1,2,3,4)]
    pairs = [dict.fromkeys(codes[i:i+2],1) for i in range(0,n,2)]
    if n == 4:
        return first + pairs + [dict.fromkeys(codes,q) for q in (1,2)]
    half = n//2
    return first + pairs + [dict.fromkeys(codes[:half],1),dict.fromkeys(codes[half:],1),dict.fromkeys(codes,1)]


def fixed_code(spec):
    if not spec or len(set(spec.values())) != 1:
        raise ValueError('Mixed quantities need a separately confirmed encoding rule')
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+',c) for c in spec):
        raise ValueError('Invalid product code')
    q = next(iter(spec.values()))
    if type(q) is not int or q < 1:
        raise ValueError('Quantity must be a positive integer')
    code = '+'.join(spec)
    return code if q == 1 else (f'{code}*{q}' if len(spec)==1 else f'({code})*{q}')


def validate_titles(titles, count):
    if len(titles) != count or len(set(titles)) != count:
        raise ValueError('Need one distinct title per link')
    for title in titles:
        if not isinstance(title,str) or title != title.strip():
            raise ValueError('Title must be text without surrounding spaces')
        chinese = sum('\u4e00' <= c <= '\u9fff' for c in title)
        if not 26 <= chinese <= 30 or len(title) > 30:
            raise ValueError(f'Title must contain 26–30 Chinese characters, total <=30: {title}')


def require_batch_choices(settings):
    for name in ('store_uid','price_sheet','save_draft'):
        if name not in settings:
            raise ValueError(f'Confirm for this batch: {name}')
    if not re.fullmatch(r'S\d+',str(settings['store_uid'])):
        raise ValueError('Invalid store UID')
    if not isinstance(settings['price_sheet'],str) or not settings['price_sheet'].strip():
        raise ValueError('Price worksheet is required')
    if type(settings['save_draft']) is not int or settings['save_draft'] not in (0,1):
        raise ValueError('Save draft must be numeric 0 or 1')


def check_unc(value, access=True):
    if not isinstance(value,str) or not value.startswith('\\\\'):
        raise ValueError('Workbook paths must be UNC paths')
    parts = value[2:].split('\\')
    if len(parts)<2 or any(not p or p in ('.','..') or ':' in p or '/' in p for p in parts):
        raise ValueError('Invalid UNC path')
    if not PureWindowsPath(value).is_absolute():
        raise ValueError('UNC path must be absolute')
    if access and not Path(value).is_dir():
        raise ValueError(f'UNC directory inaccessible: {value}')


def price_values(settings, quantities):
    import openpyxl
    tiers = settings['size_tiers']
    if len(set(tiers)) != 1:
        raise ValueError('Mixed size tiers require a confirmed pricing rule')
    with_book = openpyxl.load_workbook(settings['price_file'],data_only=True,read_only=True)
    try:
        sheet = with_book[settings['price_sheet']]
        row = settings['price_row']
        if str(sheet[f'B{row}'].value).strip() != tiers[0]:
            raise ValueError('Price row does not match the confirmed size tier')
        result = {}
        for q in quantities:
            col = settings['price_columns'][str(q)]
            if not re.fullmatch('[A-Z]+',col):
                raise ValueError('Price column must be an Excel column name')
            if sheet[f'{col}{settings["quantity_header_row"]}'].value != q:
                raise ValueError('Price quantity header mismatch')
            result[q] = money(sheet[f'{col}{row}'].value)
        return result
    finally:
        with_book.close()


def make_rows(manifest, settings, baseline):
    import openpyxl
    from allocate import verify_ready
    require_batch_choices(settings)
    verify_ready(manifest)
    links = manifest['links']
    validate_titles(settings['titles'],len(links))
    if not links or [x['id'] for x in links] != list(range(1,len(links)+1)):
        raise ValueError('Links need contiguous unique IDs starting at 1')
    codes = settings['codes']
    specs = sku_specs(codes,settings['single_code'])
    if len(settings['size_tiers']) != len(codes):
        raise ValueError('Need a size tier for every style')
    prices = price_values(settings,{sum(s.values()) for s in specs})
    positions = settings['sku_positions']
    names = settings['sku_names']
    if len(positions)!=len(specs) or len(names)!=len(specs) or len(set(names))!=len(names):
        raise ValueError('One distinct SKU name and image position per specification required')
    if any(not isinstance(n,str) or not n.strip() for n in names):
        raise ValueError('SKU names cannot be blank')
    book = openpyxl.load_workbook(baseline,read_only=True,data_only=False)
    try:
        for name,headers in [('SPU',SPU_HEADERS),('SKU',SKU_HEADERS)]:
            if [c.value for c in book[name][1]] != headers:
                raise ValueError(f'Unknown {name} schema: inspect headers before adapting')
        seed = [c.value for c in book['SPU'][settings.get('baseline_spu_row',2)]]
    finally:
        book.close()
    if any(seed[i] not in (None,'') for i in (3,4,5)):
        raise ValueError('Baseline title prefixes/keywords need review to avoid extra title text')
    if seed[22] != 1 or any(seed[i] not in (None,'') for i in (24,25,26,27)):
        raise ValueError('Baseline must use SKU table (W=1) without uniform Y:AB overrides')
    templates={t['name']:t for t in manifest['templates']}
    spu,sku,paths = [],[],set()
    for link,title in zip(links,settings['titles']):
        pages = settings['pages'][link['template']]
        if pages != templates[link['template']].get('pages'):
            raise ValueError('Page selection must match the bound verified template profile')
        if pages.get('verified') is not True:
            raise ValueError('Use verified template export mapping')
        for kind in ('main','detail','sku'):
            values = pages[kind]
            if not values or len(set(values))!=len(values) or any(type(v)is not int or v<1 for v in values):
                raise ValueError('Page positions must be distinct positive integers')
        if any(type(p)is not int or p not in pages['sku'] for p in positions):
            raise ValueError('SKU image position is absent from this template')
        paths.update(link[k] for k in ('source_folder','output_folder','psd_folder'))
        row = list(seed)
        edits = {0:settings['store_uid'],1:settings['store_uid'],2:title,8:'无',
                 9:','.join(map(str,pages['main'])),10:','.join(map(str,pages['detail'])),
                 11:link['output_folder'],12:None,13:link['id'],15:link['source_folder'],
                 16:link['psd_folder'],18:settings['save_draft'],19:0,23:0,28:0,29:'遥望星河'}
        for col,value in edits.items():row[col]=value
        spu.append(row)
        for spec,name,position in zip(specs,names,positions):
            price = prices[sum(spec.values())]
            sku.append([link['id'],fixed_code(spec),None,name,None,float(price+1),float(price),100,None,position])
    for path in paths:check_unc(path)
    return {'operation':'batch','source':str(baseline),'output':settings['output'],
            'headers':{'SPU':SPU_HEADERS,'SKU':SKU_HEADERS},
            'sheets':{'SPU':{'rows':spu,'seed_rows':[settings.get('baseline_spu_row',2)]},
                      'SKU':{'rows':sku,'seed_rows':settings['sku_seed_rows']}}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest');parser.add_argument('settings');parser.add_argument('baseline')
    parser.add_argument('--output',required=True,help='New workbook-job JSON, not an XLSX')
    args=parser.parse_args()
    result=make_rows(json.loads(Path(args.manifest).read_text('utf-8')),
                     json.loads(Path(args.settings).read_text('utf-8')),args.baseline)
    with Path(args.output).open('x',encoding='utf-8') as out:
        json.dump(result,out,ensure_ascii=False,indent=2)
    print(f"Validated {len(result['sheets']['SPU']['rows'])} SPU rows")


if __name__=='__main__':main()
