"""Seams of the start: core starts no thread, and calls no online service, on its own."""
import inspect
import logging
import sys
import threading

from . import CORE, FunctionSeam, Seam, SeamError, register

_logger = logging.getLogger(__name__)

PAIRING_CODE_MODULE = f'{CORE}.connection_manager'


class ThreadGate:
    """Holds the threads started while it is open.

    Core starts its threads when it is imported. Opened around that import, the
    gate keeps them from running until the layer is in place, and for good those
    a seam withholds.
    """

    def __init__(self):
        self.held = []
        self.withheld = []
        self.refusal = ''
        self._start = None

    def open(self):
        start = threading.Thread.start
        if self._start:
            return
        if not inspect.isfunction(start) or tuple(inspect.signature(start).parameters) != ('self',):
            self.refusal = 'threading.Thread.start is not the method the gate knows how to replace'
            return
        held = self.held

        def hold(thread):
            if thread not in held:
                held.append(thread)

        self._start = start
        threading.Thread.start = hold

    def close(self):
        if self._start:
            threading.Thread.start, self._start = self._start, None

    def release(self):
        """Start the held threads in the order they were started in, the withheld ones excepted; return them."""
        released = [thread for thread in self.held if thread not in self.withheld and thread.ident is None]
        self.held = []
        for thread in released:
            try:
                thread.start()
            except RuntimeError:
                _logger.critical(
                    'Viindoo layer integrity: the thread %s could not be started', thread.name, exc_info=True)
        return released


gate = ThreadGate()


def _is_core(thread):
    return type(thread).__module__.startswith(CORE)


@register
class HeldThreads(Seam):
    number = 1
    target = 'threading.Thread.start'
    feature = 'core starts none of its threads before the layer is in place'

    def check(self):
        if gate.refusal:
            raise SeamError(gate.refusal)
        running = [thread.name for thread in threading.enumerate() if _is_core(thread)]
        if running:
            raise SeamError('threads of core run already: %s' % ', '.join(running))
        if not any(_is_core(thread) for thread in gate.held):
            raise SeamError('importing core started no thread that the gate could hold')

    def apply(self):
        """Nothing is left to do: the gate has been holding the threads since before core was imported."""


@register
class PairingCodeThread(Seam):
    number = 2
    target = f'{PAIRING_CODE_MODULE}.connection_manager'
    feature = 'the box asks no online service for a pairing code'

    def _threads(self):
        return [thread for thread in gate.held if type(thread).__module__ == PAIRING_CODE_MODULE]

    def check(self):
        if PAIRING_CODE_MODULE not in sys.modules:
            raise SeamError(f'{PAIRING_CODE_MODULE} is not loaded')
        if not self._threads():
            raise SeamError(f'the gate holds no thread of {PAIRING_CODE_MODULE}')

    def apply(self):
        gate.withheld.extend(self._threads())


_update_check = []
_update_check_runs = threading.Lock()


def set_update_check(check):
    """Give the function that looks for a release of the box software.

    It runs on a thread of its own each time core asks for its update check - at
    the start of a box that has a server, then every night - so that neither
    registration nor the device loop ever waits on it. One run at a time: a call
    that comes while it runs starts none. A box without a server takes its
    first look at start in `update`, by the same function.
    """
    _update_check[:] = [check]


def _run_update_check(check):
    try:
        check()
    except Exception:
        _logger.exception('The update check of the box failed')
    finally:
        _update_check_runs.release()


@register
class UpdateCheck(FunctionSeam):
    number = 3
    module = f'{CORE}.tools.helpers'
    name = 'check_git_branch'
    feature = 'the box never follows the code branch of its server, and takes its software from Viindoo only'

    def replacement(self, original):
        def check_git_branch():
            if not _update_check:
                _logger.debug('No update check is installed on this box')
            elif _update_check_runs.acquire(blocking=False):
                try:
                    threading.Thread(
                        target=_run_update_check, args=(_update_check[0],), name='ViinUpdateCheck', daemon=True,
                    ).start()
                except Exception:
                    _update_check_runs.release()
                    _logger.exception('The update check of the box could not be started')

        return check_git_branch


# What core's certificate status makes of the answer: it reports the text, as it does for a certificate it cannot get.
NO_VENDOR_CERTIFICATE = 'The box does not ask the vendor of core for a certificate'


@register
class VendorCertificate(FunctionSeam):
    number = 18
    module = f'{CORE}.tools.helpers'
    name = 'load_certificate'
    feature = ('the box never sends the credential of a database - the identifier and the contract code core '
               'keeps when a box connects - to the certificate service of the vendor of core')

    def replacement(self, original):
        """Replaced outright: the function has no other work than that request."""
        def load_certificate():
            _logger.debug('%s', NO_VENDOR_CERTIFICATE)
            return NO_VENDOR_CERTIFICATE

        return load_certificate
