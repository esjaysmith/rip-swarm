# Hive checkout: worktree vs simpler options

**Status:** closed — option C picked 2026-09-17, recorded in `docs/specs/2026-09-17-design-spec.md` §13 and folded into spec §3/§11 and plan Tasks 11/12/14.  
**Date:** 2026-09-17  
**Audience:** operator + implementer  
**Related:** spec v0.2 §3, §8.5, §11, §13 (R1); plan architecture + Task 12 init.

Spec today: hive = orphan branch `swarm` on the **project repo**, pushed to the project remote (`origin/swarm`), checked out as a **nested single-branch clone** at `<repo>/_swarm/` (gitignored on code branches). That is option C below, locked on 2026-09-17 in spec §13 and folded into §3/§11. The paragraphs that follow are kept **as the decision record** — they argue from the pre-option-C state (a linked git worktree) to the choice that now stands; read them as history, not as an open question.

---

## 1. What is actually locked (do not drop)

Review 2 / R1 rejected hive files on the **code** branch. The spec’s three reasons stand:

1. **One shared line of history** while agents sit on different feature branches.
2. **Commit and push claims/heartbeats while the code tree is dirty.** `pull --rebase` on the code branch refuses; hive ops cannot wait for a clean code tree.
3. **Lost-race recovery is `git reset --hard @{u}`.** That must not wipe uncommitted project work.

Also still locked (not under discussion here):

- Git is the board; no extra hosted queue.
- First-push-wins on `claims/<task_id>.json`; JSONL is audit (`merge=union`).
- On-disk directory name `_swarm/`; path override `RIP_SWARM_HIVE` / `--hive`.
- Helpers refuse git ops unless `git rev-parse --show-toplevel` equals the hive dir and an upstream exists.
- Project remote holds hive contents + history (`git log origin/swarm`) without a second host.
- Sibling **separate repo** remains the multi-repo product option.

The load-bearing shape is: **a second work-tree root whose `HEAD` is a single shared ref (`swarm`).** Worktree vs clone vs sibling path are implementations of that shape.

---

## 2. Why hive-on-the-code-branch is out

If `_swarm/` is committed on `main` / feature branches:

- Each feature branch has its own hive history; claims do not converge.
- `git commit` of hive files lands on whatever branch the agent has checked out.
- A dirty code tree blocks rebase of hive-only commits, or stashing around git is fragile.
- `reset --hard` in that tree destroys the agent’s uncommitted code.

Surgical alternatives (stash, `git commit` subset of paths, `GIT_WORK_TREE` plumbing) either fail (1)–(3) or reimplement `git worktree` by hand. They are not simpler.

---

## 3. Options that still satisfy (1)–(3) + same remote

| Option | Shared `origin/swarm` | Push while code dirty | Safe `reset --hard` | `_swarm/` in the project | Extra host | Notes |
|--------|----------------------|----------------------|---------------------|--------------------------|------------|--------|
| A. Hive files on the code branch | No | No | No | Yes | No | Out (R1). |
| B. Orphan `swarm` + **linked worktree** at `_swarm/` (spec today) | Yes | Yes | Yes | Yes | No | Shared object store; git ≥ 2.42 for `worktree add --orphan`; `_swarm/.git` is a pointer into the parent. |
| C. **Nested clone** `git clone --single-branch --branch swarm <origin> _swarm` | Yes | Yes | Yes | Yes | No | `_swarm` is a normal repo; duplicate objects (tiny); older git. |
| D. Sibling clone of **same** `origin` `swarm` (other path) | Yes | Yes | Yes | No | No | Already allowed; needs `RIP_SWARM_HIVE`. Default only if we drop “directory is `_swarm/` in the project.” |
| E. Separate hive remote / second GitHub repo | Yes | Yes | Yes | Optional | **Yes** | Violates “nothing extra to host.” Multi-repo products only. |

Helpers already check “this directory is a git work-tree root with an upstream.” **B, C, and D all pass.** Publish loop, `git -C <hive>`, `merge=union`, first-push-wins, `_swarm/` gitignored on code branches — unchanged.

