# Synthetic, hand authored

`baseline.yaml` was typed out, not captured. Nothing in this repo has ever
talked to a cluster, so there is no `kubectl get -o yaml` behind it and no
rendered output copied in either.

It is one service's worth of objects, written to pass every rule in
`gitopsvalidate/rules.py`. Each test in `tests/test_rules.py` loads it, spoils
exactly one field, and asserts that the matching rule says no. The point of a
single passing baseline is that a test which breaks one field cannot
accidentally be passing because a second field was wrong.

The real renders are the other half: `tests/test_rendered.py` runs the same
rules over `helm template` output for all eight combinations of environment and
service, so the passing side is the actual chart rather than this file.

Names and images here are deliberately `app` and `ghcr.io/sam-farooq/x:1.0.0`,
so nothing in it reads as a record of a real deployment.
