#!/usr/bin/env python3
"""Build uflinux3-apps APKs inside an Alpine chroot and index them.

Runs on a disposable Linux build host as root. The host only needs tar, curl
and (for a foreign architecture) qemu-user binfmt registration; every build
step happens inside a chroot unpacked from a locked Alpine minirootfs, so the
resulting APKs match what Alpine's own builders produce.

Stages
  packages  unpack the rootfs, install makedepends, run abuild, collect APKs
  index     generate and sign APKINDEX.tar.gz for every architecture found
  all       packages followed by index

Layout produced under --outdir:
  packages/<arch>/*.apk   freshly built packages, one directory per architecture
  repository/<arch>/      signed repository (APKs plus APKINDEX.tar.gz)
  repository/<key id>     the public signing key to drop in /etc/apk/keys
  pages/                  the same repository under repository_path/, plus a
                          landing page: this is what GitHub Pages serves
"""

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((PROJECT / "config/repository.json").read_text())
ARCHES = json.loads((PROJECT / "config/architectures.json").read_text())
APPS = json.loads((PROJECT / "config/apps.lock.json").read_text())["apps"]
PACKAGES = sorted([*APPS, "web-browser"])

# Alpine only ships chromium for these architectures, and so do we until the
# port is proven on the emulated ones.
BROWSER_ARCHES = set(CONFIG["browser_arches"])

# Which target architectures a host can execute without emulation, keyed by the
# `uname -m` of the host. The values are Alpine architecture names, because that
# is what `--arch` takes, and the two vocabularies differ: Alpine calls 32-bit
# x86 `x86`, not the `i386`/`i686` a kernel reports, and 32-bit ARM `armv7`, not
# `armv7l`. Getting this wrong in the strict direction is harmless - a needless
# binfmt requirement - but in the loose direction it means the build proceeds
# without translation and dies on an exec format error.
#
# An x86_64 kernel runs 32-bit x86 ELF natively (CONFIG_IA32_EMULATION, standard
# on distro kernels), and Alpine's aarch64 kernel has CONFIG_COMPAT=y, so it runs
# 32-bit ARM too. Both are how the official Alpine runners build those
# architectures without qemu.
NATIVE = {
    "x86_64": {"x86_64", "x86"},
    "aarch64": {"aarch64", "armv7"},
    "armv7l": {"armv7"},
    "ppc64le": {"ppc64le"},
    "s390x": {"s390x"},
    "riscv64": {"riscv64"},
}

# The 32-bit architectures, where the compiler itself is a 32-bit process: its
# address space is capped near 3 GiB whatever runs it, so a single-codegen-unit
# LTO link of one of the bigger binaries fails with `rustc-LLVM ERROR: out of
# memory` / `Buffer allocation failed` long before a 64-bit host would care.
# This is a property of the target, not of emulation: an x86 rustc running
# natively on an x86_64 kernel has exactly as little room as one under qemu,
# which is why the Cargo profile override below is keyed on this set and not on
# NATIVE. (The browser reaches the same ceiling from the other direction and
# needs ThinLTO to stay under it; see docs/browser.md.)
NARROW = {"x86", "armv7"}

# Free space a Rust release build plus an Alpine toolchain comfortably needs.
MINIMUM_FREE_BYTES = 10 * 1024**3


