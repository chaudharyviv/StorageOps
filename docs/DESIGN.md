# StorageOps Intelligence Platform — Final Design

Nov 9, 2026 · @Vivikt

## Goal and scope

StorageOps is a NetApp-only, end-to-end AI system that forecasts ONTAP storage problems, explains them in plain language, and proposes fixes through an approval gate. It runs entirely on synthetic data and demonstrates the full loop: data, models, evaluation, serving, a governed agent, drift monitoring and retraining.

**In scope**

- ONTAP objects: aggregates, FlexVol volumes, snapshots, SnapMirror, QoS policies, storage efficiency. Trident PVCs are an extra and are simulated; the project does not claim validation against a real Trident install.
- Synthetic telemetry served by a mock ONTAP REST API, plus Harvest-style Prometheus metrics.
- A forecasting and anomaly model, a QLoRA fine-tuned incident summarizer, an eval harness with a CI promotion gate, an MCP server, a LangGraph triage agent, an approval and audit gate, and a drift-triggered retrain workflow.

**Out of scope**

- Dell EMC ECS, VMAX and any non-NetApp vendor.
- Real customer or bank data, and any real write action against an array (proposals only).
- Model pretraining and production high availability.

**Success criteria**

1. One click injects a failure and the dashboard shows forecast, explanation and proposed action for it.
2. A model that regresses on the golden incident set cannot be promoted: CI blocks it.
3. Drift or an eval drop triggers the retrain workflow.
4. The agent cannot change state without a recorded approval.
5. A reviewer can reproduce everything locally with one `docker compose up`.

The public README states plainly that all data is synthetic.

## Architecture

&#91;embedded content: architecture · 4 layers, 1 retrain loop\]

Data flows down the four layers. Every state-changing proposal stops at the approval gate, and the drift monitor sends a retrain trigger back to training. The dashboard and the agent both reach the system only through the API.

## Components

Twenty components, each with one job and a contract with its neighbours, so Claude Code can build and test them one at a time. Phase numbers refer to the build plan below.

| # | Component | Job | Tech | Phase |
| --- | --- | --- | --- | --- |
| 1 | Telemetry simulator | Generates ONTAP-style metrics and injects labelled failures | Python, NumPy | 1 |
| 2 | Mock ONTAP REST API | Serves volumes, aggregates, SnapMirror and QoS in the shape of NetApp's public ONTAP REST documentation (a documented subset), backed by the simulator | FastAPI | 1 |
| 3 | Scenario library | Failure definitions kept apart from the generator, with a held-out set | YAML + Python | 1 |
| 4 | Ingest and validate | Moves metrics into the store and checks schema, ranges and gaps | File or Redis queue, Great Expectations | 1 |
| 5 | Features and store | Growth rates, time-to-full, snapshot ratio, latency percentiles, same code for training and serving | DuckDB, Polars | 1 |
| 6 | Forecaster and anomaly model | Predicts aggregate or volume exhaustion and flags anomalies | LightGBM or Prophet | 1 |
| 7 | Experiment tracking and registry | Runs, metrics, model versions, promote or reject status | MLflow (SQLite backend) | 1 |
| 8 | Dashboard | Shows the live scenario, forecast, explanation and approval queue | Streamlit | 1 |
| 9 | Golden incident set | Hand-written incidents with expected summaries and runbook steps | JSONL | 2 |
| 10 | QLoRA summarizer | Turns alert plus context into an incident summary and runbook steps | Hugging Face PEFT/TRL, small open model | 2 |
| 11 | Eval harness | Scores forecaster and summaries; compares fine-tuned, prompted and RAG baselines | Ragas, LLM-as-judge, custom metrics | 2 |
| 12 | CI promotion gate | Blocks a model from promotion if it regresses on the golden set | GitHub Actions | 2 |
| 13 | App and model API | Single front door for models, agent and approvals | FastAPI, llama.cpp or hosted API | 3 |
| 14 | Knowledge base | Self-written ONTAP runbooks for retrieval, used as a comparison baseline | pgvector or Qdrant | 3 |
| 15 | MCP storage server | Exposes ONTAP-style tools to the agent | MCP Python SDK | 3 |
| 16 | Triage agent | Reads an alert, calls tools, explains, proposes an action | LangGraph | 3 |
| 17 | Approval and audit gate | Every state-changing proposal needs sign-off and is logged | RequestGuard-style module | 3 |
| 18 | Observability | Traces, cost, latency, service health | Langfuse, Prometheus, Grafana | 3 |
| 19 | Drift monitor and retrain trigger | Detects data and model drift, starts the retrain workflow | Evidently, scheduled GitHub Actions | 3 |
| 20 | Platform and docs | Compose stack, Helm chart, kind and Argo CD demo, README, threat model | Docker Compose, Helm, Argo CD | 3 |

