# Which models to download for teaching a coder model

Written for **this rig**: RTX 5060 Ti (16 GB) + RTX 3060 (12 GB) = 28 GB of VRAM
that can be split across two cards, Ollama already installed. Everything below
is judged on one question: *can it actually generate enough good training data
here to make a small coder model better?*

The radar keeps a live version of this list — every run scores new Hugging Face
models with a **teacher fitness 0–10** and a VRAM plan. This file is the
baseline you start from.

---

## The three roles you are staffing

Distillation is not one model. You need three, and they should not be the same
model, or the student just inherits the teacher's blind spots.

| Role | What it does | Size that makes sense here |
|---|---|---|
| **Teacher** | Generates reasoning traces, solutions, and rewrites — the raw training data | 27–32B, or an API model for the hard slice |
| **Student** | The thing you are actually improving | 7–14B dense |
| **Verifier / judge** | Rejects bad traces before they poison the dataset | 20B, different family from the teacher |

The verifier matters more than people expect. Unfiltered teacher output is the
single most common reason a distilled coder model gets *worse* at the thing you
were trying to improve.

---

## Tier 1 — pull these first

### `qwen3-coder:30b` — primary local teacher
A 30B mixture-of-experts with roughly 3.3B active parameters. The active-param
count is the whole point: it generates at the speed of a small model while
reasoning like a large one, which is exactly the trade you want when you need
100k+ traces rather than 100 great answers. Best quality-per-GB in the
consumer-GPU range.

```bash
ollama pull qwen3-coder:30b
```

VRAM: ~18 GB at Q4_K_M. That does not fit the 16 GB card alone — split it
across both cards, or run Q4 with a few layers on CPU. Check it with:

```bash
ollama run qwen3-coder:30b "write a python lru cache with ttl" --verbose
```

### `deepseek-r1-distill-qwen:32b` — reasoning-trace teacher
Already a distilled model, which makes it a strange but effective teacher: its
outputs *are* worked reasoning traces produced by a model that learned through
RL, so you are copying a format that is known to transfer. Reported ~57 on
LiveCodeBench. Use it for the "explain the fix step by step" half of your
dataset, not for raw code completion.

```bash
ollama pull deepseek-r1:32b
```

### `gpt-oss:20b` — verifier / judge *(you already have this)*
Keep it in the verifier seat and never in the teacher seat. Different family
from Qwen means its failure modes do not correlate with your teacher's, which
is the entire value of a judge.

---

## Tier 2 — worth having

| Model | Pull | Why |
|---|---|---|
| **Gemma 4 26B A4B** | `ollama pull gemma4:26b` | Another architecture family; good second opinion for judging, and a decent teacher for docs/comment generation |
| **Qwen3-Coder-Next** | HF weights | Feb 2026 agentic-coding release; ~70–71% SWE-bench with agent scaffolds. Heavier than the 30B — worth it only if you are distilling *agentic* behaviour (multi-step tool use), not single-turn completion |
| **Qwen2.5-Coder 14B** *(you have it)* | — | Your **student**. Dense, 9 GB, trains with QLoRA on the 16 GB card |
| **Qwen2.5-Coder 7B** *(you have it)* | — | Faster student for iterating on the pipeline before you commit GPU-days |

---

## Tier 3 — do not try to run these locally

`DeepSeek-V4` (≈80.6% SWE-bench Verified, MIT), `GLM-5.2`, `Kimi K2.6`,
`MiniMax M3`. These are the actual frontier of open weights, and none of them
fit in 28 GB.

The honest play: **rent the teacher, own the student.** Generate 20–50k of your
hardest traces against DeepSeek-V4 through an API for a few dollars, keep those
as your gold set, and use `qwen3-coder:30b` locally for the bulk volume. A
mixed dataset — a small hard slice from a frontier teacher plus a large easy
slice from a local one — consistently beats either alone.

MIT licensing on DeepSeek-V4 is what makes this legal to do; check the license
tag before you distil from anything, because several strong open-weight models
restrict training other models on their outputs. The radar flags the license on
every model it surfaces for exactly this reason.

---

## The pipeline these models plug into

```
seed tasks ──▶ TEACHER (qwen3-coder:30b)  ──▶ traces
                        │                        │
                        │                   VERIFIER (gpt-oss:20b)
                        │                        │
                  hard slice                 reject / keep
                (API frontier model)             │
                        └──────────┬─────────────┘
                                   ▼
                        SFT / GRPO on STUDENT (qwen2.5-coder:14b)
                                   ▼
                          eval: LiveCodeBench + your own repos
```

Tooling worth installing alongside, all tracked by the radar:

- **Unsloth** — QLoRA fine-tuning that actually fits on a 16 GB card
- **Axolotl** or **LLaMA-Factory** — config-driven SFT/DPO runs
- **TRL** — GRPO/DPO when you move past plain SFT
- **distilabel** — synthetic data pipelines with the teacher/verifier split built in
- **verl** / **OpenRLHF** — RL with verifiable rewards, if you get as far as running unit tests as the reward signal

---

## Ordered plan

1. `ollama pull qwen3-coder:30b` — confirm it runs split across both cards
2. Generate 1,000 traces on a narrow task you care about (one language, one repo shape)
3. Filter with `gpt-oss:20b` — expect to throw away 30–50%
4. QLoRA `qwen2.5-coder:7b` on the survivors with Unsloth; this is a couple of hours, not days
5. Eval against the 7B baseline on held-out tasks from your own code
6. Only if step 5 shows a real gain: scale to 14B and add the rented frontier slice

Step 5 is the one people skip. Do not skip step 5.

---

## Hardware notes

- **Never run the teacher and a ComfyUI render at once.** The radar's GPU gate
  exists because of this; give trace generation the whole 16 GB card.
- The 3060 (12 GB) is a fine home for the **verifier** — a 20B at Q4 sits at
  ~11 GB, so judging can run concurrently with generation on the other card.
- Set `OLLAMA_KEEP_ALIVE=0` during batch generation runs so a finished model
  releases VRAM immediately instead of squatting for five minutes.

## Sources

- [Best Open Source Models Distilled from RL-Trained Reasoners — Modal](https://modal.com/resources/best-open-source-models-distilled-rl-trained-reasoners)
- [Best Ollama Models 2026, ranked by VRAM & SWE-bench — Morph](https://www.morphllm.com/best-ollama-models)
- [Best Open-Source Coding Model 2026 — Morph](https://www.morphllm.com/best-open-source-coding-model-2026)
- [Qwen3-Coder-Next Technical Report](https://arxiv.org/pdf/2603.00729)
- [Best open-weight models for coding — Faros AI](https://www.faros.ai/blog/open-weight-models)
