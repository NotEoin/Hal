# ACP schema fixture

`schema.subset.json` is a hand-written JSON Schema covering the ACP v1 messages that
hal-agent sends and accepts. It is NOT the upstream file. It was written from the public
protocol description and checked only against hal-agent's own output.

Design 03 (REQ-AGT-79) asks for the upstream `schema/v1/schema.json` at tag `schema-v1.26.0`
(Apache-2.0), vendored here with its source URL and sha256. That step needs the network and a
maintainer decision, so it is not done:

1. Download the file from the `agentclientprotocol` repository at tag `schema-v1.26.0`.
2. Save it as `tests/fixtures/acp/schema.json` and record the URL and the sha256 in a
   `schema.json.source` file next to it.
3. `tests/test_acp_conformance.py` then also validates every message against it. Until the file
   exists, that part is skipped.
