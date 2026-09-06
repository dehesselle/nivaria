import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.builders.sdist import SdistBuilder

# Where a copy of tcw's package sources lives inside our own sdist, so the
# wheel-from-sdist build phase has something to pull from once the sibling
# checkout below is no longer around (sdists must be self-contained). Placed
# next to src/snowcone, matching this project's own src layout.
_VENDORED_TCW = "src/tcw"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        sibling_tcw = Path(self.root).parent / "tcw"
        vendored_tcw = Path(self.root) / _VENDORED_TCW

        if sibling_tcw.is_dir():
            _regenerate_version(sibling_tcw)
            source = sibling_tcw / "src" / "tcw"
        elif vendored_tcw.is_dir():
            source = vendored_tcw
        else:
            return

        # The wheel installs packages flat, same as src/snowcone -> snowcone.
        target = _VENDORED_TCW if self.target_name == "sdist" else "tcw"
        build_data.setdefault("force_include", {})[str(source)] = target


def _regenerate_version(tcw: Path) -> None:
    # Runs only tcw's own build hooks (in particular hatch-vcs's version-file
    # hook) instead of a full `uv build`, so this neither writes an unused
    # wheel into tcw's dist/ nor depends on `uv` being on PATH.
    with tempfile.TemporaryDirectory() as scratch:
        list(
            SdistBuilder(str(tcw)).build(
                directory=scratch, versions=["standard"], hooks_only=True
            )
        )
