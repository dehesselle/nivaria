from importlib.metadata import PackageNotFoundError, version

try:
    VERSION = version("snowcone")
except PackageNotFoundError:  # running from a source tree that was never installed
    VERSION = "0.0.0"
