"""End-to-end tests against fake Slurm/Apptainer binaries. No cluster needed.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="cs-test-"))
HOME = TMP / "home"
FAKE = TMP / "fake"
for d in (HOME, FAKE / "bin", FAKE / "jobs"):
    d.mkdir(parents=True)
os.environ["HOME"] = str(HOME)
os.environ["CLAUDE_SLURM_CONFIG"] = str(HOME / ".config" / "claude-slurm")
os.environ["FAKE"] = str(FAKE)
os.environ["SBATCH_EXPORT"] = "ALL"     # must be scrubbed by the broker
sys.path.insert(0, str(REPO))

from claude_slurm import jobscript  # noqa: E402
from claude_slurm.core import Broker, BrokerError  # noqa: E402
from claude_slurm.jobscript import RequestError  # noqa: E402
from claude_slurm.paths import project_cfg, within  # noqa: E402


def fake(name, body):
    p = FAKE / "bin" / name
    p.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


fake("sbatch", """
    n=$(( $(ls "$FAKE/jobs" | grep -c '\\.sh$') + 1000 ))
    cat > "$FAKE/jobs/$n.sh"; printf '%s\\n' "$@" > "$FAKE/jobs/$n.args"
    env > "$FAKE/jobs/$n.env"
    echo "$n|1|job|RUNNING|0:01|10:00|cpu|node1" >> "$FAKE/queue"
    echo "$n"
""")
fake("squeue", 'cat "$FAKE/queue" 2>/dev/null; true\n')
fake("scancel", 'echo "scancel $*" >> "$FAKE/calls"\n')
fake("scontrol", 'echo "scontrol $*" >> "$FAKE/calls"\n')
fake("sacct", 'echo "$3|job|COMPLETED|0:0|00:01:00|10M|cpu=1"\n')
fake("sinfo", 'echo cpu gpu\n')
fake("apptainer", """
    if [[ "$1 $2" == "exec --help" ]]; then echo "  --no-mount strings"; exit 0; fi
    printf 'ARG %s\\n' "$@"; env | grep -E '^(APPTAINER|SINGULARITY)' | sed 's/^/ENV /' | sort
