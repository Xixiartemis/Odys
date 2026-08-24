# E5 Provider Compatibility Finding

This is a provider-compatibility calibration record, not a live-success claim.

The supplied MiMo probe established:

- Responses function calling: supported.
- `previous_response_id`: unsupported; the provider returned `responses_feature_not_supported` because stored response history is not available.
- Odys Responses run `E5-LIVE-002`: two turns, one `workspace.list` tool call, then `PROVIDER_CONNECTION` during `MODEL_REQUEST`.
- Odys Chat Completions run `E5-LIVE-003`: one turn, zero tool calls, then `PROVIDER_CONNECTION`.

The compatibility profile therefore uses the Agents SDK Chat Completions model for MiMo, replays reasoning only when the origin model exactly matches the configured destination model, omits unsupported `tool_choice`, supplies the required assistant content for tool-call messages, and sends `thinking.type=enabled` through the supported `extra_body` surface. It does not use server-managed continuation.

No credential, authorization header, hidden reasoning text, or complete provider response body is recorded here. Existing E5 summary status remains `SKIPPED_CONFIG` until another real manual run supplies evidence.
