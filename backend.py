"""Combined-master Cipla mapping pipeline. All persisted mappings are account scoped.

Reuses original candidate engine, CatBoost and RapidFuzz pharmaceutical safety checks.
A prediction is never called verified merely because the model proposed it.
"""
from __future__ import annotations
import io
import os
import re
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
import pandas as pd
from src.learning_pipeline import (normalize_master, norm, clean_code, product_key,
                                   MatchingEngine, train_ranker, deduplicate_history,
                                   model_path_for_account)
from src.pharma_rapidfuzz import (compatible, score_details, first_word,
                                  MATCH_THRESHOLD)
from src.correction_memory import (load_corrections, prepare_corrections, save_corrections)
from src.universal_pharma_matcher import token_dp_similarity

ROOT = Path(os.environ.get('CIPLA_DATA_DIR', Path(__file__).resolve().parent / 'data'))
MODELS = ROOT / 'models'
MASTER_PATH = ROOT / 'combined_master.csv'
DB = ROOT / 'history.sqlite'
CORRECTIONS = ROOT / 'corrections.sqlite'


def connection_settings():
    # Deployment-configured history storage; credentials never persisted in uploads.
    mode = os.getenv('CIPLA_HISTORY_BACKEND', 'sqlite').strip().lower()
    if mode not in ('sqlite', 'sqlserver'):
        raise ValueError('CIPLA_HISTORY_BACKEND must be sqlite or sqlserver')
    return {'backend': mode, 'server': os.getenv('CIPLA_SQL_SERVER', ''),
            'database': os.getenv('CIPLA_SQL_DATABASE', ''),
            'sqlite_path': str(DB)}


def _open_history():
    cfg = connection_settings()
    if cfg['backend'] == 'sqlite':
        return sqlite3.connect(DB)
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError('Install pyodbc and the Microsoft ODBC Driver 18 to use SQL Server.') from exc
    driver = os.getenv('CIPLA_SQL_DRIVER', 'ODBC Driver 18 for SQL Server')
    server = cfg['server']; database = cfg['database']
    if not server or not database:
        raise ValueError('CIPLA_SQL_SERVER and CIPLA_SQL_DATABASE must be set.')
    # ODBC credentials come only from server environment/secrets, not user data.
    uid = os.getenv('CIPLA_SQL_USERNAME', '')
    pwd = os.getenv('CIPLA_SQL_PASSWORD', '')
    if any(';' in v or '{' in v or '}' in v for v in (driver, server, database, uid)):
        raise ValueError('Invalid SQL Server configuration characters.')
    connstr = (f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};'
               'Encrypt=yes;TrustServerCertificate=no;Connection Timeout=8;')
    if uid:
        connstr += f'UID={uid};PWD={{{pwd.replace("}", "}}")}}};'
    else:
        connstr += 'Trusted_Connection=yes;'
    return pyodbc.connect(connstr, timeout=8)


@contextmanager
def history_session():
    cx = _open_history()
    try:
        yield cx
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


def init():
    ROOT.mkdir(parents=True, exist_ok=True)
    MODELS.mkdir(parents=True, exist_ok=True)
    with history_session() as cx:
        if connection_settings()['backend'] == 'sqlserver':
            cx.execute('''IF OBJECT_ID(N'dbo.cipla_history', N'U') IS NULL
                CREATE TABLE dbo.cipla_history (
                  account NVARCHAR(255) NOT NULL, item_code NVARCHAR(500) NOT NULL,
                  raw_product_name NVARCHAR(1000) NOT NULL, mapped_sku_code NVARCHAR(255) NOT NULL,
                  source NVARCHAR(100) NOT NULL, added_at NVARCHAR(60) NOT NULL)''')
            # Name/item filters intentionally stay account-scoped; avoid UNIQUE constraints
            # on long names and allow old conflicting rows to surface for review.
        else:
            cx.execute('''CREATE TABLE IF NOT EXISTS history (
                account TEXT NOT NULL, item_code TEXT NOT NULL, raw_product_name TEXT NOT NULL,
                mapped_sku_code TEXT NOT NULL, source TEXT NOT NULL, added_at TEXT NOT NULL,
                UNIQUE(account, item_code, raw_product_name, mapped_sku_code))''')
            cx.execute('CREATE INDEX IF NOT EXISTS idx_history_name ON history(account,raw_product_name)')
            cx.execute('CREATE INDEX IF NOT EXISTS idx_history_item ON history(account,item_code)')


