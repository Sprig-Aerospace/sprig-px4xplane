# Sprig PX4-XPlane Plugin

This fork keeps upstream px4xplane attribution while shipping a Sprig-maintained X-Plane 12 + PX4 HITL build. The plugin still installs as `px4xplane/` because that folder name is accepted by X-Plane and avoids surprising assumptions in docs, configs, and operator muscle memory.

## What Sprig Changed

- X-Plane plugin identity now reports as `Sprig PX4 X-Plane` with Sprig build metadata in the X-Plane log.
- TCP client ownership is hardened for PX4 on port `4560`.
- Send failures, receive failures, `select` failures, zero-byte sends, and clean peer closes immediately close the accepted PX4 client.
- The listening socket remains open after client failure so a restarted PX4 process can reconnect without restarting X-Plane.
- If a newer client connects while an older accepted socket exists, the plugin closes the stale client and prefers the newer connection.
- Plugin disable/stop closes client and listener sockets idempotently.
- Default `config.ini` uses `config_name = Alia250`, disables debug logging, and keeps normal HITL message rates.

Active TCP probes such as `nc 127.0.0.1 4560` are discouraged during operations. The plugin is hardened so a short probe should not strand future PX4 sessions, but probes still consume an accepted client briefly and can create confusing timing during startup.

## Build On macOS

CMake build:

```bash
cmake -S . -B build/macos-cmake -DCMAKE_BUILD_TYPE=Release
cmake --build build/macos-cmake
```

Native Makefile build:

```bash
make -f Makefile.macos BUILD_TYPE=Release
```

The expected Makefile plugin binary is:

```text
build/macos/release/px4xplane/64/mac.xpl
```

The expected CMake plugin binary is:

```text
build/macos-cmake/mac/Release/px4xplane/64/mac.xpl
```

## Package

```bash
./scripts/package_macos.sh
```

The script creates:

```text
build/sprig-px4xplane-v3.4.2-sprig.1-macos.zip
```

After creating the zip, the script prints the exact install command for that package. It is safe to re-run with `unzip -o` when replacing an existing plugin install.

The zip layout is:

```text
px4xplane/
  64/
    mac.xpl
    config.ini
  px4_airframes/
    5001_xplane_cessna172
    5002_xplane_tb2
    5010_xplane_ehang184
    5020_xplane_alia250
    5021_xplane_qtailsitter
  README.md
```

## Install

Unzip the package into:

```text
~/X-Plane 12/Resources/plugins/px4xplane
```

For a locally built package, install it all at once with:

```bash
cd "$HOME/X-Plane 12/Resources/plugins" && unzip -o /Users/briankeeley/repos/sprig-px4xplane/build/sprig-px4xplane-v3.4.2-sprig.1-macos.zip
```

Then start X-Plane 12. The Sprig build starts listening on TCP `4560` when the plugin starts or is enabled.

## Verify

Confirm the plugin is listening:

```bash
lsof -nP -iTCP:4560 | grep LISTEN
```

During a healthy PX4 HITL run, X-Plane `Log.txt` should contain a Sprig plugin startup line and a single clear `PX4 connected` transition. PX4 should report its simulator connection, and the X-Plane log should not show repeated `Broken pipe` send spam.

To inspect for stale accepted sockets:

```bash
lsof -nP -iTCP:4560
```

After PX4 disconnects or a short probe exits, the plugin should close the accepted client and continue listening for the next PX4 connection.

## Local TCP Harness

Run the repository-local lifecycle harness without X-Plane:

```bash
python3 tools/validate_harness.py quick
```

Or run a single scenario:

```bash
python3 tools/px4_tcp_lifecycle_harness.py --scenario probe-resistance
```

The harness binds an ephemeral TCP port by default and exercises the Sprig socket ownership contract: clean startup, probe cleanup, stale client replacement, send-failure reconnect, and idempotent reload cleanup. Use `--port 4560` only when X-Plane is not running and the operational port is intentionally free.

## Reset

From X-Plane, open Plugin Admin, disable the Sprig PX4-XPlane plugin, then enable it again. Disable/stop closes all plugin-owned sockets. Enabling the plugin starts a fresh listener on TCP `4560`.
