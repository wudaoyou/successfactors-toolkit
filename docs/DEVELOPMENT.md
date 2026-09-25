# Development

[← README](../README.md)

```sh
git clone https://github.com/wudaoyou/successfactors-toolkit.git
cd successfactors-toolkit
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python3 scripts/check_repository.py
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for branch, review, commit, and
version rules, and [RELEASING.md](RELEASING.md) for the release
procedure.
