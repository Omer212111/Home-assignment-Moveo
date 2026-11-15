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

## Run 2: 37/37 checks passed, ~$0.16
The 50-hub answer dropped to 9.6 s and 1,147 output tokens. Duplicate calls persisted:
`baseline_top10` and `impossible_n` each called `rank_hubs` twice with identical arguments
(`impossible_n` input 26,769 tokens). Changes: each tool call records the model call (`step`) that
requested it; the prompt no longer says "your last call".

## Run 3: 36/37 checks passed, 60,542 + 4,841 tokens, ~$0.17

| Case | Input tokens (run 2 -> 3) | Model calls | Result |
|---|---|---|---|
| baseline_top10 | 10,926 -> 6,398 | 2 | 6/6, duplicate gone |
| region_hazard_small_n | 4,894 | 2 | 6/6 |
| impossible_n | 26,769 -> 29,271 | 3 | **6/7**: called `top_n=50`, then `top_n=10` |
| out_of_scope | 1,932 | 1 | 6/6 |
| follow_up_context | 10,450 + 7,597 | 3 + 2 | 12/12; turn 1 repeated an identical call in model call 2 (cached) |

Findings:
1. Duplicates always arrive in a later model call, never in the same response; which case shows
   them varies between runs. The cache saves the tool work but not the round trip.
2. `impossible_n`: the server attaches the ranking of the *last* call, so the user who asked for
   100 hubs got a 10-row table. `ranking = engine` passed because it uses the same "last call"
   rule; only the `tool arguments` check caught it.
3. Fixes are listed in `docs/IMPROVEMENTS.md` section 8 (postponed in favour of the remaining
   chat-bot capabilities).

## Run 4 (slice 3): 30/33 checks, 3 turns failed, ~$0.15

Code under test: slice 3 only (`explain_hub`, `compare_hubs`, a longer prompt). The run was made
before the trajectory commit, so thinking display was still the API default (`omitted`) and no
trajectories were saved. Result: a regression.

| Case | Model calls | Result |
|---|---|---|
| baseline_top10 | 3 | 6/6, one repeated identical call (cached) |
| region_hazard_small_n | 4 | 6/6, **two** repeated identical calls |
| impossible_n | 6 | **no answer** ("No final answer after 6 model calls") |
| out_of_scope | 1 | 6/6 |
| follow_up_context | 2 + 2 | 12/12 |
| compare_hurricane_flood | 6 | **no answer** |
| explain_premise | 6 | **no answer** (turn 2 not run) |

Finding: repeated tool calls went from occasional (runs 2-3) to a loop that uses up all 6 model
calls, including in `impossible_n`, which only uses `rank_hubs`. So the trigger is something in
slice 3 shared by all questions: the larger tool list or the new prompt rules (a suspect is
"Call all the tools you need in one response"). Without trajectories the exact cause is unknown.

Changes:
1. **Guardrail in the loop (deterministic):** after a round in which every call repeated an
   earlier one, and on the last allowed model call, the next request sets `tool_choice: none`. The
   tools stay defined (cached prefix unchanged), but the model must answer from the results it has.
   Chains of *different* calls remain possible, so slice-3 questions that need two tools still work.
2. Failures now keep their trajectory (`AgentError.trajectory`); the eval runner and `/chat` save it.
3. The cached-call note now tells the model to use the earlier result and answer.

Run 5 is the first run with trajectories (and with `display: "summarized"`), so it changes two
things at once: the guardrail stops the loop, and the trajectories should show *why* the model
repeats (its thinking summary before each repeated call).

## Run 5 (guardrail + trajectories + thinking summaries): 58/58 checks, ~$0.26

