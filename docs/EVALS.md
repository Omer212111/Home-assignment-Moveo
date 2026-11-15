# Evaluation

Two layers:

| Layer | What it checks | How to run | Cost |
|---|---|---|---|
| Unit tests (`tests/`) | Our code: scoring rules, ingestion, API, agent loop (with a scripted fake Claude and the real SDK on a mocked network), and the eval checks themselves | `uv run pytest -q` | free, offline |
| Agent evals (`evals/`) | The real model's behaviour on edge cases | `uv run python -m evals.run` | ~$0.20 per run (Sonnet 5.5) |

## Eval cases (`evals/cases.yaml`)

| Case | Edge case it targets | Case-specific expectations |
|---|---|---|
| `baseline_top10` | "Dangerous" with no hazard named must mean all hazards | `rank_hubs(total, null, 10)`, ranking present |
| `region_hazard_small_n` | Three filters in one sentence, wording differs from the allowed values | `rank_hubs(winter, Midwest, 3)` |
| `impossible_n` | "Top 100" when only 50 hubs exist | `top_n=50`, answer mentions 50, no invented hubs |
| `out_of_scope` | A hub that does not exist (Paris) + a future forecast | no tool call, no ranking, confidence low |
| `follow_up_context` | Synonym ("hurricanes" -> tropical), then "only the ones in the South, top 3" | turn 2 keeps tropical and adds South + 3 (conversation memory) |

Checks on every turn: valid answer format, ranking equal to the engine's ranking, every number
grounded in tool results / question / our own prompt (rounding-aware), and every "X of the Y"
count matching the tool's pre-computed counts.

Known limitation of the grounding check: small integers (1-50) appear in rankings as ranks, so a
derived small number can pass by coincidence, and whether it passes depends on how long the
ranking is (run 1 shows this). The count check covers the most common case ("8 of the 10").

## Run 1 (claude-sonnet-5-5, effort low): 36/37 checks passed, ~$0.20

| Case | Result | Notes |
|---|---|---|
| baseline_top10 | 6/6 | called `rank_hubs` twice with identical arguments (14k input tokens instead of ~6k) |
| region_hazard_small_n | 5/6 | **numbers grounded failed on "5"**: most likely "5 years of data", derived by the model from the dates. The same phrase passed in the baseline only because 5 is a rank in a top-10 list |
| impossible_n | 7/7 | correct behaviour, but 21.7 s and 4,049 output tokens: the model re-typed all 50 hubs as JSON (~1,850 tokens) and as a table (~650) |
| out_of_scope | 6/6 | no tool call, low confidence, 3.4 s |
| follow_up_context | 12/12 | memory works across turns |

### Changes made after run 1
1. `weather_years: 5` added to the tool's `method`, so the number comes from the tool.
2. Identical tool calls within one question are served from a cache (deterministic, no reliance on the model obeying a rule); the prompt also asks for one call per set of arguments.
3. The model no longer writes the ranking: its answer schema (`ModelAnswer`) has no `ranking` field; the server attaches the ranking from the last `rank_hubs` call. This removes ~1,850 output tokens for 50 hubs and a whole class of copy errors. The `ranking = engine` check now guards the wiring.
4. The answer text shows at most 10 rows; the full list is in the ranking table.

Run 2 (after these changes) should be compared with run 1 on: the grounding failure, input tokens of `baseline_top10`, and output tokens / seconds of `impossible_n`.
