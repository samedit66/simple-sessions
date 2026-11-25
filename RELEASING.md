# Releasing Simple Sessions

Check the package against a disposable Redis 7+ instance before tagging a release.

```sh
uv sync --locked --all-extras
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
REDIS_TEST_URL=redis://127.0.0.1:6379/0 uv run --no-sync pytest
uv build --no-sources
uv run --no-sync twine check --strict dist/*
```

For the first release, configure a [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) on PyPI with project name `simple-sessions`, the owner and name of the GitHub repository, workflow `ci-and-publish.yml`, and environment `pypi`. Create the matching GitHub environment. PyPI confirms the name when it accepts the first upload.

Update `project.version` in `pyproject.toml`, regenerate `uv.lock`, and tag the tested commit `v<version>`. A tag pushed to GitHub runs the tests and publishes the checked distributions through Trusted Publishing. For a manual upload, use `uv publish` with credentials for the project after running the same checks.