def run(args, **kwargs):
    print("+", shlex.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def digest(path, algorithm="sha512"):
    hasher = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class Build:
    def __init__(self, args):
        self.args = args
        self.arch = args.arch
        self.root = Path(args.workdir) / "root"
        self.cache = Path(args.cache).resolve()
        self.out = Path(args.outdir).resolve()
        self.packages = self.out / "packages" / self.arch
        self.repository = self.out / "repository"
        # What GitHub Pages serves: the signed repository under
        # repository_path/, the signing key next to it, and a landing page.
        self.pages = self.out / "pages"
        self.key = CONFIG["signing_key_id"]
        self.epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
        self.mounts = []
        self.env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "TERM": "dumb",
            "TZ": "UTC",
            "LC_ALL": "C.UTF-8",
            # abuild-sign accepts only RSA (SHA-1) and RSA256 (SHA-256) and
            # files the signature as .SIGN.<that>.<key id>. apk-tools 3.0.9 does
            # verify an EC signature, but it derives the digest from that member
            # name: measured on Alpine 3.24, SHA-256 is accepted and SHA-1,
            # SHA-384 and SHA-512 are all rejected as UNTRUSTED. So an EC key
            # has to be signed with SHA-256 under this misleading name, and
            # "RSA256" here means "ECDSA with a SHA-256 digest", not the key.
            "sigtype": "RSA256",
            # Keep downloads and Cargo state out of the recipe directory so an
            # Actions cache can restore them between runs.
            "SRCDEST": "/cache/distfiles",
            "CARGO_HOME": "/cache/cargo",
            "CARGO_TARGET_DIR": "/cache/target",
            "REPODEST": "/home/builder/packages",
        }
        if self.epoch:
            self.env["SOURCE_DATE_EPOCH"] = self.epoch

    # -- chroot plumbing ----------------------------------------------------

    def chroot(self, *command, **kwargs):
        env = dict(self.env, **kwargs.pop("extra_env", {}))
        argv = ["chroot", str(self.root), "/usr/bin/env", "-i",
                *[f"{key}={value}" for key, value in env.items()], *command]
        return run(argv, **kwargs)

    def write(self, name, content, mode=0o644):
        path = self.root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    def bind(self, source, destination):
        target = self.root / destination.lstrip("/")
        if Path(source).is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
        run(["mount", "--bind", source, target])
        self.mounts.append(target)

    @contextlib.contextmanager
    def mounted(self):
        try:
            self.bind("/dev", "dev")
            # Do not propagate nested device binds into the host's /dev when the
            # runner uses shared mount propagation.
            run(["mount", "--make-private", self.root / "dev"])
            for device in ("null", "zero", "full", "random", "urandom", "tty"):
                self.bind("/dev/" + device, "dev/" + device)
            self.bind("/dev/pts", "dev/pts")
            self.bind("/dev/shm", "dev/shm")
            (self.root / "proc").mkdir(exist_ok=True)
            run(["mount", "-t", "proc", "proc", self.root / "proc"])
            self.mounts.append(self.root / "proc")
            for name in ("apk", "cargo", "target", "distfiles"):
                host = self.cache / self.arch / name
                host.mkdir(parents=True, exist_ok=True)
                self.bind(host, "cache/" + name)
            yield
        finally:
            for path in reversed(self.mounts):
                if subprocess.run(["umount", "-l", str(path)], check=False).returncode:
                    print("warning: could not unmount", path, file=sys.stderr)
            self.mounts.clear()

    # -- inputs -------------------------------------------------------------

    def download(self, spec):
        cache = self.cache / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (spec["sha512"] + "-" + spec["url"].rsplit("/", 1)[1])
        if not path.exists():
            partial = path.with_suffix(".partial")
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(spec["url"], timeout=120) as response, partial.open("wb") as stream:
                        shutil.copyfileobj(response, stream)
                    if digest(partial) != spec["sha512"]:
                        raise RuntimeError("Checksum mismatch: " + spec["url"])
                    partial.replace(path)
                    break
                except OSError:
                    partial.unlink(missing_ok=True)
                    if attempt == 2:
                        raise
                    time.sleep(2 * (attempt + 1))
        elif digest(path) != spec["sha512"]:
            raise RuntimeError("Corrupt cached download: " + str(path))
        return path

    def preflight(self):
        if os.geteuid() != 0:
            raise RuntimeError("Run as root on a disposable Linux build host")
        if self.arch not in NATIVE.get(platform.machine(), set()):
            entry = next((p for p in Path("/proc/sys/fs/binfmt_misc").glob("qemu-" + ARCHES[self.arch]["qemu_user"] + "*")), None)
            if entry is None:
                raise RuntimeError("No binfmt entry for " + self.arch + "; install qemu-user-binfmt")
            information = entry.read_text()
            if "enabled" not in information or not re.search(r"^flags:.*F", information, re.M):
                raise RuntimeError("binfmt entry for " + self.arch + " is not enabled with the F (fix binary) flag")
        # The figure below is what a Craft build's target directory needs, and
        # only the build stage creates that. Indexing reads the APKs that already
        # exist and writes an index per architecture, so it must not demand the
        # same headroom - that would make "build on a big disk, index anywhere"
        # impossible and is the only thing standing between a small runner and a
        # finished repository.
        if self.args.stage in {"packages", "all"}:
            free = shutil.disk_usage(self.args.workdir).free
            if free < MINIMUM_FREE_BYTES:
                raise RuntimeError(f"Need at least {MINIMUM_FREE_BYTES // 1024**3} GiB free, found {free // 1024**3} GiB")

    def unpack(self):
        marker = self.root / ".uflinux-apps-rootfs"
        fingerprint = hashlib.sha256(json.dumps(CONFIG["rootfs"][self.arch], sort_keys=True).encode()).hexdigest()
        if marker.exists():
            if marker.read_text() != fingerprint:
                raise RuntimeError("Rootfs lock changed: use a fresh --workdir")
        else:
            if self.root.exists() and any(self.root.iterdir()):
                raise RuntimeError("Incomplete rootfs: remove this build's --workdir and retry")
            archive = self.download(CONFIG["rootfs"][self.arch])
            self.root.mkdir(parents=True, exist_ok=True)
            run(["tar", "--numeric-owner", "-xpf", archive, "-C", self.root])
            marker.write_text(fingerprint)
        resolv = self.root / "etc/resolv.conf"
        resolv.unlink(missing_ok=True)
        resolv.write_bytes(Path("/etc/resolv.conf").read_bytes())
        # apk reads its download cache from this path; mounted() binds the
        # persistent host directory underneath it, so makedepends survive a
        # runner change and never need fetching twice.
        apk_cache = self.root / "etc/apk/cache"
        if apk_cache.is_dir() and not apk_cache.is_symlink():
            shutil.rmtree(apk_cache)
        elif apk_cache.is_symlink():
            apk_cache.unlink()
        apk_cache.symlink_to("/cache/apk")
        mirror, version = CONFIG["alpine"]["mirror"], CONFIG["alpine"]["version"]
        self.write("etc/apk/repositories", "".join(f"{mirror}/v{version}/{branch}\n" for branch in CONFIG["alpine"]["branches"]))
        # Running apk is also the smoke test that this architecture can be
        # executed at all: it is the first foreign binary the build runs. If the
        # NATIVE table above ever claims more than a kernel delivers, the failure
        # here is an exec format error from chroot, which says nothing about what
        # was assumed, so ask for the architecture name instead and report the
        # assumption that broke.
        try:
            reported = self.chroot("apk", "--print-arch", stdout=subprocess.PIPE).stdout.decode().strip()
        except subprocess.CalledProcessError as failure:
            raise RuntimeError(
                f"Cannot execute the {self.arch} chroot on this {platform.machine()} host "
                f"(exit {failure.returncode}). Either install and enable qemu-user-binfmt, "
                "or, if the kernel really can run this architecture, add it to NATIVE in "
                "scripts/build.py.") from failure
        if reported != self.arch:
            raise RuntimeError(f"The chroot reports {reported!r}, expected {self.arch!r}")
        project = self.root / "project"
        if project.exists():
            shutil.rmtree(project)
        shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns(".git", ".state", ".cache", "out", "__pycache__", "*.pyc"))

    # -- signing ------------------------------------------------------------

    def signing_key(self, owner=0):
        # Paths inside the chroot: openssl runs there, so it must never see a
        # host path, and abuild reads the key names from abuild.conf, which is
        # only meaningful as an in-chroot path.
        in_root = "/var/lib/uflinux-apps/keys"
        directory = self.root / in_root.lstrip("/")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # abuild-sign derives the public key name by appending ".pub" to the
        # private one, so the two have to be <key id> and <key id>.pub.
        private = directory / Path(self.key).stem
        private_in_root = in_root + "/" + Path(self.key).stem
        public = directory / self.key
        release = os.environ.get("APK_SIGNING_PRIVATE_KEY", "")
        if release:
            expected = os.environ.get("APK_SIGNING_PUBLIC_KEY", "").replace("\r\n", "\n").strip()
            if not expected:
                raise RuntimeError("Signing needs both APK_SIGNING_PRIVATE_KEY and APK_SIGNING_PUBLIC_KEY")
            # Never reuse an existing inode: an unprivileged child could still
            # hold a descriptor to the previous key.
            private.unlink(missing_ok=True)
            fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(release.strip() + "\n")
            # -pubout is not optional: `openssl pkey -in` prints the *private*
            # key by default, which can never equal a public key, so without it
            # this check rejects a perfectly good keypair. `pkey` is also what
            # makes this check work for any algorithm, EC included.
            actual = self.chroot("openssl", "pkey", "-pubout", "-in", private_in_root,
                                 stdout=subprocess.PIPE).stdout.decode().strip()
            if actual != expected:
                raise RuntimeError("The release public and private signing keys do not match")
            public.write_text(expected + "\n")
        elif not private.exists():
            # P-384. abuild-keygen cannot generate this key, because it only ever
            # runs `openssl genrsa`, so the throwaway has to be made here. The
            # public half goes through `pkey -pubout` rather than `ec`, so the
            # file is a SubjectPublicKeyInfo like any other, which is the one
            # shape apk reads from /etc/apk/keys.
            self.chroot("openssl", "ecparam", "-name", "secp384r1", "-genkey",
                        "-noout", "-out", private_in_root)
            result = self.chroot("openssl", "pkey", "-in", private_in_root,
                                 "-pubout", stdout=subprocess.PIPE)
            public.write_bytes(result.stdout)
        os.chown(directory, owner, owner)
        os.chown(private, owner, owner)
        private.chmod(0o600)
        public.chmod(0o644)
        shutil.copy2(public, self.root / "etc/apk/keys" / self.key)
        self.env.update(PACKAGER_PRIVKEY=private_in_root,
                        PACKAGER_PUBKEY=in_root + "/" + self.key)
        return public

    # -- stages -------------------------------------------------------------

    def stage_packages(self):
        self.unpack()
        with self.mounted():
            self.chroot("apk", "add", "--no-progress", "alpine-keys", "abuild", "alpine-sdk",
                        "git", "openssl", "shadow", "sudo")
            self.chroot("sh", "-ec", "id builder >/dev/null 2>&1 || adduser -D -u 1000 builder; addgroup builder abuild 2>/dev/null || true")
            self.signing_key(owner=1000)
            source = self.root / "project/aports"
            for path in (source, *source.rglob("*")):
                os.chown(path, 1000, 1000)
            # A restored Actions cache belongs to the host user, not to uid 1000.
            self.chroot("chown", "-R", "builder:builder", "/cache/cargo", "/cache/target", "/cache/distfiles")
            self.chroot("install", "-d", "-o", "builder", "-g", "builder", "/home/builder/packages", "/home/builder/.abuild")
            self.write("home/builder/.abuild/abuild.conf",
                       f'PACKAGER="{CONFIG["packager"]}"\n'
                       f'PACKAGER_PRIVKEY="{self.env["PACKAGER_PRIVKEY"]}"\n'
                       f'PACKAGER_PUBKEY="/var/lib/uflinux-apps/keys/{self.key}"\n')
            jobs = os.environ.get("CARGO_BUILD_JOBS", "")
            # Under qemu-user, the compile job count and the final link are the
            # two places where an emulated build stops being a merely slower
            # native one. One job keeps instruction translation inside the
            # runner's memory. And because the Craft release profile asks for
            # thin LTO with a single codegen unit, the last link of the biggest
            # binary is one single-threaded pass over the whole program: measured
            # at over half an hour natively, and it is the phase most likely to
            # run past the runner's time limit - the one phase `jobs` cannot
            # shorten. Turning LTO off makes that link parallel and far shorter,
            # for a slightly larger and marginally slower binary, which is a
            # trade worth making only where the alternative is that the
            # architecture is not published at all. A 64-bit native build
            # therefore gets upstream's own profile.
            #
            # The LTO part of the override is not tied to emulation, though: a
            # 32-bit rustc OOMs on the same link on any host (see NARROW), so
            # `x86` and `armv7` get it even when their instructions run
            # natively. Only the job count stays an emulation concern.
            emulated = self.arch not in NATIVE.get(platform.machine(), set())
            cargo = {}
            if emulated:
                cargo["CARGO_BUILD_JOBS"] = "1"
            if emulated or self.arch in NARROW:
                cargo["CARGO_PROFILE_RELEASE_LTO"] = "false"
                cargo["CARGO_PROFILE_RELEASE_CODEGEN_UNITS"] = "16"
            if jobs:
                cargo["CARGO_BUILD_JOBS"] = jobs
            for package in self.args.selected():
                directory = f"cd /project/aports/apps/{package} && "
                # qemu-user cannot elevate through a setuid binary, so resolve
                # dependencies as root and compile unprivileged. Plain abuild
                # still verifies that every makedepend is installed.
                self.chroot("sh", "-ec", directory + "abuild -F deps", extra_env={"SUDO_APK": "apk"})
                try:
                    self.chroot("su", "-s", "/bin/sh", "builder", "-c", directory + "abuild",
                                extra_env=cargo)
                finally:
                    self.chroot("sh", "-ec", directory + "abuild -F undeps", extra_env={"SUDO_APK": "apk"})
            built = self.root / "home/builder/packages"
            if self.packages.exists():
                shutil.rmtree(self.packages)
            self.packages.mkdir(parents=True, exist_ok=True)
            found = 0
            for apk in sorted(built.rglob("*.apk")):
                if apk.parent.name in (self.arch, "noarch"):
                    shutil.copy2(apk, self.packages / apk.name)
                    found += 1
            if not found:
                raise RuntimeError("abuild produced no packages for " + self.arch)
            print(f"built {found} packages into {self.packages}")

    def landing_page(self, architectures):
        origin = CONFIG["origin"]
        # The Pages workflow passes the URL this site is served from; the
        # fallback keeps the page useful for a local "build.py --stage index".
        base = os.environ.get("PAGES_BASE_URL", "").strip().rstrip("/")
        repo = base + "/" + CONFIG["repository_path"] if base else \
            "https://OWNER.github.io/uflinux3-apps/" + CONFIG["repository_path"]
        lines = [
            "<!doctype html>",
            '<meta charset="utf-8">',
            "<title>" + origin + "</title>",
            "<h1>" + origin + "</h1>",
            "<p>Third-party applications for <a href=\"https://github.com/"
            "UFlinux-project/uflinux3\">UFLinux</a>, built against Alpine "
            + CONFIG["alpine"]["version"] + ".</p>",
            # apk-tools reads top-level *.list files from /etc/apk/repositories.d
            # but no package ships that directory, so it has to be created first.
            # The URL is piped through tee rather than echoed inside the elevated
            # shell, because doas resets the environment and would lose $REPO.
            "<p>On a UFLinux system:</p>",
            "<pre>REPO=\"" + repo + "\"" + ("" if base else "   # this page, minus index.html"),
            "doas wget -qO /etc/apk/keys/" + self.key + " \"$REPO/" + self.key + "\"",
            "doas mkdir -p /etc/apk/repositories.d",
            "printf '%s\\n' \"$REPO\" | doas tee /etc/apk/repositories.d/"
            + origin + ".list &gt;/dev/null",
            "doas apk update</pre>",
            "<p>Indexed for: " + ", ".join(architectures) + ".</p>",
        ]
        (self.pages / "index.html").write_text("\n".join(lines) + "\n")

    def stage_index(self):
        self.unpack()
        with self.mounted():
            self.chroot("apk", "add", "--no-progress", "alpine-keys", "abuild", "apk-tools", "openssl")
            self.signing_key()
            shutil.rmtree(self.repository, ignore_errors=True)
            self.repository.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(self.pages, ignore_errors=True)
            (self.pages / CONFIG["repository_path"]).mkdir(parents=True)
            architectures = sorted(p for p in (self.out / "packages").iterdir() if p.is_dir()) if (self.out / "packages").is_dir() else []
            if not architectures:
                raise RuntimeError("No built packages to index under " + str(self.out / "packages"))
            # Only the architectures that really produced an index are advertised
            # on the landing page: an empty directory is skipped below, and
            # promising one the site does not contain would be wrong.
            indexed = []
            for directory in architectures:
                apks = sorted(directory.glob("*.apk"))
                if not apks:
                    continue
                target = self.root / "repo" / directory.name
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True)
                for apk in apks:
                    shutil.copy2(apk, target / apk.name)
                relative = "/" + str(target.relative_to(self.root))
                try:
                    self.chroot("apk", "index", "--description", f"{CONFIG['origin']} {directory.name}",
                                "--rewrite-arch", directory.name, "--no-warnings",
                                "--output", relative + "/APKINDEX.tar.gz",
                                *[relative + "/" + apk.name for apk in apks])
                except subprocess.CalledProcessError:
                    # The usual cause is that the APKs were signed by a different
                    # key than the one being indexed with: every build shard has
                    # to see the same APK_SIGNING_PRIVATE_KEY, because apk index
                    # verifies each APK's own signature before it will describe it.
                    raise RuntimeError(
                        f"apk index failed for {directory.name}. If it reported "
                        "BAD signature, the APKs were built with a different "
                        "signing key than the one in APK_SIGNING_PRIVATE_KEY - "
                        "the build job needs the same secrets as the index job.")
                # See the `sigtype` comment: RSA256 is the SHA-256 digest, and
                # the only one apk will accept from an EC key.
                self.chroot("abuild-sign", "-t", "RSA256", relative + "/APKINDEX.tar.gz")
                final = self.repository / directory.name
                if final.exists():
                    shutil.rmtree(final)
                shutil.copytree(target, final)
                shutil.copytree(target, self.pages / CONFIG["repository_path"] /
                                directory.name)
                indexed.append(directory.name)
                print(f"indexed {len(apks)} packages for {directory.name}")
            if not indexed:
                raise RuntimeError("No built packages to index under " + str(self.out / "packages"))
            shutil.copy2(self.root / "etc/apk/keys" / self.key, self.repository / self.key)
            shutil.copy2(self.repository / self.key,
                         self.pages / CONFIG["repository_path"] / self.key)
            self.landing_page(indexed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch", choices=sorted(ARCHES), default="x86_64")
    parser.add_argument("--package", action="append", choices=PACKAGES,
                        help="Build only this package (repeatable); default: every recipe")
    parser.add_argument("--stage", choices=["packages", "index", "all"], default="all")
    parser.add_argument("--workdir", default=str(PROJECT / ".state" / "{arch}"))
    parser.add_argument("--cache", default=str(PROJECT / ".cache/build"))
    parser.add_argument("--outdir", default=str(PROJECT / "out"))
    args = parser.parse_args(argv)
    args.workdir = str(Path(args.workdir.format(arch=args.arch)).resolve())
    # preflight() measures free space on the workdir's filesystem, so it has to
    # exist before anything else looks at it.
    os.makedirs(args.workdir, exist_ok=True)
    selected = args.package or PACKAGES
    if args.stage in {"packages", "all"} and "web-browser" in selected and args.arch not in BROWSER_ARCHES:
        # Alpine only builds chromium for these architectures; the rest would
        # need out-of-tree patches, so the recipe is skipped rather than failed.
        print("note: skipping web-browser, unsupported on " + args.arch)
        selected = [p for p in selected if p != "web-browser"]
        if not selected:
            parser.error("web-browser is not built for " + args.arch)
    args.selected = lambda: selected
    build = Build(args)
    build.preflight()
    for stage in (["packages", "index"] if args.stage == "all" else [args.stage]):
        getattr(build, "stage_" + stage)()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as failure:
        print("build: " + str(failure), file=sys.stderr)
        sys.exit(1)