## Data contracts

Five records carry everything between components. They live in one shared `contracts/` package of Pydantic models that every component imports, and Claude Code must never redefine them locally.

| Record | Key fields | Written by | Read by |
| --- | --- | --- | --- |
| MetricRecord | ts, cluster, object\_type (aggregate, volume, snapmirror, qos, trident\_backend), object\_id, metric, value, unit | Simulator, ingest | Features, drift monitor |
| Alert | alert\_id, ts, object\_id, source (rule or model), severity, evidence window | Forecaster, anomaly model | Agent, dashboard |
| Incident | incident\_id, alert\_ids, summary, runbook\_steps, model\_version, retrieved\_docs | Summarizer, agent | Dashboard, eval harness |
| ProposedAction | action\_id, incident\_id, type, target, params, risk, status (proposed, approved, rejected, expired) | Agent through MCP | Approval gate, dashboard |
| AuditRecord | audit\_id, ts, actor, action\_id, decision, reason, prev\_hash | Approval gate | Dashboard, reviewers |

Three rules keep the contracts honest:

1. **Labels live apart from metrics.** The scenario label for a failure is stored in a separate table that features and models cannot read. Only the eval harness joins it.
2. **ONTAP shape.** The mock API uses ONTAP REST field names (uuid, name, space.size, space.used, svm.name, and so on). Field names come only from NetApp's public ONTAP REST API documentation, and the mock is documented as a subset, not a full ONTAP emulator. Fixtures in tests/fixtures/ are hand-written from that documentation.
3. **Audit chain.** Each AuditRecord stores the hash of the previous one, so edits to history are detectable.

## NetApp scenarios

Eight ONTAP failure types drive training and testing. Six are used for training and validation; the last two are never seen in training, so they test whether the system handles failure types it was not built around.

| # | Scenario | What it simulates | Main signals | Typical proposal | Split |
| --- | --- | --- | --- | --- | --- |
| 1 | Aggregate filling | Steady volume growth on one aggregate | Aggregate used %, growth rate, time-to-full | Move or resize volumes | Train |
| 2 | Snapshot bloat | High change-rate volume eats its snapshot reserve | Snapshot-to-active ratio, snapshot reserve used | Adjust snapshot policy, clean up old snapshots | Train |
| 3 | Autogrow ceiling | Volume hits its autogrow maximum while the aggregate still has space | Volume used % at max size | Raise autogrow limit | Train |
| 4 | Latency storm | Noisy neighbour on a shared QoS policy | Read and write latency p95, IOPS, QoS throughput | Review QoS ceiling or move workload | Train |
| 5 | SnapMirror lag creep | A replication relationship falls behind its RPO | Lag seconds, transfer errors | Resync, review schedule | Train |
| 6 | Efficiency regression | Dedupe and compression savings drop after a workload change | Efficiency ratio, physical versus logical used | Review efficiency settings | Train |
| 7 | Ransomware-like change pattern | Sudden spike in change rate and data entropy | Change rate, entropy proxy, snapshot growth | Raise alert and propose isolation steps for a human | Held out |
| 8 | Trident PVC burst | Many PVCs provisioned quickly against one backend | PVC count rate, backend capacity | Throttle provisioning, add capacity | Held out |

Each scenario has three parameter variants (mild, moderate, severe). One variant per training scenario is also held back for validation. Scenario 7 is an anomaly demonstration only and does not claim to reproduce any NetApp product behaviour. Scenario 8 is simulated, and the README says so. It is the only scenario that uses the trident\_backend object type.

## ML and evaluation

The forecaster is kept simple and reliable; the fine-tuned summarizer is the differentiator, and it has to beat cheaper alternatives to earn its place.

**Forecaster and anomaly model.** LightGBM or Prophet on per-object features, predicting time-to-full and an anomaly flag. A linear-extrapolation baseline is always reported next to it. Metrics: error in days to full, warning lead time before the event, and false-alarm rate.

**Summarizer.** QLoRA on a small open model of roughly 3B parameters, trained on a free Colab or Kaggle GPU and pushed to Hugging Face. Training pairs are alert-plus-context to summary-plus-runbook-steps, generated from templates and reviewed by hand.

**Three-way comparison.** The fine-tune is reported against two cheaper options, so the README can say whether it was worth it.

