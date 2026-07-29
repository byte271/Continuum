class ContinuumError(Exception):
    """Base class for errors safe to show to a CLI user."""


class CompileError(ContinuumError):
    """The source uses syntax outside the Continuum executable subset."""


class ExecutionError(ContinuumError):
    """The controlled runtime could not execute the program safely."""


class UnsupportedObjectError(ContinuumError):
    """A live object cannot be represented by the portable heap codec."""


class ImageError(ContinuumError):
    """A continuation image is invalid, corrupt, or incompatible."""


class ResourceError(ContinuumError):
    """A resource cannot be rebound without changing program meaning."""


class FrozenExecution(BaseException):
    """Internal non-error used only after an image is durably committed."""
