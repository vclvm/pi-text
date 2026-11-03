# pi-sandbox

Run the [Pi](https://pi.dev) coding agent on macOS so that it can work on your projects and your
Obsidian vault, and on nothing else:

- Pi runs in a container inside a Colima VM. It sees your projects folder and the vault (at the
  same paths as on the Mac), nothing else of your home folder.
- Its only network is an internal Docker network. The model is reached through the **pi-text
  bridge**, which holds the API key; the web only through a proxy that lets GET requests pass,
  each one after your approval, and refuses uploads.
- Inside those limits it has a full toolchain: Java 17/21/25 with Maven and Gradle, Node.js, Python,
  and its own Docker engine for Docker Compose and Testcontainers. Dependencies and images come from
  your artifact repository.
- Pi extensions ask before risky commands, keep file tools inside your folders and add read-only
  web tools.

You describe your environment in **one file**, `~/.pi-sandbox/config.yaml`; the `pibox` command
generates every other configuration from it and runs the sandbox.

## Prerequisites

| What | How to install |
|---|---|
| macOS 26 on Apple Silicon | |
| [Homebrew](https://brew.sh) | the installer from brew.sh, or your company's installation |
| Colima, Docker CLI with Compose and Buildx, yq | `brew install colima docker docker-compose docker-buildx yq` |
| git | `xcode-select --install`, or `brew install git` |
| An OpenAI-compatible model router and its key | see [bridge/README.md](bridge/README.md) |
| An artifact repository (recommended) | Artifactory, Nexus…: Maven, npm, PyPI and Docker remotes (HTTPS is fine) |

Pi, Node.js and Python are **not** needed on the Mac: Pi runs inside the agent's container.
`pibox check` lists whatever is missing. Details: [docs/pi-setup.md §6.1](docs/pi-setup.md#61-prerequisites).

## Quick start

```bash
git clone <URL-OF-THIS-REPOSITORY> ~/src/pi-text   # anywhere; better not under your projects folder
~/src/pi-text/bin/pibox init       # installs into ~/.pi-sandbox, creates config.yaml, links ~/.local/bin/pibox
$EDITOR ~/.pi-sandbox/config.yaml  # folders, router URL and key, models, artifact repository
pibox check                         # prerequisites and config.yaml
pibox setup                         # generates every configuration from config.yaml
pibox up                            # starts the VM, builds the images, starts bridge, proxy, Docker engine
pibox verify                        # tries everything the agent must not be able to do
```

Then, in two terminals:

```bash
pibox approve                       # the approval console for the agent's web requests
cd ~/Projects/api && pibox run      # Pi, in the folder you're in
```

After a session: `pibox review` (git hooks, risky git config, `.obsidian` changes).

## Commands

| Command | What it does |
|---|---|
| `pibox init` | Install or update the components in `~/.pi-sandbox` from the repository; creates `config.yaml` from the template; later runs rebuild it from the newest template and carry over every value you changed (previous file: `config.yaml.bak`) |
| `pibox check` | Check the prerequisites and `config.yaml` |
| `pibox setup` | (Re)generate every configuration from `config.yaml`: `.env` for Compose, the bridge's environment, Pi's `models.json`, Maven/Gradle/npm/pip/Docker settings, the Colima VM |
| `pibox config` | Show the settings in effect, secrets masked |
| `pibox up` | Start the VM, build the images, start bridge, proxy and the agent's Docker engine |
| `pibox run [args]` | Pi in a fresh container in the current folder (`args` go to Pi) |
| `pibox approve` | The approval console (detach: Ctrl-P Ctrl-Q) |
| `pibox log` | Follow the web journal |
| `pibox shell` | A bash shell in the agent's container, without Pi |
| `pibox status` | The VM and the containers |
| `pibox down [--vm]` | Stop the containers; `--vm` also stops the VM |
| `pibox verify` | The isolation checks |
| `pibox review` | Look for changes that would make your own tools run code later |

After changing `config.yaml`: `pibox setup`, then `pibox up`. After `git pull` in the repository:
`pibox init`, `pibox setup`, `pibox up`.

## Configuration

[sandbox/config.yaml](sandbox/config.yaml) is the template: every setting with its default and an
explanation. After `git pull`, `pibox init` puts your values into the new template, so new settings
show up in your file. Each model gets its own limits:

```yaml
models:
  - id: qwen3-coder
    contextWindow: 131072     # Pi compacts the conversation before it fills up
    maxTokens: 16384          # output limit of one reply; big write/edit calls need room
  - id: gpt-oss-120b
    reasoning: true
    contextWindow: 131072
    maxTokens: 32768
    compat: { supportsReasoningEffort: true }
```

Many repositories? Instead of URLs and logins, point it at the files you already have; `pibox setup`
copies them and adapts them to the sandbox (proxy, `http://`, Mac paths):

```yaml
repository:
  maven_settings: ~/.m2/settings.xml
  npmrc: ~/.npmrc
  pip_conf: ~/.config/pip/pip.conf            # e.g. from Artifactory's "Set Me Up"
  docker_daemon: ~/.config/pi-sandbox/daemon.json
  docker_config: ~/.docker/config.json             # registry logins ("auths")
  apt_sources: ~/.config/pi-sandbox/apt-sources    # Debian: the "Set Me Up" line(s), as they are
```

The sections are explained in [docs/pi-setup.md §6.5](docs/pi-setup.md#65-configyaml).

## Repository layout

```
bin/pibox            the command
sandbox/             the components pibox init installs into ~/.pi-sandbox:
                     config.yaml (template), compose.yaml, Dockerfile.pi, addons/egress_policy.py,
                     extensions/sandbox-guard.ts + web.ts, agent/settings.json
bridge/              the pi-text bridge: Pi's tool calls as text for routers that refuse native tools
docs/pi-setup.md     how it works and why: threats, isolation layers, every component, troubleshooting
```

`pibox init` copies the components instead of running from the clone: the agent can write to your
projects, and must not be able to change the code that holds the key or defines its guard rails.

## The bridge

The bridge is also usable on its own, with Pi on any machine: [bridge/README.md](bridge/README.md).
Its tests: `cd bridge && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q`.
