"""The software release a box runs: looking for one, checking it, moving to it.

A box follows one git ref of the layer repository, its channel: for a supported
box, the branch `box-release/<series>`, which only a published release moves. The
commit at the head of the channel holds a pointer file that names a release, and a
tag `box-<series>-<release>` must name that same commit. That commit is the layer,
and the pointer names the commit of core that goes with it. The
box runs that pair and no other code. Everything is checked before either tree
moves, a release that fails to start is undone by the guard the system files
install, and a release that was undone is never tried again.
"""
import contextlib
import fcntl
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import urllib3

from odoo import release as core_release

from . import seams, trust
from .seams import startup

_logger = logging.getLogger(__name__)

# The only places the box software comes from. Each git command names them itself, so that nothing a tree
# holds as a remote decides it; git verifies them against the authorities of the system, never against a
# certificate the installer trusted for the server of the box.
LAYER_REPOSITORY = 'https://github.com/Viindoo/iot-hw.git'
CORE_REPOSITORY = 'https://github.com/Viindoo/odoo.git'

POINTER = 'box-release.json'
# Written by the build of a candidate image and by nothing else: the box never creates it.
CHANNEL_FILE = 'viin-box-channel'
STATE_FILE = 'viin-box-state.json'
# Held by this software and by the roll-back guard (another process) around each change of the state file. It
# lives in memory (/tmp is a RAM disk on a box), so that taking it needs neither the card open nor a directory
# of the box user. The guard names the same path.
STATE_LOCK_FILE = '/tmp/viin-box-state.lock'
STATE_LOCK_WAIT = 120
VERSION_ROUTE = '/web/webclient/version_info'
IMAGE_VERSION_FILE = '/var/odoo/iotbox_version'
GUARD = '/usr/local/sbin/viin-box-guard'
APPLY_SCRIPT = 'box/system/apply.sh'
# What keeps the commits of a checked release, and of the release to go back to, from being pruned.
OFFERED_REF = 'refs/viin-box/offered'
PREVIOUS_REF = 'refs/viin-box/previous'

LAYER_TREE = Path(__file__).resolve().parents[1]
CORE_TREE = Path(core_release.__file__).resolve().parents[1]
SERIES = core_release.series.replace('~', '-')

UNREACHABLE = 'unreachable'
NO_CHANNEL = 'no_channel'
RELEASE_UNTAGGED = 'release_untagged'
NO_POINTER = 'no_pointer'
POINTER_INVALID = 'pointer_invalid'
CANDIDATE_REFUSED = 'candidate_refused'
NEEDS_NEW_IMAGE = 'needs_new_image'
CORE_COMMIT_UNAVAILABLE = 'core_commit_unavailable'
BAD_RELEASE = 'bad_release'
APPLY_FAILED = 'apply_failed'
UP_TO_DATE = 'up_to_date'
AVAILABLE = 'available'
APPLIED = 'applied'
# What a head was refused for stays true for as long as that head is the same commit. A channel whose head no
# published release names (RELEASE_UNTAGGED) is not among them: a tag may name it at the next look. Nor is a
# pointer whose release is not among the tags that name the head (RELEASE_TAG_REASON): it depends on the set of
# tags, not on the commit, and a tag added later settles it.
RELEASE_TAG_REASON = 'release_tag'
REMEMBERED = (NO_POINTER, POINTER_INVALID, CANDIDATE_REFUSED, NEEDS_NEW_IMAGE, CORE_COMMIT_UNAVAILABLE, APPLY_FAILED)

APPLYING_PHASE = 'applying'
APPLIED_PHASE = 'applied'
GOOD_AFTER = 120
SETTLE_PERIOD = 30
RESTART_DELAY = 1
REMOTE_TIMEOUT = 60
LAYER_FETCH_TIMEOUT = 600
CORE_FETCH_TIMEOUT = 3600
LOCAL_TIMEOUT = 600
SERVER_TIMEOUT = trust.READ_TIMEOUT
MAX_ANSWER = 64 * 1024
MAX_POINTER = 64 * 1024
MAX_REFUSED = 20