| Case | Model calls | Seconds (run 4 -> 5) | Notes |
|---|---|---|---|
| baseline_top10 | 3 | 11.4 -> 12.3 | one repeated call; the guardrail forced the answer on call 3 |
| region_hazard_small_n | 2 | 10.0 -> 17.5 | no repeat (run 4: two repeats) |
| impossible_n | 2 | failed -> 21.2 | one call with `top_n=50`; the 50 -> 10 re-call of run 3 did not happen |
| out_of_scope | 1 | 3.0 -> 10.1 | same tokens as run 4, three times slower |
| follow_up_context | 2 + 2 | 7.8 / 6.5 -> 8.9 / 6.9 | |
| compare_hurricane_flood | 2 | failed -> 9.1 | `compare_hubs(miami, houston, [tropical, flood])` |
| explain_premise | 2 + 2 | failed -> 7.8 / 8.2 | answer states rank 17; follow-up resolves "it" to Dallas |

Findings:
1. All 7 cases pass, including the three assignment questions added in slice 3.
2. The guardrail fired once (baseline). In the other cases the model did not repeat at all, so
   the run-4 loops did not reappear even without the guardrail. One run per case cannot tell
   whether that is due to the thinking setting, the reworded cache note, or chance.
3. **Latency went up**: `out_of_scope` made one model call with the same token counts as in run 4
   but took 10.1 s instead of 3.0 s; `region_hazard_small_n` 17.5 s for 2 calls. The likely cause
   was thought to be `display: "summarized"`. **The trajectory does not support that:** the single
   10.1 s call returned no thinking block at all (at effort `low` the model did not think on this
   simple question), so there was no summary to generate. More likely API latency variance; to be
   measured with repeated single-case runs under each setting before changing anything.

## Run 6 (time dimension, 15 cases / 17 turns): 110/111 checks, ~$1.12

All the new time questions chose the right tool and arguments: "in summer" -> season, "winter
storms" -> hazard, "safest" -> order least, Denver's worst month = March, seasons comparison, years
2024 vs 2021, snowfall in 2025 = 11.8%, hurricane season = months 6-11.

But the cost and the number of model calls went up sharply:

| | Run 5 | Run 6 |
|---|---|---|
| Repeated identical calls (served from cache) | 1 | **15** (in 14 of 17 turns) |
| Typical simple question | 2 model calls, ~8-9k input tokens | 3 model calls, ~21-24k input tokens |
| Total | 93k input tokens, $0.26 for 9 turns | 473k input tokens, $1.12 for 17 turns |

Findings:
1. **The repeat is now systematic**: after the first tool result the model calls the same tool again
   in almost every case; the guardrail then forces the answer on call 3. Correct answers, but each
   question pays a full extra round trip (~12k input tokens). The repeats grew with the size of the
   tool list and prompt (run 4: 3 tools, loops; run 6: 5 tools, repeats everywhere).
2. **Wandering:** `hurricane_season_comparison` made 4 rounds (compare, explain Miami, rank top 1,
   repeat). The side call to `rank_hubs(top_n=1)` made the server attach a 1-row ranking to a
   comparison answer: the only failed check.
3. `snowfall_percentage` first called `year: null`, then corrected itself to 2025 ("last year").
4. `impossible_n` repeated the run-3 pattern (50, then 10, then 50 again).

## Run 7 (two tool rounds + prompt caching): 110/111 checks, ~$0.55

| | Run 6 | Run 7 |
|---|---|---|
| Cost (17 turns) | $1.12 | **$0.55** |
| Input tokens / of which from cache | 473k / 0 | 388k / **243k (63%)** |
| Turns with 3+ model calls | 15 | 14 (none above 3) |
| Wandering beyond two rounds | yes (hurricane: 5 calls) | no |

Findings:
1. **Caching works**: about two thirds of input tokens are served from the cache; cost halved.
2. **The limit holds**: no turn exceeded 3 model calls; `hurricane_season_comparison` now passes.
3. **The model still uses its second round in most turns**: 10 identical repeats, and 4 "extra"
   calls that add little (explain the top hub after a ranking, a one-hub season breakdown after a
   two-hub one). As predicted when choosing two rounds: a repeat costs at most one round trip.
4. **The failed check is a check problem, not a wrong answer**: `compare_by_season` first called
   `hub_breakdown(boston, minneapolis)` (correct), then an extra `hub_breakdown(boston)`; the args
   check looks only at the last call of the tool.
5. `snowfall_percentage` used `year: 2025` on the first call (run 6: corrected only on the second).