| Variant | What it is | Reported on |
| --- | --- | --- |
| A. Prompted | A hosted model with a good prompt, no retrieval | Quality, cost, latency |
| B. Prompted + RAG | Variant A plus retrieval over self-written runbooks | Quality, cost, latency |
| C. Fine-tuned | The QLoRA model, with or without retrieval | Quality, cost, latency |

**Scoring.** A rubric on the golden set checks that the summary names the right object, the right cause and an allowed runbook step. Ragas faithfulness measures groundedness. A separate counter tracks proposals for actions outside the allowed list, which must be zero.

**CI gate.** A model is promoted only if it does not regress on the golden set against the current registry version and produces zero invalid actions. Thresholds are set from the first baseline run and kept in a versioned `eval/thresholds.yaml`.

**Rules that prevent circular evaluation**

1. The golden incident set and the held-out scenarios are written by hand, outside the simulator code, and never appear in training data.
2. Splits are by scenario type and by time, never by random row.
3. The judge model differs from the model being scored.
4. Simulator parameters never appear in prompts or features.
5. Where public infrastructure traces exist, the forecaster is also checked on them, and the README reports both results separately.

## Agent and governance

The agent can read everything and change nothing. It can only propose actions, and a human approves them in the dashboard before anything is applied to the mock array.

**MCP tools exposed to the agent**

| Tool | Kind | What it does |
| --- | --- | --- |
| get\_volume, get\_aggregate\_capacity | Read | Current space and performance for an object |
| get\_forecast | Read | Time-to-full and interval from the forecaster |
| list\_snapmirror\_lag | Read | Replication lag per relationship |
| list\_incidents | Read | Recent incidents with summaries |
| propose\_volume\_resize | Propose | Creates a ProposedAction; changes nothing |
| propose\_snapshot\_cleanup | Propose | Creates a ProposedAction; changes nothing |
| propose\_snapmirror\_resync | Propose | Creates a ProposedAction; changes nothing |

**Approval and audit gate**

1. The agent holds no credentials that can write to the mock array; only the gate's executor does.
2. A ProposedAction must match an allowed action type and stay within configured parameter bounds, or it is rejected before a human sees it.
3. A human approves or rejects it in the dashboard. Unanswered proposals expire.
4. Every decision writes a hash-chained AuditRecord. Approved actions run through the executor, which logs the result.
5. High-risk action types can require two approvers, reusing the quorum idea from the RequestGuard project.

**Threat model, short form**

- Alert text, volume names and runbook content are untrusted input and may contain instructions. Tool results are treated as data, never as commands.
- The agent cannot widen its own tool list or action allow-list.
- A model that proposes anything outside the allow-list fails the CI gate.
- All data is synthetic, and the README says so.

## Build phases

The build takes about ten weeks alongside a full-time job, in three phases. Each phase ends in something demonstrable and publishable as its own blog post.

| Phase | Weeks | Deliverables | Done when |
| --- | --- | --- | --- |
| 0. Foundations | Week 1 | Create the repo, the `contracts/` package, the Makefile and CI, and the Compose skeleton. Hand-write ONTAP REST fixtures in `tests/fixtures/` from NetApp's public API documentation. | Contracts import cleanly everywhere and CI is green. Fixtures list only fields found in the public documentation. |
| 1. Working core | Weeks 1 to 3 | Simulator, mock ONTAP API, scenario library (1 to 6), ingest, features, forecaster, MLflow, Streamlit dashboard | Clicking a scenario shows a forecast on the dashboard, and the forecaster beats the linear baseline on the validation variants. |
| 2. Intelligence | Weeks 4 to 6 | Golden incident set, QLoRA summarizer, eval harness with the three-way comparison, CI promotion gate | CI blocks a deliberately worse model, and the comparison table is in the README. |
| 3. Platform | Weeks 7 to 10 | API, MCP server, triage agent, approval and audit gate, Langfuse, Prometheus and Grafana, Evidently drift, retrain workflow, held-out scenarios 7 and 8, kind and Argo CD demo, Streamlit and Render live demo | Injected drift triggers retraining, no state change happens without a logged approval, and the recorded demo runs end to end. |

**Cut order if time runs short.** Drop in this order: Argo CD, the Trident scenario, the two-approver option, the SnapMirror and efficiency scenarios, the RAG baseline. Never cut the CI gate, the approval gate or the drift-to-retrain loop, since those are what the project is for.

## Hosting and testing

Everything runs locally with Docker Compose; a slim live demo runs on free hosting. Oracle Cloud is not used, because its Always Free Arm allowance was cut to 2 OCPUs and 12 GB and CPU-only inference would make the demo the weakest part.

