"""The Hal model gateway: one authenticated local endpoint in front of llama.cpp, Ollama or any
OpenAI-compatible server, with presets, router policies, a managed llama-server, the model
manifest and gpu-light claims.

It never imports `halhub` or `gpu_light`; `halgateway.gpu` will speak gpu-light's socket with a
stdlib client (resolution A6). Design: docs/design/05-gateway.md.
This package is a skeleton: the first behaviour lands in phase 3 of the implementation plan.
"""

__version__ = "0.1.0.dev0"
