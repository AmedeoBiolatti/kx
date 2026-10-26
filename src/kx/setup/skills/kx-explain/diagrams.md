# Mermaid patterns for kx explanations

Mermaid renders in GitHub, most Markdown viewers and many terminals. Keep labels short and quoted (`A["..."]`):
unquoted parentheses, brackets, `#` and `;` break the parser. One idea per diagram.

## Lineage: how we got here

Runs oldest first, left to right. A node: the run id, what it changed, its score under the reference protocol. An
edge: the change, when it is not in the node. Failed runs and dead ends are styled apart; inferred links are dashed.

```mermaid
flowchart LR
  r12["r0012 baseline LightGBM<br/>0.781 ± 0.004"]
  r15["r0015 + target encoding<br/>0.792 ± 0.004"]
  r18["r0018 + 5 seeds<br/>0.795 ± 0.003"]
  r21["r0021 deeper trees<br/>failed: OOM"]
  r24["r0024 + user features<br/>0.803 ± 0.003"]
  r12 -->|features| r15 --> r18 -->|"features (leak fixed)"| r24
  r15 -.->|inferred| r21
  classDef best fill:#fde68a,stroke:#b45309,color:#111
  classDef failed fill:#fee2e2,stroke:#b91c1c,color:#111
  classDef dead fill:#e5e7eb,stroke:#6b7280,color:#374151
  class r24 best
  class r21 failed
```

Many runs: keep the main line, and collapse side branches into one node (`side["6 speed runs, no change in
score"]`).

## Pipeline: how it works

From the code at the run's commit. Nodes are steps with the file that does them; edges carry what flows.

```mermaid
flowchart TD
  data[("data/train.parquet")] -->|"rows by user"| folds["folds: src/comp/folds.py<br/>5 by user, fixed seed"]
  folds --> feats["features: src/comp/features.py<br/>412 columns"]
  feats --> gbm["LightGBM: scripts/train.py<br/>5 seeds"]
  feats --> nn["MLP: scripts/train_mlp.py"]
  gbm --> blend["blend 0.7 / 0.3"]
  nn --> blend
  blend --> sub[/"submission.csv"/]
```

## Learning map: what we know and why

Tasks lead to findings; findings rest on runs. Withdrawn or superseded findings are dead ends.

```mermaid
flowchart LR
  T7["T7 leakage audit"] --> F3["F3 fold 3 leaks through user_id"]
  F3 --> r15["r0015"] & r16["r0016"]
  T9["T9 bigger model"] --> F5["F5 depth beyond 8 overfits"]
  F5 --> r21["r0021"]
  F2["F2 seeds matter more than depth<br/>superseded by F5"]:::dead
  classDef dead fill:#e5e7eb,stroke:#6b7280,color:#374151
```

## Timeline: where the time went

When the person asks about effort or pace. `gantt` dates come from runs' `created`/`finished` and tasks' notes.

```mermaid
gantt
  dateFormat YYYY-MM-DD
  axisFormat %b %d
  section T64 bigger model
  XL training, 4 restarts after reboots :2026-10-22, 2d
  speed work on the trained model      :2026-10-24, 1d
  section T79 losses
  shared-query diagnosis               :2026-10-23, 2d
```
