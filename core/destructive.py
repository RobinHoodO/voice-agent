"""Is this shell command destructive? — the classification, as reviewable DATA.

Robin's ruling (2026-08-09): the server surface gets a FULL shell on Thrivbe-1's own
filesystem. Reads run free; anything that could destroy or change state stages through
the same spoken confirmation gate `gmail_send` already uses.

That ruling only means something if "destructive" is a thing a human can audit. So it is
not a regex buried in a handler. It is three lists and one scanner:

  * `READ_ONLY_COMMANDS`      — binaries that only ever read.
  * `READ_ONLY_SUBCOMMANDS`   — binaries whose read-only half is a named subcommand
                                (`systemctl status`, `git log`, `docker ps`).
  * `DESTRUCTIVE_COMMANDS`    — binaries that are destructive whatever the arguments.
                                Redundant with "not on the allowlist", and kept anyway:
                                it is the list Robin actually ruled on, and it turns a
                                refusal into a sentence he can check by ear ("that
                                deletes files") instead of "not recognised".
  * `WRAPPER_COMMANDS`        — binaries that RUN another command (`env`, `nohup`,
                                `timeout`, `xargs`, `sudo`, …). A wrapper has no verdict
                                of its own: it is classified RECURSIVELY from whatever
                                is left after its own flags, exactly like `$(…)`. This
                                is the hole a blind review walked through on 2026-08-09
                                — `env` was on the read-only list, so `env rm -rf /opt`
                                classified as a read and ran on Thrivbe-1 as root with
                                no spoken yes. An allowlist that looks only at argv[0]
                                is an allowlist of NAMES, not of behaviour.
  * `OPERAND_SENSITIVE`       — allowlisted binaries that read with flags and WRITE with
                                an operand: `hostname` prints, `hostname thrivbe-9`
                                sets; `date` prints, `date 010203002026` sets the clock.

A leading `VAR=value` assignment is part of the verdict, never noise. `MANPAGER`,
`PAGER`, `LESSOPEN`, `LD_PRELOAD`, `BASH_ENV`, `ENV`, `PERL5OPT`, `PYTHONSTARTUP`,
`IFS` and `PATH` all turn an allowlisted binary into arbitrary execution, and the list
of variables that can do it is open-ended — so it is not enumerated. ANY assignment in
front of a command stages, and the spoken reason names the variable. The assignments are
stripped only to find the binary being classified, never to reach the verdict.

**The default is DESTRUCTIVE.** Anything not positively recognised as a read stages.
That is the whole design: a new binary, a typo, an unparseable line, a quoting trick —
all of them fail closed. The cost of a false positive is one spoken "yes". The cost of a
false negative is Thrivbe-1.

Structure is classified too, before any binary is looked at, because the binary is not
where the damage is:

  * a redirect that WRITES a file (`> x`, `>> x`, `&> x`) — `cat` becomes a shredder;
    `2>/dev/null` and `>&1` are not writes and stay free;
  * a pipe into a shell or interpreter (`curl … | sh`);
  * `$(…)`, backticks and `<(…)` — the inner command is classified RECURSIVELY, so a
    read wrapping a write is a write;
  * `>(…)` process substitution, which writes by definition;
  * an unbalanced quote — unparseable is unknown, and unknown is destructive.

Every separator (`;`, `&&`, `||`, `|`, `&`, newline) splits the line into segments and
the verdict is the WORST of them: `ls && rm -rf /` is not a read.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

# --- the data -------------------------------------------------------------------------

# Binaries whose every invocation only reads. Deliberately boring and deliberately
# short: `find` (-delete/-exec), `sed` (-i), `curl` (-o), `awk` (system()), `ip`,
# `mount` and `tee` all have a write mode, so none of them are here — they stage.
#
# `env` is NOT here (it is a wrapper — see WRAPPER_COMMANDS) and neither is `xxd`, whose
# second positional operand is an output file (`xxd in out` writes `out`).
#
# Being on this list is not enough on its own: every entry is screened for
# state-changing FLAGS (DESTRUCTIVE_FLAGS) and for state-changing OPERANDS
# (OPERAND_SENSITIVE), and `tests/test_destructive.py` fails if a new entry is added
# without a line in one of the two review sets.
READ_ONLY_COMMANDS = frozenset({
    "arch", "base64", "basename", "bat", "cal", "cat", "cd", "cksum", "cmp", "column",
    "comm", "cut", "date", "df", "diff", "dig", "dirname", "dmesg", "du", "echo",
    "egrep", "expr", "false", "fgrep", "file", "fold", "free", "getconf",
    "getent", "grep", "groups", "head", "hexdump", "host", "hostname", "id", "jq",
    "join", "journalctl", "last", "less", "locale", "ls", "lsblk", "lscpu", "lsof",
    "man", "md5sum", "mdfind", "netstat", "nl", "nproc", "nslookup", "od", "paste",
    "pgrep", "ping", "printenv", "printf", "ps", "pwd", "readlink", "realpath", "rev",
    "rg", "seq", "sha1sum", "sha256sum", "shasum", "sleep", "sort", "ss", "stat",
    "strings", "sw_vers", "tac", "tail", "test", "tr", "tree", "true", "tty", "type",
    "uname", "uniq", "uptime", "vmstat", "w", "wc", "which", "whoami",
})

# Binaries that exist to RUN another command. They carry no verdict of their own — the
# verdict is the RECURSIVE classification of what is left after the wrapper's own flags
# and operands, so the reason Robin hears names the real command ("deletes files (rm),
# run through env") and not the wrapper.
#
#   value_flags — flags that swallow the NEXT token, or the inner command would be read
#                 off the flag's value (`nice -n 5 rm` is `rm`, not `5`).
#   operands    — leading positional arguments that belong to the WRAPPER
#                 (`timeout 5 rm` → the `5`, `chroot /mnt rm` → the `/mnt`).
#   privileged  — a wrapper that is itself a change of state. Forced destructive
#                 whatever it wraps: `sudo ls` reads as root, and running as root is the
#                 thing being asked for.
#
# A wrapper with nothing left to run is DESTRUCTIVE, not safe — `env -S 'rm -rf /'`
# hides the command inside a flag value we deliberately do not try to re-parse.
WRAPPER_COMMANDS = {
    "env": {"value_flags": ("-u", "--unset", "-C", "--chdir", "-S", "--split-string",
                            "--block-signal", "--default-signal", "--ignore-signal")},
    "nohup": {},
    "setsid": {},
    "stdbuf": {"value_flags": ("-i", "-o", "-e", "--input", "--output", "--error")},
    "nice": {"value_flags": ("-n", "--adjustment")},
    "ionice": {"value_flags": ("-c", "-n", "-p", "-P", "-u",
                               "--class", "--classdata", "--pid")},
    "timeout": {"value_flags": ("-s", "--signal", "-k", "--kill-after"), "operands": 1},
    "chroot": {"value_flags": ("--userspec", "--groups"), "operands": 1},
    "xargs": {"value_flags": ("-I", "-i", "-n", "-P", "-s", "-d", "-E", "-a", "-L",
                              "--replace", "--max-args", "--max-procs", "--delimiter",
                              "--arg-file", "--eof", "--max-chars", "--max-lines")},
    "command": {},
    "builtin": {},
    "time": {"value_flags": ("-f", "--format", "-o", "--output")},
    "watch": {"value_flags": ("-n", "--interval")},
    "sudo": {"value_flags": ("-u", "--user", "-g", "--group", "-C", "--close-from",
                             "-p", "--prompt", "-h", "--host", "-D", "--chdir",
                             "-R", "--chroot", "-T", "--command-timeout"),
             "privileged": "escalates privileges (sudo)"},
    "doas": {"value_flags": ("-u", "-C", "-a"),
             "privileged": "escalates privileges (doas)"},
    "su": {"value_flags": ("-c", "--command", "-s", "--shell", "-g", "--group"),
           "operands": 1, "privileged": "switches user (su)"},
}

# Allowlisted binaries whose OPERAND changes state even though every flag is a read.
# `hostname` prints the hostname and `hostname thrivbe-9` sets it; `date` prints the
# time and `date 010203002026` sets the clock (BSD/macOS) — both as root, both silent.
# A `+format` operand is not a write, so it is allowed through.
OPERAND_SENSITIVE = {
    "date": "sets the system clock",
    "hostname": "sets the machine hostname",
}

# Binaries that are a read only when the subcommand says so. An unrecognised
# subcommand, or none at all, is destructive: `systemctl` with a verb we do not know is
# a change we do not understand.
READ_ONLY_SUBCOMMANDS = {
    "docker": frozenset({"ps", "images", "logs", "inspect", "stats", "version", "info",
                         "top", "port", "history", "events", "diff"}),
    "git": frozenset({"log", "status", "diff", "show", "branch", "describe",
                      "rev-parse", "blame", "shortlog", "ls-files", "ls-tree",
                      "cat-file", "reflog", "whatchanged", "grep", "count-objects",
                      "remote", "ls-remote"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "version"}),
    "systemctl": frozenset({"status", "show", "cat", "list-units", "list-unit-files",
                            "list-timers", "list-sockets", "list-dependencies",
                            "is-active", "is-enabled", "is-failed", "get-default",
                            "show-environment"}),
}

# Flags that turn a listed read into a write, or into an execution. `journalctl` reads
# by default and deletes with --vacuum-*; `git branch` reads and `git branch -D` does
# not; `sort` reads and `sort -o FILE` truncates FILE; `man -P CMD` and
# `sort --compress-program CMD` run CMD.
#
# Short flags are matched per CHARACTER inside a cluster (`sort -nro out` carries `-o`),
# which over-matches now and then — `date -Iseconds` contains an `s` and stages. That is
# the right side to be wrong on: the cost is one spoken "yes".
DESTRUCTIVE_FLAGS = {
    "bat": ("--pager",),                       # --pager runs an arbitrary command
    "date": ("-s", "--set"),                   # sets the system clock
    "dmesg": ("-C", "--clear", "-c", "--read-clear", "-D", "--console-off",
              "-E", "--console-on", "-n", "--console-level"),
    "file": ("-C", "--compile"),               # writes <magicfile>.mgc
    "git": ("-D", "--delete", "--force", "--prune", "--set-upstream",
            "-c", "--exec-path"),              # -c core.pager=CMD runs CMD
    "hostname": ("-b", "--boot", "-F", "--file"),
    "journalctl": ("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate",
                   "--flush", "--sync", "--relinquish-var", "--setup-keys"),
    "kubectl": ("--delete",),
    "less": ("-o", "-O", "--log-file", "--LOG-FILE"),   # writes the input to a log file
    "man": ("-P", "--pager"),                  # runs an arbitrary pager command
    "rg": ("--pre", "--hostname-bin"),         # --pre CMD runs CMD per file
    "sort": ("-o", "--output", "--compress-program"),
    "ss": ("-K", "--kill"),                    # kills live sockets
    "tree": ("-o", "--output"),
}

# The other half of the same screen: allowlisted binaries REVIEWED and found to have no
# flag that writes, deletes, or executes. Every read-only binary must appear here or in
# DESTRUCTIVE_FLAGS — `test_every_allowlisted_binary_has_a_flag_review` fails otherwise,
# so a binary cannot join the allowlist without someone reading its man page. Reviewed
# 2026-08-09 (the review that found `sort -o`, `dmesg -C`, `date -s`, `less -o`,
# `man -P`, `rg --pre`, `tree -o`, `ss -K`, `file -C`).
NO_WRITE_FLAGS_REVIEWED = frozenset({
    # pure text/stream filters — stdout only, no output-file flag
    "base64", "cat", "column", "comm", "cut", "diff", "cmp", "expr", "fold", "head",
    "hexdump", "join", "nl", "od", "paste", "printf", "rev", "seq",
    "strings", "tac", "tail", "tr", "uniq", "wc", "echo",
    # checksums — read a file, print a digest
    "cksum", "md5sum", "sha1sum", "sha256sum", "shasum",
    # search — no -o output file, no exec flag (ripgrep's --pre is screened above)
    "grep", "egrep", "fgrep", "jq", "mdfind",
    # inspection of the machine — read-only interfaces
    "arch", "cal", "df", "du", "free", "getconf", "getent", "groups", "id", "locale",
    "lsblk", "lscpu", "lsof", "netstat", "nproc", "pgrep", "printenv", "ps", "pwd",
    "stat", "sw_vers", "test", "tty", "type", "uname", "uptime", "vmstat", "w",
    "which", "whoami", "last",
    # paths and names
    "basename", "dirname", "readlink", "realpath", "ls", "cd",
    # network lookups — resolvers, not configuration
    "dig", "host", "nslookup", "ping",
    # timing
    "sleep", "true", "false",
})

# Flags that swallow the NEXT token, so `git -C /repo log` is still `git log` and not
# "git /repo". Without this the value reads as the subcommand and a plain read stages.
FLAGS_TAKING_A_VALUE = {
    "git": ("-C", "-c", "--git-dir", "--work-tree", "--namespace"),
    "docker": ("-H", "--host", "--context", "--config"),
    "systemctl": ("-M", "--machine", "-H", "--host", "-t", "--type", "--state",
                  "-p", "--property"),
    "kubectl": ("-n", "--namespace", "--context", "--kubeconfig"),
    # Needed by the OPERAND_SENSITIVE screen: without it `date -d yesterday` reads as
    # "date with the operand `yesterday`" and a plain read would stage.
    "date": ("-d", "--date", "-r", "--reference", "-f", "--file"),
    "hostname": ("-F", "--file"),
}

# Binaries that are destructive regardless of arguments. Robin's ruling, written down.
# Anything absent from every list above is ALSO destructive — this list exists so the
# common cases get a sentence a human can check, not so the default is permissive.
DESTRUCTIVE_COMMANDS = {
    "rm": "deletes files",
    "rmdir": "deletes a directory",
    "unlink": "deletes a file",
    "shred": "destroys a file irrecoverably",
    "dd": "writes raw blocks over a device or file",
    "mkfs": "formats a filesystem",
    "mkswap": "formats a swap device",
    "fdisk": "rewrites a partition table",
    "parted": "rewrites a partition table",
    "chown": "changes file ownership",
    "chgrp": "changes file group",
    "chmod": "changes file permissions",
    "setfacl": "changes access control lists",
    "truncate": "truncates a file",
    "tee": "writes its input to a file",
    "touch": "creates or restamps a file",
    "mkdir": "creates directories",
    "mv": "moves or renames files",
    "cp": "overwrites files",
    "ln": "creates links",
    "install": "installs files over existing ones",
    "rsync": "copies over a destination tree",
    "scp": "copies files across machines",
    "sftp": "transfers files",
    "tar": "can extract over existing files",
    "unzip": "can extract over existing files",
    "zip": "writes an archive",
    "sed": "rewrites files in place with -i",
    "awk": "can write files and shell out",
    "find": "can delete or exec with -delete/-exec",
    "curl": "can write files and post data",
    "wget": "downloads to disk",
    "ssh": "runs commands on another machine",
    # `sudo`, `doas` and `su` are NOT here: they are WRAPPER_COMMANDS with
    # `privileged` set, which keeps them unconditionally destructive AND makes the
    # spoken reason name what they are about to run.
    "kill": "kills a process",
    "killall": "kills processes by name",
    "pkill": "kills processes by pattern",
    "reboot": "reboots the machine",
    "shutdown": "shuts the machine down",
    "poweroff": "powers the machine off",
    "halt": "halts the machine",
    "service": "starts or stops a service",
    "launchctl": "loads or unloads launch agents",
    "systemd-run": "runs a transient unit",
    "crontab": "rewrites scheduled jobs",
    "apt": "installs or removes packages",
    "apt-get": "installs or removes packages",
    "aptitude": "installs or removes packages",
    "dpkg": "installs or removes packages",
    "yum": "installs or removes packages",
    "dnf": "installs or removes packages",
    "rpm": "installs or removes packages",
    "snap": "installs or removes packages",
    "brew": "installs or removes packages",
    "pip": "installs packages",
    "pip3": "installs packages",
    "uv": "installs packages",
    "npm": "installs packages and runs scripts",
    "npx": "downloads and runs code",
    "yarn": "installs packages and runs scripts",
    "pnpm": "installs packages and runs scripts",
    "gem": "installs packages",
    "cargo": "builds and installs",
    "go": "builds and installs",
    "make": "runs an arbitrary build",
    "useradd": "creates a user",
    "userdel": "deletes a user",
    "usermod": "changes a user",
    "passwd": "changes a password",
    "visudo": "edits sudoers",
    "iptables": "changes firewall rules",
    "nft": "changes firewall rules",
    "ufw": "changes firewall rules",
    "ip": "changes network configuration",
    "route": "changes routing",
    "mount": "mounts a filesystem",
    "umount": "unmounts a filesystem",
    "swapoff": "disables swap",
    "sysctl": "changes kernel parameters",
    "modprobe": "loads a kernel module",
    "insmod": "loads a kernel module",
    "rmmod": "unloads a kernel module",
    "diskutil": "changes disks",
    "hdiutil": "changes disk images",
    "softwareupdate": "installs system updates",
    "csrutil": "changes system integrity protection",
    "defaults": "rewrites macOS preferences",
    # No `osascript`/`pbcopy`/`security` entry, deliberately: `core` must not so much as
    # name the macOS shell-outs (tests/test_headless_core.py enforces that, and it is
    # the rule that keeps core portable). Nothing is lost — they are not on the
    # read-only list, so they stage anyway, which is what the default is for.
    "xattr": "changes extended attributes",
    "herdr": "drives Robin's terminal panes",
    "fleet": "acts on the whole server fleet",
}

# Interpreters: a pipe into one of these is the classic "curl | sh". They are not on the
# allowlist anyway; naming them buys a legible reason.
SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "python",
                                "python3", "perl", "ruby", "node", "deno", "bun", "php",
                                "eval", "exec", "source"})

# A redirect to one of these is not a write to anything that survives.
SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

_PLACEHOLDER = "__VA_SUBST__"


@dataclass(frozen=True)
class Verdict:
    """destructive + why. The `reason` is spoken back to Robin before he confirms, so it
    has to be a sentence about the command, not about this classifier."""
    destructive: bool
    reason: str


SAFE = Verdict(False, "read-only")


# --- the scanner ----------------------------------------------------------------------

def _substitutions(text):
    """Pull out `$(…)`, `` `…` `` and `<(…)`/`>(…)`, quote-aware.

    Returns (stripped_text, inner_commands, findings). Single quotes disable
    substitution; double quotes do not — `"$(rm -rf /)"` still runs.
    """
    out, inner, findings = [], [], []
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        c = text[i]
        if in_single:
            out.append(c)
            in_single = c != "'"
            i += 1
            continue
        if c == "'":
            in_single = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_double = not in_double
            out.append(c)
            i += 1
            continue
        opener = None
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            opener, start = "$(", i + 2
        elif c in "<>" and i + 1 < n and text[i + 1] == "(":
            opener, start = c + "(", i + 2
        elif c == "`":
            opener, start = "`", i + 1
        if opener is None:
            out.append(c)
            i += 1
            continue
        if opener == "`":
            end = text.find("`", start)
            if end < 0:
                findings.append("an unterminated command substitution")
                return "".join(out), inner, findings
            inner.append(text[start:end])
            out.append(_PLACEHOLDER)
            i = end + 1
            continue
        depth, j = 1, start
        while j < n and depth:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        if depth:
            findings.append("an unterminated command substitution")
            return "".join(out), inner, findings
        body = text[start:j - 1]
        if opener == ">(":
            findings.append("a process substitution that writes")
        inner.append(body)
        out.append(_PLACEHOLDER)
        i = j
    if in_single or in_double:
        findings.append("an unbalanced quote")
    return "".join(out), inner, findings


def _segments(text):
    """Split on unquoted `;`, `&&`, `||`, `|`, `&` and newlines.

    `&` is only a separator when it is not part of a redirect: `2>&1` and `cmd &> log`
    keep their segment, or `cat x 2>&1` would split into "cat x 2>" and "1" and the
    stray "1" would classify as an unknown binary — a false alarm on the most common
    line in the corpus.
    """
    parts, buf = [], []
    in_single = in_double = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_single:
            buf.append(c)
            in_single = c != "'"
            i += 1
            continue
        if in_double:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            in_double = c != '"'
            i += 1
            continue
        if c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        separator = c in ";|\n"
        if c == "&":
            previous = "".join(buf).rstrip()[-1:]
            following = text[i + 1] if i + 1 < n else ""
            separator = previous != ">" and following != ">"
        if separator:
            parts.append("".join(buf))
            buf = []
            while i + 1 < n and text[i + 1] in ";|&":
                i += 1
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p for p in (part.strip() for part in parts) if p]


def _write_redirect(segment):
    """The target of the first unquoted redirect that writes a real file, or None.

    `2>&1`, `>&2` and `>/dev/null` are not writes. Anything else that opens a file for
    output is, whatever the command in front of it is.
    """
    in_single = in_double = False
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if in_single:
            in_single = c != "'"
            i += 1
            continue
        if in_double:
            if c == "\\":
                i += 2
                continue
            in_double = c != '"'
            i += 1
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c != ">":
            i += 1
            continue
        j = i
        while j < n and segment[j] in ">|":
            j += 1
        while j < n and segment[j] in " \t":
            j += 1
        if j < n and segment[j] == "&":
            i = j + 1                      # fd duplication: 2>&1, >&2
            continue
        target = ""
        while j < n and segment[j] not in " \t;|&<>\n":
            target += segment[j]
            j += 1
        target = target.strip("'\"")
        if target and target not in SAFE_REDIRECT_TARGETS:
            return target
        i = max(j, i + 1)
    return None


def _is_assignment(token):
    """`FOO=bar` — a shell variable assignment in front of a command."""
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and not name[0].isdigit() and name.replace("_", "").isalnum()


def _tokens(segment):
    """The segment as shell words, or None when it cannot be tokenized."""
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return None


def _split_assignments(tokens):
    """(assignment names, remaining tokens).

    The assignments are SPLIT OFF, not discarded: `_classify_tokens` needs the rest to
    find the binary, and needs the names to say why an allowlisted binary is staging.
    """
    names = []
    rest = list(tokens)
    while rest and _is_assignment(rest[0]):
        names.append(rest[0].split("=", 1)[0])
        rest = rest[1:]
    return names, rest


def _flag_hit(args, flags):
    """The first argument that is (or contains) one of `flags`, or None.

    Three shapes, because all three appear in real command lines: the exact flag
    (`sort -o out`), a long flag with an attached value (`sort --output=out`), and a
    short flag inside a cluster (`sort -nro out`).
    """
    shorts = {f[1] for f in flags if len(f) == 2 and f.startswith("-")}
    longs = tuple(f for f in flags if f.startswith("--"))
    for arg in args:
        if arg in flags:
            return arg
        for flag in longs:
            if arg.startswith(flag + "="):
                return flag
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            # Scan the cluster only while it still looks like flag letters, so
            # `sort -o/etc/hosts` is caught by its first character and a flag VALUE that
            # happens to contain one (`date -d '-1 seconds'`) is not.
            for char in arg[1:]:
                if not char.isalpha():
                    break
                if char in shorts:
                    return "-" + char
    return None


def _subcommand(binary, args):
    """The first token that is a subcommand rather than a flag or a flag's value."""
    takes_value = FLAGS_TAKING_A_VALUE.get(binary, ())
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            skip = arg in takes_value
            continue
        return arg
    return ""