def connection_status():
    cfg = connection_settings()
    try:
        init()
        with history_session() as cx:
            table = 'dbo.cipla_history' if cfg['backend']=='sqlserver' else 'history'
            count = cx.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        return {**cfg,'connected':True,'records':int(count),'message':'Connection successful'}
    except Exception as exc:
        return {**cfg,'connected':False,'records':0,'message':str(exc)}


def read_upload(blob, filename, sheet=None):
    buf = io.BytesIO(blob)
    if filename.lower().endswith('.csv'):
        return pd.read_csv(buf, dtype=str, keep_default_na=False).fillna('')
    return pd.read_excel(buf, sheet_name=sheet or 0, dtype=str, keep_default_na=False).fillna('')


def sheet_names(blob, name):
    if name.lower().endswith('.csv'): return ['CSV']
    with pd.ExcelFile(io.BytesIO(blob)) as x: return x.sheet_names


def classify(business_unit):
    """Do not infer division from letters in SKU codes."""
    s = re.sub(r'[^A-Z0-9]', '', norm(business_unit))
    if s == 'EXPANSEIP': return 'RX'
    if s in {'GX', 'GENERIC', 'GENERICS'} or s.startswith('GX'): return 'GX'
    if s in {'CHL', 'CONSUMERHEALTH', 'CONSUMERHEALTHCARE'} or s.startswith('CHL'): return 'CHL'
    return 'OTHER' if s else 'UNKNOWN'


def save_master(df, code_col, name_col, bu_col, pack_col=None, remark_col=None):
    init()
    if len({code_col, name_col, bu_col}) != 3: raise ValueError('Select different code, name, and business-unit columns.')
    extras={'business_unit':bu_col, 'pack':pack_col, 'remark':remark_col}
    m=normalize_master(df, code_col, name_col, extras)
    if m.empty: raise ValueError('Master is empty.')
    m['Division_Type']=m.business_unit.map(classify)
    # Reject unknown BU: incorrect classification is more dangerous than an explicit review.
    if (m.Division_Type=='UNKNOWN').any():
        raise ValueError('Master has blank business units. Complete them before saving.')
    tmp=MASTER_PATH.with_suffix('.tmp')
    m.to_csv(tmp,index=False);tmp.replace(MASTER_PATH)
    return m


def master():
    if not MASTER_PATH.exists(): raise ValueError('Admin must upload the combined master first.')
    return pd.read_csv(MASTER_PATH,dtype=str,keep_default_na=False)


def history(account=None):
    init()
    with history_session() as cx:
        table = 'dbo.cipla_history' if connection_settings()['backend']=='sqlserver' else 'history'
        df=pd.read_sql_query(f'SELECT account,item_code,raw_product_name,mapped_sku_code,source AS source_file FROM {table}',cx)
    if account is not None: df=df.loc[df.account==norm(account)].reset_index(drop=True)
    return df


