"""Compatibility import for callers using the former Producer name.

PublicationPolicy contains delivery constraints only. Model score interpretation
lives in decision_client; the public PipelineResult.producer field is retained.
"""
from matchos.publication import GAPS, PublicationPolicy

Producer = PublicationPolicy
