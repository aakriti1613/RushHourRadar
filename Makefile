# Delhi Crowding Planner - reproducible pipeline (v8).
# Use the project virtualenv:  python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
PY ?= .venv/Scripts/python
.PHONY: verify all load eda match graph credits eval model validate test run demo

verify:
	$(PY) -c "import hashlib; [exit(1) for l in open('SHA256SUMS') if l.strip() and hashlib.sha256(open(l.split()[1], 'rb').read()).hexdigest() != l.split()[0]]; print('SHA256 verification passed.')"

all: load eda match graph credits eval model validate

load:     ; $(PY) etl/00_load.py
eda:      ; $(PY) etl/01_eda_study.py
match:    ; $(PY) etl/02_match.py
graph:    ; $(PY) gtfs/build_graph.py
credits:  ; $(PY) model/03_credits.py
# held-out evaluation: selects prior strength and range calibration, writes outputs/eval/summary.json
eval:     ; $(PY) validation/eval_holdout.py
# final estimates (reads the selected hyper-parameters from outputs/eval/summary.json)
model:    ; $(PY) model/04_posterior.py
validate: ; $(PY) validation/05_validate.py
test:     ; $(PY) -m pytest tests -q
run:      ; $(PY) -m uvicorn api.main:app --host 0.0.0.0 --port 8000

# one-week smoke run
demo:
	$(PY) etl/00_load.py --only 2026-10-12:2026-10-18
	$(PY) etl/02_match.py
	$(PY) gtfs/build_graph.py
	$(PY) model/03_credits.py
	$(PY) model/04_posterior.py