def store_history(df, account_col, name_col, code_col, item_col=None, account_fixed=None, replace=False, source='UPLOAD', supersede=False):
    init();m=master(); valid=set(m.sku_code)
    if len({name_col,code_col}) != 2: raise ValueError('Name and code columns must differ.')
    if account_fixed is None and not account_col: raise ValueError('Select an account column.')
    z=pd.DataFrame({
        'account': norm(account_fixed) if account_fixed else df[account_col].map(norm),
        'item_code':df[item_col].map(norm) if item_col else '',
        'raw_product_name':df[name_col].map(norm),
        'mapped_sku_code':df[code_col].map(clean_code),
    })
    z=z.loc[z.account.ne('') & z.raw_product_name.ne('') & z.mapped_sku_code.ne('')].copy()
    if z.empty: raise ValueError('No nonblank historical mappings.')
    invalid=sorted(set(z.mapped_sku_code)-valid)
    if invalid: raise ValueError(f'Codes missing from combined master: {", ".join(invalid[:12])}. No rows saved.')
    # No single account + exact name + item identity can carry multiple verified labels.
    conflicts=z.groupby(['account','item_code','raw_product_name']).mapped_sku_code.nunique()
    if (conflicts>1).any():raise ValueError('Conflicting verified codes for identical account/item/name in upload.')
    z=z.drop_duplicates(['account','item_code','raw_product_name','mapped_sku_code'])
    stamp=datetime.now(timezone.utc).isoformat()
    rows=[(*r,source,stamp) for r in z[['account','item_code','raw_product_name','mapped_sku_code']].itertuples(index=False,name=None)]
    with history_session() as cx:
        sqlserver = connection_settings()['backend']=='sqlserver'
        table = 'dbo.cipla_history' if sqlserver else 'history'
        if replace: cx.execute(f'DELETE FROM {table}')
        if supersede:
            for acc,item,name,_code in z[['account','item_code','raw_product_name','mapped_sku_code']].itertuples(index=False,name=None):
                if item:
                    cx.execute(f'DELETE FROM {table} WHERE account=? AND item_code=?',(acc,item))
                cx.execute(f'DELETE FROM {table} WHERE account=? AND raw_product_name=?',(acc,name))
        if sqlserver:
            for acc,item,name,code,source_name,added_at in rows:
                cx.execute(f'''INSERT INTO {table}(account,item_code,raw_product_name,mapped_sku_code,source,added_at)
                  SELECT ?,?,?,?,?,? WHERE NOT EXISTS
                  (SELECT 1 FROM {table} WITH (UPDLOCK,HOLDLOCK) WHERE account=? AND item_code=?
                   AND raw_product_name=? AND mapped_sku_code=?)''',
                   (acc,item,name,code,source_name,added_at,acc,item,name,code))
        else:
            cx.executemany(f'''INSERT OR IGNORE INTO {table}
              (account,item_code,raw_product_name,mapped_sku_code,source,added_at) VALUES(?,?,?,?,?,?)''',rows)
        total=cx.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    return len(rows),total


def training_rows(account):
    h=history(account)
    # Save all forms (including aliases); training engine checks conflicting names.
    return deduplicate_history(h)


def train_account(account):
    h=training_rows(account)
    return train_ranker(h, master(), MODELS, account=account)


def add_training(df, account, name_col, code_col, item_col=None):
    return store_history(df,None,name_col,code_col,item_col,account_fixed=account,source='MODEL_TRAINING')


def add_corrections(df, account, name_col, code_col, item_col=None, reviewer='ADMIN'):
    m=master()
    c=prepare_corrections(df,name_col,code_col,account,reviewer,item_col=item_col)
    result=save_corrections(CORRECTIONS,c,m)
    # Additionally persist as exact historical aliases to enable later historical validation.
    store_history(df,None,name_col,code_col,item_col,account_fixed=account,source='CORRECTED_VALUE',supersede=True)
    return result


def _candidate_index(m):
    groups={}
    for row in m.to_dict('records'):
        groups.setdefault(first_word(row['sku_name']),[]).append(row)
    return groups


def dynamic_candidates(name, idx, threshold=MATCH_THRESHOLD, max_results=5):
    """Strict existing pharma_rapidfuzz compatible() and score_details(); all code types."""
    out=[]
    for row in idx.get(first_word(name),[]):
        ok,reason=compatible(name,row['sku_name'],row.get('pack',''))
        if not ok:continue
        fuzzy_score,details=score_details(name,row['sku_name'])
        dp_score=token_dp_similarity(name,row['sku_name'])
        score=round(0.65*fuzzy_score+0.35*dp_score,2)
        if score>=threshold:
            out.append({'code':row['sku_code'],'name':row['sku_name'],'pack':row.get('pack',''),
                        'remark':row.get('remark',''),'Division_Type':row['Division_Type'],
                        'dynamic_score':round(score,2),'DP_Score':dp_score,'explanation':details})
    return sorted(out,key=lambda x:(-x['dynamic_score'],x['code']))[:max_results]


def unique_products(df,item_col,name_col):
    if item_col==name_col:raise ValueError('Choose separate item and name columns.')
    data=df.copy()
    data['item_code']=df[item_col].map(norm)
    data['raw_product_name']=df[name_col].map(norm)
    data=data.loc[data.raw_product_name.ne('')]
    return data.drop_duplicates(['item_code','raw_product_name']).reset_index(drop=True)


def _model_available(account):
    return (model_path_for_account(MODELS,account)/'LATEST_MODEL.json').exists()


