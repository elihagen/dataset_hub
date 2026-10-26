"""Thin wrapper around the marina CLI (https://github.com/uos/marina).

marina handles transport/compression/registries but only knows a dataset by
`namespace/name:tag:tag`. Our metadata lives in the catalog; marina gets a
reference derived from it so `marina pull <ref>` works for every dataset.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .schema import slug


def marina_ref(meta: Dict[str, Any], dataset_id: str) -> str:
    name = "-".join(slug(str(meta[k])) for k in ("project", "robot", "date") if meta.get(k))
    tags = [slug(str(meta[k])) for k in ("site", "robot_task") if meta.get(k)]
    return f"{slug(meta['customer'])}/{name}:" + ":".join([dataset_id] + tags)


class Marina:
    def __init__(self, binary: str = "marina", registry: str = ""):
        self.binary = binary
        self.registry = registry

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _run(self, args: List[str]) -> str:
        res = subprocess.run([self.binary, "-y", *args], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"marina {' '.join(args)} failed: {res.stderr.strip() or res.stdout}")
        return res.stdout

    def push(self, ref: str, path: Path) -> str:
        # Without --copy-to-cache marina *moves* the source into its cache,
        # which would empty the dataset folder in our storage.
        args = ["push", ref, str(path), "--no-progress", "--copy-to-cache"]
        if self.registry:
            args += ["--registry", self.registry]
        out = self._run(args)
        try:  # drop the local cache copy; the registry keeps it
            self._run(["rm", ref])
        except RuntimeError:
            pass
        return out

    def remove(self, ref: str) -> str:
        """Delete a dataset from the registry (and the local cache)."""
        args = ["rm", ref, "--remote"]
        if self.registry:
            args += ["--registry", self.registry]
        return self._run(args)
