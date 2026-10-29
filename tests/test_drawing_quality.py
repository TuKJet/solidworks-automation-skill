import copy
from scripts.drawing_quality import accept_candidate, dimension_signature, layout_candidate_specs, quality_metrics


def structure():
    return {'dimensions': [{'identity': {'source': 'p.sldprt', 'configuration': 'Default', 'canonical_key': 'p|Default|D1'},
                             'system_value': {'value': .02, 'unit': 'm', 'type': 2}, 'visibility': 'visible'}]}


def review(overlaps=0, unmatched=False):
    return {'pdf_dimension_rendering': {'matched': [] if unmatched else [{'dimension_index': 0}],
              'unmatched_dimension_indexes': [0] if unmatched else [], 'collisions': [{'first':'D1','second':'D2'}]*overlaps},
            'findings': []}


def test_candidates_respect_explicit_scale_limit_and_user_spacing():
    assert layout_candidate_specs({'scale': .5}, 1) == []
    assert layout_candidate_specs({}, 1, max_attempts=1) == []
    got = layout_candidate_specs({'layoutOptions': {'viewGapMm': 60, 'marginMm': 30}}, 1)
    assert [x['scale'] for x in got] == [1, .5]
    assert all(x['layoutOptions'] == {'viewGapMm':60, 'marginMm':30} for x in got)


def test_real_pdf_overlap_triggers_retry_but_estimate_or_empty_drawing_does_not():
    assert quality_metrics(structure(), review(2))['needs_layout_retry']
    estimated = {'findings':[{'code':'DRAWING_OUTSIDE_SHEET','severity':'fail','evidence_source':'estimated','confirmed_collision':False}]}
    assert not quality_metrics(structure(), estimated)['needs_layout_retry']
    assert not quality_metrics({'dimensions':[]}, review(2))['needs_layout_retry']
    raw = {'pdf_evidence':{'overlaps':[{'confirmedGeometryOverlap':True}]}}
    assert quality_metrics(structure(), raw)['pdf_text_overlap_count'] == 1


def test_accept_rejects_changed_value_hidden_or_lost_dimension():
    before=structure()
    assert accept_candidate(before, copy.deepcopy(before), review(2), review(0))
    for after in ({'dimensions':[]}, copy.deepcopy(before), copy.deepcopy(before)):
        if after['dimensions']:
            if after == before:
                after['dimensions'][0]['system_value']['value']=.03
        assert not accept_candidate(before, after, review(2), review(0))
    after=copy.deepcopy(before); after['dimensions'][0]['visibility']='hidden'
    assert not accept_candidate(before, after, review(2), review(0))
    after=copy.deepcopy(before); after['dimensions'][0]['identity']['source']='other.sldprt'
    assert not accept_candidate(before, after, review(2), review(0))


def test_accept_rejects_worse_matching_or_estimate_only_improvement():
    s=structure()
    assert not accept_candidate(s,s,review(2),review(0,unmatched=True))
    a=review(); a['findings']=[{'evidence_source':'estimated','code':'risk'}]
    assert not accept_candidate(s,s,a,review())
    assert not accept_candidate(s,s,review(1),review(2))


def test_signature_requires_real_identity_and_value_and_tracks_multiplicity():
    s=structure(); assert dimension_signature(s)
    missing=copy.deepcopy(s); del missing['dimensions'][0]['identity']['source']
    assert dimension_signature(missing) is None
    extra=copy.deepcopy(s); extra['dimensions']*=2
    assert dimension_signature(extra)!=dimension_signature(s)


def test_unmatched_indexes_are_original_dimension_indexes():
    s=structure(); s['dimensions'].insert(0, {'visibility':'hidden'})
    r={'pdf_dimension_rendering':{'matched':[], 'unmatched_dimension_indexes':[1]}}
    assert quality_metrics(s,r)['unmatched_visible_dimension_count']==1


def test_candidate_cannot_move_dimension_to_different_view_or_sheet():
    before=structure()
    before['dimensions'][0].update(sheet='Sheet1', view='View1', semantic_view='front')
    for key,value in (('sheet','Sheet2'),('semantic_view','top')):
        after=copy.deepcopy(before)
        after['dimensions'][0][key]=value
        assert not accept_candidate(before,after,review(2),review(0))


def test_candidate_cannot_change_display_text_or_tolerance():
    before=structure()
    for key,value in (('rendered_text','0.03'),('tolerance',{'upper':.1}),('kind','diameter')):
        after=copy.deepcopy(before)
        after['dimensions'][0][key]=value
        assert not accept_candidate(before,after,review(2),review(0))


def test_layout_improvement_cannot_introduce_missing_dimension_requirement():
    before=structure()
    after_review=review(0)
    after_review['findings']=[{'severity':'fail','code':'DRAWING_REQUIRED_DIMENSIONS_MISSING','missing':['diameter']}]
    assert not accept_candidate(before,before,review(2),after_review)
