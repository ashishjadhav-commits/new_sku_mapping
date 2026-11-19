"""Integration smoke tests for combined master, history, and review safety."""
import os
import importlib
import pandas as pd
import pytest

@pytest.fixture
def b(tmp_path,monkeypatch):
    monkeypatch.setenv('CIPLA_DATA_DIR',str(tmp_path))
    import backend
    backend=importlib.reload(backend)
    backend.init()
    sample=pd.DataFrame({
        'Code':['1001','ABC9','CHL33'],
        'Product':['FORACORT 200 INHALER','VITAMIN B TABLET','HERBAL SHAMPOO 100ML'],
        'BU':['EXPANSE(IP)','GX','CHL'],
        'Pack':['120MD','10','100ML'],
        'Remark':['EXISTING']*3,
    })
    backend.save_master(sample,'Code','Product','BU','Pack','Remark')
    return backend


def test_master_combined(b):
    assert b.master()['Division_Type'].tolist()==['RX','GX','CHL']
    assert 'ABC9' in b.master().sku_code.tolist()


def test_auto_unique_and_exact_history(b):
    h=pd.DataFrame({'account':['MEDPLUS'],'name':['FORACORT 200 INHALER'],'code':['1001']})
    b.store_history(h,'account','name','code')
    raw=pd.DataFrame({'item':['xx','xx','y'],'name':['FORACORT 200 INHALER']*2+['VITAMIN B TABLET']})
    init=b.initial_mapping(raw,'MEDPLUS','item','name')
    assert len(init)==2
    result,review=b.validate(init,'MEDPLUS')
    assert result.iloc[0].Final_Code=='1001'
    assert result.iloc[0].Validation_Status=='VALIDATED'


def test_non_rx_not_false_verified_without_model(b):
    raw=pd.DataFrame({'item':['a'],'name':['VITAMIN B TABLET']})
    init=b.initial_mapping(raw,'NEW ACCOUNT','item','name')
    out,review=b.validate(init,'NEW ACCOUNT')
    assert out.iloc[0].Final_Code=='ABC9'
    assert out.iloc[0].Validation_Status=='NEEDS APPROVAL'
    assert not review.empty
    final=b.apply_approvals(out,review.assign(Approve=True))
    assert final.iloc[0].Validation_Status=='VALIDATED'


def test_invalid_codes_roll_back(b):
    bad=pd.DataFrame({'account':['MEDPLUS'],'name':['FAKE'],'code':['BAD-CODE']})
    with pytest.raises(ValueError):b.store_history(bad,'account','name','code')
    assert b.history().empty


def test_history_alias_append(b):
    base=pd.DataFrame({'name':['VITAMIN-B'],'code':['ABC9']})
    b.store_history(base,None,'name','code',account_fixed='MEDPLUS')
    raw=pd.DataFrame({'item':['p'],'name':['VITAMIN B TABLET']})
    a=b.initial_mapping(raw,'MEDPLUS','item','name')
    r,_=b.validate(a,'MEDPLUS')
    assert r.iloc[0].Validation_Source!='HISTORICAL_NAME'
    alias=pd.DataFrame({'name':['VITAMIN B TABLET'],'code':['ABC9']})
    b.store_history(alias,None,'name','code',account_fixed='MEDPLUS',source='MANUAL_APPEND')
    r,_=b.validate(a,'MEDPLUS')
    assert r.iloc[0].Validation_Source=='HISTORICAL_NAME'


def test_conflicting_history_not_arbitrarily_applied(b):
    a=pd.DataFrame({'name':['FORACORT 200 INHALER'],'code':['1001']})
    b.store_history(a,None,'name','code',account_fixed='M')
    alt=pd.DataFrame({'name':['FORACORT 200 INHALER'],'code':['ABC9']})
    b.store_history(alt,None,'name','code',account_fixed='M')
    raw=pd.DataFrame({'item':['x'],'name':['FORACORT 200 INHALER']})
    initial=b.initial_mapping(raw,'M','item','name')
    result,_=b.validate(initial,'M')
    assert result.iloc[0].Validation_Source=='CONFLICTING_HISTORY'
    assert result.iloc[0].Validation_Status=='UNVALIDATED'


def test_supersede_correction_in_history(b):
    old=pd.DataFrame({'name':['VITAMIN B TABLET'],'code':['1001']})
    b.store_history(old,None,'name','code',account_fixed='M')
    revised=pd.DataFrame({'name':['VITAMIN B TABLET'],'code':['ABC9']})
    b.store_history(revised,None,'name','code',account_fixed='M',supersede=True)
    h=b.history('M')
    assert h.mapped_sku_code.tolist()==['ABC9']


def test_final_download_four_columns_and_verified_only(b):
    h=pd.DataFrame({'account':['MEDPLUS'],'name':['FORACORT 200 INHALER'],'code':['1001']})
    b.store_history(h,'account','name','code')
    raw=pd.DataFrame({'item':['01','02'],'name':['FORACORT 200 INHALER','HERBAL SHAMPOO 100ML']})
    initial=b.initial_mapping(raw,'MEDPLUS','item','name')
    out,review=b.validate(initial,'MEDPLUS')
    final=b.final_download(out)
    assert final.columns.tolist()==['product_name','Cipla code','Division_Type','item_code']
    assert final.iloc[0].to_dict()=={'product_name':'FORACORT 200 INHALER','Cipla code':'1001','Division_Type':'RX','item_code':'01'}
    assert final.iloc[1]['Cipla code']==''
    assert final.iloc[1]['Division_Type']==''
    assert not review.empty


def test_connection_manager_sqlite(b):
    state=b.connection_status()
    assert state['connected']
    assert state['backend']=='sqlite'
    assert state['records']==0


def test_strict_agreement_requires_real_model_and_threshold(b):
    raw=pd.DataFrame({'item':['3'],'name':['VITAMIN B TABLET']})
    initial=b.initial_mapping(raw,'BRAND NEW','item','name')
    assert initial.iloc[0].Model_Code==''
    initial.loc[0,'Model_Code']='ABC9'
    initial.loc[0,'Model_Score']=75.0  # below threshold: not trustworthy
    final,_=b.validate(initial,'BRAND NEW',threshold=88.0)
    assert final.iloc[0].Validation_Status!='VALIDATED'


def test_manual_approval_download(b):
    raw=pd.DataFrame({'item':['3'],'name':['VITAMIN B TABLET']})
    initial=b.initial_mapping(raw,'BRAND NEW','item','name')
    final,review=b.validate(initial,'BRAND NEW')
    assert len(review)==1
    approved=b.apply_approvals(final,review.assign(Approve=True))
    download=b.final_download(approved)
    assert download.iloc[0]['Cipla code']=='ABC9'
    assert download.iloc[0]['Division_Type']=='GX'


def test_universal_separate_account_history(b):
    verified=pd.DataFrame({'name':['HERBAL SHAMPOO 100ML'],'code':['CHL33']})
    b.store_history(verified,None,'name','code',account_fixed='UNIVERSAL')
    raw=pd.DataFrame({'item':['a'],'name':['HERBAL SHAMPOO 100ML']})
    universal=b.initial_mapping(raw,'UNIVERSAL','item','name')
    universal_result,_=b.validate(universal,'UNIVERSAL')
    assert universal_result.iloc[0].Validation_Source=='HISTORICAL_NAME'
    medplus=b.initial_mapping(raw,'MEDPLUS','item','name')
    medplus_result,_=b.validate(medplus,'MEDPLUS')
    assert medplus_result.iloc[0].Validation_Source!='HISTORICAL_NAME'
