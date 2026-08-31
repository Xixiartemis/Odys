# Odys reliability benchmark contract

These scenarios are deterministic infrastructure only. No live model result is
recorded here. Before a Hermes/Odys comparison, the runner must replace each
placeholder digest and freeze every `fairness` field once for both harnesses.

Both runners receive the same model, provider, objective, fixture bytes,
repository state, capabilities, timeout, turn/API budget, validator, and fault
injection boundary. Harness-internal state representations may differ; task
conditions and acceptance authority may not.

The files intentionally contain no Odys-specific acceptance criterion. A
runner that omits a declared metric or changes scenario identity fails closed.