| Piece | Where it runs |
| --- | --- |
| Full stack (simulator, features, MLflow, Langfuse, Prometheus, Grafana, Evidently, agent, MCP) | Laptop, Docker Compose in `deploy/local/` |
| Dashboard | Streamlit Community Cloud |
| API (mock ONTAP, forecaster, agent and approval endpoints) | Render free web service, embedded DuckDB or SQLite file, telemetry generated on demand |
| QLoRA fine-tune | Free Colab or Kaggle GPU, model pushed to Hugging Face |
| Live LLM | Hugging Face Space or a hosted API; the fine-tune comparison is shown as static results |
| Scheduled jobs (drift check, eval gate, retrain trigger) | GitHub Actions |
| Kubernetes, Argo CD, Trident angle | Local kind cluster plus a recorded video, not hosted |

**Render free limits to design around.** A free web service spins down after 15 minutes without traffic and takes about a minute to wake, with local files lost. Workers, cron jobs and persistent disks are not free, and the free Postgres expires after 30 days. So the demo uses an embedded database, generates telemetry on demand, shows a "waking the API" message, and leaves scheduled work to GitHub Actions.

**Kubernetes and Argo CD, tested locally.** Create a kind cluster, install Argo CD into an `argocd` namespace, port-forward its UI, and point an Application at the Helm chart in `deploy/k8s/` in the public repo. The demo changes a value in Git and watches Argo sync, then edits the live deployment and watches Argo restore it.

**Trident, two honest levels**

| Level | How | What the README may claim |
| --- | --- | --- |
| Simulated (default) | Mock ONTAP plus simulated PVC events, with a local storage class standing in for Trident in kind | Designed for Trident-backed clusters, simulated in the demo |
| Real Trident (not claimed) | A real Trident install or any lab validation, which this project does not do | Nothing; the README says Trident is simulated |

All ONTAP behaviour comes from NetApp's public documentation and the mock is described as a subset, so the README never claims validation against a real array.

## Repo, risks and working rules

**Repo layout.** One directory per component, with shared contracts at the root.

```text
storageops/
  contracts/        shared Pydantic models (the five records)
  simulator/        generator, scenarios/, mock_ontap_api/
  ingest/           queue, validation
  features/         feature code used by training and serving
  models/           forecaster/, summarizer/ (configs, training scripts)
  eval/             golden_set/, harness, thresholds.yaml
  api/              FastAPI front door
  mcp_server/       ONTAP-style tools
  agent/            LangGraph triage agent
  governance/       approval gate, executor, audit chain
  ui/               Streamlit dashboard
  monitoring/       Evidently jobs, Prometheus and Grafana config
  deploy/           local/ (Compose), demo/ (Render, Streamlit), k8s/ (Helm)
  tests/fixtures/   captured or hand-written ONTAP REST responses
  docs/             architecture, threat model, cost notes
  .github/workflows/  CI, eval gate, scheduled drift and retrain
  CLAUDE.md  README.md
```

**Risks and mitigations**

| Risk | Mitigation |
| --- | --- |
| Circular evaluation: the model learns the simulator | Hand-written golden set, held-out scenario types, type and time splits |
| Scope creep over ten weeks | Phase deliverables are independent; follow the cut order |
| No GPU for the fine-tune | Free Colab or Kaggle session, small model, QLoRA |
| Mock drifts from real ONTAP behaviour | Build only from NetApp's public REST documentation, document the mock as a subset, and describe Trident as simulated |
| Render cold starts spoil the demo | Waking message, recorded demo video as the fallback |
| Heavy self-hosted observability stack | Self-host locally only; use hosted free tiers for the live demo |
| Synthetic data looks unrealistic | Match the ONTAP REST shape to the public documentation and explain the limits in the README |

**Working rules for Claude Code.** Put these in `CLAUDE.md`.

1. Read `contracts/` first and never redefine a record locally.
2. Build one component per session, with tests, and run the full test command before finishing.
3. Never write or edit the golden set or the held-out scenarios; the author writes them.
4. The agent package must not import the executor or any write credentials.
5. No real data, no secrets in the repo, and every example is synthetic.
6. Keep commits small, one component or one fix each.

**README and demo plan.** The README opens with the architecture diagram and a two-minute demo video: inject an aggregate-filling scenario, show the forecast, the plain-language explanation, a proposed action, the approval, and the audit record. It then covers the three-way comparison table, the CI gate, the threat model, cost notes, a glossary of NetApp terms, and a short section on what would change for a real bank or storage customer.
