# Cipla Combined-Master SKU Mapper (Streamlit)

## Setup

```bash
pip install -r requirements.txt
```

Configure admin credentials (server-side, **not** via the UI):

```bash
python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass('Admin password: ').encode()).hexdigest())"
```

Set `CIPLA_ADMIN_USERNAME` and `CIPLA_ADMIN_PASSWORD_HASH` in your environment, or create `.streamlit/secrets.toml` (do not commit this file):

```toml
[admin]
username = "admin"
password_hash = "<output from command>"
```

Then:

```bash
streamlit run app.py
```

## New workflow

- **Master Upload** (admin-only): one combined master for all accounts. Select Product Code, Product Name, Business Unit, optional Pack Size and Remark. `Division_Type` is RX when the business unit normalizes to `EXPANSE(IP)`, GX/CHL for explicit GX/CHL business units, and OTHER otherwise. Alphabetic codes are allowed.
- **Admin → Model Training**: import verified account mappings; train account-specific CatBoost. Universal training accepts combined verified mappings from different source accounts.
- **Admin → Corrected Values**: import manually verified corrections; append them to history and retrain.
- **Admin → Previous Database**: combined historical mappings in a single upload, with source account column; choose append or replace.
- **Admin → Append in Database**: new manually confirmed aliases saved for future history checks.
- **User → Current Month Data**: import entire raw file, select sheet and columns. Unique `item_code + item_name` pairs are extracted in the backend; download initial (unvalidated) mapping.
- **User → Historical Validation**: one Validate button. Exact account-scoped name/item history, then strong agreement between a genuinely loaded CatBoost model and strict RapidFuzz + token-DP matching, are counted as validated. Remaining candidate suggestions require explicit user approval. Download final mapping even when optional review is skipped.

## Verification and safety

- Original CatBoost candidate engineering and pharmaceutical RapidFuzz checks are reused in `src/`.
- Dynamic matching examines all same-brand candidates in the combined master, including non-numeric codes; dose/form/pack restrictions are applied.
- Non-RX codes missing from history are **provisional suggestions** rather than falsely labelled verified. The final CSV has only verified/approved codes; unmatched rows remain with blank `Final_Code` and explicit status.
- If multiple same-name candidates qualify, show them both in optional review.
- Replacing the master invalidates previous trained-model signatures; retraining is required.
- Persistent data is in `data/`. Protect this directory and use secure admin access before internet-facing deployment. Never upload unreviewed predictions as verified training labels.

## Important current limitations

- The existing legacy Universal model is RX-only. The new application instead trains an account-namespaced **UNIVERSAL** CatBoost ranker using verified data on the combined master. Train it afresh; legacy models are not bundled.
- Historical matching is on exact normalized product name or unique item code, not freely fuzzy-matched aliases. Verified aliases can be added through Append in Database.
- `OTHER` is a possible classification for any business unit not explicitly identified as RX/GX/CHL. Review your actual BU labels and adjust `classify()` if necessary.
- Never assume that agreement between two algorithms independently guarantees pharmaceutical correctness; the UI labels it validated per the requested rule, and strict specification compatibility is required.
- Database is local SQLite; for multi-user deployment, replace with managed transactional storage, robust identity management, and audit logs.

## Release update — final output and database connection

The **Download Final Mapping** button now exports **exactly four columns**, in order:

`product_name`, `Cipla code`, `Division_Type`, `item_code`.

- `product_name` is the uploaded item name (normalized to the application's existing matching convention).
- `Cipla code` is populated **only** if the mapping passed validation or a user explicitly approved it. Others remain blank; no silent model guesses.
- `Division_Type` comes from the corresponding code's **combined master Business Unit**, not from numeric/alphabetic code format. It stays blank when the code is unverified.
- `item_code` is the uploaded account item identifier.
- **Initial Mapping** download intentionally remains detailed/unvalidated for diagnostics.

### Database Manager → Database Connection

The Admin panel includes a **Database Manager** tab where an administrator can see the active database and run **Test Database Connection**. The initial installation uses local SQLite for history. The manager reports the historical record count. SQL Server is supported for the history store when configured **on the server**, not through a publicly writable database-credentials form.

To use Microsoft SQL Server, set the following process environment variables (or root-level Streamlit server secrets) **before** starting Streamlit:

```text
CIPLA_HISTORY_BACKEND=sqlserver
CIPLA_SQL_SERVER=<server hostname or host,port>
CIPLA_SQL_DATABASE=<existing database>
CIPLA_SQL_USERNAME=<database login>
CIPLA_SQL_PASSWORD=<database password>
CIPLA_SQL_DRIVER=ODBC Driver 18 for SQL Server
```

Install `pip install pyodbc` and **Microsoft ODBC Driver 18 for SQL Server** on the host. The service login needs permission to create/use `dbo.cipla_history` (or have an administrator pre-create it with the schema from `backend.py`). For Azure and other SQL Server hosts, enable encrypted connectivity with a valid server certificate. Windows integrated authentication is used if no SQL username is set and is only appropriate for supported Windows environments.

**SQLite and SQL Server are separate history stores.** Switching the backend does not migrate previous data: import the combined historical file into the new database using **Previous Database**, after uploading the combined master. CatBoost model files, correction memory, and combined master remain in the filesystem under `CIPLA_DATA_DIR` and must be hosted in **persistent storage**. A SQL Server connection alone does not persist those files.

**Deployment caveats:** SQL Server connectivity has not been exercised against a live server in this package. Confirm firewall access, driver availability, schema permissions, and a backup/restore procedure in staging. Enable HTTPS, secure the admin credentials and storage volume, and conduct a real-data validation regression before production use. Model agreement is a configurable confidence heuristic, **not a guarantee** of medicine identity or clinical safety.

### Universal cold-start flow

For accounts with no account-specific model, the service tries the trained combined-master `UNIVERSAL` CatBoost model; strict dynamic matching separately uses the original pharmaceutical `compatible()` gates and RapidFuzz score alongside token-alignment dynamic programming. A no-history result is automatically validated only if a real model and dynamic matcher agree on the same code, model and dynamic scores both meet the validation threshold, and the next candidate is at least 5 points below the best (or no second candidate exists). Ambiguous or low-confidence candidates remain in optional review; no review is required to download a file with unverified codes blank. **Train the Universal model before onboarding new accounts** — if no model exists, only provisional suggestions will be available.