def train_universal_combined(df, name_col, code_col, account_col=None):
    """Train the shared cold-start CatBoost on verified aliases from any source account.

    Training and prediction are both on the SAME combined master and account namespace.
    Only manually verified codes may be supplied by the administrator.
    """
    count, total = store_history(df, None, name_col, code_col,
                                 account_fixed='UNIVERSAL', source='UNIVERSAL_VERIFIED')
    return train_account('UNIVERSAL'), count


def initial_mapping(df, account, item_col, name_col, threshold=88.0):
    """One prediction per unique item+name; never falsely auto-verify a suggestion."""
    m=master();raw=unique_products(df,item_col,name_col)
    if raw.empty:raise ValueError('No products in uploaded sheet.')
    c=load_corrections(CORRECTIONS,latest_only=True) if CORRECTIONS.exists() else pd.DataFrame()
    account_has_model=_model_available(account)
    use_universal=(account.upper() == 'UNIVERSAL') or (not account_has_model and _model_available('UNIVERSAL'))
    engine_account='UNIVERSAL' if use_universal else account
    h=training_rows(engine_account)
    engine=MatchingEngine(m,MODELS,h,c,account=engine_account)
    model_loaded=engine.model is not None
    index=_candidate_index(m)
    records=[]
    for row in raw.itertuples(index=False):
        item=row.item_code; name=row.raw_product_name
        ranked=engine.rank(name,account=engine_account,limit=3,item_code=item)
        ml_code='';ml_score=0.;source='NO TRAINED MODEL'
        if not ranked.empty:
            top=ranked.iloc[0]
            ml_code=clean_code(top.sku_code)
            ml_score=round(float(top.ml_score)*100,2)
            source=('UNIVERSAL_CATBOOST' if use_universal else 'ACCOUNT_CATBOOST') if model_loaded else 'FUZZY_BASELINE'
        dyn=dynamic_candidates(name,index,threshold)
        d_code=dyn[0]['code'] if dyn else ''
        suggested=ml_code or d_code
        records.append({'item_code':item,'item_name':name,
            'Model_Code':ml_code if model_loaded else '',
            'Model_Score':ml_score if model_loaded else '', 'Model_Source':source,
            'Dynamic_Code':d_code,'Dynamic_Score':dyn[0]['dynamic_score'] if dyn else '',
            'Suggested_Code':suggested,'Division_Type':'','Match_Status':'NOT VALIDATED',
            'Candidate_Count':len(dyn)})
    out=pd.DataFrame(records)
    by_code=m.set_index('sku_code')
    out['Division_Type']=out.Suggested_Code.map(by_code.Division_Type).fillna('')
    out['Master_Product']=out.Suggested_Code.map(by_code.sku_name).fillna('')
    return out


def _historical_index(h):
    # Names and item IDs are strictly account-scoped. Never arbitrarily resolve conflicts.
    names={};items={}
    for r in h.itertuples(index=False):
        n=norm(r.raw_product_name); c=clean_code(r.mapped_sku_code); item=norm(r.item_code)
        names.setdefault(n,set()).add(c)
        if item:items.setdefault(item,set()).add(c)
    return names,items


