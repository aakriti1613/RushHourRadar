# v8 change log

Backup of the pre-v8 code (excluding raw/interim data): `backups/pre_v8_code.zip`; v7 reports are preserved in `docs/legacy_v7/` and `outputs/legacy_v7/`.

## Added
| File | Purpose |
|---|---|
| `model/crowd_model.py` | Vectorised v8 model shared by the pipeline and the evaluation harness |
| `validation/eval_holdout.py` | Leave-one-week-out evaluation: baselines, v7 reproduction, ablations, prior-strength selection, range calibration, split-half reliability |
| `api/main.py`, `api/__init__.py` | FastAPI backend wrapping the router and Parquet outputs |
| `web/index.html`, `web/assets/app.js`, `web/assets/styles.css` | New interface: Plan, Routes, Operators, Method & evidence |
| `tests/test_api.py` | API tests |
| `docs/CRITICAL_REVIEW_AND_ROADMAP.md`, `docs/CHANGELOG_v8.md` | Review and this log |
| `outputs/eval/*`, `outputs/model_report.json`, `outputs/validation_v8.json` | Evidence |

## Rewritten
| File | Change |
|---|---|
| `model/04_posterior.py` | v8 model: learned shrinkage, NB2 ranges, capacity-capped κ per route, route capacity from fleet mix hook, weekend pooling, distinct-ticket evidence, vectorised (≈ 25 s) |
| `validation/05_validate.py` | J1–J6 computed from data and held-out results; no self-referential pass flags |
| `tests/test_posterior.py`, `tests/test_router.py`, `tests/test_operator.py` | Test the real code and outputs (32 tests) |
| `README.md`, `docs/technical_note.md`, `Makefile`, `requirements.txt`, `config.yaml` | Updated to v8 (pinned versions) |

## Modified
| File | Change |
|---|---|
| `router/router.py` | Removed the hard-coded city profile and the router-side re-shrinkage; capacity and tier from the model; "No estimate" state; time shifts only if strictly better tier; comfort penalty includes P(crowded) and a Low-evidence penalty; distinct-ticket confidence text; worst-stretch trust; one-change journeys |

## Moved (superseded v7 claims)
`COMPREHENSIVE_RESULTS_AND_PLAN.md`, `additions.md` → `docs/legacy_v7/`; `calibration_report.json`, `validation_j1_j6_report.json` → `outputs/legacy_v7/`.

## Removed
`app/` (the old Streamlit interface). Recoverable from `backups/pre_v8_code.zip`.

## Known gaps
Bus-size mapping needs the fleet list; capture is non-uniform across routes (see the technical note); kernel/bin/timestamp sensitivity not re-run; journeys with more than one change are not planned.