_COMMIT = re.compile(r'[0-9a-f]{40}')
_SERIES = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,19}')
_RELEASE = re.compile(r'[0-9A-Za-z][0-9A-Za-z._-]{0,39}')
# How a release says it is a candidate: its name is, or ends in a hyphen and, `rc.<number>`.
_CANDIDATE_MARK = re.compile(r'(?:.*-)?rc\.[0-9]+')
_IMAGE = re.compile(r'([0-9]{1,4})\.([0-9]{1,4})')
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')
# What would make git trust a server for another reason than the authorities of the system.
_TRUST_VARIABLES = (
    'GIT_SSL_NO_VERIFY', 'GIT_SSL_CAINFO', 'GIT_SSL_CAPATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CURL_CA_BUNDLE')
_GIT_OPTIONS = ('-c', 'http.sslVerify=true', '-c', 'credential.helper=', '-c', 'advice.detachedHead=false')

_state_lock = threading.RLock()
_busy = threading.Lock()
_activity = []
_unsettled = []


class _Stop(Exception):
    """Ends the look at a channel: `state` says where it stands, `reason` why."""

    def __init__(self, state, reason=''):
        super().__init__(f'{state} {reason}'.strip())
        self.state = state
        self.reason = reason


def _helpers():
    return seams.core('tools.helpers')


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _git(tree, *arguments, timeout=LOCAL_TIMEOUT, check=True):
    environment = {name: value for name, value in os.environ.items() if name not in _TRUST_VARIABLES}
    environment.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    return subprocess.run(
        ['git', '-C', str(tree), *_GIT_OPTIONS, *arguments],
        capture_output=True, check=check, timeout=timeout, env=environment)


