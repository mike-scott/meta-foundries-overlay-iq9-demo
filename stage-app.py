#!/usr/bin/env python3
"""Stage a compose app for `fiocli updates upload` without a container registry.

Takes each service image as an OCI image layout tarball (plain .tar), validates
them, extracts only the blobs for --arch into <update-dir>/apps/blobs, writes the
compose app artifact (pinned compose bundle, bundle index, app manifest) the way
`composectl publish` would, and lets `composectl pull` verify the store and build
<update-dir>/apps/apps/<name>/<sha256>.

usage: stage-app.py --app-ref <host>/[<repo>/]<name>
                    --image <compose-image>=<image.tar> [--image ...]
                    <app-dir> <update-dir> [--arch arm64]
"""

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

import yaml

# Mirrors composeapp pkg/compose/v1/app.go and internal/manifest.go.
APP_LAYER_MEDIA_TYPE = "application/octet-stream"
APP_SERVICE_HASH_LABEL = "io.compose-spec.config-hash"
ANN_BUNDLE_INDEX_DIGEST = "org.foundries.app.bundle.index.digest"
ANN_BUNDLE_INDEX_SIZE = "org.foundries.app.bundle.index.size"
EMPTY_JSON = b"{}"
APP_MANIFEST_MAX_SIZE = 50 * 1024

INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
# Manifests, indexes and configs; anything larger is not metadata.
MAX_METADATA_SIZE = 4 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
COMPRESSED_MAGIC = {
    b"\x1f\x8b": "gzip",
    b"\x28\xb5\x2f\xfd": "zstd",
    b"BZh": "bzip2",
    b"\xfd7zXZ\x00": "xz",
}


def die(msg):
    sys.exit(f"stage-app: {msg}")


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def blob_path(store, digest):
    return os.path.join(store, "blobs", "sha256", digest.split(":", 1)[1])


def blob_ok(path, digest, size):
    return os.path.exists(path) and os.path.getsize(path) == size and file_digest(path) == digest


def write_blob(store, digest, chunks):
    """Atomically add a blob unless a valid copy exists; returns False if it was present.

    An existing blob is never replaced: apps/<name>/<sha256> hardlinks into the
    blob store, and other apps staged in the same store may share it.
    """
    dst = blob_path(store, digest)
    tmp_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".", suffix=".part")
    try:
        h = hashlib.sha256()
        with os.fdopen(tmp_fd, "wb") as out:
            for chunk in chunks:
                h.update(chunk)
                out.write(chunk)
        if "sha256:" + h.hexdigest() != digest:
            die(f"blob {digest} content does not match its digest")
        os.chmod(tmp, 0o644)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def put_blob(store, data):
    desc = {"digest": sha256(data), "size": len(data)}
    if not blob_ok(blob_path(store, desc["digest"]), desc["digest"], desc["size"]):
        write_blob(store, desc["digest"], [data])
    return desc


def strip_tag(image):
    name = image.split("@", 1)[0]
    slash = name.rfind("/")
    colon = name.rfind(":")
    return name[:colon] if colon > slash else name


def normalize_repo(image):
    """Fully qualified repo, as publish pins it (reference.Domain + "/" + reference.Path)."""
    name = strip_tag(image)
    first, sep, rest = name.partition("/")
    if not sep or ("." not in first and ":" not in first and first != "localhost"):
        first, rest = "docker.io", name
    if first == "docker.io" and "/" not in rest:
        rest = "library/" + rest
    return f"{first}/{rest}"


