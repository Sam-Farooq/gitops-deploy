"""Checks on rendered Kubernetes manifests that the templates cannot express.

A Helm template can refuse a bad value for the object it is rendering. It
cannot see the other seven renders, the node pools, or the namespace quota, so
anything that is a fact about an environment rather than about one object
lives here.
"""

__version__ = "0.12.1"

ENVIRONMENTS = ("staging", "prod")

SERVICES = ("rag-agent", "model-serving", "hybrid-search", "embedding-worker")
