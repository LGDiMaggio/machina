"""Machina on-demand evaluation harnesses.

Not a pytest suite and never wired into CI: the runners under this
package drive real local models (Ollama) and an optional cloud
reference. Only the scenario schema/loader is CI-tested
(``tests/unit/test_eval_scenario_schema.py``).
"""