class OciTar:
    """Random access to an OCI image layout inside an uncompressed tar."""

    def __init__(self, path):
        if not path.endswith(".tar"):
            die(f"{path}: --image must be a plain .tar file")
        with open(path, "rb") as f:
            head = f.read(8)
        for magic, kind in COMPRESSED_MAGIC.items():
            if head.startswith(magic):
                die(f"{path}: is {kind}-compressed; --image must be an uncompressed .tar")
        try:
            # "r:" refuses compressed input and keeps the file seekable.
            self.tar = tarfile.open(path, mode="r:")
        except tarfile.TarError as e:
            die(f"{path}: not a tar archive: {e}")
        # Reads headers only; member data is seeked over.
        self.members = {m.name.removeprefix("./"): m for m in self.tar.getmembers()}
        self.path = path

    def member(self, name):
        m = self.members.get(name)
        if m is None:
            return None
        if not m.isfile():
            die(f"{self.path}: '{name}' is not a regular file")
        return m

    def read_json(self, name):
        m = self.member(name)
        if m is None:
            die(f"{self.path}: missing '{name}'; not an OCI image layout "
                "(docker export / legacy docker save archives are not supported)")
        return json.load(self.tar.extractfile(m))

    def blob_member(self, desc):
        algo, _, hexdigest = desc["digest"].partition(":")
        if algo != "sha256":
            die(f"unsupported digest algorithm in {desc['digest']}")
        m = self.member(f"blobs/sha256/{hexdigest}")
        if m is None:
            die(f"{self.path}: blob {desc['digest']} ({desc.get('mediaType')}) is missing")
        if m.size != desc["size"]:
            die(f"{self.path}: blob {desc['digest']} is {m.size} bytes, descriptor says {desc['size']}")
        return m

    def read_blob(self, desc):
        m = self.blob_member(desc)
        if m.size > MAX_METADATA_SIZE:
            die(f"{desc['digest']} ({desc.get('mediaType')}) is too large to be metadata")
        data = self.tar.extractfile(m).read()
        if sha256(data) != desc["digest"]:
            die(f"{self.path}: blob {desc['digest']} content does not match its digest")
        return data

    def extract_blob(self, desc, store):
        """Stream one blob into the store, verifying its digest on the way."""
        if blob_ok(blob_path(store, desc["digest"]), desc["digest"], desc["size"]):
            return False
        src = self.tar.extractfile(self.blob_member(desc))
        return write_blob(store, desc["digest"], iter(lambda: src.read(COPY_CHUNK), b""))


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(COPY_CHUNK):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def resolve_image(oci, arch):
    """Validate the layout and return (root digest, descriptors to extract) for arch."""
    layout = oci.read_json("oci-layout")
    if "imageLayoutVersion" not in layout:
        die(f"{oci.path}: oci-layout has no imageLayoutVersion")
    roots = oci.read_json("index.json").get("manifests", [])
    if len(roots) != 1:
        die(f"{oci.path}: index.json must reference exactly one image, found {len(roots)}")
    root = roots[0]
    needed = [root]

    if root.get("mediaType") in INDEX_TYPES:
        index = json.loads(oci.read_blob(root))
        matches = [m for m in index.get("manifests", [])
                   if m.get("platform", {}).get("os") == "linux"
                   and m.get("platform", {}).get("architecture") == arch]
        if len(matches) != 1:
            found = sorted({m.get("platform", {}).get("architecture", "?") for m in index.get("manifests", [])})
            die(f"image index has {len(matches)} linux/{arch} manifests (architectures: {', '.join(found)})")
        manifest_desc = matches[0]
        needed.append(manifest_desc)
    elif root.get("mediaType") in MANIFEST_TYPES:
        manifest_desc = root
    else:
        die(f"unsupported root media type {root.get('mediaType')}")

    manifest = json.loads(oci.read_blob(manifest_desc))
    if manifest.get("mediaType", manifest_desc.get("mediaType")) not in MANIFEST_TYPES:
        die(f"{manifest_desc['digest']} is not an image manifest")
    config = json.loads(oci.read_blob(manifest["config"]))
    if config.get("architecture") != arch or config.get("os") != "linux":
        die(f"image is {config.get('os')}/{config.get('architecture')}, expected linux/{arch}")
    needed.append(manifest["config"])
    for layer in manifest.get("layers", []):
        oci.blob_member(layer)
        needed.append(layer)
    return root["digest"], needed


