# hal-remote developer notes

How the SSH add-on is built and tested. The design is [07-remote.md](../design/07-remote.md).
The protocol, threat model and manual checklist are added when the add-on is complete.

## sshd tests

The tests in `packages/hal-remote/tests/sshd/` start an unprivileged `sshd` on
`127.0.0.1:<free port>` with its own host key, config and `authorized_keys` in a temp
directory. They need no root and change nothing on the host. They carry the `slow` marker, so
the default run skips them:

```sh
uv run pytest -m slow packages/hal-remote/tests/sshd   # needs an sshd binary
```

The fixture uses `$HAL_TEST_SSHD`, else `/usr/sbin/sshd`, and skips when neither exists. A
machine without `openssh-server` can use the package without installing it:

```sh
apt-get download openssh-server                       # downloads the .deb, no root
dpkg-deb -x openssh-server_*.deb sshd-root            # extracts into ./sshd-root
export HAL_TEST_SSHD=$PWD/sshd-root/usr/sbin/sshd     # tests use this binary
```

| Symptom | Fix |
| --- | --- |
| Every sshd test is skipped | Install `openssh-server` or set `HAL_TEST_SSHD`. |
| `sshd did not start` | Read the log it prints; an extracted package needs `SshdSessionPath` and `SshdAuthPath` next to the binary (the fixture adds them). |
| A forced-command test prints nothing | Check `serve.log` under the test's temp `state` directory. |
