# Tests

Offline tests for `scripts/probe.py` and `scripts/validate-pr.py`. `xash3d-query`
is replaced by `fakequery.py` and DNS is stubbed, so nothing touches the network.
Needs only `python3` and `git`.

```sh
./tests/run.sh                                  # everything
./tests/run.sh -v                               # verbose
./tests/run.sh test_probe                       # one module
./tests/run.sh test_validate_pr.TestDuplicates  # one class
```
