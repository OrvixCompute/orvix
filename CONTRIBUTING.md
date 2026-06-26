# Contributing to Orvix

Thank you for your interest in Orvix! 🎉 All contributions are welcome — code, docs,
bug reports, feature ideas, and design. This guide explains how to get involved.

## Code of Conduct

This project follows our [Code of Conduct](CODE_OF_CONDUCT.md). We're committed to a
respectful, inclusive community, and we expect everyone — maintainers, contributors, and
users — to uphold it.

## How to Contribute

### Reporting bugs

- Check [existing issues](https://github.com/OrvixCompute/orvix/issues) first.
- Use the bug report template.
- Include: steps to reproduce, expected vs. actual behavior, and environment
  (OS, Python version, package, commit SHA).
- **Security issues:** see [SECURITY.md](SECURITY.md) — do **not** open a public issue.

### Suggesting features

- Open a [discussion](https://github.com/OrvixCompute/orvix/discussions) first to avoid
  duplicate proposals.
- Use the feature request template.
- Explain the use case and why existing solutions don't fit.

### Submitting code

Fork the repo, create a branch, make your change, and open a pull request (details below).

## Development Setup

Orvix is a monorepo with two independent Python packages. Set up whichever you're working on.

**Orchestrator:**

```bash
cd orchestrator
python -m venv venv
source venv/bin/activate        # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
# Configure .env with your Supabase credentials
uvicorn app.main:app --reload
```

**Node:**

```bash
cd orvix-node
python -m venv venv
source venv/bin/activate
pip install -e .
orvix-node config init
# Edit ~/.orvix/config.yaml
ORVIX_NODE_STUB_GPU=true orvix-node start
```

## Pull Request Process

1. Fork the repo and create a branch using the convention `feat/`, `fix/`, `docs/`, or `chore/`.
2. Make your changes.
3. Run tests: `pytest` in the affected package directory.
4. Run the linter: `ruff check .`
5. Commit using [Conventional Commits](#commit-message-format).
6. Open a PR with a clear description.
7. Address review feedback.
8. Squash and merge once approved.

## Deployed-Node / Pod Code Discipline

> 🚨 **CRITICAL:** Any code written or patched **directly on a deployed node or
> pod** (e.g. a RunPod GPU pod) **MUST be ported back to the repo before the next
> deploy or pod restart.**
>
> Code that lives only on a pod is **lost** when the container disk is wiped
> (RunPod stop/start), when the pod is terminated, or when nodes are redeployed
> from git. We hit exactly this: a working `vLLM` HTTP-proxy backend existed only
> on the pod while the repo still had a `NotImplementedError` skeleton.
>
> After any work on a pod/node, before stopping or redeploying it:
> 1. `git status` and `git diff` on the pod checkout to see what changed.
> 2. Commit the change to a branch and push, or copy the file back to your local
>    repo and commit there.
> 3. Confirm `git status` is clean on the pod (everything is in the repo).
>
> See [docs/operations.md](docs/operations.md#code-synchronization-discipline).

## Coding Standards

- **Style:** PEP 8, enforced by `ruff`.
- **Line length:** 100.
- **Type hints:** required for public functions.
- **Docstrings:** required for public functions (Google style).
- **Logging:** no `print()` — use `loguru`.

## Testing Requirements

- All new code must have tests.
- Aim for >80% coverage on new code.
- Tests must pass locally before opening a PR.
- Unit tests live in `tests/` and must be hermetic — no live database or network.
- Integration tests that require running services live in `tests/integration/`.

## Commit Message Format

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
**Scope:** `orchestrator`, `node`, `docs`, `ci`, `deps`

Examples:

- `feat(orchestrator): add tier-aware node selection`
- `fix(node): handle reconnect when orchestrator returns 503`
- `docs(readme): clarify provider setup steps`

## Project Structure

- `orchestrator/` — FastAPI backend (routes, services, models, migrations).
- `orvix-node/` — provider agent (CLI, WebSocket client, executor, pluggable backends).

See [ARCHITECTURE.md](ARCHITECTURE.md) for a deep dive.

## Communication

- [GitHub Discussions](https://github.com/OrvixCompute/orvix/discussions)
- Discord — [discord.gg/orvix](https://discord.gg/orvix) *(placeholder)*
- Twitter — [@OrvixCompute](https://twitter.com/OrvixCompute)

Asking questions is welcome — we'd rather answer ten "beginner" questions than have you
struggle silently.

## Recognition

Contributors are credited in the project's contributors list and release notes. Significant
contributions are highlighted in the [CHANGELOG](CHANGELOG.md).

We're early stage, so these processes will evolve — thank you for helping shape them.
