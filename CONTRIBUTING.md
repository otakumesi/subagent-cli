# Contributing

Thanks for contributing to `subagent-cli`.

## Development Setup
1. Install Python 3.11+ and `uv`.
2. Sync dependencies:
`uv sync`
3. Run tests:
`PYTHONPATH=src uv run python -m unittest discover -s tests -v`

## Pull Request Checklist
- Add or update tests for behavior changes.
- Keep CLI/user-facing docs in sync (`README.md`, `docs/`).
- Confirm all tests pass locally.

## Release (Maintainers)
1. Ensure version is updated (`pyproject.toml`, and package version if needed).
2. Build artifacts:
`uv build`
3. Validate package metadata:
`uv run --group release python -m twine check dist/*`
4. Upload to TestPyPI (recommended first):
`uv run --group release python -m twine upload --repository testpypi dist/*`
5. Upload to PyPI:
`uv run --group release python -m twine upload dist/*`

Notes:
- Python requirement is 3.11+.
- Keep PyPI credentials/tokens in local config (for example `.pypirc`, gitignored).