def validate(mapping, account, threshold=88.0):
    m=master();by=m.set_index('sku_code');h=history(account)
    names,items=_historical_index(h)
    idx=_candidate_index(m)
    output=mapping.copy()
    selections=[]; reviews=[]
    for row in output.itertuples(index=False):
        name=norm(row.item_name);item=norm(row.item_code)
        codes=names.get(name,set())
        reason='';code='';status='UNVALIDATED'
        if len(codes)==1:
            code=next(iter(codes));reason='HISTORICAL_NAME';status='VALIDATED'
        elif len(codes)>1:
            reason='CONFLICTING_HISTORY'
        else:
            # Item-code match is allowed only when unambiguous and not contradictory.
            item_codes=items.get(item,set()) if item else set()
            if len(item_codes)==1:
                code=next(iter(item_codes));reason='HISTORICAL_ITEM';status='VALIDATED'
            elif len(item_codes)>1:reason='CONFLICTING_ITEM_HISTORY'
        dyn=dynamic_candidates(name,idx,threshold,max_results=10)
        ml=clean_code(row.Model_Code)
        if status!='VALIDATED' and not reason.startswith('CONFLICTING'):
            # Agreement provides additional evidence but still requires a strong
            # pharmaceutical match with no tied candidates.
            if (ml and pd.notna(row.Model_Score) and str(row.Model_Score).strip()
                and float(row.Model_Score) >= threshold and dyn and dyn[0]['code']==ml
                and dyn[0]['dynamic_score'] >= threshold
                and (len(dyn)==1 or dyn[0]['dynamic_score']-dyn[1]['dynamic_score'] >= 5.0)):
                code=ml;reason='MODEL_DYNAMIC_AGREEMENT';status='VALIDATED'
            elif (ml or (dyn[0]['code'] if dyn else '')) and (ml or dyn[0]['code']) in by.index and by.loc[ml or dyn[0]['code'],'Division_Type'] in {'GX','CHL','OTHER'}:
                # Requested non-RX override is retained as an explicit *proposal*;
                # not misrepresented as verified historical mapping.
                code=ml or dyn[0]['code'];reason='NON_RX_PROVISIONAL_OVERRIDE';status='NEEDS APPROVAL'
        if code and code not in by.index:
            code='';reason='CODE_NOT_IN_MASTER';status='UNVALIDATED'
        if status!='VALIDATED':
            for cand in dyn:
                reviews.append({'item_code':item,'item_name':name,
                    'Suggested_Code':cand['code'],'Master_Product':cand['name'],
                    'Pack_Size':cand['pack'],'Remark':cand['remark'],
                    'Division_Type':cand['Division_Type'],'Score':cand['dynamic_score'],
                    'Approve':False})
            if not dyn and code:
                r=by.loc[code]
                reviews.append({'item_code':item,'item_name':name,'Suggested_Code':code,
                    'Master_Product':r.sku_name,'Pack_Size':r.get('pack',''),
                    'Remark':r.get('remark',''),'Division_Type':r.Division_Type,
                    'Score':'','Approve':False})
        selections.append((code,reason,status))
    output[['Final_Code','Validation_Source','Validation_Status']]=pd.DataFrame(selections,index=output.index)
    output['Division_Type']=output.Final_Code.map(by.Division_Type).fillna('')
    output['Final_Master_Product']=output.Final_Code.map(by.sku_name).fillna('')
    review=pd.DataFrame(reviews,columns=['item_code','item_name','Suggested_Code','Master_Product','Pack_Size','Remark','Division_Type','Score','Approve'])
    return output,review


def apply_approvals(final, reviewed):
    """Explicit per-row approval; one selected suggestion per item/name."""
    out=final.copy()
    approved=reviewed.loc[reviewed.Approve.fillna(False).astype(bool)].copy()
    if approved.empty:return out
    counts=approved.groupby(['item_code','item_name']).Suggested_Code.nunique()
    if (counts>1).any():raise ValueError('Select only ONE code per product.')
    valid=set(master().sku_code)
    if not set(approved.Suggested_Code).issubset(valid):raise ValueError('Selected code is not in combined master.')
    choices={(r.item_code,r.item_name):r.Suggested_Code for r in approved.itertuples(index=False)}
    m=master().set_index('sku_code')
    for pos,r in out.iterrows():
        key=(r['item_code'],r['item_name'])
        if key in choices and r['Validation_Status']!='VALIDATED':
            code=choices[key]
            out.loc[pos,['Final_Code','Validation_Source','Validation_Status','Division_Type','Final_Master_Product']]=[
                code,'USER_APPROVED','VALIDATED',m.loc[code,'Division_Type'],m.loc[code,'sku_name']]
    return out


def csv_bytes(df):
    return df.to_csv(index=False).encode('utf-8-sig')

FINAL_COLUMNS = ['product_name','Cipla code','Division_Type','item_code']


def final_download(final):
    """Exactly four columns. Only verified or explicitly approved codes are exported."""
    m=master().drop_duplicates('sku_code').set_index('sku_code')
    valid=final['Validation_Status'].eq('VALIDATED')
    codes=final['Final_Code'].where(valid,'').map(clean_code)
    codes=codes.where(codes.isin(m.index),'')
    division=codes.map(m['Division_Type']).fillna('')
    return pd.DataFrame({'product_name':final['item_name'].fillna(''),
                         'Cipla code':codes,
                         'Division_Type':division,
                         'item_code':final['item_code'].fillna('')},columns=FINAL_COLUMNS)