def pin_compose(compose, digests):
    """Same transform as publish's pinServiceImages + pinServiceConfigs."""
    pinned = {}
    for name, svc in compose["services"].items():
        image = svc["image"]
        pinned[image] = f"{normalize_repo(image)}@{digests[image]}"
        svc.pop("build", None)
        svc["image"] = pinned[image]
        labels = svc.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(l.split("=", 1) if "=" in l else (l, "") for l in labels)
        # Device-side composectl only compares this hash to detect config drift; it
        # need not be byte-identical to the Go yaml.v3 rendering publish hashes.
        labels[APP_SERVICE_HASH_LABEL] = hashlib.sha256(
            yaml.safe_dump(svc, sort_keys=True).encode()).hexdigest()
        svc["labels"] = labels
    return yaml.safe_dump(compose, sort_keys=False).encode(), pinned


def read_ignores(app_dir):
    path = os.path.join(app_dir, ".composeappignores")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        pats = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return pats + [".composeappignores"]


def make_bundle(app_dir, pinned_compose):
    """Deterministic tgz of the app dir with the pinned compose, like publish's createTgz."""
    ignores = read_ignores(app_dir)
    hashes = {}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(app_dir):
            dirs.sort()
            for entry in sorted(dirs) + sorted(files):
                full = os.path.join(root, entry)
                rel = os.path.relpath(full, app_dir)
                if any(fnmatch.fnmatch(rel, p) for p in ignores):
                    if entry in dirs:
                        dirs.remove(entry)
                    continue
                info = tar.gettarinfo(full, arcname=rel)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if not info.isfile():
                    tar.addfile(info)
                    continue
                if rel == "docker-compose.yml":
                    data = pinned_compose
                else:
                    with open(full, "rb") as f:
                        data = f.read()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                hashes[rel] = sha256(data)
    if "docker-compose.yml" not in hashes:
        die("docker-compose.yml missing from bundle (excluded by .composeappignores?)")
    return gzip.compress(raw.getvalue(), mtime=0), hashes


def describe(data):
    return {"digest": sha256(data), "size": len(data)}


def make_app_blobs(bundle, hashes):
    """Build the app artifact in memory; returns (app digest, blobs to store)."""
    index = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    index_desc = describe(index)
    bundle_desc = describe(bundle)
    config_desc = describe(EMPTY_JSON)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.empty.v1+json", **config_desc},
        "layers": [{
            "mediaType": APP_LAYER_MEDIA_TYPE, **bundle_desc,
            "annotations": {
                ANN_BUNDLE_INDEX_DIGEST: index_desc["digest"],
                ANN_BUNDLE_INDEX_SIZE: str(index_desc["size"]),
            },
        }],
        "annotations": {"compose-app": "v1"},
        "artifactType": "application/vnd.fio+compose-app",
    }
    data = json.dumps(manifest, indent=3).encode()
    if len(data) >= APP_MANIFEST_MAX_SIZE:
        die(f"app manifest is {len(data)} bytes, limit {APP_MANIFEST_MAX_SIZE}")
    return sha256(data), [index, bundle, EMPTY_JSON, data]


def check_no_other_version(store, app_name, app_digest):
    """The update-server reads one version per app name; a second one would be ambiguous."""
    app_dir = os.path.join(store, "apps", app_name)
    if not os.path.isdir(app_dir):
        return
    others = sorted(set(os.listdir(app_dir)) - {app_digest.split(":", 1)[1]})
    if others:
        die(f"{app_dir} already holds another version ({', '.join(others)}); "
            "stage each app version into a fresh update dir")


def parse_image_args(values, compose):
    """Map each compose image to its tarball; every image needs exactly one."""
    tars = {}
    for value in values:
        image, sep, path = value.rpartition("=")
        if not sep or not image or not path:
            die(f"--image '{value}': expected <compose-image>=<image.tar>")
        if image in tars:
            die(f"--image given twice for '{image}'")
        tars[image] = path
    used = {}
    for name, svc in compose["services"].items():
        image = svc.get("image")
        if not image:
            die(f"service '{name}' has no image; build-only services cannot be staged")
        used[image] = name
    if missing := sorted(set(used) - set(tars)):
        die("no --image for: " + ", ".join(f"'{i}' (service '{used[i]}')" for i in missing))
    if unused := sorted(set(tars) - set(used)):
        die("--image not used by any service: " + ", ".join(f"'{i}'" for i in unused))
    return tars


