import os
import sys
import errno
import atexit
import signal
import logging
import tempfile
if sys.platform == "win32":
    import msvcrt  # NOQA
    # Using psutil library for windows instead of os.kill call
    import psutil
else:
    import fcntl

from .utils import determine_pid_directory, effective_access


__version__ = "2.2.5"

DEFAULT_PID_DIR = determine_pid_directory()
PID_CHECK_EMPTY = "PID_CHECK_EMPTY"
PID_CHECK_NOFILE = "PID_CHECK_NOFILE"
PID_CHECK_SAMEPID = "PID_CHECK_SAMEPID"
PID_CHECK_NOTRUNNING = "PID_CHECK_NOTRUNNING"


class PidFileError(Exception):
    pass


class PidFileConfigurationError(Exception):
    pass


class PidFileUnreadableError(PidFileError):
    pass


class PidFileAlreadyRunningError(PidFileError):
    pass


class PidFileAlreadyLockedError(PidFileError):
    pass


class SamePidFileNotSupported(PidFileError):
    pass


class PidFileBase(object):
    __slots__ = (
        "pid", "pidname", "piddir", "enforce_dotpid_postfix",
        "register_term_signal_handler", "register_atexit", "filename",
        "fh", "lock_pidfile", "chmod", "uid", "gid", "force_tmpdir",
        "allow_samepid", "_logger", "_is_setup", "_need_cleanup",
    )

    def __init__(self, pidname=None, piddir=None, enforce_dotpid_postfix=True,
                 register_term_signal_handler="auto", register_atexit=True,
                 lock_pidfile=True, chmod=0o644, uid=-1, gid=-1, force_tmpdir=False,
                 allow_samepid=False):
        self.pidname = pidname
        self.piddir = piddir
        self.enforce_dotpid_postfix = enforce_dotpid_postfix
        self.register_term_signal_handler = register_term_signal_handler
        self.register_atexit = register_atexit
        self.lock_pidfile = lock_pidfile
        self.chmod = chmod
        self.uid = uid
        self.gid = gid
        self.force_tmpdir = force_tmpdir
        self.allow_samepid = allow_samepid

        self.fh = None
        self.filename = None
        self.pid = None

        self._logger = None
        self._is_setup = False
        self._need_cleanup = False

    @property
    def logger(self):
        if not self._logger:
            self._logger = logging.getLogger("PidFile")

        return self._logger

    def setup(self):
        if not self._is_setup:
            self.logger.debug("%r entering setup", self)
            if self.filename is None:
                self.pid = os.getpid()
                self.filename = self._make_filename()
                self._register_term_signal()

            if self.register_atexit:
                atexit.register(self.close)

            # setup should only be performed once
            self._is_setup = True

    def _make_filename(self):
        pidname = self.pidname
        piddir = self.piddir
        if pidname is None:
            pidname = "%s.pid" % os.path.basename(sys.argv[0])
        if self.enforce_dotpid_postfix and not pidname.endswith(".pid"):
            pidname = "%s.pid" % pidname
        if piddir is None:
            if os.path.isdir(DEFAULT_PID_DIR) and self.force_tmpdir is False:
                piddir = DEFAULT_PID_DIR
            else:
                piddir = tempfile.gettempdir()

        if os.path.exists(piddir) and not os.path.isdir(piddir):
            raise IOError("Pid file directory '%s' exists but is not a directory" % piddir)
        if not os.path.isdir(piddir):
            os.makedirs(piddir)
        if not effective_access(piddir, os.R_OK):
            raise IOError("Pid file directory '%s' cannot be read" % piddir)
        if not effective_access(piddir, os.W_OK | os.X_OK):
            raise IOError("Pid file directory '%s' cannot be written to" % piddir)

        filename = os.path.abspath(os.path.join(piddir, pidname))
        return filename

    def _register_term_signal(self):
        register_term_signal_handler = self.register_term_signal_handler
        if register_term_signal_handler == "auto":
            if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
                register_term_signal_handler = True
            else:
                register_term_signal_handler = False

        if callable(register_term_signal_handler):
            signal.signal(signal.SIGTERM, register_term_signal_handler)
        elif register_term_signal_handler:
            # Register TERM signal handler to make sure atexit runs on TERM signal
            def sigterm_noop_handler(*args, **kwargs):
                raise SystemExit(1)

            signal.signal(signal.SIGTERM, sigterm_noop_handler)

    def _pid_exists(self, pid):
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                # this pid is not running
                return False
            raise PidFileAlreadyRunningError(exc)
        return True

    def _inner_check(self, fh):
        try:
            fh.seek(0)
            pid_str = fh.read(16).split("\n", 1)[0].strip()
            if not pid_str:
                return PID_CHECK_EMPTY
            pid = int(pid_str)
        except (IOError, ValueError) as exc:
            self.close(fh=fh)
            raise PidFileUnreadableError(exc)
        else:
            if self.allow_samepid and self.pid == pid:
                return PID_CHECK_SAMEPID

        try:
            if self._pid_exists(pid):
                raise PidFileAlreadyRunningError("Program already running with pid: %d" % pid)
            else:
                return PID_CHECK_NOTRUNNING
        except PidFileAlreadyRunningError:
            self.close(fh=fh, cleanup=False)
            raise

    def _flock(self, fileno):
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _chmod(self):
        if self.chmod:
            os.fchmod(self.fh.fileno(), self.chmod)

    def _chown(self):
        if self.uid >= 0 or self.gid >= 0:
            os.fchown(self.fh.fileno(), self.uid, self.gid)

    def check(self):
        self.setup()

        self.logger.debug("%r check pidfile: %s", self, self.filename)

        if self.fh is None:
            if self.filename and os.path.isfile(self.filename):
                with open(self.filename, "r") as fh:
                    return self._inner_check(fh)
            return PID_CHECK_NOFILE

        return self._inner_check(self.fh)

    def create(self):
        self.setup()

        self.logger.debug("%r create pidfile: %s", self, self.filename)
        self.fh = open(self.filename, "a+")
        if self.lock_pidfile:
            try:
                self._flock(self.fh.fileno())
            except IOError as exc:
                if not self.allow_samepid:
                    self.close(cleanup=False)
                    raise PidFileAlreadyLockedError(exc)

        check_result = self.check()
        if check_result == PID_CHECK_SAMEPID:
            return

        self._chmod()
        self._chown()

        self.fh.seek(0)
        self.fh.truncate()
        # pidfile must be composed of the pid number and a newline character
        self.fh.write("%d\n" % self.pid)
        self.fh.flush()
        self.fh.seek(0)
        self._need_cleanup = True

    def close(self, fh=None, cleanup=None):
        self.logger.debug("%r closing pidfile: %s", self, self.filename)
        cleanup = self._need_cleanup if cleanup is None else cleanup

        if not fh:
            fh = self.fh
        try:
            if fh is None:
                return
            fh.close()
        except IOError as exc:
            # ignore error when file was already closed
            if exc.errno != errno.EBADF:
                raise
        finally:
            if self.filename and os.path.isfile(self.filename) and cleanup:
                os.remove(self.filename)
                self._need_cleanup = False

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type=None, exc_value=None, exc_tb=None):
        self.close()


if sys.platform == "win32":
    class PidFileWindows(PidFileBase):
        def __init__(self):
            super(PidFileWindows, self).__init__()
            if sys.platform == "win32" and self.allow_samepid:
                raise SamePidFileNotSupported("Flag allow_samepid is not supported on non-POSIX systems")

        def _inner_check(self, fh):
            # Try to read from file to check if it is locked by the same process
            try:
                fh.seek(0)
                fh.read(1)
            except IOError as exc:
                self.close(fh=fh, cleanup=False)
                if exc.errno == 13:
                    raise PidFileAlreadyRunningError(exc)
                else:
                    raise
            return super(PidFileWindows, self)._inner_check(fh)

        def _pid_exists(self, pid):
            return psutil.pid_exists(pid)

        def _flock(self, fileno):
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            # Try to read from file to check if it is actually locked
            self.fh.seek(0)
            self.fh.read(1)

        def _chmod(self):
            if self.chmod:
                raise PidFileConfigurationError("chmod supported on win32")

        def _chown(self):
            if self.uid >= 0 or self.gid >= 0:
                raise PidFileConfigurationError("chown supported on win32")
else:
    class PidFile(PidFileBase):
        pass
