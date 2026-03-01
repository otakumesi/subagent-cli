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
### Trusted Publishing (recommended)
1. Ensure version is updated (`pyproject.toml`, and package version if needed).
2. Push the release commit and tag (for example `v0.1.1`).
3. Create a GitHub Release from that tag.
4. The workflow `.github/workflows/publish-pypi.yml` builds and publishes to PyPI via OIDC.

PyPI trusted publisher settings should match:
- Owner: `otakumesi`
- Repository: `subagent-cli`
- Workflow: `publish-pypi.yml`
- Environment: `pypi`

### Manual token-based publish (fallback)
1. Build artifacts:
`uv build`
2. Validate package metadata:
`uv run --group release python -m twine check dist/*`
3. Upload to PyPI:
`uv run --group release python -m twine upload dist/*`

Notes:
- Python requirement is 3.11+.
- Keep PyPI credentials/tokens in local config (for example `.pypirc`, gitignored).
