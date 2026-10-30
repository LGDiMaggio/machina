# Redistributing Images

What comes with an image built from Machina's Dockerfiles, what you owe
third parties when you hand that image to someone else, and how to generate
the evidence.

!!! note "Verification status: resolution-based"
    The components and obligations on this page come from resolving each
    image's Python dependencies and reading the base image's package list.
    Built images have not yet been scanned and reviewed, so this page does not
    describe image redistribution as verified.

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
| LGPL native libraries inside wheels: FFmpeg in `opencv-python-headless`, GEOS in `shapely`, libquadmath in the Linux wheels of `opencv-python-headless`, `numpy` and `scipy` | the RAG parsing stack (`docs-rag-parsing`, `docs-rag-pro`, `all`); `numpy` and `scipy` come with every RAG extra | Ship the licence texts the wheels carry (for example `LICENSE_GEOS`), offer the libraries' source, keep them replaceable, and do not forbid reverse engineering |
| Files under MPL-2.0: `certifi` (in the default `machina` image), `tqdm`, `orjson`, the CA bundles inside `botocore` and `grpcio` | most network extras | Tell recipients where the source is. For unmodified packages, that is the package itself |
| Apache-2.0 `NOTICE` files, for example `botocore`'s | the `litellm` and RAG extras | Pass the `NOTICE` files on with the image |
| FreeType in `pillow`; OpenSSL 1.1 in the Linux wheels of `opencv-python-headless` | the RAG parsing stack | Credit "The FreeType Project" in the documentation of what you ship. Include the OpenSSL and SSLeay acknowledgements |

Most other dependencies are under permissive licences (MIT, BSD, Apache-2.0,
ISC, PSF-2.0). For those, shipping their licence texts and notices is enough.
The notices bundle below collects those texts.

## NVIDIA's terms (Linux images with RAG extras)

On Linux, on both x86_64 and arm64, the RAG extras `docs-rag-rerank`,
`docs-rag-parsing`, `docs-rag-pro` and `all` pull the CUDA build of `torch`
from PyPI, and with it NVIDIA's CUDA wheels. These come under the
[CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html); cuDNN and
cuSPARSELt come under NVIDIA's
[software licence agreement](https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html).
Each wheel also carries its licence text.

In summary, a distributor:
- may redistribute only the files the CUDA EULA lists in its Attachment A, and cuDNN only under its own agreement;
- must distribute them inside an application that adds material functionality, never on their own;
- must not reverse engineer them;
- must bind downstream recipients to terms consistent with NVIDIA's;
- must not place them under an open-source licence that requires source disclosure. Both agreements forbid this.

Someone who runs `pip install` on their own machine accepts NVIDIA's terms
directly. The distribution duties arise only for someone who ships an image
that contains the wheels. To leave the wheels out, build with CPU-only torch
(`TORCH_INDEX_URL`, see [Build Arguments](docker.md#build-arguments)).

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
`--site-packages /usr/local/lib/python3.11/site-packages`. With another
Python version in `PYTHON_BASE`, put that version in place of `python3.11`.

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

**Not covered:**
- The base image's Debian packages: their copyright files are in
  `/usr/share/doc/` inside the image.
- The models downloaded at run time.
- The source of any library: the bundle holds texts and notices, not
  source.
