"""The gate: how the layer takes its place before the box software starts.

The box loads this module first. `start` imports core while the threads core
starts on import are held, puts the layer in place, then lets them run. Run as a
program - `python3 boot.py --config <configuration file of the box>` - this file
checks the seams against the core that configuration names, and starts nothing.
"""
import argparse
import importlib
import importlib.machinery
import importlib.util
import logging
import os
import pkgutil
import sys

# Nothing but the standard library is imported up here: run as a program, this
# file trims the import path before any other library is loaded.
_logger = logging.getLogger(__name__)

PACKAGE = 'odoo.addons.viin_hw_drivers'
CORE = 'odoo.addons.hw_drivers'
HOMEPAGE = 'viin_hw_homepage'
# The seam the home page module declares in the file that closes and locks the routes of core.
CORE_ROUTES_SEAM = 13
# The USB library, which the device thread of core and the receipt printer module of core, imported by the box
# service after the layer, both import first. Its package imports its own modules as it starts, so two threads that
# import it at once may each be handed a module halfway loaded, and both lose it: the USB devices and the receipt
# printer module with them. Imported once before the threads of core run, it is whole for each of them.
USB_LIBRARY = 'usb.core'


def _part(name):
    return importlib.import_module(f'{PACKAGE}.{name}')


def start():
    """Put the layer in place and let core start. Never raises: the box starts whatever goes wrong here."""
    problems = []
    try:
        _reset_if_asked()
    except Exception as error:
        _logger.critical(
            'Viindoo layer integrity: the reset file of the card could not be looked for, or not honoured',
            exc_info=True)
        problems.append(('reset file', f'{type(error).__name__}: {error}'))
    try:
        _start(problems)
    except Exception as error:
        _logger.critical('Viindoo layer integrity: the layer could not be put in place', exc_info=True)
        _report_unfinished(error)


def _report_unfinished(error):
    """Have the state of the layer name a gate that did not run to its end; never raises."""
    try:
        _part('seams').report_problem('gate', f'{type(error).__name__}: {error}')
    except Exception:
        _logger.critical('Viindoo layer integrity: the state of the layer cannot be recorded', exc_info=True)


def _reset_if_asked():
    pairing = _part('pairing')
    marker = pairing.reset_file()
    if marker is None:
        return
    # Core, when it is loaded already, has read its configuration: only its own helpers can make it read again.
    pairing.reset(sys.modules.get(f'{CORE}.tools.helpers') or _core_helpers_alone(), marker)
    _logger.warning(
        'Viindoo layer: %s was found on the card; this box forgot its server, its lock and the certificates '
        'it trusted', marker.name)


def _core_helpers_alone():
    """Core's helper file under a name of its own.

    Importing it the usual way imports core, which reads its configuration and
    prepares its threads at once; the reset must come before that.
    """
    from odoo.tools.misc import file_path

    spec = importlib.util.spec_from_file_location(
        'viin_hw_drivers_core_helpers', file_path('hw_drivers/tools/helpers.py'))
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    return helpers


def _start(problems):
    seams = _part('seams')
    for source, message in problems:
        seams.report_problem(source, message)
    _guarded(seams, HOMEPAGE, _expect_homepage, seams)
    gate = _thread_gate(seams)
    in_order = CORE not in sys.modules
    if in_order and gate:
        gate.open()
    withheld = released = ()
    try:
        if in_order:
            _guarded(seams, 'core', importlib.import_module, CORE)
            seams.load()
            seams.apply()
        else:
            seams.load()
            seams.refuse_load_order()
            _logger.critical(
                'Viindoo layer integrity: %s was loaded before the layer, so the layer changes nothing in it. '
                'Put viin_hw_drivers first in server_wide_modules.', CORE)
        _load_files(seams)
        _guarded(seams, 'USB library', _import_usb_library)
        _guarded(seams, 'page use', lambda: _part('page_use').begin())
        _guarded(seams, 'box certificate', _keep_certificate, restart_web_server=not in_order)
    finally:
        if gate:
            gate.close()
            withheld = list(gate.withheld)
            released = gate.release()
    seams.mark_in_place()
    by_state = {}
    for seam in seams.integrity()['seams']:
        by_state.setdefault(seam['state'], []).append(seam['number'])
    _logger.info(
        'Viindoo layer in place: seams %s; threads held, then started: %s; threads never started: %s',
        by_state, [_thread_label(thread) for thread in released], [_thread_label(thread) for thread in withheld])


def _thread_gate(seams):
    """The gate that holds the threads of core; None, and a problem of the layer, when its file cannot be loaded."""
    try:
        return _part('seams.startup').gate
    except Exception as error:
        _logger.critical(
            'Viindoo layer integrity: the threads of core cannot be held; they start before the layer is in place.',
            exc_info=True)
        seams.report_problem('gate', f'{type(error).__name__}: {error}')
        return None


