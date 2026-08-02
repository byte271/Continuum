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


class CheckpointError(ContinuumError):
    """A rolling checkpoint could not be committed, scanned, or recovered.

    `published_generation` and `published_slot` are set when the commit failed
    *after* the atomic replace had already installed the image under a slot
    name. That generation is visible to any reader from that moment on, so the
    caller must never issue it again: reusing it would put two valid slots at
    the same generation and make recovery permanently ambiguous.

    They are attributes rather than something a caller parses out of the
    message, so the state machine never depends on error text.
    """

    def __init__(
        self,
        message: str,
        *,
        published_generation: int | None = None,
        published_slot: str | None = None,
    ):
        super().__init__(message)
        self.published_generation = published_generation
        self.published_slot = published_slot


class FrozenExecution(BaseException):
    """Internal non-error used only after an image is durably committed."""
