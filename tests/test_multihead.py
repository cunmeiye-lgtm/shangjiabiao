"""Metadata and opaque byte sentinels only; no raster generation or production I/O."""
import copy
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import allocate


def fixture(n=23, heads=(1, 2, 3, 1), slots=(3, 4, 5, 3)):
    paths=[f'C:/metadata/effect{i:03d}.png' for i in range(n)]
    records={path:{'source':path,'sha256':f'{i:064x}',
                   'pixel_sha256':f'{i+1000:064x}'} for i,path in enumerate(paths)}
    fixed='C:/metadata/product.png'
    records[fixed]={**records[paths[0]],'source':fixed}
    config={'seed':20261030,'product_key':'CS-D','batch_root':'C:/metadata/batch',
            'pools':{'common':paths},'templates':[]}
    for i,(k,count) in enumerate(zip(heads,slots)):
        names=[f'图案{j+1}' for j in range(count)]
        config['templates'].append({'name':f'模板{i+1}','psd_dir':f'C:/metadata/psd/{i+1}',
            'effect_slots':[{'name':name,'pool':'common'} for name in names],
            'fixed_slots':[{'name':'产品','source':fixed}],
            'head_slot':names[0],'head_slots':names[:k],'head_slice':1})
    return config,records


def signed(manifest):
    return {**manifest,'digest':allocate.manifest_digest(manifest)}


def effects_for(manifest,link):
    return [r for r in manifest['copies'] if r['link_id']==link['id'] and r['role']=='effect']


def test_n23_real_slot_counts_capacity_cycle_and_schema():
    config,records=fixture();manifest=allocate.build_plan(config,records)
    assert manifest['capacities']==dict(zip(['模板1','模板2','模板3','模板4'],[23,11,7,23]))
    assert len(manifest['links'])==64
    assert sum(r['role']=='effect' for r in manifest['copies'])==217
    assert [l['template'] for l in manifest['links'][:8]]==['模板1','模板2','模板3','模板4']*2
    assert [l['template'] for l in manifest['links'][-4:]]==['模板1','模板4']*2
    assert all(len(link)==8 for link in manifest['links'])
    allocate.validate_manifest(manifest)


def test_all_head_positions_share_one_unique_set():
    config,records=fixture();manifest=allocate.build_plan(config,records)
    for template in manifest['templates']:
        ids={l['id'] for l in manifest['links'] if l['template']==template['name']}
        pixels=[r['pixel_sha256'] for r in manifest['copies'] if r['link_id'] in ids and r['slot'] in template['head_slots']]
        assert len(set(pixels))==len(pixels)


def secondary_head_corruption():
    config,records=fixture();manifest=allocate.build_plan(config,records)
    links=[l for l in manifest['links'] if l['template']=='模板2']
    first=effects_for(manifest,links[0])[0]
    link=next(l for l in links[1:] if first['pixel_sha256'] not in {r['pixel_sha256'] for r in effects_for(manifest,l)})
    target=effects_for(manifest,link)[1]
    target.update({k:first[k] for k in ('source','sha256','pixel_sha256')})
    return signed(manifest)


def test_secondary_head_reuse_is_rejected_by_builtin_validator():
    with pytest.raises(ValueError,match='head'):allocate.validate_manifest(secondary_head_corruption())


@pytest.mark.parametrize('gate',[allocate.execute_copy,allocate.verify_ready])
def test_native_copy_and_ready_gates_reject_before_profile_or_io(gate):
    with patch.object(allocate,'verify_profiles',side_effect=AssertionError('Should not reach profile/IO')):
        with pytest.raises(ValueError,match='head'):gate(secondary_head_corruption())


def test_link_duplicate_rejected_fixed_product_exempt():
    config,records=fixture();manifest=allocate.build_plan(config,records)
    assert len({r['pixel_sha256'] for r in manifest['copies'] if r['role']=='fixed'})==1
    rows=effects_for(manifest,manifest['links'][0])
    rows[-1].update({k:rows[0][k] for k in ('source','sha256','pixel_sha256')})
    with pytest.raises(ValueError,match='within link'):allocate.validate_manifest(signed(manifest))


