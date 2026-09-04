class PPRPCError(Exception):
    """Base class for all exceptions."""

    #: Whether the request had already gone out on the socket when this was
    #: raised. It exists because two very different situations otherwise share
    #: one exception class: ``_write`` raises NotConnectedError with nothing
    #: sent, while a session torn down mid-wait raises the same class after the
    #: packet left. For most commands the difference is academic; for WifiSet it
    #: is the whole question, because "sent, no reply" is what SUCCESS looks
    #: like and "never sent" means the camera was never touched.
    sent = False


class NotConnectedError(PPRPCError):
    """Error when the camera is not connected."""


class AlreadyConnectedError(PPRPCError):
    """Error when the camera is already connected."""


class AuthError(PPRPCError):
    """Error when LanAuth is rejected.

    Usually a stale ``lsign``: the token is minted per discovery reply and
    expires, so re-discover immediately before retrying.
    """


class CommandError(PPRPCError):
    """Error when a command returns a non-zero response code."""

    def __init__(self, cmd, code, message=''):
        self.cmd = cmd
        self.code = code
        super().__init__(message or f'command {cmd} failed with code {code}')


class UnsupportedFeatureError(PPRPCError):
    """The camera's own capability declaration says it cannot do this.

    Raised instead of letting a command through that is known to harm the
    device -- see Session.start_talk().
    """


class PacketError(PPRPCError, ValueError):
    """Raised when a packet is truncated or violates the known framing."""
