# Native binaries

Reviewctl remains Python. PyInstaller bundles its existing CLI, CPython and
runtime modules into one native executable. The executable uses the same
entry point as `uv tool install`; `reviewctl --version` reports the package
version. A recipient does not need to install Python for bundled operations.

The Python wheel/source release remains available. The current release
workflow does not publish native assets. The **Native build proof** workflow
is manual, uses a GitHub-hosted Linux runner, and uploads an Actions artifact;
it never publishes a release, installs onto a user's machine, or uses provider
credentials. A successful local proof does not mean that workflow has run.

## Build an exact committed revision

On the target OS and architecture, install Python 3.14 or newer. From a clean
checkout containing the packaging scripts, select an explicit source commit
and a new, private output directory outside the checkout:

```sh
REVISION="$(git rev-parse HEAD)"
BUILD_DIR="$HOME/.local/state/reviewctl/native-build-$REVISION"
python3.14 packaging/native/build-native.py \
  --repo . --revision "$REVISION" --output "$BUILD_DIR"
python3.14 "$BUILD_DIR/smoke-native-executed.py" \
  --binary "$BUILD_DIR/dist/reviewctl" \
  --require-trace \
  --expected-version "$(python3.14 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')" \
  --output "$BUILD_DIR/smoke"
```

The driver reads application source through `git archive`, so application
changes outside the selected commit are excluded. It also compares the
executed build driver, requirements, entry point and smoke script byte for
byte with that revision's archived packaging files. A mismatch fails before
creating a venv. Commit intended changes before building a distributable
artifact. For an earlier revision, use that revision's expected version;
the archived source must support the native smoke contract. The output
directory must not already exist. The build creates a private venv and
installs pinned build tools only there. It never changes the source checkout
or installs packages globally.

The current application has no runtime dependencies, packaged data files,
dynamic imports or package-metadata lookups. The native recipe collects all
Reviewctl submodules from the archived package. It rejects newly declared
runtime dependencies or non-Python package data until an explicit pinned
bundling policy and representative smoke checks are added. Build-tool pins
do not authorize silently ignoring future runtime requirements.

For an exploratory proof of an older revision without matching packaging,
`--allow-external-packaging` permits external packaging inputs and records
`packagingMatchesSource: false`. That artifact combines the recorded source
commit with separate packaging hashes; it is not a final release build of
that commit. Normal builds require `packagingMatchesSource: true`.

For a separate native runner, transfer the archive and packaging scripts,
check the archive SHA-256 against a trusted manifest, then replace `--repo`
with `--archive /path/to/source.tar`. The driver checks the git archive's
commit header against `--revision`. That consistency check does not
authenticate the archive's publisher.

Build outputs include `dist/reviewctl` (`reviewctl.exe` on Windows), the
Reviewctl license, binary SHA256SUMS, a generated PyInstaller spec,
`build-freeze.txt`, and `build-metadata.json`. Metadata records the source
commit/archive hash, package version, interpreter, platform, dependency
versions, script hashes, and binary hash. Exact executed scripts are copied
beside it. This is a repeatable build recipe, not a claim of bit-for-bit
reproducibility.

## Verify and install locally

Inspect the source revision and build metadata, verify the binary's digest
against a trusted expected hash, and run the smoke harness before installing.
For a locally built binary, SHA256SUMS detects corruption relative to that
build; it does not establish an independent publisher identity.

```sh
cd "$BUILD_DIR/dist"
sha256sum -c SHA256SUMS        # macOS: shasum -a 256 -c SHA256SUMS
./reviewctl --version
./reviewctl --help
mkdir -p "$HOME/.local/bin"
install -m 755 reviewctl "$HOME/.local/bin/reviewctl"
```

Choose an installation destination deliberately if another Reviewctl is
already installed. Add that directory to PATH as appropriate for your shell.
No Python runtime is needed after installation for the verified bundled
commands. System libraries and an executable temporary directory are still
required: one-file bundles extract runtime files before starting.

