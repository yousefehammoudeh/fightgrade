"""Judge Lens: what each judge rewards in a round (DESIGN.md §8).

    data.py      the training rows: REFINED.fct_judge_round_score with both
                 fighters' round stats from REFINED.fct_ufc_round_stats
    features.py  the shared scoring terms, all differences (A - B), so the model
                 gives the same answer whichever fighter is listed first
    model.py     the ordinal model: a population score plus a small style vector
                 per rater, shrunk toward the average judge
    metrics.py   log loss, calibration, 10-8 and the named-judge subsets
    train.py     cross-validation, the forward check and the locked test; main()
                 is the ML.TRAIN_JUDGE_LENS stored procedure's handler
    checks.py    the gradient and fighter-swap checks deploy runs first
    deploy.py    zips this package to a stage and creates the procedure (local)

Pure numpy / scipy / pandas apart from train.main and deploy, so the same code
runs on a laptop against DEV and inside Snowflake.
"""
