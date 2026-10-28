import zipfile
from pathlib import Path
import pytest
from scripts.sw_project_inventory import inventory_project

def test_session_lock_is_not_a_part(tmp_path):
    (tmp_path / 'part.sldprt').write_bytes(b'part')
    (tmp_path / '~$part.sldprt').write_bytes(b'lock')
    assert inventory_project(tmp_path)['distinct_files'] == 1

def test_reopened_different_reference_cannot_pass(tmp_path):
    from scripts.sw_delivery import validate_packaged_references
    first, second = tmp_path / 'a.sldprt', tmp_path / 'b.sldprt'
    first.write_bytes(b'a')
    second.write_bytes(b'b')
    result = validate_packaged_references(tmp_path, [first], reopened_references=[second])
    assert not result['self_contained']
    assert len(result['reference_set_mismatch']) == 2

def test_inventory_hash_dedup_and_excludes_output(tmp_path):
    (tmp_path/'a.sldprt').write_bytes(b'x'); (tmp_path/'b.sldprt').write_bytes(b'x')
    (tmp_path/'output').mkdir(); (tmp_path/'output'/'c.sldprt').write_bytes(b'x')
    result=inventory_project(tmp_path)
    assert result['file_count']==2 and result['dedup_groups']==1
    assert all('output' not in x['relative_path'] for x in result['files'])

def test_inventory_rejects_zip_traversal_before_extract(tmp_path):
    zpath=tmp_path/'bad.zip'
    with zipfile.ZipFile(zpath,'w') as z: z.writestr('../escape.sldprt',b'x')
    with pytest.raises(ValueError,match='路径不安全'): inventory_project(tmp_path,tmp_path/'r.json',tmp_path/'stage')

def test_inventory_extracts_only_owned_staging(tmp_path):
    zpath=tmp_path/'ok.zip'
    with zipfile.ZipFile(zpath,'w') as z: z.writestr('sub/part.sldprt',b'x')
    stage=tmp_path/'stage'; result=inventory_project(tmp_path,extract_root=stage)
    assert result['zip_archives'][0]['extracted'] is True
    extracted=Path(result['zip_archives'][0]['extract_root'])/'sub'/'part.sldprt'
    assert extracted.read_bytes()==b'x'

def test_zip_same_member_name_versions_and_root_hash_are_indexed(tmp_path):
    (tmp_path/'root.sldprt').write_bytes(b'same')
    for name, payload in [('one.zip', b'same'), ('two.zip', b'other')]:
        with zipfile.ZipFile(tmp_path/name,'w') as z: z.writestr('variant.sldprt', payload)
    result=inventory_project(tmp_path)
    assert result['file_count']==3
    assert result['distinct_files']==2
    assert result['basename_variants']==1
    assert sum(1 for x in result['files'] if x['sha256']==result['files'][0]['sha256']) >= 2
