{% docs pub_judge_lens %}
One row per decision: what the real panel, an average panel, the crowd and random
panels make of the fight's round statistics, beside what happened (DESIGN.md §8).
Judges are scored with a shared round and fight effect (ml/judge_lens/panel.py), so
panels agree as much as real ones do. In sample: the version trained on these fights;
ML.JUDGE_LENS_PREDICTIONS is the out-of-sample record.
{% enddocs %}