def _expect_homepage(seams):
    """Count the layer as incomplete until its home page module has closed and locked the routes of core.

    Without that module every route of core is served as core serves it, on a box that has a server too.
    """
    import odoo

    lacking = 'no route of the box software is closed or locked on this box'
    spec = importlib.util.find_spec(f'odoo.addons.{HOMEPAGE}') if HOMEPAGE in odoo.conf.server_wide_modules else None
    if spec and spec.has_location:
        seams.expect(CORE_ROUTES_SEAM, HOMEPAGE, f'not loaded: {lacking}')
    else:
        _logger.critical(
            'Viindoo layer integrity: %s is not among server_wide_modules, or not on the addons path: %s.',
            HOMEPAGE, lacking)
        seams.report_problem(HOMEPAGE, f'not among server_wide_modules, or not on the addons path: {lacking}')


def _thread_label(thread):
    return f'{type(thread).__module__}.{type(thread).__qualname__}({thread.name})'


def _guarded(seams, source, step, *args, **kwargs):
    """Run one step of the start; what fails is logged and recorded, and the start goes on."""
    try:
        step(*args, **kwargs)
    except Exception as error:
        _logger.critical('Viindoo layer integrity: %s failed; the box starts without it.', source, exc_info=True)
        seams.report_problem(source, f'{type(error).__name__}: {error}')


def _load_files(seams):
    """Import every service file and controller file the layer holds, each on its own.

    A file that cannot be loaded costs what it provides and nothing else. The
    gate is still holding the threads: one a file starts when it is imported
    runs when those of core do.
    """
    names = [found.name for found in pkgutil.iter_modules(sys.modules[PACKAGE].__path__)
             if not found.ispkg and found.name != 'boot']
    _guarded(seams, 'controllers', lambda: names.extend(
        f'controllers.{found.name}' for found in pkgutil.iter_modules(_part('controllers').__path__)))
    for name in sorted(names):
        _guarded(seams, f'{name.replace(".", "/")}.py', _part, name)


def _import_usb_library():
    """Import the USB library whole, before the threads of core run. A box without it starts as core does without it."""
    try:
        importlib.import_module(USB_LIBRARY)
    except ModuleNotFoundError:
        _logger.warning('Viindoo layer: the USB library %s is not installed; core finds no USB device', USB_LIBRARY)


def _keep_certificate(restart_web_server):
    trust = _part('trust')
    try:
        if restart_web_server:
            trust.refresh_box_certificate()
        else:
            # Core restarts the web server as its first act, once its threads are let go.
            trust.ensure_box_certificate()
    finally:
        trust.CertificateKeeper(after_core_starts_web_server=not restart_web_server).start()


def _load_layer_modules(seams):
    """Import the other modules of the layer that the configuration loads: each declares the seams of its own."""
    import odoo

    layer = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in odoo.conf.server_wide_modules:
        if f'odoo.addons.{name}' != PACKAGE and os.path.isfile(os.path.join(layer, name, '__manifest__.py')):
            _guarded(seams, name, importlib.import_module, f'odoo.addons.{name}')


def _layer_without_start():
    """Make the files of the layer importable without running the file that starts it."""
    spec = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
    spec.submodule_search_locations = [os.path.dirname(os.path.abspath(__file__))]
    sys.modules[PACKAGE] = importlib.util.module_from_spec(spec)


def check(arguments=None):
    """Check every seam against the core of a configuration, start nothing, and return the exit code.

    0 when every seam holds, 1 when one does not or a part of the layer is not
    loaded, 2 when core itself cannot be imported. A seam that is applied only
    once core has loaded its device handlers is checked against what is on the
    disk and printed as deferred.
    """
    parser = argparse.ArgumentParser(
        description='Check that the box software still has every symbol the Viindoo layer relies on.')
    parser.add_argument('-c', '--config', required=True, help='configuration file of the box (odoo.conf)')
    options = parser.parse_args(arguments)

    import odoo

    odoo.tools.config.parse_config(['--config', options.config])
    _layer_without_start()
    seams = _part('seams')
    gate = _part('seams.startup').gate
    gate.open()
    try:
        try:
            importlib.import_module(CORE)
        except Exception as error:
            print(f'{CORE} cannot be imported: {type(error).__name__}: {error}')  # noqa: T201
            return 2
        seams.load()
        _guarded(seams, HOMEPAGE, _expect_homepage, seams)
        _load_layer_modules(seams)
        results = seams.check()
    finally:
        gate.close()
    for seam, reason in results:
        note = f': {reason} - off: {seam.feature}' if reason else ''
        if seam.after and not reason:
            note = f' - {seams.DEFERRED}: seam {seam.after} applies it once core has loaded its device handlers'
        print(f'seam {seam.number:>2}  {"FAILED" if reason else "ok":<6}  {seam.target}{note}')  # noqa: T201
    problems = seams.integrity()['problems']
    for problem in problems:
        print(f'FAILED   {problem["source"]}: {problem["message"]}')  # noqa: T201
    return int(bool(problems or any(reason for _seam, reason in results)))


if __name__ == '__main__':
    # The directory of this file must not answer the imports of the libraries the box runs on.
    sys.path[:] = [entry for entry in sys.path
                   if os.path.abspath(entry or os.curdir) != os.path.dirname(os.path.abspath(__file__))]
    sys.exit(check())
