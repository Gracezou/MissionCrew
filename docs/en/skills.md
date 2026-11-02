# Project skills as complete directories

A MissionCrew project skill is a complete directory, not just a prompt snippet stored in the database. Each project has its own drop directory:

```text
${MISSIONCREW_HOME}/projects/<project-id>/skills/
├── code-review/
│   ├── SKILL.md
│   ├── scripts/
│   ├── references/
│   └── assets/
└── release/
    └── SKILL.md
```

By default `MISSIONCREW_HOME` is `~/.missioncrew` under the user's home directory. The Skills page shows the absolute drop path of the current project.

## Valid skills

Each skill must be a plain directory that contains exactly one file named `SKILL.md` (the file name is matched case-insensitively). MissionCrew normalizes the file name to uppercase when loading it. The file must start with YAML frontmatter and contain at least the string properties `name` and `description`:

```markdown
---
name: code-review
description: Review code changes for correctness, risks, and maintainability
---

# Workflow

Read `references/checklist.md`, then run `scripts/inspect.py` when needed.
```

Other frontmatter properties are kept as is, for compatibility with the extensions of different agent runtimes. `scripts/`, `references/`, `assets/` and any other files in the same directory are all kept as part of the skill package.

The skill id defaults to the directory name and may only contain letters, digits, underscores and hyphens. When importing a ZIP or a local directory, if the name of the directory containing `SKILL.md` does not satisfy the id rules (which is the case when `SKILL.md` sits directly at the ZIP root), MissionCrew uses the frontmatter `name` as the id instead, provided that it also satisfies the id rules.

## Three ways to import

### Upload a ZIP

On the project Skills page, choose "Upload ZIP" (上传 ZIP). The ZIP can contain a single skill or multiple skill directories at any nesting depth; the platform recursively detects valid `SKILL.md` files and installs each complete directory into the project skill root.

### Import a local directory

Choose "Import local directory" (导入本地目录) and use the server machine's directory picker to select a skill directory or a directory containing a collection of skills. The platform recursively detects and copies the complete directories. After the import, the source directory is not managed by MissionCrew, and later changes to the source directory are not synced automatically.

### Drop directly

Copy the skill folder directly into the project drop directory shown on the page. This directory is managed by first-level subdirectories: each subdirectory corresponds to one skill. Polling of the web overview, a manual "Rescan" (重新扫描) or the assembly for the next agent run all discover changes automatically; there is no need to call the import endpoint.

Existing legacy text skills are migrated automatically to `skills/<id>/SKILL.md` when the project is first initialized. A newly created project gets an empty `skills/` directory immediately.

## Version history

Each project keeps its own bare Git history in `projects/<project-id>/skill-history.git`. A version is a complete directory snapshot of all currently valid skills, so `SKILL.md`, `scripts/`, `references/`, `assets/`, other files and the files' executable permissions all go into the version; the enabled state is stored in the Project index and is not part of the skill package content version.

Saving, importing, overwriting, deleting, restoring from the recycle bin and restoring a historical version through the web UI or the Agent Tool all commit a version. Changes in the drop directory are committed during the web UI's automatic scan, a manual rescan or the scan during the next agent assembly; no empty version is created when neither content nor permissions have changed. The Skills page can show the complete file tree and file contents of any historical version, compare two packages A → B for added, deleted and modified files and text diffs, and restore the complete directory to an old version. A restore creates a new commit and does not rewrite existing history.

The version repository lives in the platform's project data directory; it is not exposed to the runtime and is not part of the business code repository. After a skill is deleted, its Git history is still kept; the recycle bin restores the complete directory that is currently deleted, while version history is for viewing and restoring old contents of skills that still exist.

## Duplicates, deletion and safety boundaries

- When an import finds a skill with the same id, it first returns a list of conflicts and overwrites only after the user confirms.
- When a skill is overwritten or deleted from the web UI, the old complete directory goes into the project's unified recycle bin instead of being permanently deleted right away; it can be restored or permanently deleted on the single recycle bin page. Contents of the legacy `.skill-trash/` are migrated into the unified recycle bin when the service starts.
- ZIP import rejects absolute paths, `..` path traversal and symbolic links; the archive, individual files, the number of files and the total extracted size all have upper limits.
- Local directory import rejects symbolic links, so that a skill cannot use links to reference files outside the import root.
- Invalid entries in a ZIP or a local collection are shown under scan issues; the other valid skills can still be imported.

## Runtime adaptation

Before each run, MissionCrew scans the project skill library and applies the following generic adaptation:

1. Only enabled skills are mapped, as directory links, into the project-level shared view `projects/<project-id>/runtime-context/skills/<id>/`; `.missioncrew/skills` in each agent workspace is a link to this shared view, so the agent-side path is still `.missioncrew/skills/<id>/`.
2. The unified entry point is passed to the runtime through `MISSIONCREW_SKILLS_DIR`.
3. The real project skill root and the agent harness root are added to `MISSIONCREW_ALLOWED_DIRS`, so runtimes that support native multi-directory authorization also get the corresponding command-line permissions.
4. The durable shared context lists each skill's id, name, description, complete directory content version and `SKILL.md` path; the body is not preloaded.
5. When the agent decides that a skill is relevant to the task, it reads `SKILL.md` and then resolves scripts, reference materials and asset files relative to that skill directory.

A change to any file in the directory changes the content version, so a runtime session reused by the same role in the same channel receives the updated shared context in the next turn. This adaptation does not depend on any CLI's native skill search directory, so the print-mode and ACP runtimes supported by MissionCrew all use the same project skills.

## API

- `GET /api/projects/<project>/skills/library`: read the drop path, the file list and scan issues.
- `GET /api/projects/<project>/skills/<skill-id>/history`: list complete package versions.
- `GET /api/projects/<project>/skills/<skill-id>/history/<revision>`: read the file tree and `SKILL.md` of a historical version.
- `GET /api/projects/<project>/skills/<skill-id>/file?path=<relative-path>&revision=<revision>`: read a text file in the current or a historical version.
- `POST /api/projects/<project>/skills/<skill-id>/compare`: compare two complete package versions.
- `POST /api/projects/<project>/skills/<skill-id>/restore`: restore the complete directory to the specified version and create a new version.
- `POST /api/projects/<project>/skills/import-zip`: the request body is the raw ZIP bytes; `overwrite=true` confirms overwriting.
- `POST /api/projects/<project>/skills/import-folder`: import a directory on the server's local file system.
- `POST /api/projects/<project>/skills/rescan`: rescan the drop directory immediately.
