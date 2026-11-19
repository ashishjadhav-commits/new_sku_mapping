# Cipla SKU ML — Seven-Account Historical Learning Studio

**Upgrade of `Cipla_SKU_ML_Experiment_Generalized_Rules.zip`**, keeping the existing experimental code, legacy Epoch 1/2/3 snapshots, and the generalized pharmaceutical-specification rules. This is a **separate local Streamlit experiment**, not a change to your production Cipla SQL database.

## What is new in this ZIP

- Import **CSV, XLSX or XLS** for the current **RX master**, multiple **last-year historical mapping** files, current-month **raw SKU lists**, and human-reviewed corrections.
- Each historical account file can have **different column names**. Select its sheet, product-name column, final *verified* Cipla/RX code column, optional `item_code`, and account name. Save files from **all seven accounts** into one persistent learning history.
- Clicking **Save mappings and learn from files** appends/deduplicates history, and **automatically trains or retrains CatBoost** by default. Previous historical aliases remain available for cross-account lookups, even if you later add another account. Training only uses verified labels; raw current-month predictions do not teach themselves without review.
- Generate several RX master candidates for current-month `item_code` + `productname`, even when the raw account file provides **no other specifications**. Matching combines historical aliases, similar aliases, first-brand candidate retrieval, numeric/strength/form/pack features and a CatBoost candidate ranker.
- Show and compare optional **RX master specifications**: molecule/composition, strength, form, pack, status and division. Generalized audit rules detect obvious conflicts, such as 20 mg vs 40 mg, tablet vs inhaler, PLUS/SR/XR variants, 10S vs 6S, device differences, 120MD vs 60MD, or 5×3ML vs 1×3ML. Missing raw facts are marked unknown, **not invented**.
- Flag contradictory historical aliases (the same normalized name mapped to different codes) and codes missing from the master. **These historical rows cannot train until checked.**
- Excel export includes **one proposed code per original raw row**, all candidate options, unmatched, missing historical codes, ambiguous historical aliases and model metadata. CSV exports are also available. Source columns and text `item_code` values are retained.
- Reviewers can fill `final_verified_code` in the export and upload it under **Verified corrections**. Those reviewed corrections are added to history and cause another retraining run.
- Trained models are versioned; the current master has a fingerprint so an outdated model is not silently used with a different RX master.

## Windows — first-time installation

**Requirements:** Python 3.11–3.13 (64-bit recommended), internet access to install Python packages on first setup.

1. Unzip the project (e.g. to `D:\\Cipla_SKU_ML`).
2. Double-click **`setup_and_start.bat`**; it creates `.venv`, installs required packages and launches Streamlit.
3. On subsequent sessions, use **`start_experiment.bat`**. Alternatively use these commands in PowerShell inside the extracted `Cipla_SKU_ML_Experiment` folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run experiment_app.py
```

The app opens in your browser, usually at `http://localhost:8501`. Use the same project folder every month to retain history and models.

## Use the five UI tabs

| Tab | Upload | What to select / do |
| --- | --- | --- |
| **1 · RX master** | CSV, XLSX, XLS | Official RX/Cipla **SKU code**, **product name**, optional molecule, strength, dosage form, pack, status and division. Save master. |
| **2 · Past mappings & auto-training** | Any number of prior-account mapping files | For **every file** (or all worksheets with identical column layouts), choose account name, source product-name column and its **verified RX code** column. Leave auto-training checked, then click **Save mappings and learn from files**. |
| **3 · Current-month mapping** | Current-month account raw data | Choose `item_code` and `productname`, account and candidate count; generate and export an Excel workbook or CSV. |
| **4 · Verified corrections** | Human-reviewed Excel / CSV (e.g. Mapping_Suggestions sheet) | Reviewer fills `final_verified_code`. Upload and explicitly confirm the corrected codes are verified, then **Add verified corrections and retrain**. |
| **5 · Model & audit** | None needed | Inspect metrics, model version, and download example import templates. |

### Input examples (illustrative codes ONLY — do not treat as real RX codes)

**RX master**

| Cipla Code | Cipla Name | Strength | Form | Pack Size | Remark |
| --- | --- | --- | --- | --- | --- |
| DEMO001 | ACIVIR | | | | EXISTING PRODUCT |
| DEMO002 | ACIVIR IV 200MG | 200MG | INJECTION | 1 VIAL | EXISTING PRODUCT |