@pytest.mark.parametrize('kind',['too_few','different_pool','missing_heads','duplicate_heads','outside_heads','wrong_first_head','string_heads'])
def test_infeasible_or_unsupported_configuration_fails(kind):
    config,records=fixture(n=3,heads=(2,),slots=(3,));template=config['templates'][0]
    if kind=='too_few':template['effect_slots'].append({'name':'图案4','pool':'common'})
    elif kind=='different_pool':
        config['pools']['second']=config['pools']['common'];template['effect_slots'][-1]['pool']='second'
    elif kind=='missing_heads':template['head_slots']=[]
    elif kind=='duplicate_heads':template['head_slots']=['图案1','图案1']
    elif kind=='outside_heads':template['head_slots']=['产品']
    elif kind=='wrong_first_head':template['head_slots']=['图案2','图案1']
    elif kind=='string_heads':template['head_slots']='图案1'
    with pytest.raises(ValueError):allocate.build_plan(config,records)


def test_seed_reproducible_and_inputs_immutable():
    config,records=fixture();original=copy.deepcopy((config,records))
    a=allocate.build_plan(config,records);b=allocate.build_plan(config,records)
    assert a==b and (config,records)==original
    config['seed']+=1
    assert allocate.build_plan(config,records)['digest']!=a['digest']


def test_pixel_duplicate_pool_deduplicates_capacity():
    config,records=fixture(heads=(2,),slots=(4,));duplicate='C:/metadata/duplicate.png'
    records[duplicate]={**records[config['pools']['common'][0]],'source':duplicate}
    config['pools']['common'].append(duplicate)
    manifest=allocate.build_plan(config,records)
    assert manifest['capacities']=={'模板1':11}
    allocate.validate_manifest(manifest)


def test_round_robin_and_capacity_corruption_rejected():
    config,records=fixture();manifest=allocate.build_plan(config,records)
    corrupt=copy.deepcopy(manifest);corrupt['capacities']['模板2']+=1
    with pytest.raises(ValueError,match='capacity'):allocate.validate_manifest(signed(corrupt))
    corrupt=copy.deepcopy(manifest)
    corrupt['templates'][0],corrupt['templates'][1]=corrupt['templates'][1],corrupt['templates'][0]
    with pytest.raises(ValueError,match='round'):allocate.validate_manifest(signed(corrupt))


def test_same_set_same_order_across_templates_rejected():
    config,records=fixture(n=5,heads=(1,1),slots=(3,3));manifest=allocate.build_plan(config,records)
    source=effects_for(manifest,manifest['links'][0])
    right=next(l for l in manifest['links'] if l['template']=='模板2' and effects_for(manifest,l)[0]['pixel_sha256']==source[0]['pixel_sha256'])
    for old,new in zip(effects_for(manifest,right),source):
        old.update({k:new[k] for k in ('source','sha256','pixel_sha256')})
    with pytest.raises(ValueError,match='cross-template'):allocate.validate_manifest(signed(manifest))


def test_head_swap_resolves_same_set_without_capacity_loss():
    config,records=fixture(n=2,heads=(2,2),slots=(2,2));manifest=allocate.build_plan(config,records)
    assert len(manifest['links'])==2
    left,right=[effects_for(manifest,link) for link in manifest['links']]
    assert [r['pixel_sha256'] for r in left]==[r['pixel_sha256'] for r in reversed(right)]
    allocate.validate_manifest(manifest)


def test_bounded_failure_never_claims_global_infeasibility():
    config,records=fixture(n=2,heads=(2,2,2),slots=(2,2,2))
    with pytest.raises(ValueError,match='256 attempts'):allocate.build_plan(config,records)


def test_explicit_count_and_noncontiguous_heads():
    config,records=fixture(heads=(2,),slots=(4,));template=config['templates'][0]
    template.update({'head_slot':'图案2','head_slots':['图案2','图案4'],'count':3})
    manifest=allocate.build_plan(config,records)
    assert manifest['capacities']=={'模板1':11} and len(manifest['links'])==3
    allocate.validate_manifest(manifest)