The smoke harness relocates the executable, changes cwd, uses an empty HOME
and PATH, and supplies unusable PYTHONHOME/PYTHONPATH. It generates local
synthetic fixtures to verify valid, modified and missing receipt hashes,
policy allow/deny decisions, agent guidance, project initialization and empty
findings. It does not invoke a provider, model, account or evidence service.
It also runs a formal synthetic range review through its child `run` command
with only Git available in its controlled PATH and an explicitly absent
transport executable, requires an unavailable receipt and
an unapproved aggregate, and verifies that receipt's integrity. This checks
the frozen executable's self-invocation path without inventing a successful
provider review.

Linux needs strace for `--require-trace`; install it with your system package
manager before invoking the smoke harness. The manual workflow installs it
and requires the trace. The check requires successful reads of embedded
libpython and the standard-library archive under `_MEI`, rejects external
Python library/source paths, and rejects network connections or external
interpreter execution during receipt verification. Without `--require-trace`,
an unavailable Linux tracer is explicitly recorded as `straceChecked: false`.
macOS
checks dynamic-loader output for the embedded Python and absence of build
venv, source and Homebrew dependencies.

Receipt `verify` validates canonical digest integrity and, for V2, receipt
structure. It does not verify a digital signature or establish trusted
origin, review correctness, acceptance, approval or permission to merge.
Someone able to rewrite a digest-only receipt can recompute its hash.

## Runtime boundary

The executable does not bundle subprocess transports, provider plugins,
accounts, API keys or organization policy. Install and configure optional
runtime tools from the README when selecting those features. For example,
source Git provenance and range operations still require Git;
OpenRouter still needs curl and its credential; llm, Codex, Agy, Gemini, Muse,
Pi and Age remain external when applicable. Bundling does not turn an
unqualified backend into an approved merge gate. TLS trust, optional harness
behavior and real provider flows need separate platform verification.

## Platform and distribution status

Native private proofs establish Linux x86_64 on glibc 2.39 and macOS arm64
on the tested host. They do not establish compatibility with older glibc,
musl, other Linux distributions, older macOS releases, Intel macOS, or ARM
Linux. Each deliverable must record the final committed source revision and
pass native smoke checks; earlier exploratory artifacts are not current
release assets. The manual workflow currently covers Linux x86_64 only.

Windows needs a native Windows runner and artifact verification. No Windows
executable is claimed here. PyInstaller builds for the active platform; this
workflow does not cross-compile. The smoke harness and platform packaging
must be adapted and verified on Windows before advertising support.

For Potzal distribution alongside potz and Autobahn, publish versioned native
assets from Reviewctl and have Potzal's catalog reference their source
revision, OS/architecture, digest and provenance. Do not silently rebuild
unrelated sources in the installer. Suggested asset names are
`reviewctl-vVERSION-linux-x86_64.tar.gz`,
`reviewctl-vVERSION-macos-arm64.tar.gz`, and a Windows ZIP when verified.

Before publishing native releases: establish supported platform baselines;
run the source unit/contract suite and native smoke checks; verify selected
external transports with synthetic inputs; pin dependency downloads by hash;
include complete bundled third-party license notices and an SBOM; create
checksums and attest the native assets themselves. The existing wheel/sdist
attestation does not cover a new executable automatically. Configure Developer
ID signing/notarization for macOS and appropriate Windows signing separately.
PyInstaller's ad hoc macOS signature supplies no publisher identity.

Potzal should select the verified native target, verify a trusted release
manifest/provenance, install atomically and report the installed version.
Hash integrity, publisher signature, build provenance, review approval and
deployment authorization remain distinct checks.

References: [PyInstaller operating model](https://pyinstaller.org/en/stable/operating-mode.html)
and [native build options](https://pyinstaller.org/en/stable/usage.html).

## 0.3.4 bounded-read qualification

Native artifacts for 0.3.4 must be rebuilt from the exact committed source;
existing 0.3.3 binaries do not acquire the verifier read bound from an updated
receipt or documentation. Before native build/smoke qualification, run
`tests/test_filesystem.py` and `tests/test_run_repository.py`, including sparse
oversized evidence and growth after the descriptor-size check. The default is
64 MiB per confined byte/text read. Oversized evidence must produce verification
failure, never a valid receipt result. Receipt fields cannot override this bound.
The source suite proves the Python contract on its recorded platform; qualify
each native executable separately and retain its source revision and digest.
