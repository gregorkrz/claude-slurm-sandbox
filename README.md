# claude-slurm-sandbox

Run Claude Code on an HPC login node so that:

1. **Claude's shell is sandboxed.** It can write only inside one project directory (plus any extra directories you list), and it cannot reach Slurm.
2. **Slurm access goes through a broker.** A small MCP server you control is Claude's only path to Slurm. It submits, cancels and modifies jobs within per-project limits, and only touches jobs it submitted itself.
3. **Every job runs inside a fixed Apptainer image** with only the bind mounts you allowed at init time. The broker writes the batch script; Claude only supplies a bash script that runs *inside* the container.
4. **Optional: background services.** Watchers that must outlive the session (wait for jobs, then post-process or publish) run as systemd user units, each in its own bubblewrap sandbox with no network. Their only way out is a socket to the broker, which applies the same policy. See [Background services](#background-services-opt-in).

```
 login node                                   compute node
┌───────────────────────────────────────┐     ┌──────────────────────────────────┐
│ claude (Claude Code)                  │     │ batch script (generated)         │
│  ├─ Bash ──► bubblewrap sandbox       │     │  └─ apptainer exec --containall  │
│  │          rw: project only          │     │       --no-home                  │
│  │          no munge / slurmctld      │     │       --no-mount bind-paths,...  │
│  └─ MCP ──► broker (outside sandbox)  │────►│       --bind project:/work       │
│              validates, then sbatch   │     │       --bind <your binds>        │
│              │                        │     │       image.sif bash /work/x.sh  │
│              │ start_service (opt-in) │     └──────────────────────────────────┘
│              ▼                        │                     ▲
│ systemd-run --user (outlives session) │                     │
│  └─ _service.py ── broker socket ─────┼─────────────────────┘
│      └─ bubblewrap sandbox            │   cs-slurm list/info/cancel, cs-run
│          bash watcher.sh              │
│          rw: project only, no network │
└───────────────────────────────────────┘
```

## Requirements

- **Python ≥ 3.10** on the login node (e.g. `module load python`).
- **Claude Code**, with `claude` on PATH.
- **`bwrap` and `socat`** on PATH. Claude Code's Linux sandbox needs both, and bubblewrap also needs unprivileged user namespaces. Check with `unshare -Ur true && echo ok`. If user namespaces work but `bwrap` isn't installed, build it from source into `~/.local/bin`; no root or setuid is needed. If user namespaces are disabled on your cluster, this design doesn't work as-is (see "Limits" below).
- **Apptainer** (or SingularityCE ≥ 3.7, which has `--no-mount`).
- **Slurm client tools** on PATH when you run `claude-slurm init`. Their absolute paths are recorded.

## Install (once)

```bash
git clone <this repo> ~/src/claude-slurm-sandbox
cd ~/src/claude-slurm-sandbox
./install.sh                    # --prefix, --bindir, --python are optional
```

The installer copies the code to `~/.local/share/claude-slurm`, builds a venv with the MCP SDK, and links `claude-hpc` and `claude-slurm` into `~/.local/bin`. The installed copy is what runs, so it doesn't matter if the git checkout sits inside a project.

## Set up a project (once per directory)

```bash
claude-slurm init ~/projects/myproject \
  --image /scratch/you/images/env.sif \
  --logs-dir /scratch/you/slurm-logs/myproject \
  --partition gpu --partition normal \
  --bind /scratch/you/datasets/mydata:/data \
  --bind /scratch/you/runs/myproject:/runs:rw \
  --account myacct \
  --max-gpus 4 --max-minutes 720 \
  --allow-domain pypi.org --allow-domain files.pythonhosted.org
```

### Policy as a JSON file

Every init flag also has a JSON key, so a policy can live in a file:

```bash
claude-slurm show ~/projects/myproject --json > myproject.json   # export an existing policy
claude-slurm init --config myproject.json                        # create it again from the file
claude-slurm init --config myproject.json --force --max-gpus 2   # flags given explicitly override the file
```

Keys: `project`, `project_mount`, `image`, `logs_dir`, `binds` (strings, or `{host, cont, mode}`), `partitions`, `account`, `qos`, `accounts`, `qos_list`, `deny_accounts` (lists), `gpu_flag`, `max_minutes` / `max_cpus` / `max_mem_gb` / `max_gpus` / `max_active` / `max_array`, `slurm_bin_dir`, `apptainer`, `apptainer_gpu_flag`, `no_mount` (list), `net_none`, `hash_image`, `job_prelude` (the snippet itself, not a file), `login_write`, `allow_domains`, `deny_read`, `auto_submit`, `ask_cancel`, `allow_project_settings`. Paths must be absolute or start with `~`. Unknown keys are an error. A repeatable flag given on the command line replaces the whole list from the file. The merged result goes through the same checks as flags.

Keep the file somewhere Claude can't write (e.g. `~/.config/claude-slurm/`), since re-running init from it applies whatever it contains. `refresh` doesn't read the file. It warns when a file in `~/.config/claude-slurm/policies/` for the project differs from the stored policy, and names the keys that differ. Re-running `init` also refreshes `install`.

### What init needs

**Required:**

| Option | Meaning |
|---|---|
| `PROJECT` (positional, default `.`) | Directory Claude may write. It's mounted read-write at `/work` in jobs. |
| `--image PATH` | The `.sif` every job runs in. It must live outside every writable area, including the project, rw binds, `--login-write` directories, and other projects. |
| `--partition P` | Allowed partition. Repeat the flag for several. |
| `--logs-dir DIR` | Where Slurm writes job logs (`<jobid>.out`, `<jobid>_<task>.out`). Created if missing. Must be on a filesystem compute nodes can write, and must not overlap the project, rw binds, `--login-write` dirs, other projects' writable areas, `~/.claude`, or claude-slurm's config/install dirs. Claude can read it but not write it, and jobs can't see it. |

**Job mounts:**

| Option | Meaning |
|---|---|
| `--bind HOST[:CONT][:ro\|rw]` | Extra mount inside jobs. Defaults to read-only at the same path. Repeatable. |
| `--project-mount /work` | Where the project appears inside jobs. |
| `--net-none` | Run jobs with `--net --network none`, if your site permits it. |
| `--no-mount LIST` | Apptainer `--no-mount` list. Default `bind-paths,hostfs` disables the site's automatic binds. |
| `--job-prelude FILE` | Bash snippet run before `apptainer`, e.g. `module load apptainer`. You control this; Claude doesn't. |

**Slurm limits** enforced by the broker. Defaults are shown in brackets.

| Option | Meaning |
|---|---|
| `--account`, `--qos` | Default for every job. |
| `--allow-account A`, `--allow-qos Q` | Extra account or QOS a job may request with `submit_job(account=..., qos=...)`. Repeat the flag for several. Without these flags, jobs always use the defaults. |
| `--deny-account A` | An account no job may use. `othergroup` also denies `othergroup:default`, `othergroup:<anything>`, etc. Case doesn't matter. Repeat the flag for several. If any account is denied, every job must name an account (directly or via `--account`), so Slurm can't fall back to your default account. `init` refuses a default or allowed account that is also denied. |
| `--max-minutes` [480] | Time limit cap. |
| `--max-cpus` [32] | CPUs per task cap. |
| `--max-mem-gb` [128] | Memory cap. |
| `--max-gpus` [4] | GPU cap. |
| `--max-active` [20] | Maximum pending + running jobs. |
| `--max-array` [100] | Maximum array tasks per submission. |
| `--gpu-flag gpus\|gres` | Request GPUs as `--gpus=N` or `--gres=gpu:N`, whichever your site uses. |

**Login-node sandbox:**

| Option | Meaning |
|---|---|
| `--login-write DIR` | Extra directory Claude's shell may write. Repeatable. |
| `--allow-domain D` | Network domain sandboxed commands may reach, e.g. pypi. Repeatable. |
| `--deny-read PATH` | Extra path hidden from Claude. `~/.ssh`, `~/.aws` and similar are always hidden. |

**Claude permissions:**

| Option | Meaning |
|---|---|
| `--auto-submit` | Don't prompt before `submit_job`. By default you approve each submission. |
| `--ask-cancel` | Prompt before cancel and update. These are auto-allowed by default. |
| `--allow-project-settings` | Load the project's own `.claude/settings*.json`. Ignored by default. |

Edits are auto-allowed in the project and `--login-write` dirs, the same set sandboxed Bash can write. The protected paths above are still denied. Network access to a domain outside `--allow-domain` prompts in the default permission mode.

Init refuses unsafe configurations:

- a project or rw bind that contains `$HOME`
- an rw bind that would expose the policy, the installed code, or any project's image
- binds that would shadow system directories such as `/etc` or `/usr`
- overlapping container paths
- a logs dir inside anything Claude or a job can write (Claude could plant a symlink at the next `<jobid>.out` and Slurm would write job output through it), or inside `~/.claude` / claude-slurm's own directories
- a read-only bind of all of `$HOME`, unless you pass `--force`

Re-run with `--force` to change a project's policy.

## Allowed commands (e.g. publishing over ssh)

Some steps need credentials the sandbox hides, like an rsync over ssh. List them under `allowed_commands` in the `--config` file. The broker runs them outside the sandbox through the `run_allowed` MCP tool:

```json
"allowed_commands": {
  "publish": {
    "description": "Publish a results dir to the web portal",
    "argv": ["rsync", "-rtm", "-e", "ssh -o BatchMode=yes", "-R",
             "--include=*/", "--include=*.html", "--exclude=*",
             "{rel_path}", "dtn:/www/outputs/"],
    "cwd": "/data/me/outputs",
    "placeholders": {"rel_path": {"type": "rel_path", "root": "/data/me/outputs"}},
    "timeout": 900, "ask": true, "min_interval_s": 0
  }
}
```

- **The command line is fixed.** Claude fills only the `{placeholders}`. There is no shell, and a value always stays one argument. `argv[0]` is resolved to an absolute path at init.
- **Placeholder types:**
  - `rel_path` must be relative. Every component must be made of `[A-Za-z0-9._+@=-]`, must not be `.` or `..`, and must not start with `-`. The path must resolve, symlinks followed, to an existing path under `root` (`kind`: `any`, `file` or `dir`). The command receives the resolved path.
  - `enum` must be one of `values`.
- **Environment:** the command gets `PATH=/usr/bin:/bin`, `HOME`, `USER`, `LOGNAME`, `LANG` and `SSH_AUTH_SOCK`, and nothing else. It runs in `cwd` with no stdin and is killed after `timeout` seconds.
- **Approval:** with `ask: true` (the default), you approve each `run_allowed` call. `min_interval_s` rate-limits a command.
- **Logging:** every run is appended to `<logs-dir>/commands.log` with its parameters, argv, exit code and output tails. Claude can read this file but not write it.
- **Limit:** Claude can still write under `root`, so it chooses what gets published there. After the check, a parent directory could also be swapped for a symlink before the command runs. Point `root` at a directory whose contents you're happy to publish, and keep `ask: true` for anything that leaves the cluster.

## Background services (opt-in)

Watchers that wait for jobs and then post-process can't run in Claude's shell: it stops with the session. They also can't use `systemd-run` directly, because a unit runs with no sandbox. Enable `services` in the `--config` file, and the broker starts them instead:

```json
"services": {
  "enabled": true, "max_services": 4, "max_lifetime_hours": 72, "memory_max_gb": 16,
  "nice": 10, "allow_submit": false, "allow_cancel": true,
  "commands": ["publish"], "path": ["/opt/myenv/bin"]
}
```

- **MCP tools:** `start_service(name, script, args)`, `list_services`, `service_status(name)` (state + log tail) and `stop_service(name)`. You approve each `start_service`.
- **The unit:** each service is `systemd-run --user --unit=cs-<project>-<name>`, so it outlives the session. With linger enabled (`loginctl show-user $USER -p Linger`), it also outlives your login. It is killed after `max_lifetime_hours` and by `claude-slurm remove`.
- **The sandbox:** the unit runs the script under bubblewrap with the same write set as Claude's shell (the project and `--login-write` dirs) and the same hidden paths. On top of that:
  - It has **no network** at all, not even allowed domains, and its own empty `/tmp`, `/run` and `/dev/shm`. So systemd, D-Bus, munge, docker and other users' sockets are out of reach.
  - It has its own PID namespace and a clean environment: `PATH` is the clients, then `services.path`, then `/usr/bin:/bin`.
  - `memory_max_gb` is an address-space rlimit. On cgroup v1 hosts, systemd's `MemoryMax`/`CPUQuota` are ignored.
- **Inside a service there is no Slurm.** Use the clients, which talk to a per-service broker socket with the project's policy and the same "own jobs only" rule:
  ```bash
  cs-slurm list --json        # instead of squeue
  cs-slurm info JOBID
  cs-slurm submit --script s.sh --partition P --minutes 60 --after-ok 123   # needs allow_submit
  cs-slurm cancel|hold|release JOBID                                        # needs allow_cancel
  cs-run publish rel_path=runs/42                                           # only names in services.commands
  ```
  Commands listed in `services.commands` and submissions with `allow_submit` run **without** your per-call approval, because nobody is there to approve. List only what you'd let run unattended.
- **Logs:** each service writes to `<logs-dir>/services/<name>.log`, including one line per broker request. Claude can read it but not write it. The service sockets live in `$XDG_RUNTIME_DIR/claude-slurm/`, which Claude's shell can't see.
- **Limit:** a service can create `.mcp.json` in the project if none exists. bwrap can only block a path by mounting over it, which would leave a stray file behind. `claude-hpc` ignores the project's `.mcp.json`.

## Verify (do this once per cluster, and after any change)

```bash
claude-slurm check ~/projects/myproject --job --partition normal --wait 600
claude-slurm check ~/projects/myproject --service        # if services are enabled
claude-slurm check ~/projects/myproject --login-probe
cd ~/projects/myproject && claude-hpc      # then ask: "run bash .slurm/login-probe.sh"
```

- **The service probe** runs the login probe's checks as a real service, plus: `systemd-run`/`systemctl --user` fail, the bus, docker and munge sockets aren't visible, there's no network, `cs-slurm list` works, and `cs-run` refuses unknown commands.

- **The job probe** runs through the real broker and checks the container from inside. It verifies that `/work` is writable, that read-only binds are read-only, and that your host home, the policy directory, the logs dir and the munge socket aren't visible. It also lists every host filesystem mount it can see and flags unexpected ones.
- **The login probe** runs inside Claude's sandbox. It tries to write outside the project, write `.claude/`, read `~/.ssh`, run `sbatch`, `squeue` and `scontrol`, and reach an unlisted domain. Every line should say `PASS`.

Inside Claude, `/sandbox` and `/status` also show the active sandbox settings and which settings files are loaded.

## Daily use

```bash
cd ~/projects/myproject     # or any subdirectory
claude-hpc                 # extra args are passed to claude, e.g. claude-hpc --model opus
```

Claude gets these MCP tools: `policy`, `submit_job`, `list_jobs`, `job_info`, `update_job`, `cancel_job`, `run_allowed` when the policy has allowed commands, and the service tools when services are enabled. A short description of the environment is appended to its system prompt (`context.md`). Logs land in the `--logs-dir` you gave at init; `job_info` lists a job's log files.

### Sandbox plain `claude` too (`install`)

`claude-hpc` passes the sandbox on its command line, so a session started any other way isn't sandboxed. That includes plain `claude`, IDE extensions, and apps that run Claude Code through the Agent SDK. `install` writes the same pieces into the files every session started in the project reads:

```bash
claude-slurm install ~/projects/myproject     # also refreshes after a policy change
claude-slurm status ~/projects/myproject      # installed? in sync with the policy?
claude-slurm uninstall ~/projects/myproject   # removes exactly what install added
```

- **Settings:** the sandbox block, the deny/ask/allow rules and `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` are merged into `.claude/settings.local.json`. Your own rules there stay. Deny beats allow, so an existing `Bash(srun *)` allow rule has no effect. `./` paths are made absolute, so a session started in a subdirectory gets the same protection. `permissions.disableBypassPermissionsMode` is set, so bypass mode can't be used in the project.
- **MCP:** the broker is added as a local-scope server (`claude mcp add-json slurm ... -s local`), so nothing is committed. Your other MCP servers and claude.ai connectors still load. They run outside the sandbox, as they do anywhere else. `--strict-mcp` loads only the broker, like `claude-hpc`. Tools an embedding app registers in-process skip that filter. Deny them with `--deny-tool mcp__<server>` (repeatable).
- **Context:** `context.md` goes into a marked block in `CLAUDE.local.md`. Both files are added to `.git/info/exclude` if git doesn't already ignore them.
- **Undo:** what was added is recorded in `<config>/install.json`, which Claude can't write. `uninstall` reverses that record. `refresh` re-installs, and `remove` uninstalls first.
- **Limits:**
  - Install at the git repository's root: Claude Code reads `.claude/settings.local.json` and local MCP servers there.
  - Plain `claude` also loads the project's `.claude/settings.json`. Lists in settings files merge, so an `excludedCommands` or `allowWrite` entry there loosens the sandbox. `status` warns about these.
  - Run `install` outside the sandbox: the sandbox write-protects `.claude/`.

Then start `claude` in the project and run the login probe (`claude-slurm check --login-probe`, then ask Claude to run `bash .slurm/login-probe.sh`).

Other commands:

```bash
claude-slurm list                         # registered projects
claude-slurm show [PROJECT]               # policy
claude-slurm refresh --accept-image       # after rebuilding the .sif (launch is blocked until you do)
claude-slurm refresh                      # re-render config after upgrading the install
claude-slurm remove [PROJECT]             # unregister (project files untouched)
```

## Files

```
~/.local/share/claude-slurm/      installed code + venv            (read-only to Claude)
~/.config/claude-slurm/projects/<name>-<hash>/
    policy.json                   the policy init wrote            (read-only to Claude)
    settings.json                 Claude Code sandbox + permission rules
    mcp.json                      broker server definition
    context.md                    appended to Claude's system prompt
    jobs.jsonl                    ledger of broker-submitted job ids (ownership)
<logs-dir>/                       Slurm job logs                   (read-only to Claude, not visible in jobs)
<project>/.slurm/                 probe scripts written by `claude-slurm check`
```

`claude-hpc` runs:

```
claude --settings <cfg>/settings.json --setting-sources user \
       --strict-mcp-config --mcp-config <cfg>/mcp.json \
       --append-system-prompt-file <cfg>/context.md
```

It sets `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` and first checks that the image is unchanged and that `bwrap` and `socat` are present.

## How the pieces enforce things

- **Claude can't reach Slurm from Bash.** Sandboxed commands get no network route and no Unix sockets, so `sbatch` can't talk to munge or slurmctld. `allowUnsandboxedCommands: false` removes the retry-outside-the-sandbox escape hatch, and `failIfUnavailable: true` refuses to start without a working sandbox.
- **Claude can't redirect job output.** Slurm opens log files as you and follows symlinks, so logs go to a separate `--logs-dir` that neither Claude nor jobs can write. `claude-slurm check` writes its probe scripts into the project without following symlinks.
- **Claude can't loosen its own config.** The sandbox denies writes to the config and install directories, the logs dir, the image, and the project's `.claude/` and `.mcp.json`. Edit-tool deny rules cover the same paths plus `~/.claude`, shell rc files and `~/.ssh`. Project settings files aren't loaded unless you pass `--allow-project-settings`.
- **The broker can't be hijacked through the project.** It runs `python -I` (isolated mode), so it never imports from its cwd, which is the project. Every Slurm option goes on the `sbatch` command line rather than into `#SBATCH` lines, and inherited `SBATCH_*` / `SLURM_*` variables are stripped. The batch script contains no user-controlled shell code; every value is quoted. Script paths must resolve inside the project, so symlinks can't escape. Cancel, update and dependencies only accept job ids in the broker's own ledger.
- **Bind mounts are fully controlled.** `--containall --no-home --no-mount bind-paths,hostfs` plus your explicit `--bind` list. Before launching Apptainer, the batch script unsets every `APPTAINER_*`, `SINGULARITY_*`, `APPTAINERENV_*` and `SINGULARITYENV_*` variable, so nothing can add binds behind your back. Selected Slurm and GPU variables (`SLURM_ARRAY_TASK_ID`, `CUDA_VISIBLE_DEVICES`, etc.) are forwarded explicitly with `APPTAINERENV_`.
- **Services can't escape their sandbox.** The broker builds the fixed `systemd-run` command itself; Claude only picks a name, a project script and its args. Inside bwrap the service has no network, no D-Bus, no systemd, no munge, and the same write set as Claude's shell. Its only way out is the per-service broker socket, served by `_service.py` outside the sandbox, which allows only the operations the policy grants (`allow_submit`, `allow_cancel`, `services.commands`).
- **The image can't be swapped silently.** Its size, mtime and inode are recorded at init. The broker refuses to submit, and the launcher refuses to start, if the image changes.

## Limits (read these)

- **Jobs still run as your UID.** The container gives you mount-namespace isolation, not privilege separation. That's the right tool against a confused agent or a prompt injection, but not against a deliberate container escape.
- **Jobs have network access** unless you pass `--net-none` and your site supports it. Anything in the bound directories could leave the cluster.
- **Your user-level `~/.claude/settings.json` still loads**, and array settings merge across files. `claude-hpc` warns if that file has `excludedCommands`, Unix-socket allowances, `allowWrite` entries or hooks.
- **The resource caps are this tool's own checks.** For limits Slurm itself enforces, ask your admins for a QOS with `MaxJobs` / `GrpTRES` and pass it with `--qos`.
- **If unprivileged user namespaces are disabled,** bubblewrap can't run. The alternative is to run Claude Code itself inside Apptainer and serve the broker over HTTP from outside the container. That isn't implemented here.

## Tests

The tests run against fake Slurm and Apptainer binaries, so no cluster is needed:

```bash
python3 -m venv /tmp/v && /tmp/v/bin/pip install 'mcp>=1.2,<2'
/tmp/v/bin/python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
