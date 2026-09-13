# Working in Light open source

This repository is a collection of independent open source projects. The root README
is the public directory; each project owns its implementation and developer experience.

## Project boundaries

- Put every new project in `projects/<project-name>/`. Use a unique, descriptive,
  lowercase kebab-case name. Never use vague names such as `app`, `demo`, or `utils`.
- Keep source, tests, examples, documentation, assets, dependency manifests, lockfiles,
  build configuration, and generated-output ignore rules inside that project folder.
  Use the language's conventional layout within the folder.
- A project must install, build, test, and run from its own directory without installing
  or building another project. Document any external service it requires.
- Do not import source from a sibling project, use relative file dependencies on a
  sibling, or share a virtual environment, dependency directory, or lockfile.
- Do not add a root application, package manifest, workspace configuration, shared
  runtime, or shared `src/` or `lib/` directory as part of adding a project.
  Propose repository-wide tooling changes separately.
- Keep changes to an existing project inside its folder, except for its catalog entry
  and any workflow dedicated to that project. Do not rename, reformat, or refactor
  neighboring projects as part of your change.
- `.claude-plugin/marketplace.json` is a thin discovery registry for Claude Code
  plugins. A plugin may add an entry pointing inside its own project folder, but
  its implementation and standalone validation must remain in that project.
- Use distinct package names, CLI names, container names, release tags (for example,
  `<project-name>/v1.0.0`), and project-specific environment variable names where the
  project controls them. Preserve names required by upstream tools and services.

## Add a project

1. Read `CONTRIBUTING.md`, inspect `projects/`, and choose an unused project name.
2. Create `projects/<project-name>/README.md` from
   `docs/project-readme-template.md`. Replace every placeholder and remove sections
   that do not apply. Include the purpose, maintainer, status, prerequisites, setup,
   a working usage example, validation commands, and license.
3. Add the implementation and appropriate tests in that same folder. Include its
   dependency manifest and lockfile where the ecosystem supports one. Add a local
   `.gitignore` for dependencies, generated files, and secrets. Provide a sanitized
   `.env.example` if configuration is needed.
4. Include the project's `LICENSE`, preserve required third-party notices, and state
   its license in its README. Use the license specified for that project; do not
   assume that another project's license applies or silently relicense imported code.
5. Add a project-local `AGENTS.md` when its commands or conventions need more detail.
   Those instructions supplement these repository-wide boundaries.
6. Add exactly one row to the root README's project catalog between
   `<!-- projects:start -->` and `<!-- projects:end -->`. Use this table format and
   keep rows alphabetical by name:

   ```markdown
   | Project | What it does |
   | --- | --- |
   | [Project name](projects/project-name/) | One sentence describing its purpose.<br><sub>TypeScript · Experimental</sub> |
   ```

   Link directly to the project folder. Include language and status beneath the
   description to keep the table readable on narrow screens. Use `Experimental`, `Active`, or `Archived`
   consistently with its README. Give each project its own row and description;
   never combine unrelated projects in one entry. The Featured section is curated
   separately and must distinguish external repositories from projects hosted here.
   For Claude Code plugins, also register the plugin in the root marketplace and
   validate that its `source` resolves to the project's plugin directory. Keep the
   root and project-local marketplace entries consistent when both exist.
7. Validate the project from a clean checkout, starting in its directory. Run its
   documented setup, example, and appropriate checks. Verify the catalog link and
   README links. State any checks you could not run in the PR.
8. Open a focused PR describing the project and how it was validated. Complete the
   PR template and record any required setup or known limitations.

## CI and releases

- Project workflows belong in `.github/workflows/<project-name>-<purpose>.yml`, where
  GitHub requires them. Scope triggers to the project's paths and its own workflow;
  set each shell step's working directory to the project folder.
- Keep workflow permissions minimal. Run checks on pull requests; publish packages
  only through a deliberately configured release workflow, never on every PR.
- Version and release each project independently. Publishing one project must not
  require releasing or changing another.

## Presentation and review

- Keep the root README concise: Light branding, featured links, project directory,
  and contribution links. Put setup instructions and screenshots in project READMEs.
- Use relative links for files in this repository, descriptive link text, and alt
  text for images. Verify README changes in GitHub's rendered view, including narrow
  screens and light/dark appearance when changing visual assets.
- Do not add empty example projects, invented projects, placeholder catalog rows,
  fabricated badges, or claims about tests, maintenance, or adoption without evidence.
- Do not move code from another repository unless the task explicitly calls for it.
- This is a public repository. Keep credentials, private endpoints, customer data,
  and internal-only source out of commits and examples.
- Work on a fresh branch in an isolated worktree based on current `main`. Check the
  branch before every commit and push, and finish with a pull request.