def test_requested_count_above_capacity_fails():
    config,records=fixture(heads=(3,),slots=(5,));config['templates'][0]['count']=8
    with pytest.raises(ValueError,match='capacity'):allocate.build_plan(config,records)


def test_multihead_metadata_required_and_consistent():
    config,records=fixture(heads=(2,),slots=(4,));manifest=allocate.build_plan(config,records)
    missing=copy.deepcopy(manifest);missing.pop('multihead_pools',None)
    with pytest.raises(ValueError,match='pool metadata'):allocate.validate_manifest(signed(missing))
    outside=copy.deepcopy(manifest);outside['copies'][0]['pixel_sha256']='outside'
    with pytest.raises(ValueError,match='pool|pixel'):allocate.validate_manifest(signed(outside))


def test_legacy_single_head_and_multiple_pools_remain_supported():
    config,records=fixture(n=3,heads=(1,),slots=(3,));template=config['templates'][0]
    del template['head_slots']
    paths=config['pools']['common']
    config['pools']={'head':[paths[0]],'wide':paths[1:],'narrow':[paths[1]]}
    for slot,name in zip(template['effect_slots'],['head','wide','narrow']):slot['pool']=name
    manifest=allocate.build_plan(config,records)
    assert len(manifest['links'])==1 and 'multihead_pools' not in manifest
    assert [r['source'] for r in effects_for(manifest,manifest['links'][0])]==[paths[0],paths[2],paths[1]]
    allocate.validate_manifest(manifest)


@pytest.mark.parametrize('change',['wrong_pool','empty_pixels','duplicate_pixels','wrong_type','extra_template','wrong_metadata_type'])
def test_multihead_pool_metadata_tampering_fails(change):
    config,records=fixture(heads=(2,),slots=(4,));manifest=allocate.build_plan(config,records)
    metadata=manifest['multihead_pools'];entry=metadata['模板1']
    if change=='wrong_pool':entry['pool']='unknown'
    elif change=='empty_pixels':entry['pixels']=[]
    elif change=='duplicate_pixels':entry['pixels'].append(entry['pixels'][0])
    elif change=='wrong_type':metadata['模板1']='not a record'
    elif change=='extra_template':metadata['extra']=copy.deepcopy(entry)
    elif change=='wrong_metadata_type':manifest['multihead_pools']=[]
    with pytest.raises(ValueError,match='pool metadata'):allocate.validate_manifest(signed(manifest))


def test_mixed_single_multi_pool_and_multihead_profiles():
    config,records=fixture(n=8,heads=(1,2),slots=(3,4))
    paths=config['pools']['common'];config['pools']['narrow']=paths[:2]
    config['templates'][0]['effect_slots'][1]['pool']='narrow'
    manifest=allocate.build_plan(config,records)
    assert manifest['capacities']=={'模板1':8,'模板2':4}
    assert set(manifest['multihead_pools'])=={'模板2'}
    allocate.validate_manifest(manifest)


