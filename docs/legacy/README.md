# docs/legacy/ — frozen reference docs

These documents are kept for historical reference but are **no longer
maintained against the current codebase**.  They describe the project
state at the time they were written and may use phase numbering,
template names, or playbook names that no longer match the current
split-stack flow (see top-level `README.md` and `ansible/README.md`
for the current authoritative descriptions).

| File | Frozen as of | What it covers | Current replacement |
|------|--------------|----------------|---------------------|
| `design-and-development-summary.md` | 2026-05-13 | Early design rationale: External Platform choice, CCM/CAPA layering, initial implementation plan. Still accurate at the conceptual level. | High-level architecture: top-level `README.md` "Architecture" section. CCM specifics: `docs/CCM.md`. |
| `validation-checklist.md` | 2026-05-17 | "First end-to-end run" verification checklist using the legacy "阶段 0–N" numbering (predates Phase 00–08). | Per-phase validations now live inside each `ansible/playbooks/0N-*.yml` (assert / debug tasks) and in `docs/test-walkthrough.md`. |

Do **not** copy commands or playbook names verbatim from these files —
cross-check against the current source tree first.
