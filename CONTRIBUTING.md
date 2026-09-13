# Contributing

Light open source is a home for independent tools, integrations, and experiments.
Each project has its own setup, dependencies, tests, license, and release cycle.

## Work on an existing project

Find the project in the [directory](README.md#projects), then read its README and any
local `AGENTS.md` or `CONTRIBUTING.md`. Run commands from that project's directory.
Keep your PR focused on that project and describe how you checked the change.

## Add a project

1. Choose a descriptive, unused kebab-case name and create `projects/<project-name>/`.
2. Copy the [README template](docs/project-readme-template.md) into that directory,
   replace all placeholders, and remove sections that do not apply.
3. Add the source, tests, examples, dependency files, local `.gitignore`, and license
   inside that folder. Document prerequisites, ownership, and any external services.
4. Verify that someone can follow the README from a clean checkout without installing
   or building another project in this repository.
5. Add a row to the [root project catalog](README.md#projects) with a direct folder
   link, one-sentence description, language, and status. Keep the catalog alphabetical.
   Use the table format in [AGENTS.md](AGENTS.md#add-a-project).
6. Open a pull request describing what the project does, who maintains it, and which
   checks and examples you ran. Call out any setup a reviewer needs.

An example layout, using a placeholder name:

```text
projects/
└── your-project/
    ├── README.md           # Purpose, owner, setup, usage, checks, license
    ├── LICENSE             # License for this project
    ├── .gitignore          # Project-specific dependencies and generated files
    ├── src/                # Source, using the language's conventions
    ├── tests/              # Appropriate verification for this project
    └── examples/           # When useful
```

Keep the project's manifests, lockfiles, build configuration, and optional local
`AGENTS.md` in the same directory. Do not create root dependency files or import code
from neighboring projects. See [all project boundary rules](AGENTS.md#project-boundaries).

Claude Code plugins also get an entry in the root `.claude-plugin/marketplace.json`
with a source path inside their own project folder. This file is only a discovery
registry; plugin files and their validation stay inside the project.

## Licenses

Each project declares its own license in its `LICENSE` and README. Check that license
before using or contributing to the project. Preserve third-party copyright and
license notices when bringing in existing code. A link to a separate repository does
not change that repository's license.

## Documentation changes

Use relative links for repository files. Check the rendered Markdown, links, and
images. For landing-page changes, also check narrow screens and light/dark appearance.
No application build is needed for documentation-only changes.