---

## 4. Worktree costs (why this discussion exists)

Linked worktree is correct. It is not free:

- `git worktree add --orphan -b swarm _swarm` needs **git ≥ 2.42**. Spec already has a worse fallback (`worktree add --detach` then `checkout --orphan`).
- `_swarm/.git` is a **file** pointing at the parent. An agent that `cd`s into `_swarm` and runs git is operating on the **project** repository (other worktrees, `checkout` of `main` refused or chaotic).
- Worktree metadata uses **absolute paths**. Move or copy the project, the worktree breaks until repaired.
- Delete `_swarm/` without `git worktree remove` → prune debt.
- Shared `.git` means ref locks with the code tree (usually fine; more surprising than two repos).
- Shared objects are a weak win: the hive is claims + JSONL, not the project blob set.

Init in the spec is already the complicated part (create vs attach vs `--no-git` vs old git). Most of that complexity is worktree-specific.

---

## 5. Nested clone (option C) in concrete terms

Same remote, same branch, same `_swarm/` path. No worktree.

```bash
# first agent (origin/swarm does not exist yet)
git checkout --orphan swarm
# empty the index/tree, copy templates/_swarm, commit, push -u origin swarm
git checkout <previous-branch>   # back to code
echo '_swarm/' >> .gitignore
git clone --single-branch --branch swarm "$(git remote get-url origin)" _swarm

# every later clone (origin/swarm exists)
echo '_swarm/' >> .gitignore     # if missing
git clone --single-branch --branch swarm "$(git remote get-url origin)" _swarm
```

The first-time orphan can be done in a **temporary directory** (not by switching the user’s code branch), then clone into `_swarm/`. That avoids “checkout --orphan in the code worktree.”

`cd _swarm && git status` is only the hive. `git -C _swarm reset --hard @{u}` cannot see the code tree. `--single-branch` does not fetch `main` into the nested repo.

Disk: a second object store of hive-sized history. Irrelevant next to a project clone.

---

## 6. Sibling path (option D)

Identical git model to C, directory not under the project:

```text
~/src/myapp/           # code
~/src/myapp-swarm/     # clone of origin, branch swarm
# or RIP_SWARM_HIVE=~/.local/share/rip-swarm/<id>
```

Keep for **multi-repo products** (one hive, many code checkouts). Do not make it the default if Goal 1 / operator lock wants `_swarm/` sitting in the project so agents find it without env.

---

## 7. Recommendation

- **Keep** `origin/swarm` as the one hive ref, `_swarm/` as the default directory, helpers as `git -C <hive>` on a work-tree root.
- **Change** the default checkout from linked worktree (B) to **nested single-branch clone (C)**.
- **Keep** D as the multi-repo attach (`--hive` / `RIP_SWARM_HIVE`).
- **Keep** B available as an attach method if someone wants shared objects — not what PROTOCOL or `gitops` should require.
- **Do not** return to A.

If accepted, spec edits are local: §3 layout prose + init commands, §8.5 “safe because own work-tree root” (drop “linked worktree” as the only story), §11 init create/attach, §13 R1 disposition, plan Task 12 init. Claim primitive, SoT table, publish loop, union-merge, `_swarm/` name — untouched.

If rejected, record “worktree stays; nested clone is not simpler enough” in §13 and close this file.

---

## 8. Decision (blank)

| Choice | Meaning |
|--------|---------|
| C nested clone (recommended) | Default init/attach = `git clone --single-branch -b swarm` into `_swarm/`. Worktree not required. |
| B worktree (status quo) | Spec v0.2 stands. This file becomes a closed discussion. |
| D sibling-as-default | Drop in-project `_swarm/`; always `RIP_SWARM_HIVE`. |
| Other | Write it here. |

**Picked:** **C — nested single-branch clone.** Refinement on §5: the first-time orphan branch is created in a temporary directory and pushed straight to the remote; `checkout --orphan` is never run in the code checkout, not even as an option. B stays permitted because the helper check (`show-toplevel` == hive dir, upstream present) accepts it.
