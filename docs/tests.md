# Tests

- 1,023 tests, all passed in a full run by someone else on 2026-10-26 after the third scraper output round ([output](test-runs/2026-10-26-pytest-after-third-round.txt)). That round's six scripts replay as intended ([output](test-runs/2026-10-26-third-pass-replay.txt)). Earlier counts: 954, 686, 597, 442 and 435 ([outputs](test-runs/)).
- The part number, matching and mapping rules have tests for the case that must match and the near miss that must not. Part numbers and the X12 reader also have Hypothesis properties.
- Seeded random worlds (`tests/test_pipeline_simulation.py`): 24 worlds are run clean and again with injected failures. The two end states must be identical by content.
- A real process is killed mid ingest (`test_a_process_killed_mid_run_leaves_a_consistent_checkpoint_and_the_rerun_finishes`) and the rerun finishes the file.
- Mutation harness (`tools/mutation_check.py`, 323 guards, edits source in place so run it in a copy). All 323 were killed in a full run by someone else on 2026-10-26, after the third scraper output round, in 1,037 s ([output](test-runs/2026-10-26-mutation-full-after-third-round.txt)). 93 derive their edit from the syntax tree with `tools/mutate_ast.py` and 230 are text pairs. The history of earlier runs, a survivor that a hand written test now kills and the two weaker guards are in [MUTATION.md](MUTATION.md).
