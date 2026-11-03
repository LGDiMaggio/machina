# Redistributing Images

What comes with an image built from Machina's Dockerfiles, what you owe
third parties when you hand that image to someone else, and how to generate
the evidence.

!!! note "Verification status: image inventories scanned and reconciled; redistribution compliance not verified"
    On 2 November 2026, the three default Docker images were built and scanned
    on a GitHub Actions Linux amd64 runner using the `Image evidence` workflow
    ([run 41422273364](https://github.com/LGDiMaggio/machina/actions/runs/41422273364))
    at commit `8e416ed1`. The template image installs `machina-ai` 0.4.0 from
    PyPI, so its evidence describes that release, not this commit. The two
    optional RAG variants, CUDA and CPU-only (the Machina image with
    `docs-rag-pro` added to its default extras, built with
    `MACHINA_CONSTRAINTS`), are Linux amd64 images that were built locally on
    29 October 2026 from separately recorded build inputs and scanned locally
    on 30 October 2026 with the Windows build of syft 1.54.1. The GitHub Actions
    runner did not build or scan them.

    For these five specific images, the installed Debian and Python package
    inventories were reconciled across the available syft SBOMs, Debian package
    listings and Python environment inventories. No unexplained
    package-inventory discrepancy remained.

    **The evidence is not a complete redistribution compliance assessment.**
    For some third-party distributions the notices collector found no licence
    file, and some native libraries bundled inside wheels are not named in the
    licence files collected from those wheels (see
    [Licence evidence missing from collected package files](#licence-evidence-missing-from-collected-package-files)).
    Statically linked components, embedded third-party SBOMs and corresponding
    source-code obligations have not been assessed. The CycloneDX 1.7
    documents were successfully decoded by syft but not independently
    validated against the official schema.

    The findings apply only to the recorded builds, platforms and dependency
    resolutions: Linux amd64, base image
    `python:3.11-slim-bookworm@sha256:94a8fee3…`, and the packages resolved
    when the images were built (29 October 2026 for the RAG variants, 2
    November 2026 for the default images). Other platforms, optional
    configurations and future rebuilds may contain different components. The
    workflow run's artifacts hold each default image's SBOM, package lists and
    notices bundles until 31 January 2027; the
    RAG scans and the reconciliation are kept in the maintainers' audit
    records, outside the repository.

    No conclusion is made that these images are legally compliant or ready for
    redistribution. Outstanding licence, notice and source-code obligations
    require separate review before distribution.

This page is technical guidance, not legal advice.

## What the project distributes

- **Source** (the GitHub repository and the sdist) and the **wheel** on PyPI.
  The wheel contains only the `machina` package and its `LICENSE`.
  Dependencies are declared, not bundled.
- **Dockerfiles, not images.** The project publishes no image.

The obligations below therefore fall on whoever **distributes a built
image**: pushes it to a registry others pull from, hands it to a client, or
ships it in an appliance or VM. Building and running an image inside your own
organisation distributes nothing.

## What an image contains

| Dockerfile | Python packages | Notes |
|------------|-----------------|-------|
| `deploy/docker/Dockerfile` | Machina with `MACHINA_EXTRAS` (default `cmms-rest,mcp`) and their dependencies | Installs from your checkout |
| `deploy/docker/mock-cmms/Dockerfile` | FastAPI, uvicorn and their dependencies | |
| `templates/odl-generator-from-text/Dockerfile` | `machina-ai[excel,litellm]` and their dependencies | Installs Machina from PyPI, not from your checkout |

Every image also contains its base image, `python:3.11-slim-bookworm`: about
a hundred Debian 12 packages, CPython, and the base image's own `pip`,
`setuptools`, `wheel` and `packaging`.

## Obligations by component

| Component | Where it comes from | What a redistributor must do |
|-----------|---------------------|------------------------------|
| Debian packages under GPL or LGPL (bash, coreutils, glibc, gnutls, …) | every image's base | Provide the corresponding source of the exact packages in the image, or a written offer for it. Keep `/usr/share/doc/*/copyright` in the image |
| LGPL Python libraries: asyncua, python-telegram-bot, JayDeBeApi | the `opcua`, `telegram` and `sql-jdbc` extras (`all` includes the first two) | Ship the LGPL and GPL texts and keep the notices. Provide or offer the library's source. Keep it replaceable: an unmodified package in site-packages qualifies. Do not forbid reverse engineering for debugging the library |
| LGPL native libraries inside wheels: FFmpeg in `opencv-python-headless`, GEOS in `shapely`, libquadmath in the Linux wheels of `opencv-python-headless`, `numpy` and `scipy` | the RAG parsing stack (`docs-rag-parsing`, `docs-rag-pro`, `all`); `numpy` and `scipy` come with every RAG extra | Ship the licence texts the wheels carry (for example `LICENSE_GEOS`). The collected texts do not name every library a wheel bundles: libquadmath in `opencv-python-headless` is among those listed under [Licence evidence missing from collected package files](#licence-evidence-missing-from-collected-package-files). Offer the libraries' source, keep them replaceable, and do not forbid reverse engineering |
| Files under MPL-2.0: `certifi` (in the default `machina` image), `tqdm`, `orjson`, the CA bundles inside `botocore` and `grpcio` | most network extras | Tell recipients where the source is. For unmodified packages, that is the package itself |
| Apache-2.0 `NOTICE` files, for example `botocore`'s | the `litellm` and RAG extras | Pass the `NOTICE` files on with the image |
| FreeType in `pillow`; OpenSSL 1.1 in the Linux wheels of `opencv-python-headless` | the RAG parsing stack | Credit "The FreeType Project" in the documentation of what you ship. Include the OpenSSL and SSLeay acknowledgements |

Most other dependencies are under permissive licences (MIT, BSD, Apache-2.0,
ISC, PSF-2.0). For those, shipping their licence texts and notices is enough.
The notices bundle below collects the licence and notice files it finds in
each installed distribution; where it finds none, the text has to come from
elsewhere.

## Licence evidence missing from collected package files

The notices collector reads two places in each installed distribution: its
`.dist-info/licenses/` folder, and the files its `RECORD` lists under a
licence- or notice-like name. In the scanned images, what it collected left
two kinds of gap. These are gaps in the collected evidence. They do not show
that a licence text is absent from the distribution or from the image: the
text may sit in a file with another name, inside another file, or only in the
package metadata. Before distributing an image, identify the obligations that
apply to these components and resolve the evidence gaps. The notices bundle
lists the first group as "no licence file found"; it says nothing about the
second.

- **Distributions in which the collector found no licence file:**
  `tokenizers` 0.23.3 (template and RAG images); in the RAG images, `docling`
  2.145.0, `flatbuffers` 25.12.19, `langchain-community` 0.4.2 and
  `latex2mathml` 3.81.1; in the CUDA image, `cuda-toolkit` 13.2.2. Except for
  `cuda-toolkit`, each declares a licence in its metadata although no file
  under a licence-like name was found: MIT for `docling`,
  `langchain-community` and `latex2mathml`, Apache 2.0 for `flatbuffers`, and
  the Apache Software License classifier for `tokenizers`. `cuda-toolkit`
  declares no licence.
- **Native libraries in a wheel's `*.libs` folder that the licence files
  collected from that wheel do not name** (RAG images; searched by library
  name):
    - `opencv-python-headless` 5.0.0.93: libgfortran, libquadmath, OpenBLAS
      and libdrm. Its `LICENSE-3RD-PARTY.txt` names other bundled libraries,
      FFmpeg and OpenSSL among them, but not these four.
    - `torchvision` 0.29.2: libjpeg, libpng, libwebp, libsharpyuv and zlib; in
      the CUDA image also NVIDIA's libcudart and libnvjpeg (see
      [NVIDIA's terms](#nvidias-terms-linux-images-with-rag-extras)).
    - `rtree` 1.4.1: libspatialindex.

The versions are those of the scanned images; another build may resolve
different ones. Libraries a wheel ships outside a `*.libs` folder, and code
compiled statically into extension modules, were not examined and are not in
these lists.

## NVIDIA's terms (Linux images with RAG extras)

On Linux, on both x86_64 and arm64, the RAG extras `docs-rag-rerank`,
`docs-rag-parsing`, `docs-rag-pro` and `all` pull the CUDA build of `torch`
from PyPI, and with it NVIDIA's CUDA wheels. The `nvidia-*` wheels among them
come under the [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html);
cuDNN and cuSPARSELt come under NVIDIA's
[software licence agreement](https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html).
In the scanned CUDA image, the `nvidia-*` wheels declare an NVIDIA proprietary
licence in their metadata, while `cuda-bindings` and `cuda-pathfinder`, also
pulled in, declare Apache-2.0. The notices collector found a licence file in
each of these wheels except the `cuda-toolkit` meta-package, in which it found
none and whose metadata declares no licence. Whether each file found is the
complete agreement that applies was not checked.

In the scanned CUDA image (Linux amd64), the CUDA wheels were not the only
NVIDIA components: the `torchvision` wheel also bundles NVIDIA's CUDA runtime
(`libcudart`) and nvJPEG (`libnvjpeg`) libraries in `torchvision.libs`, and
the licence file collected from `torchvision` does not mention them. Which
terms cover these copies, and what they require of a distributor, has not
been determined.

In summary, a distributor:
- may redistribute only the files the CUDA EULA lists in its Attachment A, and cuDNN only under its own agreement;
- must distribute them inside an application that adds material functionality, never on their own;
- must not reverse engineer them;
- must bind downstream recipients to terms consistent with NVIDIA's;
- must not place them under an open-source licence that requires source disclosure. Both agreements forbid this.

Someone who runs `pip install` on their own machine accepts NVIDIA's terms
directly. The distribution duties above arise for someone who ships an image
that contains the wheels; for the NVIDIA libraries bundled in `torchvision`,
see the previous paragraph. To leave the wheels out, build with CPU-only torch
(`TORCH_INDEX_URL`, see [Build Arguments](docker.md#build-arguments)). The
scanned CPU-only image contained neither the CUDA wheels nor those two
libraries.

## Generating the evidence

Run these on the image you will ship, from the repository root, with the
image's tag in place of `machina:latest`. Both write into `./evidence`.
`--user` makes the files yours on a Linux host. Docker Desktop for Windows
does not need it, so leave it out there.

**Licence and notice files of the Python packages.**
`scripts/compliance/collect_notices.py` uses the standard library only, so it
runs on the image's own Python:

```bash
mkdir -p evidence
docker run --rm --user "$(id -u):$(id -g)" --entrypoint python \
  -v "$PWD/scripts/compliance:/compliance:ro" -v "$PWD/evidence:/out" \
  machina:latest /compliance/collect_notices.py \
  --site-packages /opt/machina-venv/lib/python3.11/site-packages \
  --out /out/notices
```

The bundle has one folder per distribution and an `INDEX.json`. It lists every
distribution for which no licence file was found and supplies no missing text,
so it is not a complete notice pack. For the mock CMMS image, use
`--site-packages /usr/local/lib/python3.11/site-packages`. The Machina and
template images also keep the base image's own Python next to their virtual
environment, with its `pip`, `setuptools`, `wheel` and `packaging`: run the
command a second time with that path and `--out /out/notices-base-python`.
With another Python version in `PYTHON_BASE`, put that version in place of
`python3.11`.

**A CycloneDX SBOM of the same packages.** The SBOM tool goes into its own
temporary virtual environment, so it does not appear in the SBOM:

```bash
docker run --rm --user "$(id -u):$(id -g)" --entrypoint sh \
  -v "$PWD/evidence:/out" machina:latest -c \
  'python -m venv /tmp/cdx && /tmp/cdx/bin/pip install --quiet --no-cache-dir "cyclonedx-bom==7.5.0" &&
   /tmp/cdx/bin/cyclonedx-py environment --sv 1.6 --gather-license-texts --output-reproducible \
     -o /out/sbom.cdx.json /opt/machina-venv/bin/python'
```

The mock CMMS image has no virtual environment, so give it
`/usr/local/bin/python` as the target instead.

**Without building an image.** The repository's manual `Distribution evidence`
workflow builds the wheel and writes the same SBOM and notices bundle for six
install profiles on a fresh Linux x86_64 environment. Two of the profiles use
the images' default extras. They approximate the images, without the base
image, and are not scans of them.

**The whole image, Debian packages included.** [syft](https://github.com/anchore/syft)
writes a CycloneDX SBOM of a built image from the local Docker daemon, Debian and
Python packages alike, with the licences their metadata declares; in the
scans, syft 1.54.1 left the licence empty for distributions that declare it
only as a trove classifier, such as `tokenizers`. Download a
release from its GitHub page and check it against the release's
`checksums.txt` before running it. The repository's manual `Image evidence`
workflow does this with syft 1.54.1 for the three default images, built with no
build arguments, on Linux amd64.

```bash
syft scan docker:machina:latest -o cyclonedx-json=evidence/sbom-image.cdx.json
```

The source packages behind the Debian packages, for a source offer, and the
native libraries bundled inside Python wheels:

```bash
docker run --rm --entrypoint dpkg-query machina:latest -W \
  -f='${Package}\t${Version}\t${source:Package}\t${source:Version}\n' > evidence/debian.tsv
docker run --rm --entrypoint sh machina:latest -c \
  'find /opt/machina-venv/lib/python3.11/site-packages -path "*.libs/*" -name "*.so*"' \
  > evidence/bundled-libs.txt
```

**Not covered:**
- The source of any package: the commands above list Debian source packages
  and the bundle holds licence texts and notices; neither fetches source.
- The models downloaded at run time.
- Code compiled statically into extension modules, and the third-party SBOMs
  some wheels embed in their `.dist-info`: neither the scans nor the notices
  bundle read them.
- Validation against the official CycloneDX schema: syft 1.54.1 writes
  CycloneDX 1.7 and reads it back, which is not schema validation.
- What a scan cannot decide: whether an obligation is met, and the licence of
  a file that no package metadata describes.
