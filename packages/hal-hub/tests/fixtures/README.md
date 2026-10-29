# hal-hub test fixtures

Synthetic packs for the hub's tests (04-hub.md, "Test plan"). They hold no personal data and
no real secrets.

| Directory | What it is |
| --- | --- |
| `demo` | v2 pack: read, act, reversible and untrusted-output tools, one declared secret |
| `legacy` | v1 module in the shape of the old `hal-ecosystem/scaffold.py` template |
| `probe` | v2 pack whose tools print, raise, sleep with a child, crash, read secrets or connect |
| `broken` | v2 pack whose tools module raises on import |
| `oneshot` | v1 `exec` module: a small Python script speaking the one-shot protocol |
| `routines` | v2 pack with one routine-defining tool (`routine = {id, steps}`) |