def parse_app_ref(ref):
    """Validate --app-ref the way composectl's ParseAppRef splits it; returns the app name."""
    if "@" in ref:
        die(f"--app-ref '{ref}': must not include a digest; it is computed")
    host, _, path = ref.partition("/")
    parts = path.split("/") if path else []
    if not host or not 1 <= len(parts) <= 2 or not all(parts):
        die(f"--app-ref '{ref}': expected <host>/[<repo>/]<name>")
    if ":" in parts[-1]:
        die(f"--app-ref '{ref}': must not include a tag")
    return parts[-1]


def find_composectl(name):
    """Resolve composectl and make sure it can pull from a local store, before any work."""
    exe = shutil.which(name)
    if exe is None:
        die(f"composectl not found ('{name}'); install it or pass --composectl <path>")
    try:
        out = subprocess.run([exe, "pull", "--help"], capture_output=True, text=True, timeout=30)
    except OSError as e:
        die(f"cannot run {exe}: {e}")
    except subprocess.TimeoutExpired:
        die(f"{exe} pull --help did not finish")
    # Older composectl releases cannot pull from a local store.
    if out.returncode != 0 or "--source-store-path" not in out.stdout + out.stderr:
        die(f"{exe} does not support 'pull --source-store-path'; a newer composectl is required")
    return exe


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app-ref", required=True,
                    help="app reference without digest, e.g. localhost:5000/shellhttpd; its last element names the app")
    ap.add_argument("--image", required=True, action="append", metavar="IMAGE=TAR",
                    help="compose service image and its OCI image layout tarball (uncompressed .tar); repeatable")
    ap.add_argument("app_dir")
    ap.add_argument("update_dir")
    # Only this platform's blobs are staged; default to the device's.
    ap.add_argument("--arch", default="arm64")
    ap.add_argument("--composectl", default="composectl", help="composectl name or path (default: from PATH)")
    args = ap.parse_args()
    composectl = find_composectl(args.composectl)

    app_name = parse_app_ref(args.app_ref)
    app_dir = os.path.abspath(args.app_dir)
    with open(os.path.join(app_dir, "docker-compose.yml")) as f:
        compose = yaml.safe_load(f)
    tars = parse_image_args(args.image, compose)

    # Validate everything before writing into the update dir.
    resolved = {}
    for image, path in tars.items():
        oci = OciTar(path)
        resolved[image] = (oci, *resolve_image(oci, args.arch))
    pinned, pinned_refs = pin_compose(compose, {image: r[1] for image, r in resolved.items()})
    bundle, hashes = make_bundle(app_dir, pinned)
    app_digest, app_blobs = make_app_blobs(bundle, hashes)

    store = os.path.join(os.path.abspath(args.update_dir), "apps")
    check_no_other_version(store, app_name, app_digest)
    os.makedirs(os.path.join(store, "blobs", "sha256"), exist_ok=True)
    for image, (oci, _, needed) in resolved.items():
        print(f"{image} <- {oci.path}")
        for desc in needed:
            state = "extracted" if oci.extract_blob(desc, store) else "present"
            print(f"  {state:9} {desc['digest']} {desc['size']:>12} {desc.get('mediaType', '')}")
    for data in app_blobs:
        put_blob(store, data)
    app_uri = f"{args.app_ref}@{app_digest}"

    # Source and destination are the same store: pull copies nothing, but verifies
    # the app tree and writes the apps/<name>/<sha256> view.
    with tempfile.TemporaryDirectory(prefix="stage-app-") as compose_root:
        rc = subprocess.run([composectl, "--store", store, "--compose", compose_root,
                             "--arch", args.arch, "pull", "--source-store-path", store, app_uri]).returncode
    if rc != 0:
        die(f"composectl pull failed (exit {rc}); {store} holds the blobs written so far")

    for ref in pinned_refs.values():
        print(f"image:   {ref}")
    print(f"app uri: {app_uri}")
    print(f"staged:  {store}/apps/{app_name}/{app_digest.split(':', 1)[1]}")
    print(f"fiocli:  --apps {app_name}={app_digest.split(':', 1)[1]}")


if __name__ == "__main__":
    main()