""")

PROJECT = TMP / "proj"
DATA = TMP / "data"
LOGS = TMP / "logs" / "proj"
for d in (PROJECT, DATA):
    d.mkdir()
IMAGE = TMP / "img" / "env.sif"
IMAGE.parent.mkdir()
IMAGE.write_bytes(b"fake sif")
(PROJECT / "run.sh").write_text("#!/bin/bash\n#SBATCH --account=evil\necho hi\n")


def cli(*args, check=True):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, "-m", "claude_slurm.cli", *args],
                       capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise AssertionError(f"cli {args} failed:\n{r.stdout}\n{r.stderr}")
    return r


def _norm(x):
    """JSON with list order ignored."""
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return sorted((_norm(i) for i in x), key=json.dumps)
    return x


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cli("init", str(PROJECT), "--image", str(IMAGE), "--logs-dir", str(LOGS),
            "--partition", "cpu",
            "--partition", "gpu", "--bind", f"{DATA}:/data", "--account", "acct",
            "--slurm-bin-dir", str(FAKE / "bin"), "--apptainer", str(FAKE / "bin" / "apptainer"),
            "--max-gpus", "2", "--max-array", "10", "--allow-domain", "pypi.org")
        cls.cfg = project_cfg(PROJECT)
        cls.b = Broker(cls.cfg / "policy.json")

    def test_files_written(self):
        for f in ("policy.json", "settings.json", "mcp.json", "context.md", "jobs.jsonl"):
            self.assertTrue((self.cfg / f).exists(), f)
        s = json.loads((self.cfg / "settings.json").read_text())
        self.assertFalse(s["sandbox"]["allowUnsandboxedCommands"])
        self.assertTrue(s["sandbox"]["failIfUnavailable"])
        self.assertEqual(s["sandbox"]["excludedCommands"], [])
        self.assertIn("mcp__slurm__submit_job", s["permissions"]["ask"])
        self.assertIn("Bash(sbatch *)", s["permissions"]["deny"])
        self.assertIn(f"Edit(/{PROJECT}/**)", s["permissions"]["allow"])
        self.assertIn("Edit(./.claude/**)", s["permissions"]["deny"])
        self.assertIn(f"Edit(/{self.cfg.parent.parent}/**)", s["permissions"]["deny"])
        self.assertIn(f"Edit(/{LOGS}/**)", s["permissions"]["deny"])
        self.assertIn(str(LOGS), s["sandbox"]["filesystem"]["denyWrite"])
        self.assertTrue(LOGS.is_dir())
        self.assertFalse(within(LOGS, self.cfg.parent.parent))

    def test_locate(self):
        (PROJECT / "sub").mkdir(exist_ok=True)
        out = cli("locate", str(PROJECT / "sub")).stdout.split("\n")
        self.assertEqual(out[0], str(PROJECT))
        self.assertEqual(out[2], "user")
        self.assertNotEqual(cli("locate", str(TMP), check=False).returncode, 0)

    def test_submit_and_script(self):
        res = self.b.submit(script="run.sh", partition="gpu", minutes=30, cpus=2,
                            mem_gb=8, gpus=1, name="t; rm -rf /", args=["a b", "$(x)"],
                            env={"FOO": "1,2 3"})
        jid = res["job_id"]
        args = (FAKE / "jobs" / f"{jid}.args").read_text().split("\n")
        self.assertIn("--export=NONE", args)
        self.assertIn("--account=acct", args)
        self.assertIn("--gpus=1", args)
        self.assertIn(f"--output={LOGS}/%j.out", args)
        self.assertIn("--job-name=t__rm_-rf__", args)
        env = (FAKE / "jobs" / f"{jid}.env").read_text()
        self.assertNotIn("SBATCH_EXPORT", env)
        script = (FAKE / "jobs" / f"{jid}.sh").read_text()
        self.assertNotIn("#SBATCH", script)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        # Execute the generated batch script against fake apptainer.
        run_env = {"PATH": "/usr/bin:/bin", "SLURM_JOB_ID": jid,
                   "CUDA_VISIBLE_DEVICES": "0,1", "APPTAINER_BIND": "/:/host",
                   "SINGULARITY_BINDPATH": "/etc"}
        pol = json.loads((self.cfg / "policy.json").read_text())
        pol_script = script.replace(pol["apptainer"]["bin"], str(FAKE / "bin" / "apptainer"))
        out = subprocess.run(["bash"], input=pol_script, text=True, env=run_env,
                             capture_output=True, check=True).stdout
        self.assertIn("ENV APPTAINERENV_CUDA_VISIBLE_DEVICES=0,1", out)
        self.assertIn("ENV APPTAINERENV_FOO=1,2 3", out)
        self.assertIn(f"ENV APPTAINERENV_SLURM_JOB_ID={jid}", out)
        self.assertNotIn("APPTAINER_BIND=", out)
        self.assertNotIn("SINGULARITY_BINDPATH", out)
        argv = [l[4:] for l in out.splitlines() if l.startswith("ARG ")]
        self.assertEqual(argv[:4], ["exec", "--containall", "--no-home", "--no-mount"])
        self.assertIn(f"{PROJECT}:/work:rw", argv)
        self.assertIn(f"{DATA}:/data:ro", argv)
        self.assertIn("--nv", argv)
        self.assertEqual(argv[-3:], ["/work/run.sh", "a b", "$(x)"])

    def test_rejections(self):
        bad = [
            dict(script="../x.sh", partition="cpu", minutes=1),
            dict(script="/etc/passwd", partition="cpu", minutes=1),
            dict(script="run.sh", partition="bigmem", minutes=1),
            dict(script="run.sh", partition="cpu", minutes=100000),
            dict(script="run.sh", partition="cpu", minutes=1, gpus=3),
            dict(script="run.sh", partition="cpu", minutes=1, array="0-99"),
            dict(script="run.sh", partition="cpu", minutes=1, after_ok=["999999"]),
            dict(script="run.sh", partition="cpu", minutes=1, env={"A B": "x"}),
            dict(script="run.sh", partition="cpu", minutes=True),
        ]
        for req in bad:
            with self.assertRaises(RequestError, msg=str(req)):
                self.b.submit(**req)

    def test_accounts_and_qos(self):
        proj = TMP / "p5"
        proj.mkdir()
        (proj / "run.sh").write_text("echo hi\n")
        base = ["init", str(proj), "--image", str(IMAGE), "--logs-dir", str(TMP / "logs" / "p5"),
                "--partition", "cpu", "--slurm-bin-dir", str(FAKE / "bin"),
                "--apptainer", str(FAKE / "bin" / "apptainer")]
        for extra in (["--account", "othergroup:default", "--deny-account", "othergroup"],
                      ["--allow-account", "x y"],
                      ["--account", "a", "--allow-account", "b", "--deny-account", "b"]):
            self.assertNotEqual(cli(*base, *extra, check=False).returncode, 0, extra)
        cli(*base, "--account", "mygroup:default", "--allow-account", "mygroup:other",
            "--qos", "normal", "--allow-qos", "low", "--deny-account", "othergroup")
        b = Broker(project_cfg(proj) / "policy.json")

        def args(**kw):
            jid = b.submit(script="run.sh", partition="cpu", minutes=1, **kw)["job_id"]
            return (FAKE / "jobs" / f"{jid}.args").read_text().split()

        a = args()
        self.assertIn("--account=mygroup:default", a)
        self.assertIn("--qos=normal", a)
        a = args(account="mygroup:other", qos="low")
        self.assertIn("--account=mygroup:other", a)
        self.assertIn("--qos=low", a)
        for kw in (dict(account="othergroup:default"), dict(account="OTHERGROUP"), dict(account="other"),
                   dict(qos="high"), dict(account="mygroup:default --qos=x")):
            with self.assertRaises(RequestError, msg=str(kw)):
                b.submit(script="run.sh", partition="cpu", minutes=1, **kw)
        pol = json.loads(b.describe())
        self.assertEqual(pol["accounts"]["allowed"], ["mygroup:other"])
        self.assertEqual(pol["qos"]["default"], "normal")
        ctx = (project_cfg(proj) / "context.md").read_text()
        self.assertIn("low", ctx)
        self.assertIn("`othergroup`", ctx)

    def test_refresh_warns_on_policy_file_drift(self):
        pols = HOME / ".config" / "claude-slurm" / "policies"
        pols.mkdir(parents=True, exist_ok=True)
        f = pols / "proj.json"
        f.write_text(cli("show", str(PROJECT), "--json").stdout)
        self.addCleanup(f.unlink)
        r = cli("refresh", str(PROJECT))
        self.assertNotIn("differs", r.stderr)
        d = json.loads(f.read_text())
        d["qos_list"] = ["low"]
        d["max_minutes"] = 7
        f.write_text(json.dumps(d))
        r = cli("refresh", str(PROJECT))
        self.assertIn("differs from the stored policy (max_minutes, qos_list)", r.stderr)
        self.assertIn("init --config", r.stderr)

    def test_symlink_escape(self):
        link = PROJECT / "link.sh"
        if not link.exists():
            link.symlink_to("/etc/hostname")
        with self.assertRaises(RequestError):
            self.b.submit(script="link.sh", partition="cpu", minutes=1)

    def test_ownership(self):
        jid = self.b.submit(script="run.sh", partition="cpu", minutes=5)["job_id"]
        self.b.cancel(jid)
        self.b.update(jid, minutes=3, hold=True)
        calls = (FAKE / "calls").read_text()
        self.assertIn(f"scancel {jid}", calls)
        self.assertIn(f"TimeLimit=3", calls)
        for other in ("4242", "1; rm -rf ~", ""):
            with self.assertRaises(RequestError):
                self.b.cancel(other)
        self.assertIn(jid, self.b.list_jobs())
        self.assertIn("COMPLETED", self.b.job_info(jid))
        (LOGS / f"{jid}.out").write_text("x")
        (LOGS / f"{jid}9.out").write_text("x")       # another job sharing the prefix
        info = self.b.job_info(jid)
        self.assertIn(str(LOGS / f"{jid}.out"), info)
        self.assertNotIn(f"{jid}9.out", info)

    def test_array_and_deps(self):
        a = self.b.submit(script="run.sh", partition="cpu", minutes=5, array="0-9%2")
        self.assertTrue(a["log"].endswith("%A_%a.out"))
        b = self.b.submit(script="run.sh", partition="cpu", minutes=5, after_ok=[a["job_id"]])
        args = (FAKE / "jobs" / f"{b['job_id']}.args").read_text()
        self.assertIn(f"--dependency=afterok:{a['job_id']}", args)
        c = self.b.submit(script="run.sh", partition="cpu", minutes=5,
                          after_ok=[a["job_id"]], after_any=[b["job_id"]])
        args = (FAKE / "jobs" / f"{c['job_id']}.args").read_text()
        self.assertIn(f"--dependency=afterok:{a['job_id']},afterany:{b['job_id']}", args)
        with self.assertRaises(RequestError):
            self.b.submit(script="run.sh", partition="cpu", minutes=5, after_any=["999999"])
        self.assertEqual(jobscript.array_size("0-9:2,20,30-31%3"), 8)

    def test_image_change_blocks(self):
        IMAGE.write_bytes(b"swapped image!")
        try:
            with self.assertRaises(BrokerError):
                self.b.submit(script="run.sh", partition="cpu", minutes=1)
        finally:
            cli("refresh", str(PROJECT), "--accept-image")
            self.b = Broker(self.cfg / "policy.json")
            type(self).b = self.b

    def test_config_roundtrip(self):
        exported = json.loads(cli("show", str(PROJECT), "--json").stdout)
        self.assertEqual(exported["binds"], [{"host": str(DATA), "cont": "/data", "mode": "ro"}])
        p3 = TMP / "p3"
        p3.mkdir(exist_ok=True)
        conf = TMP / "p3.json"
        conf.write_text(json.dumps({**exported, "project": str(p3), "max_gpus": 1}))
        # explicit flags win; a repeated flag replaces the file's list
        cli("init", "--config", str(conf), "--logs-dir", str(TMP / "logs" / "p3"),
            "--partition", "gpu")
        again = json.loads(cli("show", str(p3), "--json").stdout)
        self.assertEqual(again["partitions"], ["gpu"])
        self.assertEqual(again["max_gpus"], 1)
        self.assertEqual(again["logs_dir"], str(TMP / "logs" / "p3"))
        for k in set(exported) - {"project", "logs_dir", "partitions", "max_gpus"}:
            self.assertEqual(again[k], exported[k], k)
        cli("remove", str(p3))

        base = {**exported, "project": str(p3), "logs_dir": str(TMP / "logs" / "p3")}
        conf.write_text(json.dumps(base))
        cli("init", "--config", str(conf))          # the good file works
        cli("remove", str(p3))
        for bad in ({"bogus": 1}, {"image": "rel/x.sif"}, {"max_gpus": "2"},
                    {"binds": [{"path": "/x"}]}, {"binds": [{"host": str(HOME), "mode": "rw"}]},
                    {"logs_dir": str(p3 / "logs")}):
            conf.write_text(json.dumps({**base, **bad}))
            r = cli("init", "--config", str(conf), check=False)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("error", r.stderr, bad)

    def test_allowed_commands(self):
        pub = TMP / "pub"
        (pub / "run1").mkdir(parents=True, exist_ok=True)
        (pub / "run1" / "a.html").write_text("x")
        (pub / "escape").unlink(missing_ok=True)
        (pub / "escape").symlink_to(TMP / "data")
        fake("fakepub", f'printf "%s\\n" "$PWD" "$@" > {FAKE}/pub.args; env > {FAKE}/pub.env\n')
        p4 = TMP / "p4"
        p4.mkdir(exist_ok=True)
        spec = {"argv": [str(FAKE / "bin" / "fakepub"), "-R", "--include=*/", "{rel_path}",
                         "dest:/out/"],
                "cwd": str(pub), "min_interval_s": 0,
                "placeholders": {"rel_path": {"type": "rel_path", "root": str(pub)}}}
        base = {**json.loads(cli("show", str(PROJECT), "--json").stdout),
                "project": str(p4), "logs_dir": str(TMP / "logs" / "p4"),
                "allowed_commands": {"publish": spec}}
        conf = TMP / "p4.json"
        conf.write_text(json.dumps(base))
        cli("init", "--config", str(conf))
        cfg = project_cfg(p4)
        b = Broker(cfg / "policy.json")
        s = json.loads((cfg / "settings.json").read_text())
        self.assertIn("mcp__slurm__run_allowed", s["permissions"]["ask"])
        self.assertIn("`publish`", (cfg / "context.md").read_text())

        res = b.run_command("publish", {"rel_path": "run1/"})
        self.assertEqual(res["rc"], 0, res)
        self.assertEqual((FAKE / "pub.args").read_text().split("\n")[:6],
                         [str(pub), "-R", "--include=*/", "run1", "dest:/out/", ""])
        self.assertNotIn("SBATCH_EXPORT", (FAKE / "pub.env").read_text())
        log = (TMP / "logs" / "p4" / "commands.log").read_text().splitlines()
        self.assertEqual(json.loads(log[-1])["params"], {"rel_path": "run1/"})

        for name, params in (("publish", {"rel_path": "../data"}),
                             ("publish", {"rel_path": "/etc"}),
                             ("publish", {"rel_path": "-e sh"}),
                             ("publish", {"rel_path": "run1/-x"}),
                             ("publish", {"rel_path": "escape"}),        # symlink out of root
                             ("publish", {"rel_path": "missing"}),
                             ("publish", {"rel_path": "run1 x"}),
                             ("publish", {"rel_path": "."}),
                             ("publish", {"rel_path": "run1", "extra": "x"}),
                             ("publish", {}),
                             ("other", {"rel_path": "run1"})):
            with self.assertRaises(RequestError, msg=str(params)):
                b.run_command(name, params)

        spec_rl = {**spec, "min_interval_s": 3600}
        conf.write_text(json.dumps({**base, "allowed_commands": {"publish": spec_rl}}))
        cli("init", "--config", str(conf), "--force")
        b = Broker(cfg / "policy.json")
        (TMP / "logs" / "p4" / "commands" / "publish.last").unlink()
        b.run_command("publish", {"rel_path": "run1"})
        with self.assertRaises(RequestError):
            b.run_command("publish", {"rel_path": "run1"})

        bad_specs = [
            {**spec, "argv": ["{rel_path}", "x"]},                       # placeholder program
            {**spec, "argv": [spec["argv"][0], "{other}"]},              # undeclared
            {**spec, "argv": [spec["argv"][0], "x"]},                    # declared, unused
            {**spec, "argv": [spec["argv"][0], "{rel_path}}"]},          # stray brace
            {**spec, "placeholders": {"rel_path": {"type": "rel_path", "root": str(HOME)}}},
            {**spec, "placeholders": {"rel_path": {"type": "shell"}}},
            {**spec, "timeout": 0},
            {**spec, "argv": ["/nonexistent/prog", "{rel_path}"]},
            {**spec, "env": {"X": "1"}},
        ]
        for bs in bad_specs:
            conf.write_text(json.dumps({**base, "allowed_commands": {"publish": bs}}))
            r = cli("init", "--config", str(conf), "--force", check=False)
            self.assertNotEqual(r.returncode, 0, bs)
        cli("remove", str(p4))

    def test_services(self):
        from claude_slurm import services
        pol = json.loads((self.cfg / "policy.json").read_text())
        self.assertFalse(pol["services"]["enabled"])            # off by default
        with self.assertRaises(services.ServiceError):
            services.Services(pol, self.cfg, self.cfg / "policy.json").start("w", "run.sh")
        s = json.loads((self.cfg / "settings.json").read_text())
        self.assertNotIn("mcp__slurm__start_service", s["permissions"]["allow"])
        self.assertTrue(any(d.endswith("/claude-slurm") for d in s["sandbox"]["filesystem"]["denyRead"]))

        for bad in ({"bogus": 1}, {"enabled": "yes"}, {"max_services": 0},
                    {"commands": ["nope"]}, {"path": ["rel"]}, {"memory_max_gb": 0}):
            with self.assertRaises(services.ServiceError, msg=str(bad)):
                services.validate(bad, {})
        svc = services.validate({"enabled": True, "commands": []}, {})
        self.assertIn("bwrap", svc["bin"])

        pol["services"] = svc
        sock = TMP / "svc" / "x.sock"
        sock.parent.mkdir(exist_ok=True)
        argv = services.bwrap_argv(pol, PROJECT / "run.sh", ["a b"], sock, "w")
        joined = " ".join(argv)
        self.assertIn("--unshare-all", argv)
        self.assertNotIn("--share-net", argv)
        self.assertIn(f"--bind {PROJECT} {PROJECT}", joined)
        self.assertIn(f"--ro-bind {LOGS} {LOGS}", joined)
        self.assertIn(f"--ro-bind {sock} {services.SOCKET_IN_SANDBOX}", joined)
        self.assertIn("--tmpfs /run", joined)
        self.assertEqual(argv[-3:], ["/bin/bash", str(PROJECT / "run.sh"), "a b"])
        self.assertEqual(argv[argv.index("--") + 1:argv.index("--") + 3], ["/usr/bin/env", "-i"])

        for script in ("../x.sh", "/etc/passwd", "missing.sh", "."):
            with self.assertRaises(services.ServiceError, msg=script):
                services.check_script(pol, script)

        h = lambda op, **a: services.handle_request(self.b, pol, {"op": op, "args": a})
        self.assertIsInstance(h("list"), list)
        with self.assertRaises(services.ServiceError):
            h("submit", script="run.sh", partition="cpu", minutes=1)   # allow_submit false
        with self.assertRaises(services.ServiceError):
            h("run", name="publish", params={})                         # not in commands
        with self.assertRaises(services.ServiceError):
            h("exec", cmd="sh")
        with self.assertRaises(RequestError):
            h("info", job_id="999999")                                  # not ours
        pol["services"] = {**svc, "allow_submit": True}
        with self.assertRaises(services.ServiceError):
            h("submit", script="run.sh", partition="cpu", minutes=1, nodelist="n1")
        with self.assertRaises(RequestError):                           # not in the policy
            h("submit", script="run.sh", partition="cpu", minutes=1, account="evil")
        self.assertIn("job_id", h("submit", script="run.sh", partition="cpu", minutes=1))

    def test_install(self):
        proj = TMP / "p4"
        (proj / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(proj)], check=True)
        fake("claude", f"""
            exec {sys.executable} - "$@" <<'PY'
            import json, os, subprocess, sys
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                  text=True).stdout.strip() or os.getcwd()
            f = os.path.join(os.environ["HOME"], ".claude.json")
            d = json.load(open(f)) if os.path.exists(f) else {{}}
            srv = d.setdefault("projects", {{}}).setdefault(root, {{}}).setdefault("mcpServers", {{}})
            a = sys.argv[1:]
            if a[:2] == ["mcp", "add-json"]:
                if a[2] in srv: sys.exit("already exists")
                srv[a[2]] = json.loads(a[3])
            elif a[:2] == ["mcp", "remove"]:
                if a[2] not in srv: sys.exit("no such server")
                del srv[a[2]]
            json.dump(d, open(f, "w"))
            PY
        """)
        env_bin = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = str(FAKE / "bin" / "claude")
        self.addCleanup(lambda: os.environ.pop("CLAUDE_BIN") if env_bin is None
                        else os.environ.__setitem__("CLAUDE_BIN", env_bin))
        user = {"permissions": {"allow": ["Bash(srun *)", "Read(//x/**)"],
                                "deny": ["Bash(sbatch *)"]}, "model": "opus"}
        sl = proj / ".claude" / "settings.local.json"
        sl.parent.mkdir()
        sl.write_text(json.dumps(user))
        (proj / "CLAUDE.local.md").write_text("my notes\n")
        cli("init", str(proj), "--image", str(IMAGE), "--logs-dir", str(TMP / "logs" / "p4"),
            "--partition", "cpu", "--slurm-bin-dir", str(FAKE / "bin"),
            "--apptainer", str(FAKE / "bin" / "apptainer"))
        cfg = project_cfg(proj)
        self.assertEqual(cli("status", str(proj), check=False).returncode, 1)

        cli("install", str(proj))
        s = json.loads(sl.read_text())
        self.assertTrue(s["sandbox"]["enabled"])
        self.assertFalse(s["sandbox"]["allowUnsandboxedCommands"])
        self.assertEqual(s["model"], "opus")
        self.assertIn("Bash(srun *)", s["permissions"]["allow"])
        self.assertEqual(s["permissions"]["deny"].count("Bash(sbatch *)"), 1)
        self.assertIn(f"Edit(/{proj}/.claude/**)", s["permissions"]["deny"])
        self.assertNotIn("Edit(./.claude/**)", s["permissions"]["deny"])
        self.assertIn(f"{proj}/.claude", s["sandbox"]["filesystem"]["denyWrite"])
        self.assertEqual(s["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertEqual(s["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")
        for k in ("allowedMcpServers", "disableClaudeAiConnectors"):  # only with --strict-mcp
            self.assertNotIn(k, s)
        srv = json.loads((HOME / ".claude.json").read_text())["projects"][str(proj)]["mcpServers"]
        self.assertIn("slurm", srv)
        cli("install", str(proj), "--strict-mcp", "--deny-tool", "mcp__host")
        s = json.loads(sl.read_text())
        self.assertIn("mcp__host", s["permissions"]["deny"])
        self.assertTrue(s["disableClaudeAiConnectors"])
        self.assertEqual(s["allowedMcpServers"],
                         [{"serverCommand": [srv["slurm"]["command"], *srv["slurm"]["args"]]}])
        cli("install", str(proj))
        s = json.loads(sl.read_text())
        self.assertNotIn("allowedMcpServers", s)
        self.assertNotIn("mcp__host", s["permissions"]["deny"])
        md = (proj / "CLAUDE.local.md").read_text()
        self.assertTrue(md.startswith("my notes\n") and "claude-slurm:begin" in md)
        excl = (proj / ".git" / "info" / "exclude").read_text()
        self.assertIn("/CLAUDE.local.md", excl)
        r = cli("status", str(proj))
        self.assertNotIn("FAIL", r.stdout)

        # idempotent, and survives Claude Code adding its own rules in between
        s["permissions"]["allow"].append("Bash(ls *)")
        sl.write_text(json.dumps(s))
        snapshot = sl.read_text()
        cli("install", str(proj))
        self.assertEqual(_norm(json.loads(sl.read_text())), _norm(json.loads(snapshot)))
        self.assertEqual((proj / ".git" / "info" / "exclude").read_text(), excl)
        self.assertEqual((proj / "CLAUDE.local.md").read_text(), md)

        # drift: a removed entry and a policy edit both show up in status
        s = json.loads(sl.read_text())
        s["sandbox"]["filesystem"]["denyWrite"].remove(f"{proj}/.claude")
        sl.write_text(json.dumps(s))
        r = cli("status", str(proj), check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn(".claude", r.stdout)
        cli("install", str(proj))
        pol = json.loads((cfg / "policy.json").read_text())
        pol["slurm"]["max_minutes"] = 17
        (cfg / "policy.json").write_text(json.dumps(pol))
        self.assertIn("matches the current policy", cli("status", str(proj), check=False).stdout)
        self.assertEqual(cli("status", str(proj), check=False).returncode, 1)
        cli("refresh", str(proj))
        self.assertIn("17 min", (proj / "CLAUDE.local.md").read_text())
        cli("init", str(proj), "--image", str(IMAGE), "--logs-dir", str(TMP / "logs" / "p4"),
            "--partition", "cpu", "--max-minutes", "23", "--force", "--slurm-bin-dir",
            str(FAKE / "bin"), "--apptainer", str(FAKE / "bin" / "apptainer"))
        self.assertIn("23 min", (proj / "CLAUDE.local.md").read_text())   # init re-installs
        self.assertEqual(cli("status", str(proj)).returncode, 0)

        cli("uninstall", str(proj))
        after = json.loads(sl.read_text())
        user["permissions"]["allow"].append("Bash(ls *)")
        self.assertEqual(_norm(after), _norm(user))
        self.assertEqual((proj / "CLAUDE.local.md").read_text(), "my notes\n")
        self.assertNotIn("CLAUDE.local.md", (proj / ".git" / "info" / "exclude").read_text())
        srv = json.loads((HOME / ".claude.json").read_text())["projects"][str(proj)]["mcpServers"]
        self.assertNotIn("slurm", srv)
        self.assertFalse((cfg / "install.json").exists())
        self.assertIn("not installed", cli("uninstall", str(proj)).stdout)

        # a fresh project: uninstall deletes the files install created
        sl.unlink()
        (proj / "CLAUDE.local.md").unlink()
        cli("install", str(proj))
        cli("remove", str(proj))
        self.assertFalse(sl.exists())
        self.assertFalse((proj / "CLAUDE.local.md").exists())

        # a project below a git root is refused (settings would be read at the root)
        sub = proj / "sub"
        cli("init", str(sub), "--image", str(IMAGE), "--logs-dir", str(TMP / "logs" / "p4s"),
            "--partition", "cpu", "--slurm-bin-dir", str(FAKE / "bin"),
            "--apptainer", str(FAKE / "bin" / "apptainer"))
        r = cli("install", str(sub), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git repository", r.stderr)

    def test_init_guards(self):
        (TMP / "p2" / "img").mkdir(parents=True, exist_ok=True)
        (TMP / "p2" / "img" / "x.sif").write_bytes(b"x")
        cases = [
            ["init", str(TMP / "p2"), "--image", str(TMP / "p2" / "img" / "x.sif"),
             "--partition", "cpu"],
            ["init", str(IMAGE.parent.parent), "--image", str(TMP / "p2" / "img" / "x.sif"),
             "--partition", "cpu"],
            ["init", str(HOME), "--image", str(IMAGE), "--partition", "cpu"],
            ["init", str(TMP / "p2"), "--image", str(PROJECT / "run.sh"), "--partition", "cpu"],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--bind", f"{HOME}:/h:rw"],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--bind", f"{DATA}:/etc"],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--bind", f"{HOME / '.config'}:/c:rw"],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--bind", f"{DATA}:/work"],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--login-write", str(HOME)],
            # logs dir guards
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(TMP / "p2" / "logs")],                       # inside project
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(PROJECT / "logs")],                          # other project
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(HOME / ".claude" / "logs")],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(HOME / ".config" / "claude-slurm" / "logs")],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(HOME)],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(TMP)],                                        # contains projects
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(TMP / "rwdata" / "logs"),
             "--bind", f"{TMP / 'rwdata'}:/rw:rw"],
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu",
             "--logs-dir", str(HOME / ".ssh" / "logs")],                     # unreadable
            ["init", str(TMP / "p2"), "--image", str(IMAGE), "--partition", "cpu"],  # missing
        ]
        (TMP / "rwdata").mkdir(exist_ok=True)
        (TMP / "p2").mkdir(exist_ok=True)
        for c in cases:
            if "--logs-dir" not in c and c != cases[-1]:
                c = [*c, "--logs-dir", str(TMP / "logs" / "p2")]
            r = cli(*c, "--slurm-bin-dir", str(FAKE / "bin"),
                    "--apptainer", str(FAKE / "bin" / "apptainer"), check=False)
            self.assertNotEqual(r.returncode, 0, f"should fail: {c}\n{r.stdout}")

    def test_broker_ignores_project_modules(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp not installed")
        evil = PROJECT / "claude_slurm"
        evil.mkdir(exist_ok=True)
        (evil / "__init__.py").write_text("")
        (evil / "broker.py").write_text("open('/tmp/cs-pwned','w').write('x')\n")
        (evil / "core.py").write_text("open('/tmp/cs-pwned','w').write('x')\n")
        cfg = json.loads((self.cfg / "mcp.json").read_text())["mcpServers"]["slurm"]
        if os.path.exists("/tmp/cs-pwned"):
            os.remove("/tmp/cs-pwned")
        # Start the server exactly as Claude Code would (cwd = project), then close stdin.
        subprocess.run([cfg["command"], *cfg["args"]], cwd=PROJECT, input="",
                       env=dict(os.environ, **cfg["env"]), capture_output=True, text=True,
                       timeout=30)
        self.assertFalse(os.path.exists("/tmp/cs-pwned"))
        import shutil
        shutil.rmtree(evil)

    def test_probe_write_ignores_symlinks(self):
        victim = TMP / "victim"
        victim.write_text("precious")
        slurm = PROJECT / ".slurm"
        slurm.mkdir(exist_ok=True)
        link = slurm / "login-probe.sh"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(victim)
        cli("check", str(PROJECT), "--login-probe", check=False)
        self.assertEqual(victim.read_text(), "precious")
        self.assertFalse(link.is_symlink())
        self.assertIn("PASS", link.read_text())
        # .slurm itself replaced by a symlink: refuse
        import shutil
        shutil.rmtree(slurm)
        (TMP / "elsewhere").mkdir(exist_ok=True)
        slurm.symlink_to(TMP / "elsewhere")
        r = cli("check", str(PROJECT), "--login-probe", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((TMP / "elsewhere" / "login-probe.sh").exists())
        slurm.unlink()

    def test_probes_are_valid_bash(self):
        from claude_slurm import probes
        pol = json.loads((self.cfg / "policy.json").read_text())
        for s in (probes.login_probe(pol), probes.job_probe(pol)):
            subprocess.run(["bash", "-n"], input=s, text=True, check=True)

    def test_mcp_server_imports(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp not installed")
        env = dict(os.environ, PYTHONPATH=str(REPO),
                   CLAUDE_SLURM_POLICY=str(self.cfg / "policy.json"))
        code = ("import asyncio, claude_slurm.broker as b;"
                "print(sorted(t.name for t in asyncio.run(b.mcp.list_tools())))")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, check=True).stdout
        for t in ("submit_job", "cancel_job", "update_job", "list_jobs", "job_info", "policy"):
            self.assertIn(t, out)


if __name__ == "__main__":
    unittest.main()