def _operand(binary, args):
    """The first argument that is an operand rather than a flag or a flag's value.
    `+format` is not an operand for this purpose — `date +%s` is a read."""
    takes_value = FLAGS_TAKING_A_VALUE.get(binary, ())
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("-") and arg != "-":
            skip = arg in takes_value
            continue
        if arg.startswith("+"):
            continue
        return arg
    return None


def _wrapper_remainder(args, spec):
    """What a wrapper is about to RUN: its own flags, flag values and operands removed."""
    value_flags = spec.get("value_flags", ())
    operands = spec.get("operands", 0)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg.startswith("-") and arg != "-":
            index += 2 if arg in value_flags else 1
            continue
        if operands:
            operands -= 1
            index += 1
            continue
        break
    return args[index:]


# How deep a stack of wrappers is classified before the line is simply refused.
# `env nice timeout 1 nohup …` is not a shape any honest command has, and an unbounded
# recursion is its own denial of service.
MAX_WRAPPER_DEPTH = 8


def _classify_wrapper(binary, args, piped_into, depth):
    spec = WRAPPER_COMMANDS[binary]
    inner = _wrapper_remainder(args, spec)
    privileged = spec.get("privileged")
    if not inner:
        return Verdict(True, f"{binary} with no command after it is not a known read")
    nested = _classify_tokens(inner, piped_into, depth + 1)
    target = os.path.basename(inner[0]) if not _is_assignment(inner[0]) else inner[0]
    if privileged:
        return Verdict(True, f"{privileged} and {nested.reason}" if nested.destructive
                       else f"{privileged} to run {target}")
    if nested.destructive:
        return Verdict(True, f"{nested.reason}, run through {binary}")
    return SAFE


