[![CI](https://github.com/DiamondLightSource/test-rig-bluesky/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/test-rig-bluesky/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/test-rig-bluesky/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/test-rig-bluesky)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# test_rig_bluesky

Bluesky plans to be run on Diamond's test rigs e.g. ViSR, P45, etc

## Running the system tests

This repository contains some system tests in `tests/system_tests`, they are scripts that are intended to be run against live test rigs to prove they are working as expected. These should run regularly to make sure the beamline stays in a working state.

```
# Login so blueapi will accept your commands
blueapi -c configuration/b01-1-blueapi-client.yaml login
# Follow prompts...

# Run system tests
tox -e system-test
```
