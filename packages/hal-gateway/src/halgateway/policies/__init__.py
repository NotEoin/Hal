"""Request and response policies of the router backends (REQ-GW-40..49).

A policy is a pure function of the request body or response body where it can be, so it is
testable without sockets. The handler applies them on `llama-router` backends for the
`/v1/messages` and `/v1/chat/completions` routes only (design decision 6).
"""
