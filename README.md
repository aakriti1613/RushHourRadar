<div align="center">

# 🚌 Rush Hour Radar

### Know the crowd before you board.

**A Delhi bus planner that shows not just *how long* a trip takes, but *how crowded the bus will be*.**

![Python](https://img.shields.io/badge/Python-3.13-1f2a44?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-0072B2?style=flat-square)
![Tests](https://img.shields.io/badge/tests-32%20passing-009E73?style=flat-square)
![Data](https://img.shields.io/badge/tickets-2.56M-E69F00?style=flat-square)
![Not live](https://img.shields.io/badge/typical%20day-not%20live-D55E00?style=flat-square)

[🌐 **Live website**](YOUR_WEBSITE_LINK_HERE) · [🎬 **Demo video**](YOUR_DEMO_VIDEO_LINK_HERE) · [📄 **Technical note**](docs/technical_note.md)

*Built for Transport Stack Hackathon 2026 · Problem 2 · Phase 1*

</div>

---

## 📸 See it

<!-- Add screenshots to a docs/images folder, then replace the paths below -->

| Plan a trip | Crowding on a route |
|:---:|:---:|
| ![Plan page](docs/images/plan.png) | ![Routes page](docs/images/routes.png) |
| **For bus operators** | **Proof it works** |
| ![Operators page](docs/images/operators.png) | ![Evidence page](docs/images/evidence.png) |

---

## 💡 The idea in one minute

Every journey planner tells you the fastest bus. None tells you the bus will be packed.

Delhi records every bus ticket. Each ticket says **where and when someone got on, and where they plan to get off**. If we add up those tickets, we can work out **how many people are usually on a bus, on every stretch, at every time of day**.

Rush Hour Radar turns that into three simple levels:

| | Level | Meaning |
|:-:|---|---|
| 🔵 | **Seats available** | You can sit |
| 🟡 | **Standing room** | You will probably stand |
| 🟠 | **Crowded** | Packed. Think twice |

Every colour also has an icon and a word, so it works for colour-blind users too.

---

## ✨ What you can do

**🧭 Commuters**
- Type where you are going and when. See every bus option with its crowding level, stretch by stretch.
- Pick **Fastest**, **Balanced** or **Comfortable**.
- See a **"Best time to leave"** strip: how crowded your bus is if you go earlier or later.
- No direct bus? It finds a trip with **one change**.

**🗺️ Anyone curious**
- Open any route and see a heat map of crowding along the route through the whole day.

**🏢 Bus operators**
- Find the most overloaded routes and hours.
- See **where one extra bus helps most**, and test "what if we add 2 buses?".
- Spot buses that run nearly empty.

**🔬 Judges and skeptics**
- A full "Method and evidence" page showing how well the model works, with honest limits.

---

## 🧠 How it works (in plain words)

1. **Match tickets to buses.** Each ticket's stop names are matched to the official bus schedule (GTFS).
2. **Count riders on each stretch.** A ticket means the rider sits on the bus from the boarding stop to the stop they get off. Since people sometimes get off a stop early or late, we spread it a little (15% / 70% / 15%).
3. **Divide by the buses that ran.** More riders per bus means more crowded.
4. **Fix thin data.** Some routes and hours have very few tickets. We borrow strength from similar routes and times, using a standard statistical method (Gamma-Poisson).
5. **Scale up to real people.** Only about **1 in 40** riders shows up in the tickets. So we multiply, using Delhi's published daily ridership.
6. **Cap the impossible.** If the numbers say 300 people are on one bus, we limit it to a believable maximum.
7. **Show a simple level.** Seats, standing or crowded, plus a note on how much evidence we have.

---

## 📊 Results, in easy language

We tested the model like an exam: **hide one whole week, train on the others, then check how well it guesses the hidden week.** We did this for four weeks.

### 🎯 How good are the guesses?

| Model | Error score (lower is better) | Does it rank busy vs quiet correctly? (busy cells) |
|---|:-:|:-:|
| Simple route-and-hour average | 0.424 | 45% |
| Raw ticket counts | 0.401 | 82% |
| Our earlier version (v7) | 0.229 | 76% |
| **Rush Hour Radar (v8)** | **0.212** ✅ | **82%** ✅ |

- **The honest summary:** the new model is the best of the group. Against our earlier version the gain is real but small.
- It agrees with the hidden week's crowding level (seats / standing / crowded) **about 90% of the time**.

### 🎚️ Can you trust the "usual range"?

When we say "usually 30 to 60 people", the true number should fall inside that range about **80%** of the time.

| How many tickets behind the estimate | Earlier version | **Now** |
|---|:-:|:-:|
| Under 10 | 88% | **92%** |
| 10 to 29 | 72% | **80%** |
| 30 to 99 | 62% | **81%** |
| 100 or more | 🔴 **52%** | **81%** ✅ |

The earlier version was overconfident on busy routes. This is fixed.

### ✅ Does it match what we know about Delhi?

| Check | Result |
|---|---|
| Morning rush vs midday | **5.9 times** more crowded |
| Evening rush vs midday | **3.9 times** more crowded |
| Sunday vs weekday travel | **Half** (49%) |
| Saturday vs weekday travel | **79%** |
| Tickets matched to the bus schedule | **93.6%** of passengers |
| Fares rise with distance | ✅ (correlation 0.79 to 0.83) |
| Busy hubs show up | Nehru Place 6th, Anand Vihar 13th, Dhaula Kuan 18th |
| Journey planner checks | **9 of 9** test trips pass, in about **5 ms** |

### 🧪 What we tried and dropped

Two "smart" ideas made predictions **worse**, so we removed them: smoothing across time, and using one city-wide prior for everything. We kept only what the test proved.

---

## ⚠️ Honest limits (please read)

We would rather tell you than hide it.

- **Only 1 in 40 riders is in the data.** Exact headcounts are estimates. Rankings and crowding levels are more reliable than exact numbers.
- **Ticket use is uneven across routes.** On 678 routes (84% of ticketed riders), tickets imply more people than a bus can hold, so we cap them. After the cap, our total is about **54% of Delhi's published ridership**. Be careful reading exact headcounts there.
- **No real on-board counts exist,** so we could not check absolute loads against the truth.
- **Typical day, not live.** It does not know about delays, bunching or cancelled buses.
- **Weekends have little data** (4 to 5 days each).
- **All buses are treated as full-size 12 m buses.** Small 9 m buses need the operator's fleet list.
- **Thin routes** (late night, feeder buses) show *"Too few tickets for a reliable headcount."*
- **October only.** No festivals or monsoon.

Full details are in the [technical note](docs/technical_note.md).

---

## 🚀 Run it yourself

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn api.main:app --port 8000
```

Open **http://localhost:8000**. The first start takes about 40 seconds while the model loads.

Prefer the live version? 👉 **[YOUR_WEBSITE_LINK_HERE](YOUR_WEBSITE_LINK_HERE)**

### Rebuild everything from raw data

Put `October-Delhi.csv.zip` in `data/raw/` and the GTFS files in `data/raw/gtfs/`, then:

```powershell
make verify   # check the input files
make all      # load, match, model, test it, validate
make test     # run the 32 tests
```

| Step | What it does |
|---|---|
| `etl/` | Loads tickets and matches stops to routes |
| `gtfs/` | Builds route patterns, run times and trips per half hour |
| `model/` | Counts riders, estimates crowding, builds the operator table |
| `validation/` | The "hide a week" exam and the known-pattern checks |
| `router/` | Finds bus options with crowding |
| `api/` + `web/` | The backend and the website |

Everything is set in one file: `config.yaml`.

---

## 🗂️ What is where

```
api/         FastAPI backend (also serves the website)
web/         The website (HTML, CSS, JavaScript)
model/       The crowding model
router/      The journey planner
crowding/    Query the estimates from code
validation/  Tests of accuracy and known patterns
tests/       32 automated tests
docs/        Technical note, review, change log
outputs/     All result files the website shows
```

---

## 🎬 Demo

<!-- Replace with your video / GIF -->
**Video:** [YOUR_DEMO_VIDEO_LINK_HERE](YOUR_DEMO_VIDEO_LINK_HERE)

**Try this:** Anand Vihar → Connaught Place, weekday, 6 PM.

---

## 👥 Team

<!-- Add names and links -->
| Name | Role | Link |
|---|---|---|
| YOUR_NAME | | |
| YOUR_NAME | | |
| YOUR_NAME | | |

---

<div align="center">

**Rush Hour Radar** · Transport Stack Hackathon 2026 · Data: Delhi Transport Stack (October 2026 e-tickets, static GTFS)

*Crowding is a model estimate of a typical day, not a live measurement.*

</div>
