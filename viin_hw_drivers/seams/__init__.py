"""The places where the layer touches the box software it starts.

A seam names one symbol of core, says how the layer expects it to look and what
it puts there. A seam whose symbol moved is left alone: the box loses that one
feature, says so in its log and in `integrity()`, and starts all the same.
"""
import functools
import importlib
import inspect
import logging
import pkgutil
import sys

_logger = logging.getLogger(__name__)

CORE = 'odoo.addons.hw_drivers'

PENDING = 'pending'
APPLIED = 'applied'
DEFERRED = 'deferred'
FAILED = 'failed'
SKIPPED = 'skipped'


class SeamError(Exception):
    """A symbol of core is not where, or not shaped as, a seam needs it."""


class Seam:
    """One symbol of core the layer relies on."""

    number = 0      # order of application, and the name of the seam in every report
    target = ''     # the symbol, as a person would look for it in core
    feature = ''    # what the box does only while the seam holds
    # For a seam whose symbol exists only once core has loaded its device handlers: the number, lower than its
    # own, of the seam whose replacement applies it then, with `apply(only=...)`. The gate checks such a seam
    # against what is on the disk and leaves it `deferred`.
    after = 0

    def check(self):
        """Raise SeamError unless the symbol is where, and shaped as, `apply` needs it."""
        raise NotImplementedError

    def apply(self):
        """Put the layer in place on the symbol; doing it a second time changes nothing."""
        raise NotImplementedError

    def settle(self):
        """Apply the seam if it holds; raise for what could not be applied."""
        self.check()
        self.apply()


class FunctionSeam(Seam):
    """A function or a method of `module` that `replacement` wraps or replaces."""

    module = ''
    name = ''           # dotted for a method: 'Manager.run'
    parameters = ()     # the parameter names the replacement was written for

    def __init__(self):
        self._wrapper = None

    @property
    def target(self):
        return f'{self.module}.{self.name}'

    def _resolve(self):
        try:
            owner = importlib.import_module(self.module)
        except ImportError:
            raise SeamError(f'{self.module} cannot be imported') from None
        *path, attribute = self.name.split('.')
        try:
            for part in path:
                owner = getattr(owner, part)
            return owner, attribute, getattr(owner, attribute)
        except AttributeError:
            raise SeamError(f'{self.target} does not exist') from None

    def check(self):
        function = self._resolve()[2]
        try:
            found = tuple(inspect.signature(function).parameters)
        except (TypeError, ValueError):
            raise SeamError(f'{self.target} is not a function') from None
        if found != tuple(self.parameters):
            raise SeamError('signature changed: expected (%s), found (%s)' % (
                ', '.join(self.parameters), ', '.join(found)))

    def _in_place(self, function):
        while function is not None:
            if function is self._wrapper:
                return True
            function = getattr(function, '__wrapped__', None)
        return False

    def apply(self):
        owner, attribute, function = self._resolve()
        if self._in_place(function):
            return
        wrapper = functools.wraps(function)(self.replacement(function))
        # What callers reach through the function itself, like the reset of a cache, still reaches the original.
        for name in dir(function):
            if not name.startswith('_') and not hasattr(wrapper, name):
                setattr(wrapper, name, getattr(function, name))
        self._wrapper = wrapper
        setattr(owner, attribute, wrapper)

    def replacement(self, original):
        """The function that takes the place of `original`, which it calls unless the seam replaces it outright."""
        raise NotImplementedError


class PartsSeam(Seam):
    """A seam made of parts that hold or move one by one.

    A part that moved is left alone and the others are applied: the seam is
    then `failed`, and its reason names the parts that are off.
    """

    parts = ()

    def check_part(self, part):
        """Raise SeamError unless `part` is where, and shaped as, `apply_part` needs it."""
        raise NotImplementedError

    def apply_part(self, part):
        """Put the layer in place on `part`; doing it a second time changes nothing."""
        raise NotImplementedError

    def _settle_parts(self, apply):
        moved = []
        for part in self.parts:
            error = _attempt(self.check_part, part) or (apply and _attempt(self.apply_part, part))
            if error:
                moved.append(f'{part}: {_describe(error)}')
        if moved:
            kept = ' - the other parts are applied' if apply and len(moved) < len(self.parts) else ''
            raise SeamError('; '.join(moved) + kept)

    def check(self):
        self._settle_parts(apply=False)

    def apply(self):
        self._settle_parts(apply=True)

    def settle(self):
        self.apply()


class _Integrity:

    def __init__(self):
        self.in_place = False
        self.load_order_ok = True
        self.seams = {}
        self.states = {}
        self.problems = []
        self.owed = {}


_integrity = _Integrity()


def core(module):
    """A module of core that is loaded already.

    No file of the layer imports core by itself: the first import of core starts
    it, and only the gate may decide when that happens.
    """
    return sys.modules[f'{CORE}.{module}']


