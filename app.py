"""Cipla mapping: a minimal, functional Streamlit workspace."""
from __future__ import annotations
import io
import os
import hashlib
import hmac
import pandas as pd
import streamlit as st
import backend as b
from src.learning_pipeline import norm

st.set_page_config(page_title='Cipla SKU Mapping',page_icon='💊',layout='wide')
st.markdown('''<style>
.stApp{background:#f7f9fb} .block-container{max-width:1100px;padding-top:1.7rem}
[data-testid="stSidebar"]{background:#142b44} [data-testid="stSidebar"] *{color:#f5f8ff}
h1,h2,h3{color:#193650} div[data-testid="stMetric"]{border:1px solid #e1e7ee;border-radius:12px;background:white;padding:12px}
[data-testid="stVerticalBlockBorderWrapper"]{background:white;border-radius:12px}
</style>''',unsafe_allow_html=True)
b.init()
ACCOUNTS=['Medplus','Entero','Ascent','Frankross','PharmEasy','Udaan','Sastasundar']
UNIVERSAL_LABEL='Universal Account'


def secret_value(name):
    try:return str(st.secrets['admin'][name])
    except (FileNotFoundError,KeyError,TypeError):return os.getenv('CIPLA_ADMIN_'+name.upper(),'')


def uploader(title,key,fields,optional=()):
    file=st.file_uploader(title,type=['xlsx','xls','csv'],key=key+'_file')
    if not file:return None
    content=file.getvalue()
    try:
        sheet=st.selectbox('Sheet',b.sheet_names(content,file.name),key=key+'_sheet')
        df=b.read_upload(content,file.name,None if sheet=='CSV' else sheet)
        if df.empty:st.warning('This sheet is empty.');return None
        choices=['— None —']+list(df.columns)
        picked={}
        ncols=st.columns(min(3,max(1,len(fields))))
        for ix,(k,label,aliases) in enumerate(fields):
            found=next((c for c in df.columns if any(a in str(c).lower().replace(' ','_') for a in aliases)),None)
            opts=choices if k in optional else list(df.columns)
            default=opts.index(found) if found in opts else (0 if k in optional else 0)
            with ncols[ix%len(ncols)]:picked[k]=st.selectbox(label,opts,index=default,key=key+'_'+k)
            if picked[k]=='— None —':picked[k]=None
        defined=[v for v in picked.values() if v]
        if len(defined)!=len(set(defined)):st.warning('Select a different column for each field.')
        return df,picked
    except Exception as ex:
        st.error(f'Cannot read file: {ex}');return None


def run_action(label,fn,key):
    if st.button(label,key=key,type='primary'):
        try:
            with st.spinner('Processing...'):return fn()
        except Exception as e:st.error(str(e))
    return None


def download(label,data,filename,key):
    st.download_button(label,b.csv_bytes(data),file_name=filename,mime='text/csv',key=key)


def account_box(key):
    name=st.selectbox('Account',ACCOUNTS+[UNIVERSAL_LABEL,'New account'],key=key)
    if name==UNIVERSAL_LABEL:
        return 'UNIVERSAL'
    return st.text_input('Account name',key=key+'_new').strip() if name=='New account' else name

with st.sidebar:
    st.markdown('## Cipla Mapping')
    page=st.radio('Workspace',['User','Admin','Master Upload'],label_visibility='collapsed')
    st.divider()
    st.caption('Admin signed in' if st.session_state.get('admin_ok') else 'User mode')

if page in ('Admin','Master Upload') and not st.session_state.get('admin_ok'):
    st.title('Admin Login')
    username=st.text_input('Username',key='admin_username')
    password=st.text_input('Password',type='password',key='admin_password')
    if st.button('Sign in',type='primary'):
        expected_user=secret_value('username')
        expected_hash=secret_value('password_hash')
        supplied_hash=hashlib.sha256(password.encode()).hexdigest()
        if expected_user and expected_hash and hmac.compare_digest(username,expected_user) and hmac.compare_digest(supplied_hash,expected_hash):
            st.session_state.admin_ok=True;st.rerun()
        else:st.error('Login failed. Ask your administrator for access.')
    if not secret_value('username') or not secret_value('password_hash'):
        st.info('Admin credentials are not configured. See README.md.')
    st.stop()