def _classify_binary(tokens, piped_into, depth):
    if not tokens:
        return SAFE
    binary, args = os.path.basename(tokens[0]), tokens[1:]
    if _PLACEHOLDER in tokens[0]:
        # The name of the command comes out of a command substitution, so this
        # classifier cannot know what runs: `$(echo rm) -rf /opt`.
        return Verdict(True, "takes its command name from a command substitution")
    if not binary:
        return SAFE
    if binary in SHELL_INTERPRETERS:
        return Verdict(True, "pipes into a shell" if piped_into
                       else f"runs arbitrary code through {binary}")
    if binary in WRAPPER_COMMANDS:
        return _classify_wrapper(binary, args, piped_into, depth)
    if binary in DESTRUCTIVE_COMMANDS:
        return Verdict(True, f"{DESTRUCTIVE_COMMANDS[binary]} ({binary})")
    hit = _flag_hit(args, DESTRUCTIVE_FLAGS.get(binary, ()))
    if binary in READ_ONLY_SUBCOMMANDS:
        # Subcommand before flags, so the refusal names the verb Robin would recognise:
        # "git push is not a read" beats "git --force changes state".
        verb = _subcommand(binary, args)
        if not verb:
            return Verdict(True, f"{binary} with no subcommand is not a known read")
        if verb not in READ_ONLY_SUBCOMMANDS[binary]:
            return Verdict(True, f"{binary} {verb} is not a read")
        return Verdict(True, f"{binary} {verb} {hit} changes state") if hit else SAFE
    if hit:
        return Verdict(True, f"{binary} {hit} changes state")
    if binary in OPERAND_SENSITIVE:
        operand = _operand(binary, args)
        if operand is not None:
            return Verdict(True, f"{OPERAND_SENSITIVE[binary]} ({binary} {operand})")
    if binary in READ_ONLY_COMMANDS:
        return SAFE
    return Verdict(True, f"{binary} is not on the read-only list")


