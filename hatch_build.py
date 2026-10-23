import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.builders.sdist import SdistBuilder

# Where a copy of tcoda's package sources lives inside our own sdist, so the
# wheel-from-sdist build phase has something to pull from once the sibling
# checkout below is no longer around (sdists must be self-contained). Placed
# next to src/nivaria, matching this project's own src layout.
_TCODA_IN_SDIST = "src/tcoda"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # The "editable" version is what `uv sync` installs for local dev; it
        # must resolve tcoda via the sibling checkout's own editable install
        # (see [tool.uv.sources] in pyproject.toml), not a vendored snapshot
        # baked in here. Force-including source only makes sense for a real
        # distributable wheel/sdist, so skip it for editable builds.
        if version == "editable":
            return

        sibling_tcoda = Path(self.root).parent / "tcoda"
        tcoda_in_sdist = Path(self.root) / _TCODA_IN_SDIST

        if sibling_tcoda.is_dir():
            _regenerate_version(sibling_tcoda)
            source = sibling_tcoda / "src" / "tcoda"
        elif tcoda_in_sdist.is_dir():
            source = tcoda_in_sdist
        else:
            # Without tcoda the wheel builds fine but fails to import, so
            # refuse to build rather than ship something broken.
            raise RuntimeError(
                f"tcoda sources not found: neither {sibling_tcoda} nor "
                f"{tcoda_in_sdist} exists"
            )

        # The wheel installs packages flat, same as src/nivaria -> nivaria.
        target = _TCODA_IN_SDIST if self.target_name == "sdist" else "tcoda"
        build_data.setdefault("force_include", {})[str(source)] = target


def _regenerate_version(tcoda: Path) -> None:
    # Runs only tcoda's own build hooks (in particular hatch-vcs's version-file
    # hook) instead of a full `uv build`, so this neither writes an unused
    # wheel into tcoda's dist/ nor depends on `uv` being on PATH.
    with tempfile.TemporaryDirectory() as scratch:
        list(
            SdistBuilder(str(tcoda)).build(
                directory=scratch, versions=["standard"], hooks_only=True
            )
        )