def test_multihead_byte_sentinel_copy_resume_and_ready(tmp_path):
    """Exercise native gates without creating any decodable image/PSD content."""
    config,_=fixture(n=4,heads=(2,),slots=(3,));records={};paths=[]
    for index in range(4):
        path=tmp_path/f'opaque{index}.png'
        path.write_bytes(f'Opaque sentinel {index}; not raster data'.encode())
        source=str(path);paths.append(source)
        records[source]={'source':source,'sha256':allocate.file_sha256(path),
                         'pixel_sha256':f'metadata-only-pixel-{index}'}
    config['pools']={'common':paths};config['batch_root']=str(tmp_path/'batch')
    template=config['templates'][0];directory=tmp_path/'psd';directory.mkdir()
    documents=[]
    for name in ('主图.psd','详情.psd','sku.psd','白底.psd'):
        path=directory/name;path.write_bytes(b'Opaque document fingerprint sentinel')
        stat=path.stat()
        documents.append({'path':str(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                          'sha256':allocate.file_sha256(path)})
    template.update({'psd_dir':str(directory),'documents':documents,
                     'pages':{'verified':True,'main':[1],'detail':[1],'sku':[1]},
                     'fixed_slots':[{'name':'产品','source':paths[0]}]})
    manifest=allocate.build_plan(config,records)
    assert not (tmp_path/'batch').exists()
    actual_pixels={source:record['pixel_sha256'] for source,record in records.items()}
    with patch('allocate.image_metadata', side_effect=lambda source:{'pixel_sha256':actual_pixels[str(source)]}):
        assert allocate.execute_copy(manifest)['copied']==8
        assert allocate.execute_copy(manifest)['resumed']==8
        assert allocate.verify_ready(manifest)['verified']
    for row in manifest['copies']:
        assert Path(row['destination']).read_bytes()==Path(row['source']).read_bytes()


def test_same_file_sha_cannot_claim_different_pixels():
    config,records=fixture(n=4,heads=(2,),slots=(3,));manifest=allocate.build_plan(config,records)
    first=manifest['copies'][0]
    for row in manifest['copies']:
        row['source']=first['source'];row['sha256']=first['sha256']
    with pytest.raises(ValueError,match='pixel|binding'):allocate.validate_manifest(signed(manifest))


@pytest.mark.parametrize('field',['id','link_id','combination','head_slice'])
def test_boolean_identifiers_are_rejected(field):
    config,records=fixture(n=4,heads=(2,),slots=(3,));config['templates'][0]['count']=1
    manifest=allocate.build_plan(config,records)
    if field=='link_id':
        for row in manifest['copies']:row['link_id']=True
    else:manifest['links'][0][field]=True
    with pytest.raises(ValueError,match='integer|IDs'):allocate.validate_manifest(signed(manifest))


def test_generator_checks_named_slot_mapping_as_well_as_display_order():
    config,records=fixture(n=3,heads=(1,1),slots=(3,3))
    template=config['templates'][1]
    template['effect_slots'][1:]=reversed(template['effect_slots'][1:])
    try:
        manifest=allocate.build_plan(config,records)
    except ValueError as exc:
        assert '256 attempts' in str(exc)
        return
    mappings={}
    for link in manifest['links']:
        signature=tuple(sorted((r['slot'],r['pixel_sha256']) for r in effects_for(manifest,link)))
        assert signature not in mappings or mappings[signature]==link['template']
        mappings[signature]=link['template']


def test_validator_rejects_named_mapping_even_when_display_order_differs():
    config,records=fixture(n=3,heads=(1,1),slots=(3,3));manifest=allocate.build_plan(config,records)
    source=effects_for(manifest,manifest['links'][0])
    target=next(l for l in manifest['links'] if l['template']=='模板2' and effects_for(manifest,l)[0]['pixel_sha256']==source[0]['pixel_sha256'])
    for old,new in zip(effects_for(manifest,target),source):
        old.update({k:new[k] for k in ('source','sha256','pixel_sha256')})
    template=manifest['templates'][1]
    template['effect_slots'][1:]=reversed(template['effect_slots'][1:])
    with pytest.raises(ValueError,match='cross-template'):allocate.validate_manifest(signed(manifest))


def test_multihead_manifest_binds_all_pool_records_including_unused():
    config,records=fixture(n=5,heads=(2,),slots=(3,));config['templates'][0]['count']=1
    manifest=allocate.build_plan(config,records)
    bound=manifest['multihead_pools']['模板1'].get('records',[])
    assert len(bound)==5
    assert {r['source'] for r in bound}==set(config['pools']['common'])


def gate_fixture(tmp_path):
    """Five unique opaque files, only three random slots in one requested link."""
    config,_=fixture(n=5,heads=(2,),slots=(3,));records={};paths=[]
    for index in range(5):
        path=tmp_path/f'input{index}.png';path.write_bytes(f'opaque source {index}'.encode())
        source=str(path);paths.append(source)
        records[source]={'source':source,'sha256':allocate.file_sha256(path),
                         'pixel_sha256':f'actual-metadata-{index}'}
    config.update({'pools':{'common':paths},'batch_root':str(tmp_path/'batch')})
    template=config['templates'][0];directory=tmp_path/'psd';directory.mkdir();documents=[]
    for name in ('主图.psd','详情.psd','sku.psd','白底.psd'):
        path=directory/name;path.write_bytes(b'opaque profile fingerprint')
        stat=path.stat()
        documents.append({'path':str(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                          'sha256':allocate.file_sha256(path)})
    template.update({'count':1,'psd_dir':str(directory),'documents':documents,
                     'pages':{'verified':True,'main':[1],'detail':[1],'sku':[1]},
                     'fixed_slots':[{'name':'产品','source':paths[0]}]})
    return allocate.build_plan(config,records),{source:r['pixel_sha256'] for source,r in records.items()}


@pytest.mark.parametrize('gate_name',['execute_copy','verify_ready'])
def test_statically_consistent_fake_pixel_claim_blocked_by_actual_decoder(tmp_path,gate_name):
    manifest,pixels=gate_fixture(tmp_path)
    with patch('allocate.image_metadata',side_effect=lambda source:{'pixel_sha256':pixels[str(source)]}):
        if gate_name=='verify_ready':allocate.execute_copy(manifest)
        entry=manifest['multihead_pools']['模板1'];record=entry['records'][0]
        old=record['pixel_sha256'];record['pixel_sha256']='forged-decoded-pixel'
        entry['pixels']=[record['pixel_sha256'] if value==old else value for value in entry['pixels']]
        for row in manifest['copies']:
            if row['source']==record['source']:row['pixel_sha256']=record['pixel_sha256']
        manifest=signed(manifest)
        allocate.validate_manifest(manifest)  # Static declarations agree; real decoding must reject.
        with pytest.raises(ValueError,match='pixel metadata mismatch'):
            getattr(allocate,gate_name)(manifest)
    if gate_name=='execute_copy':assert not Path(manifest['batch_root']).exists()


@pytest.mark.parametrize('gate_name',['execute_copy','verify_ready'])
def test_unassigned_pool_member_is_checked_before_copy_or_ready(tmp_path,gate_name):
    manifest,pixels=gate_fixture(tmp_path)
    assigned={row['source'] for row in manifest['copies']}
    unused=next(row for row in manifest['multihead_pools']['模板1']['records'] if row['source'] not in assigned)
    with patch('allocate.image_metadata',side_effect=lambda source:{'pixel_sha256':pixels[str(source)]}):
        if gate_name=='verify_ready':allocate.execute_copy(manifest)
        Path(unused['source']).write_bytes(b'changed unassigned capacity member')
        with pytest.raises(ValueError,match='Source hash changed'):
            getattr(allocate,gate_name)(manifest)
    if gate_name=='execute_copy':assert not Path(manifest['batch_root']).exists()


def test_metadata_gate_decodes_each_unique_source_sha_and_includes_unused(tmp_path):
    manifest,pixels=gate_fixture(tmp_path)
    with patch('allocate.image_metadata',side_effect=lambda source:{'pixel_sha256':pixels[str(source)]}) as decoder:
        result=allocate.verify_material_metadata(manifest)
    assert result=={'sources':5,'decoded_groups':5,'verified':True}
    assert decoder.call_count==5
    assert {str(call.args[0]) for call in decoder.call_args_list}==set(pixels)
    assert not Path(manifest['batch_root']).exists()


def test_source_changed_during_decoder_is_rejected(tmp_path):
    manifest,pixels=gate_fixture(tmp_path)
    def decoder(source):
        Path(source).write_bytes(b'changed during decoder call')
        return {'pixel_sha256':pixels[str(source)]}
    with patch('allocate.image_metadata',side_effect=decoder):
        with pytest.raises(ValueError,match='during metadata verification'):
            allocate.verify_material_metadata(manifest,workers=1)
    assert not Path(manifest['batch_root']).exists()


def test_production_decoder_does_not_accept_opaque_sentinels(tmp_path):
    from PIL import UnidentifiedImageError
    manifest,_=gate_fixture(tmp_path)
    with pytest.raises(UnidentifiedImageError):allocate.execute_copy(manifest)
    assert not Path(manifest['batch_root']).exists()


if __name__=='__main__':
    raise SystemExit(pytest.main([__file__,'-q','-p','no:cacheprovider']))