if page=='Master Upload':
    st.title('Master Upload')
    st.caption('One master for all accounts.')
    if b.MASTER_PATH.exists():
        m=b.master();st.success(f'{len(m):,} products in master')
        st.dataframe(m[['sku_code','sku_name','business_unit','Division_Type']].head(10),hide_index=True)
    input_data=uploader('Upload Combined Master','master',[
        ('code','Product Code',['sku_code','material_code','cipla_code','product_code']),
        ('name','Product Name',['sku_name','material_name','product_name']),
        ('bu','Business Unit',['business_unit','businessunit','division']),
        ('pack','Pack Size',['pack_size','pack']),
        ('remark','Remark',['remark','status'])],optional=('pack','remark'))
    if input_data:
        df,c=input_data
        if all(c.get(k) for k in ('code','name','bu')):
            result=run_action('Save Master',lambda:b.save_master(df,c['code'],c['name'],c['bu'],c['pack'],c['remark']),'save_master')
            if result is not None:st.success(f'Master saved: {len(result):,} products. Retrain account models after master changes.')
    if st.button('Log out',key='master_logout'):st.session_state.admin_ok=False;st.rerun()

elif page=='Admin':
    st.title('Admin')
    account=account_box('admin_account')
    tabs=st.tabs(['Model Training','Corrected Values','Previous Database','Append in Database','Database Manager'])
    with tabs[0]:
        st.subheader('Model Training')
        if account=='UNIVERSAL':
            st.caption('Train the separate Universal Account model with verified mappings from any account.')
        uploaded=uploader('Upload Training File','training',[
            ('name','Product Name',['product_name','item_name','productname']),
            ('code','Verified Code',['final_verified_code','mapped_sku_code','cipla_code']),
            ('item','Item Code',['item_code','itemcode'])],optional=('item',))
        if uploaded and account:
            df,c=uploaded
            if st.button('Save Training & Train Model',type='primary'):
                try:
                    if account=='UNIVERSAL':
                        # Universal is a dedicated training namespace, not a regular account.
                        # Store manually verified aliases and train one shared cold-start model.
                        report,count=b.train_universal_combined(df,c['name'],c['code'])
                    else:
                        count,total=b.add_training(df,account,c['name'],c['code'],c['item'])
                        report=b.train_account(account)
                    st.success(f'{count:,} training records processed. Model step: {report.get("training_decision","complete")}')
                except Exception as e:st.error(str(e))
    with tabs[1]:
        st.subheader('Corrected Values')
        st.caption('Only upload confirmed, corrected codes.')
        f=uploader('Upload Corrections','corrections',[
            ('name','Product Name',['item_name','productname','product_name']),
            ('code','Correct Code',['final_verified_code','verified_code','cipla_code']),
            ('item','Item Code',['item_code'])],optional=('item',))
        if f and account and st.button('Save Corrections & Retrain',type='primary'):
            try:
                df,c=f
                b.add_corrections(df,account,c['name'],c['code'],c['item'])
                meta=b.train_account(account)
                st.success(f'Corrections saved. Model: {meta.get("training_decision","complete")}')
            except Exception as e:st.error(str(e))
    with tabs[2]:
        st.subheader('Previous Database')
        f=uploader('Upload Historical Data (combined file)','history',[
            ('account','Account',['account','platform']),
            ('name','Product Name',['item_name','productname','product_name']),
            ('code','Verified Code',['final_verified_code','mapped_sku_code','cipla_code']),
            ('item','Item Code',['item_code'])],optional=('item',))
        if f:
            replace=st.checkbox('Replace existing history (all accounts)',value=False)
            if st.button('Save Historical Data',type='primary'):
                try:
                    df,c=f
                    saved,total=b.store_history(df,c['account'],c['name'],c['code'],c['item'],replace=replace,source='COMBINED_HISTORY')
                    st.success(f'{saved:,} records processed. Database total: {total:,}.')
                except Exception as e:st.error(str(e))
    with tabs[3]:
        st.subheader('Append in Database')
        f=uploader('Upload Manually Verified Products','append',[
            ('name','Product Name',['item_name','productname','product_name']),
            ('code','Verified Code',['final_verified_code','mapped_sku_code','cipla_code']),
            ('item','Item Code',['item_code'])],optional=('item',))
        if f and account and st.button('Append Verified Products',type='primary'):
            try:
                df,c=f
                saved,total=b.store_history(df,None,c['name'],c['code'],c['item'],account_fixed=account,source='MANUAL_APPEND')
                st.success(f'{saved:,} records processed. Database total: {total:,}.')
            except Exception as e:st.error(str(e))
    st.divider()
    with tabs[4]:
        st.subheader('Database Connection')
        info=b.connection_settings()
        st.selectbox('Connection', ['SQLite (local)'] if info['backend']=='sqlite' else ['SQL Server (configured)'],disabled=True)
        if info['backend']=='sqlite':
            st.caption('Local database: '+info['sqlite_path'])
            st.warning('For hosted deployment, local SQLite data may be lost on restart. Configure SQL Server for persistent history.')
        else:
            st.text_input('Server',value=info['server'],disabled=True)
            st.text_input('Database',value=info['database'],disabled=True)
        if st.button('Test Database Connection'):
            check=b.connection_status()
            if check['connected']: st.success(f"Connected · {check['records']:,} historical records")
            else: st.error('Connection failed: '+check['message'])
        st.caption('Set CIPLA_HISTORY_BACKEND=sqlserver and SQL Server credentials in deployment secrets; restart the app to switch. Credentials are never shown here.')
    st.divider()
    if st.button('Log out'):st.session_state.admin_ok=False;st.rerun()