def register(seam_class):
    """Class decorator: declare a seam. Its file is loaded by `load`, so declaring is all it takes."""
    seam = seam_class()
    if seam.number in _integrity.seams:
        raise ValueError(f'seam {seam.number} is declared twice')
    _integrity.seams[seam.number] = seam
    _integrity.states[seam.number] = (PENDING, '')
    return seam_class


def load():
    """Import every seam file of this package; a file that cannot be loaded costs its own seams only."""
    for found in sorted(pkgutil.iter_modules(__path__), key=lambda module: module.name):
        try:
            importlib.import_module(f'{__name__}.{found.name}')
        except Exception as error:
            _logger.critical(
                'Viindoo layer integrity: the seam file %s cannot be loaded; what it provides is off on this box.',
                found.name, exc_info=True)
            report_problem(f'seams/{found.name}.py', _describe(error))


def _declared():
    return [_integrity.seams[number] for number in sorted(_integrity.seams)]


def _describe(error):
    return str(error) if isinstance(error, SeamError) else f'{type(error).__name__}: {error}'


def _attempt(step, *arguments):
    try:
        step(*arguments)
    except Exception as error:
        return error
    return None


def check():
    """Check every declared seam and change nothing: [(seam, reason)], reason empty when the seam holds.

    A seam applied by another one holds only if that one does.
    """
    results, reasons = [], {}
    for seam in _declared():
        error = _attempt(seam.check)
        reason = _describe(error) if error else ''
        if not reason and seam.after and reasons.get(seam.after, 'not declared'):
            reason = f'seam {seam.after}, which applies it, does not hold'
        reasons[seam.number] = reason
        results.append((seam, reason))
    return results


def _fail(seam, error):
    _integrity.states[seam.number] = (FAILED, _describe(error))
    _logger.critical(
        'Viindoo layer integrity: seam %s on %s is not applied (%s). Off on this box: %s.',
        seam.number, seam.target, _describe(error), seam.feature,
        exc_info=None if isinstance(error, SeamError) else error)


def apply(only=None):
    """Check each seam, in the order of their numbers, and apply those that hold; an applied seam is left alone.

    Without `only`, the call of the gate, a seam that names another in `after`
    is checked and left `deferred`. `only` gives the numbers of the seams to
    apply now: those another module of the layer declares once the box has
    started, and the deferred ones when their symbol exists.
    """
    for seam in _declared():
        if _integrity.states[seam.number][0] == APPLIED or (only is not None and seam.number not in only):
            continue
        if only is None and seam.after:
            if _integrity.states.get(seam.after, (PENDING, ''))[0] != APPLIED:
                error = SeamError(f'seam {seam.after}, which applies it, is not applied')
            else:
                error = _attempt(seam.check)
            if error:
                _fail(seam, error)
            else:
                _integrity.states[seam.number] = (DEFERRED, '')
            continue
        error = _attempt(seam.settle)
        if error:
            _fail(seam, error)
        else:
            _integrity.states[seam.number] = (APPLIED, '')


def refuse_load_order():
    """Record that core was loaded before the layer: no seam is applied on such a box."""
    _integrity.load_order_ok = False
    for seam in _declared():
        _integrity.states[seam.number] = (SKIPPED, 'core was loaded before the layer')


def report_problem(source, message):
    """Record a part of the layer that is not working; `source` says which, in a few words."""
    _integrity.problems.append({'source': source, 'message': message})


def expect(number, source, message):
    """Record that `source`, which the box loads after the gate, declares the seam `number`.

    As long as that seam is not declared, `message` is a problem of `source` in `integrity()`.
    """
    _integrity.owed[number] = {'source': source, 'message': message}


def mark_in_place():
    """Record that the gate ran to its end: `integrity()` is not ok on a box where it did not."""
    _integrity.in_place = True


def integrity():
    """The state of the layer, for whoever shows it to a person.

    {'ok', 'load_order_ok', 'seams': [{'number', 'target', 'feature', 'state', 'reason'}],
    'problems': [{'source', 'message'}]}; `state` is pending, applied, deferred
    (it holds, and another seam applies it once core has loaded its device
    handlers), failed or skipped. 'ok' asks for a gate that ran to its end, no
    problem, and every seam applied or deferred.
    """
    seams = [{
        'number': seam.number,
        'target': seam.target,
        'feature': seam.feature,
        'state': _integrity.states[seam.number][0],
        'reason': _integrity.states[seam.number][1],
    } for seam in _declared()]
    problems = [dict(problem) for problem in _integrity.problems]
    problems += [dict(_integrity.owed[number]) for number in sorted(_integrity.owed) if number not in _integrity.seams]
    return {
        'ok': (_integrity.in_place and _integrity.load_order_ok and not problems
               and all(seam['state'] in (APPLIED, DEFERRED) for seam in seams)),
        'load_order_ok': _integrity.load_order_ok,
        'seams': seams,
        'problems': problems,
    }
