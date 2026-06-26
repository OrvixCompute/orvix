# Operations

Operational runbook notes for running Orvix in production.

## Code Synchronization Discipline

**The repository is the single source of truth for all code.** Anything running
in production must be reproducible from a clean `git clone`.

### The hazard

GPU compute nodes typically run on **ephemeral container storage**. Code written
or patched directly on a node is **lost** when:

- the container is restarted (ephemeral storage is wiped),
- the node is terminated, or
- a node is redeployed from git (overwrites local edits).

This actually happened: a working **vLLM HTTP-proxy backend** was implemented
directly on a deployed GPU node during an end-to-end test, while the repo still
carried a `NotImplementedError` skeleton
(`orvix-node/orvix_node/inference/vllm.py`). The implementation survived only
because it had also been copied into a local working tree; it was later ported
back to the repo on branch `feat/sync-vllm-backend`. Had the node been recycled
first, the work would have been gone.

### The rule

> Any code written directly on a deployed GPU node **MUST** be ported back to the
> repo **before** the next deploy or container restart.

### Checklist after any on-node work

1. On the node checkout: run `git status` and `git diff` to see every change.
2. Commit to a branch and `git push`, **or** copy the changed files into your
   local repo and commit there.
3. Re-run `git status` on the node and confirm it is **clean** — nothing
   uncommitted, nothing untracked that matters.
4. Only then stop, restart, or redeploy the node.

### Related deploy notes

- The production orchestrator at `/opt/orvix` is currently a **file copy**, not a
  git checkout — deploys rsync `orchestrator/` from a fresh clone of `main`
  (preserving `.env` and `.venv`). Treat `main` as the source of truth and keep
  the VPS in sync with it.
- Prefer making the VPS checkout a real `git` clone so `git status` there can
  catch drift the same way.
