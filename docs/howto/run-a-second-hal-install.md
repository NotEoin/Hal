# Run a second Hal install

This page shows how to run two gateways side by side, for example a stable one and a test
checkout. All gateway state sits under the state directory, so separate directories, ports and
claim names keep them apart. Unverified end to end: the design test
`test_two_instances_do_not_share_state` is not written yet.

## Steps

1. Give the second install its own directories and config.

   ```sh
   export HAL_CONFIG=$HOME/.config/hal-test/config.toml   # separate config
   export HAL_STATE_DIR=$HOME/.local/state/hal-test       # separate state, tokens, logs
   export HAL_RUNTIME_DIR=$XDG_RUNTIME_DIR/hal-test       # separate socket; mkdir -m 700 it
   ```

2. Change every port and claim name in that config.

   ```toml
   [gateway]
   tcp_port = 8742

   [gateway.gpu]
   reclaimer_name = "hal-gateway-test"
   v1_claim_name = "hal-gateway-test"

   [gateway.backends.local]
   port = 8081
   ```

3. Check the config, then start it.

   ```sh
   hal-gateway check-config   # reports the key and file of any error
   hal-gateway serve
   ```

4. Do not run two routers that load models at once. They share one GPU; the second `mode`
   switch answers 409 `gpu_busy_peer` only if you list the first as a peer.

   ```toml
   [[gateway.gpu.peers]]
   url = "http://127.0.0.1:8080"
   ```

| Symptom | Fix |
| --- | --- |
| Address already in use | A port is shared. Change `tcp_port` or the router `port` |
| Both installs show the same tokens | `HAL_STATE_DIR` is not set in this shell |
| Socket path too long | The path is over 107 bytes. Shorten `HAL_RUNTIME_DIR` |
| Claims fight over the GPU | Both use one claim name. Rename `reclaimer_name` |