else:
    st.title('User')
    account=account_box('user_account')
    if account=='UNIVERSAL':
        st.caption('Universal Account uses its own trained model and its own history. Other accounts remain separate.')
    tab1,tab2=st.tabs(['Current Month Data','Historical Validation'])
    with tab1:
        st.subheader('Current Month Data')
        incoming=uploader('Upload Current Month File','current',[
            ('item','Item Code',['item_code','itemcode','product_code']),
            ('name','Item Name',['item_name','productname','product_name'])])
        if incoming and account:
            df,c=incoming
            threshold=st.slider('Matching threshold',min_value=70,max_value=100,value=88,step=1)
            signature=(account,hashlib.sha256(incoming[0].to_csv(index=False).encode()).hexdigest(),tuple(c.values()),threshold)
            if st.button('Map Products',type='primary'):
                try:
                    with st.spinner('Matching products...'):
                        result=b.initial_mapping(df,account,c['item'],c['name'],threshold)
                    st.session_state['mapping']=result
                    st.session_state['mapping_sig']=signature
                    st.session_state['mapping_account']=account
                    st.session_state.pop('final',None)
                    st.session_state.pop('review',None)
                except Exception as e:st.error(str(e))
            if st.session_state.get('mapping_sig')==signature and 'mapping' in st.session_state:
                mapping=st.session_state['mapping']
                x,y=st.columns(2)
                x.metric('Products',len(mapping))
                y.metric('Suggested',int(mapping.Suggested_Code.ne('').sum()))
                download('Download Initial Mapping',mapping,'initial_mapping.csv','initial_download')
                st.caption('Initial mapping has not been validated.')
    with tab2:
        st.subheader('Historical Validation')
        current=st.session_state.get('mapping') if st.session_state.get('mapping_account')==account else None
        if current is None:st.info('Upload and map the current month first.')
        else:
            if st.button('Validate Data',type='primary'):
                try:
                    with st.spinner('Validating...'):
                        final,review=b.validate(current,account)
                    st.session_state.final=final
                    st.session_state.review=review
                except Exception as e:st.error(str(e))
            if 'final' in st.session_state:
                result=st.session_state.final
                one,two,three=st.columns(3)
                one.metric('Validated',int(result.Validation_Status.eq('VALIDATED').sum()))
                two.metric('Needs Approval',int(result.Validation_Status.eq('NEEDS APPROVAL').sum()))
                three.metric('Unvalidated',int(result.Validation_Status.eq('UNVALIDATED').sum()))
                # Only confirmed or explicitly approved product codes belong in final mapping.
                finalized=result.copy()
                finalized.loc[finalized.Validation_Status.ne('VALIDATED'),'Final_Code']=''
                download('Download Final Mapping',b.final_download(finalized),'final_validated_mapping.csv','final_download')
                if st.session_state.get('review') is not None and len(st.session_state.review):
                    with st.expander('Review Remaining Products (optional)'):
                        st.caption('Select one approved code per product. Unapproved suggestions are never added automatically.')
                        edited=st.data_editor(st.session_state.review,hide_index=True,
                                              disabled=[c for c in st.session_state.review.columns if c!='Approve'],
                                              use_container_width=True,key='approval_editor')
                        if st.button('Apply Selected Approvals'):
                            try:
                                st.session_state.final=b.apply_approvals(st.session_state.final,edited)
                                st.success('Approved mappings included in final download.')
                                st.rerun()
                            except Exception as e:st.error(str(e))