**Previous verified mapping file — Account A**

| item_code | productname | Cipla Code |
| --- | --- | --- |
| A-019 | Acivir IV | DEMO001 |

**Previous verified mapping file — Account B**

| item_code | productname | Cipla Code |
| --- | --- | --- |
| B-777 | Acivir IV 200 | DEMO001 |

**Current-month raw file — Account C**

| item_code | productname |
| --- | --- |
| C-125 | ACIVIR IV 200 |

If these **historical codes were verified**, the learner can retrieve DEMO001 for the new account despite the name differences. **But** the raw mention of `IV 200` is not verified by official product name `ACIVIR` alone. The tool flags the missing route/numeric details and requires checking the official specifications before approving the code. The real `ACIVIR IV 200MG` SKU might instead be the correct one: the tool does not claim otherwise.

## How training works (and doesn't work)

1. A mapping **label** is a historically reviewed pair `(account product name → official RX code)`. Names-only raw uploads are **not labels**.
2. History is deduplicated by `(account, product name, mapped code)` across repeated monthly files; conflicts are surfaced and excluded. Master codes missing from the current master are also excluded.
3. Training constructs correct product-to-master pairs and competing SKU pairs, extracting name similarity, strength/pack/form signals and brand similarity features.
4. A CatBoost classifier learns to **rank** candidates. Holdout evaluation splits by normalized raw product name to reduce duplicate-name leakage. Offline top-1 metrics **include the known correct code among test candidates**, so they do *not* measure real candidate recall, unseen-month accuracy, or safety. Test against real independently reviewed current-month mappings before relying on it.
5. Separately, the learner retains all trusted cross-account historical alias examples. At inference it can use exact or similar aliases from earlier accounts to retrieve SKUs not suggested by the master-name fuzzy search alone. **Explicit specification conflicts are demoted**, not silently hidden.
6. User-uploaded **verified corrections** become additional training labels. Unreviewed predictions **never** self-train, preventing feedback-loop contamination.

The ranking score is not a validated correctness probability. Product names are often under-specified; **all suggestions require human verification**. Do not execute SQL mapping updates from these suggestions without review.

## Local project files

```text
Cipla_SKU_ML_Experiment/
├── experiment_app.py            # New 5-tab Streamlit workflow
├── src/
│   ├── learning_pipeline.py     # Cross-account aliases, training, ranking, exports
│   ├── rules_engine.py          # Generalized conservative checks
│   ├── database.py / features.py / model.py  # Legacy experimental modules retained
├── tests/                       # Original tests plus multi-account regression tests
├── models/                      # Versioned CatBoost models created after training
├── .storage/
│   ├── rx_master.csv            # Created when master saved
│   └── historical_mappings.csv  # Created when history saved
├── legacy_reference/            # Original Epoch 1/2/3 snapshots and rules workbook
├── setup_and_start.bat          # First-time Windows setup + run
└── start_experiment.bat         # Run from an existing virtual environment
```

**Back up `models/` and `.storage/`**. Those contain your uploaded data and trained models; the distributed ZIP **does not include private datasets** or trainable history from previous chats. Saving a new master invalidates the old model's applicability until retrained. When an exact code has different names in the master, fix the master conflicts before saving.

## Operational cautions

- Upload **verified** historical matches, not unchecked previous fuzzy predictions. A bad verified label is more damaging than an unknown SKU.
- A few historical records may not be enough to train: the app requires at least **5 distinct unambiguous raw names**. More diverse, accurate historical data generally improves coverage but does not guarantee accuracy.
- Duplicate alias-to-different-code collisions across accounts are excluded because they cannot safely be applied to another account without additional identity evidence.
- Codes are read as strings. If Excel has already changed a code (for example stripped leading zeroes or rounded a long numerical code), set the source column to **Text in Excel before exporting** and verify the codes.
- Workbook exports have Excel's approximately 1.05-million-row limit per sheet; for large outputs use the per-sheet CSV downloads or split batches.
- Master specification data improves safety checks, but it cannot reveal dosage, composition, pack or device attributes that are **absent from the raw product name**. Do not force a match when the product is not reliably identifiable.
- The app **does not connect to SQL Server**, automatically publish mapped data, or guarantee drug equivalence.

## Test the packaged code

```powershell
python -m pytest -q tests
```