def _said(tree, *arguments):
    """What a git command that changes nothing prints about `tree`, '' when it fails."""
    try:
        found = _git(tree, *arguments, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    return '' if found.returncode else found.stdout.decode(errors='replace').strip()


def _commit(tree, revision='HEAD'):
    """The commit `revision` names in `tree`, '' when the tree holds no such commit."""
    name = _said(tree, 'rev-parse', '--quiet', '--verify', f'{revision}^{{commit}}')
    return name if _COMMIT.fullmatch(name) else ''


def _own_commit(tree):
    """The commit `tree` is at, '' unless it is a git repository of its own.

    A tree that sits inside the repository of something else is never moved:
    git would move that repository.
    """
    top = _said(tree, 'rev-parse', '--show-toplevel')
    return _commit(tree) if top and Path(top).resolve() == tree else ''


def _pointer(data):
    """The release a pointer file names; `_Stop(POINTER_INVALID)` when it names none a box can follow."""
    if len(data) > MAX_POINTER:
        raise _Stop(POINTER_INVALID, 'too_large')
    try:
        pointer = json.loads(data.decode())
    except ValueError:
        raise _Stop(POINTER_INVALID, 'not_json') from None
    if not isinstance(pointer, dict):
        raise _Stop(POINTER_INVALID, 'not_an_object')
    shapes = {'release': _RELEASE, 'series': _SERIES, 'odoo_commit': _COMMIT, 'min_image': _IMAGE}
    for key, shape in shapes.items():
        value = pointer.get(key)
        if not isinstance(value, str) or not shape.fullmatch(value):
            raise _Stop(POINTER_INVALID, key)
    if not isinstance(pointer.get('candidate'), bool):
        raise _Stop(POINTER_INVALID, 'candidate')
    # `rc.` in a name is for a candidate alone: any other name that holds it is refused, so that a tag pattern
    # which lets anyone create `rc.` tags can never name a supported release.
    if 'rc.' in pointer['release'] and not _CANDIDATE_MARK.fullmatch(pointer['release']):
        raise _Stop(POINTER_INVALID, 'rc_in_name')
    # A candidate is told from a supported release by its name too, wherever that name is shown.
    if pointer['candidate'] != bool(_CANDIDATE_MARK.fullmatch(pointer['release'])):
        raise _Stop(POINTER_INVALID, 'candidate_mark')
    return {key: pointer[key] for key in (*shapes, 'candidate')}


def _read_running():
    running = {
        'layer_commit': _own_commit(LAYER_TREE), 'odoo_commit': _own_commit(CORE_TREE),
        'release': '', 'candidate': False, 'pointer': 'missing', 'matches_pointer': False,
    }
    try:
        pointer = _pointer((LAYER_TREE / POINTER).read_bytes())
    except OSError:
        return running
    except _Stop:
        return dict(running, pointer='invalid')
    return dict(
        running, release=pointer['release'], candidate=pointer['candidate'], pointer='ok',
        matches_pointer=pointer['odoo_commit'] == running['odoo_commit'])


# The pair this process was started from: the trees move only when the box is about to restart.
_RUNNING = _read_running()


def state_path():
    return Path(_helpers().path_file(STATE_FILE))


def read_state():
    """What the box keeps about its releases, and what other parts of the layer keep beside it; {} without a file.

    The guard that undoes a release reads `trial` of this file on its own: a
    key of that record, once written, keeps its meaning in every later release.
    """
    try:
        state = json.loads(state_path().read_bytes().decode())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _logger.warning('The state file of the box software cannot be read: the box starts from an empty one')
        return {}
    return state if isinstance(state, dict) else {}


def _store(state):
    """Write the state file whole or not at all; the caller made the card writable."""
    path = state_path()
    staged = path.with_name(path.name + '.new')
    with open(staged, 'w', encoding='utf-8') as file:
        json.dump(state, file, indent=1, sort_keys=True)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    os.replace(staged, path)


def _open_lock_file():
    """A descriptor of the lock file, created when absent.

    An existing file is opened without O_CREAT: with `fs.protected_regular` set, opening a file of another
    user with O_CREAT in a sticky directory is refused even to root. A file that appears meanwhile is opened.
    """
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    for _attempt in range(3):
        try:
            return os.open(STATE_LOCK_FILE, flags)
        except FileNotFoundError:
            pass
        try:
            return os.open(STATE_LOCK_FILE, flags | os.O_CREAT | os.O_EXCL, 0o660)
        except FileExistsError:
            pass
    return os.open(STATE_LOCK_FILE, flags)


@contextlib.contextmanager
def _state_file_lock():
    """Hold, for the block, the lock the roll-back guard takes around its own changes of the state file.

    The caller holds `_state_lock`. `change_state` takes this lock before it makes the card writable, as the
    guard does; `_apply` and `_resume` keep the card writable for a whole move and take this lock only around
    each change of the state file, never while the guard runs or the box service restarts: both take it
    themselves. The card being open is no lock, so the two orders cannot wait on each other.
    Raises TimeoutError, an OSError, when the guard holds it for STATE_LOCK_WAIT seconds.
    """
    descriptor = _open_lock_file()
    try:
        deadline = time.monotonic() + STATE_LOCK_WAIT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('the state file is held by the roll-back guard') from None
                time.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


def change_state(change):
    """Read the state file, let `change(state)` alter it, write it back, and return it.

    Another part of the layer keeps what it must not lose at a restart under a
    key of its own. Raises OSError when the file cannot be written.
    """
    with _state_lock, _state_file_lock(), _helpers().writable():
        state = read_state()
        change(state)
        _store(state)
        return state


def auto_update():
    """Whether a release the box found and checked is applied without anyone asking."""
    return (_helpers().get_conf(trust.AUTO_UPDATE_KEY) or '1').strip() != '0'


def set_auto_update(enabled):
    _helpers().update_conf({trust.AUTO_UPDATE_KEY: '1' if enabled else '0'})


def is_candidate_box():
    """Whether this box was written from a candidate image: such a box is never a customer's."""
    return Path(_helpers().path_file(CHANNEL_FILE)).exists()


def _candidate_channel():
    """The ref the channel file names; '' when it names none git could hold."""
    try:
        lines = Path(_helpers().path_file(CHANNEL_FILE)).read_text(encoding='utf-8', errors='replace').split('\n')
    except OSError:
        return ''
    name = lines[0].strip()
    usable = _REF.fullmatch(name) and '..' not in name and '//' not in name and not name.endswith(('/', '.', '.lock'))
    if not usable:
        return ''
    return name if name.startswith('refs/') else f'refs/heads/{name}'


def _series_channel(series):
    """The release channel of a series: the branch that only a published release moves."""
    return f'refs/heads/box-release/{series}'


def channel():
    """The ref of the layer repository this box follows."""
    return (_candidate_channel() if is_candidate_box() else '') or _series_channel(SERIES)


def release():
    """The name of the release this box runs, '' when its layer tree names none."""
    return _RUNNING['release']


def _image_version(text):
    found = _IMAGE.fullmatch((text or '').strip())
    return (int(found.group(1)), int(found.group(2))) if found else None


def _server_series():
    """The series of the server of the box, '' when the box has none or the server does not say."""
    server = _helpers().get_odoo_server_url()
    if not server:
        return ''
    try:
        response = trust.pool_manager().request(
            'POST', server + VERSION_ROUTE, body='{}', headers={'Content-Type': 'application/json'},
            timeout=SERVER_TIMEOUT, retries=False, preload_content=False)
        try:
            answer = json.loads(response.read(MAX_ANSWER).decode())
        finally:
            response.release_conn()
        series = answer['result']['server_serie']
    except (OSError, urllib3.exceptions.HTTPError, ValueError, LookupError, TypeError) as error:
        _logger.info('The server of the box did not say which series it runs (%s)', type(error).__name__)
        return ''
    series = series.replace('~', '-') if isinstance(series, str) else ''
    return series if _SERIES.fullmatch(series) else ''


def _remote_head(ref):
    try:
        listing = _git(LAYER_TREE, 'ls-remote', LAYER_REPOSITORY, ref, timeout=REMOTE_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        raise _Stop(UNREACHABLE) from None
    for line in listing.decode(errors='replace').splitlines():
        name, _tab, listed = line.partition('\t')
        if listed == ref and _COMMIT.fullmatch(name):
            return name
    raise _Stop(NO_CHANNEL)


def _release_listing(series):
    """(head, releases) of the release channel of `series`, from one question to the repository of the layer.

    `releases` holds the names of the published releases of the series (tags `box-<series>-<release>`) that
    name the commit at `head`. A channel with no such release is `_Stop(RELEASE_UNTAGGED)`: the commit is not
    looked at, and the next look asks again.
    """
    ref, prefix = _series_channel(series), f'refs/tags/box-{series}-'
    try:
        listing = _git(
            LAYER_TREE, 'ls-remote', LAYER_REPOSITORY, ref, f'{prefix}*', timeout=REMOTE_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        raise _Stop(UNREACHABLE) from None
    head, tags = '', {}
    for line in listing.decode(errors='replace').splitlines():
        name, _tab, listed = line.partition('\t')
        if not _COMMIT.fullmatch(name):
            continue
        if listed == ref:
            head = name
        elif listed.startswith(prefix):
            # A tag with an annotation is listed twice: the line ending in `^{}` gives the commit it names.
            tag = listed[len(prefix):]
            if tag.endswith('^{}'):
                tags[tag[:-3]] = name
            else:
                tags.setdefault(tag, name)
    if not head:
        raise _Stop(NO_CHANNEL)
    releases = [tag for tag, commit in tags.items() if commit == head and _RELEASE.fullmatch(tag)]
    if not releases:
        raise _Stop(RELEASE_UNTAGGED)
    return head, releases


def _clear_locks(tree):
    """Remove the lock files a git command that was cut off left in the repository of `tree`.

    Git refuses to write beside one for ever. Nothing but this file writes these
    repositories while the box service runs, and it runs one command at a time.
    """
    repository = Path(tree, '.git')
    for lock in (*repository.glob('*.lock'), *repository.glob('refs/**/*.lock')):
        with contextlib.suppress(OSError):
            lock.unlink()


def _fetch(tree, repository, source, timeout):
    """Bring `source` of `repository` into the repository of `tree`, whose working tree does not move."""
    with _helpers().writable():
        _clear_locks(tree)
        _git(tree, 'fetch', '--quiet', '--no-tags', '--depth=1', '--no-write-fetch-head',
             repository, f'+{source}:{OFFERED_REF}', timeout=timeout)


def _fetch_layer(ref, head):
    """Have the commit `head` of the channel `ref` in the layer repository."""
    if _commit(LAYER_TREE, head):
        return
    try:
        _fetch(LAYER_TREE, LAYER_REPOSITORY, ref, LAYER_FETCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise _Stop(UNREACHABLE, 'layer_fetch') from None
    if not _commit(LAYER_TREE, head):
        # The channel moved between the two questions: the next look meets its new head.
        raise _Stop(UNREACHABLE, 'channel_moved')


def _core_repository_answers():
    try:
        _git(CORE_TREE, 'ls-remote', CORE_REPOSITORY, 'HEAD', timeout=REMOTE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _fetch_core(commit):
    """Have `commit` in the core repository.

    A repository that answers and twice does not give the commit does not
    serve it; anything else is a network that failed, and is tried again.
    """
    if _commit(CORE_TREE, commit):
        return
    for _attempt in range(2):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            _fetch(CORE_TREE, CORE_REPOSITORY, commit, CORE_FETCH_TIMEOUT)
        if _commit(CORE_TREE, commit):
            return
        if not _core_repository_answers():
            raise _Stop(UNREACHABLE, 'core_fetch')
    raise _Stop(CORE_COMMIT_UNAVAILABLE)


def _refusal_key(ref, head):
    return f'{ref} {head}'


def _checked(ref, series, candidate_channel, head, state, again, releases=None):
    """The release at `head` of `ref`, once everything a box needs of it is on the card and checked.

    `releases` are the published releases that name `head`; the pointer must name one of them. It is None on a
    candidate channel, which is not bound to a published release.
    """
    if head in state.get('bad', {}):
        raise _Stop(BAD_RELEASE)
    remembered = state.get('refused', {}).get(_refusal_key(ref, head))
    if remembered and not again and remembered.get('reason') != RELEASE_TAG_REASON:
        raise _Stop(remembered.get('state', POINTER_INVALID), remembered.get('reason', ''))
    _fetch_layer(ref, head)
    try:
        shown = _git(LAYER_TREE, 'show', f'{head}:{POINTER}', check=False)
    except (OSError, subprocess.SubprocessError):
        raise _Stop(NO_POINTER) from None
    if shown.returncode:
        raise _Stop(NO_POINTER)
    pointer = _pointer(shown.stdout)
    if releases is not None and pointer['release'] not in releases:
        raise _Stop(POINTER_INVALID, RELEASE_TAG_REASON)
    if pointer['series'] != series:
        raise _Stop(POINTER_INVALID, 'series')
    if pointer['candidate'] and not candidate_channel:
        raise _Stop(CANDIDATE_REFUSED)
    image = _image_version(_helpers().read_file_first_line(IMAGE_VERSION_FILE))
    if image is None or _image_version(pointer['min_image']) > image:
        raise _Stop(NEEDS_NEW_IMAGE)
    _fetch_core(pointer['odoo_commit'])
    return dict(pointer, layer_commit=head, channel=ref)


def _listed(series):
    """What `_release_listing` answers for `series`: (head, releases), or the `_Stop` that ended it."""
    try:
        return _release_listing(series)
    except _Stop as stop:
        return stop


def _look_at(ref, series, candidate_channel, state, again, listing=None):
    """Where the box stands with the channel `ref`: a record with `state`, and `offer` when it can move.

    A candidate channel is read with `_remote_head`, a release channel with `_release_listing`; a caller that
    asked the release channel already hands over what `_listed` answered.
    """
    look = {'channel': ref, 'series': series, 'head': '', 'release': '', 'reason': ''}
    try:
        if candidate_channel:
            head, releases = _remote_head(ref), None
        else:
            listing = listing or _listed(series)
            if isinstance(listing, _Stop):
                raise listing
            head, releases = listing
        look['head'] = head
        offer = _checked(ref, series, candidate_channel, head, state, again, releases)
    except _Stop as stop:
        look.update(state=stop.state, reason=stop.reason)
        return look
    look['release'] = offer['release']
    runs_it = (offer['layer_commit'], offer['odoo_commit']) == (_RUNNING['layer_commit'], _RUNNING['odoo_commit'])
    if runs_it:
        return dict(look, state=UP_TO_DATE)
    return dict(look, state=AVAILABLE, offer=offer)


def _step(ref, state, reason=''):
    return {'channel': ref, 'series': SERIES, 'head': '', 'release': '', 'state': state, 'reason': reason}


def _look(state, again):
    """Look at what the box follows: (record of the look, release to move to or None, whether a candidate
    box found its channel gone). The `steps` of the record are the channels looked at, the last one deciding.

    Nothing is asked of the server of the box before the repository of the
    layer answered: without it there is no release to move to.
    """
    steps = []
    own = _series_channel(SERIES)
    promote = False
    if not (_RUNNING['layer_commit'] and _RUNNING['odoo_commit']):
        return _record([_step(channel(), UNREACHABLE, 'not_a_repository')], '')
    if is_candidate_box():
        ref = _candidate_channel()
        steps.append(_look_at(ref, SERIES, True, state, again) if ref else _step('', NO_CHANNEL, 'channel_file'))
        if steps[-1]['state'] != NO_CHANNEL:
            return _record(steps, '')
        # The channel of a candidate box is gone: from here on the box looks where a supported box looks.
        promote = True
    listing = _listed(SERIES)
    if isinstance(listing, _Stop) and listing.state == UNREACHABLE:
        return _record([_step(own, UNREACHABLE)], '')
    server_series = _server_series()
    if server_series and server_series != SERIES:
        steps.append(_look_at(_series_channel(server_series), server_series, False, state, again))
        if steps[-1]['state'] in (AVAILABLE, UNREACHABLE):
            return _record(steps, server_series, promote)
    steps.append(_look_at(own, SERIES, False, state, again, listing))
    return _record(steps, server_series, promote)


def _record(steps, server_series, promote=False):
    last = steps[-1]
    record = {
        'at': _now(), 'state': last['state'], 'reason': last['reason'], 'channel': last['channel'],
        'head': last['head'], 'release': last['release'], 'server_series': server_series,
        'steps': [{key: value for key, value in step.items() if key != 'offer'} for step in steps],
    }
    offer = last.get('offer')
    if offer:
        offer = dict(offer, promote=promote)
    return record, offer, promote


def _remember(state, record):
    refused = state.setdefault('refused', {})
    for step in record['steps']:
        key = _refusal_key(step['channel'], step['head'])
        if step['state'] in REMEMBERED and step['head'] and step['reason'] != RELEASE_TAG_REASON:
            known = refused.get(key, {})
            if (known.get('state'), known.get('reason')) != (step['state'], step['reason']):
                refused[key] = {'state': step['state'], 'reason': step['reason'], 'at': record['at']}
        elif step['state'] in (UP_TO_DATE, AVAILABLE):
            refused.pop(key, None)
    for key in sorted(refused, key=lambda key: refused[key].get('at', ''))[:-MAX_REFUSED]:
        del refused[key]


def _trial(state):
    trial = state.get('trial')
    return trial if isinstance(trial, dict) else None


def _promote_now(state, record, promote):
    """A candidate box that runs, for good, the supported release its series names has nothing left of a candidate."""
    if promote and record['state'] == UP_TO_DATE and not _RUNNING['candidate'] and not _trial(state):
        _leave_candidate_channel()


def _leave_candidate_channel():
    _helpers().unlink_file(CHANNEL_FILE)
    _logger.warning(
        'This box followed a candidate channel that is gone; it now follows the release channel of its series')


def check(apply=None, again=False):
    """Look for a release on the channel of the box, now; return `status()`.

    A release that is found and fully checked is applied when `apply` is true,
    kept as an offer when it is false, and left to the setting of the box
    (`auto_update`) when it is None. `again` checks anew a head that was
    refused before; a release the box undid is never applied again. One look
    at a time: a call that comes during one changes nothing.
    """
    if not _busy.acquire(blocking=False):
        return status()
    try:
        _activity[:] = ['checking']
        state = read_state()
        record, offer, promote = _look(state, again)
        waits = bool(offer and _trial(state))
        if waits:
            record['reason'] = 'trial_pending'

        def keep(kept):
            _remember(kept, record)
            kept.update(last_check=record, offered=offer)

        change_state(keep)
        _logger.info(
            'Release check on %s: %s %s', record['channel'] or 'no channel', record['state'], record['reason'])
        _promote_now(state, record, promote)
        if offer and not waits and (auto_update() if apply is None else apply):
            _activity[:] = ['applying']
            _apply(offer)
    except Exception:
        _logger.exception('The look for a release of the box software failed')
    finally:
        del _activity[:]
        _busy.release()
    return status()


def _in_background(**options):
    threading.Thread(target=check, kwargs=options, name='ViinUpdateCheck', daemon=True).start()
    said = status()
    return dict(said, busy=said['busy'] or 'checking')


def check_now():
    """Start a look for a release that applies nothing, and return `status()` at once; the look ends later."""
    return _in_background(apply=False, again=True)


def apply_now():
    """Start a look that applies the release it finds, and return `status()` at once.

    The box restarts into the release when one is applied.
    """
    return _in_background(apply=True, again=True)


def _run_apply_script():
    subprocess.run(
        ['sudo', '-n', 'bash', str(LAYER_TREE / APPLY_SCRIPT)],
        capture_output=True, check=True, timeout=LOCAL_TIMEOUT)


def _move(commits):
    """Put the core tree, then the layer tree, at `commits`, and install the system files of that layer."""
    for tree, commit in ((CORE_TREE, commits['odoo_commit']), (LAYER_TREE, commits['layer_commit'])):
        _clear_locks(tree)
        _git(tree, 'reset', '--hard', '--quiet', commit)
    _run_apply_script()


def _apply(offer):
    """Move both trees to the release `offer` names, then restart the box into it.

    The pair to go back to is written down before either tree moves: from
    there on the guard can undo the move without anything of the layer.
    """
    previous = {key: _RUNNING[key] for key in ('layer_commit', 'odoo_commit', 'release')}
    if not (previous['layer_commit'] and previous['odoo_commit']):
        _fail_apply(offer, 'running_pair_unknown')
        return
    trial = {
        'phase': APPLYING_PHASE, 'starts': 0, 'since': _now(), 'promote': bool(offer.get('promote')),
        'trees': {'layer': str(LAYER_TREE), 'core': str(CORE_TREE)},
        'previous': previous,
        'target': {key: offer[key] for key in (*previous, 'candidate', 'channel', 'series')},
    }
    with _state_lock, _helpers().writable():
        with _state_file_lock():
            state = read_state()
            if _trial(state):
                return
            state.update(trial=trial, offered=None)
            _store(state)
        try:
            for tree, commit in ((CORE_TREE, previous['odoo_commit']), (LAYER_TREE, previous['layer_commit'])):
                _git(tree, 'update-ref', PREVIOUS_REF, commit)
            _move(offer)
            # Core, when the layer is not there to stop it, follows the branch that has the name of its server's series.
            for tree in (CORE_TREE, LAYER_TREE):
                _git(tree, 'branch', '-M', offer['series'], check=False)
        except (OSError, subprocess.SubprocessError) as error:
            _logger.error('The release %s could not be put in place (%s)', offer['release'], type(error).__name__)
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _move(previous)
            with _state_file_lock():
                state = read_state()
                state['trial'] = None
                _note_failure(state, offer, 'trees_or_system_files')
                _store(state)
            return
        trial['phase'] = APPLIED_PHASE
        with _state_file_lock():
            state = read_state()
            state['trial'] = trial
            state['last_check'] = dict(state.get('last_check') or {}, state=APPLIED, reason='', at=_now())
            _store(state)
        # The guard takes the lock of the state file itself: it is not held here.
        _count_start()
    _logger.warning(
        'The box moves from release %s to release %s and restarts', previous['release'] or '?', offer['release'])
    _helpers().odoo_restart(RESTART_DELAY)


def _note_failure(state, offer, reason):
    record = {'state': APPLY_FAILED, 'reason': reason, 'at': _now()}
    state.setdefault('refused', {})[_refusal_key(offer['channel'], offer['layer_commit'])] = record
    state['last_check'] = dict(state.get('last_check') or {}, **record)


def _fail_apply(offer, reason):
    _logger.error('The release %s is not applied: %s', offer['release'], reason)
    change_state(lambda state: _note_failure(state, offer, reason))


def _count_start():
    """Have the guard count the start the box is about to make by itself, as it counts those of the system.

    The caller holds the card open: the guard, which opens it on its own, then leaves it so.
    """
    if os.access(GUARD, os.X_OK):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([GUARD], capture_output=True, check=False, timeout=LOCAL_TIMEOUT)


def _runs_normally():
    """Whether the box runs as a box does: the layer in place on every seam, and the device manager of core alive."""
    integrity = seams.integrity()
    in_place = integrity['load_order_ok'] and all(
        seam['state'] in (seams.APPLIED, seams.DEFERRED) for seam in integrity['seams'])
    try:
        return in_place and seams.core('main').manager.is_alive()
    except (KeyError, AttributeError):
        return False


def _runs(pair):
    return (pair.get('layer_commit'), pair.get('odoo_commit')) == (_RUNNING['layer_commit'], _RUNNING['odoo_commit'])


def _resume():
    """End a move the box did not finish: both trees go back, and the release is neither good nor bad.

    The guard does this before a start; the box does it itself when it started without the guard.
    """
    trial = _trial(read_state())
    if not trial or trial.get('phase') != APPLYING_PHASE:
        return
    previous = trial.get('previous') or {}
    _logger.warning('A move to another release was cut short: the box goes back to the release it ran')
    # No look for a release may start between here and the restart.
    _busy.acquire()
    with _state_lock, _helpers().writable():
        if all(_COMMIT.fullmatch(previous.get(key) or '') for key in ('layer_commit', 'odoo_commit')):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _move(previous)
        with _state_file_lock():
            state = read_state()
            state['trial'] = None
            _store(state)
    if _runs(previous):
        _busy.release()
    else:
        _helpers().odoo_restart(RESTART_DELAY)


def _settle():
    """Mark the release on trial good once the box runs normally; True when no trial is left to settle."""
    trial = _trial(read_state())
    if not trial or trial.get('phase') != APPLIED_PHASE:
        return True
    target = trial.get('target') or {}
    if _runs(target) and not _runs_normally():
        if not _unsettled:
            _unsettled.append(target.get('release'))
            _logger.critical(
                'The box does not run normally on release %s: the release is not marked good',
                target.get('release'))
        return False

    change_state(lambda state: state.update(trial=None))
    if _runs(target):
        _logger.warning('Release %s ran normally for its first minutes: it is marked good', target.get('release'))
        if trial.get('promote'):
            _leave_candidate_channel()
    return True


def _look_without_server():
    """Look for a release now when the box has no server.

    Core looks at its start only when the box has a server, so a box that is not
    paired yet would wait for the night. This is the look core would take: the
    same function, on its own thread, asking nothing but the release channel
    (`_look` asks the server of the box only when there is one).
    """
    if not _helpers().get_odoo_server_url():
        threading.Thread(target=check, name='ViinUpdateCheck', daemon=True).start()


class _Keeper(threading.Thread):
    """Settles, once the box has started, what an update left open, and takes the first look of a box without a
    server."""

    def __init__(self):
        super().__init__(name='ViinReleaseKeeper', daemon=True)

    def run(self):
        try:
            _resume()
        except Exception:
            _logger.exception('A move to another release that was cut short could not be ended')
        try:
            _look_without_server()
        except Exception:
            _logger.exception('The first look for a release of the box could not be started')
        time.sleep(GOOD_AFTER)
        while True:
            try:
                if _settle():
                    return
            except Exception:
                _logger.exception('The release on trial could not be settled')
            time.sleep(SETTLE_PERIOD)


def status():
    """What the box says of its software.

    {'release', 'series', 'candidate_box', 'channel', 'running', 'auto_update',
    'busy', 'last_check', 'offered', 'trial', 'rolled_back', 'bad'}:

    * 'release' - the name of the release the box runs; a candidate's is, or ends in, `rc.<number>`;
    * 'candidate_box' - the box was written from a candidate image and is not for production use;
    * 'running' - {'layer_commit', 'odoo_commit', 'release', 'candidate', 'pointer', 'matches_pointer'}:
      the pair the box runs, whether its layer tree names a release ('ok', 'missing', 'invalid') and
      whether core is at the commit that release names;
    * 'busy' - 'checking', 'applying' or '';
    * 'last_check' - None, or {'at', 'state', 'reason', 'channel', 'head', 'release', 'server_series',
      'steps'}; `steps` holds one such record per channel looked at, the last being the one that decided;
    * 'offered' - None, or the release found, checked and not applied;
    * 'trial' - None, or {'release', 'phase', 'starts', 'since'} of the release that is not marked good yet;
    * 'rolled_back' - None, or {'release', 'layer_commit', 'odoo_commit', 'at', 'starts'} of the last
      release that was undone;
    * 'bad' - the releases that were undone and are never applied again.
    """
    state = read_state()
    trial = _trial(state)
    offered = state.get('offered')
    if not isinstance(offered, dict) or _runs(offered):
        offered = None
    bad = state.get('bad') if isinstance(state.get('bad'), dict) else {}
    return {
        'release': _RUNNING['release'],
        'series': SERIES,
        'candidate_box': is_candidate_box(),
        'channel': channel(),
        'running': dict(_RUNNING),
        'auto_update': auto_update(),
        'busy': _activity[0] if _activity else '',
        'last_check': state.get('last_check'),
        'offered': offered,
        'trial': trial and {
            'release': (trial.get('target') or {}).get('release', ''), 'phase': trial.get('phase', ''),
            'starts': trial.get('starts', 0), 'since': trial.get('since', ''),
        },
        'rolled_back': state.get('rolled_back'),
        'bad': [dict(record, layer_commit=commit) for commit, record in sorted(bad.items())],
    }


startup.set_update_check(check)
_Keeper().start()