def _classify_tokens(tokens, piped_into, depth=0):
    """The verdict for one already-tokenized command, assignments included.

    A leading assignment is never noise. It can hand an allowlisted binary a program to
    run (`MANPAGER='sh -c …' man ls`), and in a PERSISTENT shell — which is what both
    surfaces have — it also outlives the command that carried it. So: any assignment,
    and the read stages with the variable named. The binary is still classified first,
    because "deletes files (rm)" is a better sentence than "sets PATH".
    """
    if depth > MAX_WRAPPER_DEPTH:
        return Verdict(True, "nests command wrappers too deeply to classify")
    names, rest = _split_assignments(tokens)
    verdict = _classify_binary(rest, piped_into, depth)
    if names and not verdict.destructive:
        target = os.path.basename(rest[0]) if rest else ""
        return Verdict(True, f"sets {names[0]} before running {target}" if target
                       else f"sets {names[0]} in the shell, which outlives this command")
    return verdict


def _classify_segment(segment, piped_into, depth=0):
    tokens = _tokens(segment)
    if tokens is None:
        return Verdict(True, "could not be parsed, so it is treated as destructive")
    return _classify_tokens(tokens, piped_into, depth)


def classify(command: str) -> Verdict:
    """The verdict for one `run_shell` command line. Destructive unless proven read."""
    text = (command or "").strip()
    if not text:
        return SAFE
    stripped, inner, findings = _substitutions(text)
    if findings:
        return Verdict(True, f"contains {findings[0]}")
    for body in inner:
        nested = classify(body)
        if nested.destructive:
            return Verdict(True, f"runs a command substitution that {nested.reason}")
    # Redirects are scanned across the WHOLE line, before it is cut into segments: a
    # write is a write wherever it sits, and the scan needs the redirect operator and
    # its target still next to each other.
    redirect = _write_redirect(stripped)
    if redirect:
        return Verdict(True, f"writes to {redirect}")
    segments = _segments(stripped)
    if not segments:
        return SAFE
    piped = "|" in stripped
    for index, segment in enumerate(segments):
        verdict = _classify_segment(segment, piped_into=piped and index > 0)
        if verdict.destructive:
            return verdict
    return SAFE


def is_destructive(command: str) -> bool:
    return classify(command).destructive
