"""The software release a box runs: what it moves to, what it refuses, and the guard that undoes a release.

The box looks at local repositories that git reaches under the names of the two places the box
software comes from, and moves trees of its own: the trees the suite runs from never move.
"""
import fcntl
import json
import os
import subprocess
import tempfile
import threading

import common
import services_common as box

SERIES = box.SERIES
CANDIDATE_CHANNEL = f'box-candidate/{SERIES}'
RELEASE_CHANNEL = f'refs/heads/{box.release_channel(SERIES)}'
OTHER_SERIES = '19.0'
OTHER_RELEASE_CHANNEL = f'refs/heads/{box.release_channel(OTHER_SERIES)}'
# The lock the box software and the roll-back guard (two processes) both take around a change of the state file.
STATE_LOCK_FILE = '/tmp/viin-box-state.lock'
NOT_A_COMMIT_OF_CORE = '0123456789abcdef0123456789abcdef01234567'


def _box_runs_the_trees_of(update, releases):
    """The box was started from the trees of `releases`: the pair it runs is read from them, as at a start."""
    update.LAYER_TREE, update.CORE_TREE = releases.layer_tree, releases.core_tree
    update._RUNNING.update(update._read_running())


def _start_on_the_release(update, releases):
    """The box starts on the pair its trees hold, and runs for the minutes after which it marks a release good."""
    _box_runs_the_trees_of(update, releases)
    update.GOOD_AFTER = 0
    keeper = update._Keeper()
    keeper.start()
    keeper.join(30)
    # The first look of a box without a server is over before the case goes on with a look of its own.
    for thread in threading.enumerate():
        if thread.name == 'ViinUpdateCheck':
            thread.join(60)


def _box_that_looks():
    """Start a box on the trees of a new set of releases, with the guard of the case and no state of an earlier one.

    Gives (releases, update, happened, look): `look(case, **options)` has the box look for a release as
    `update.check` does and records in `happened[case]` what it did.
    """
    box.STATE_FILE.write_text('{}')
    releases = box.Releases(tempfile.mkdtemp(prefix='box-suite-'))
    common.start()
    update, selftest = common.layer('update'), common.layer('selftest')
    # The starts are counted by the guard the case runs itself, never twice by one installed on the machine.
    update.GUARD = os.path.join(tempfile.mkdtemp(prefix='box-suite-'), 'viin-box-guard')
    _box_runs_the_trees_of(update, releases)
    os.environ['GIT_CONFIG_GLOBAL'] = releases.reachable
    happened = {'core_commits': releases.core_commits}

    def look(case, **options):
        restarts = len(common.restarts)
        offered_before = _offered_ref(releases)
        status = update.check(**options)
        last = status['last_check'] or {}
        line = next(line for line in selftest.run(wait=0)['lines'] if line['id'] == 'software')
        # The self-test keeps its last result in the state file of the box: let it be written before anything else.
        common.wait_until(lambda: any(
            kept == {'id': 'software', 'state': line['state'], 'reason': line['reason']}
            for kept in (selftest.last_kept() or {}).get('lines', [])), 'the self-test to be kept')
        happened[case] = {
            'state': last.get('state'), 'reason': last.get('reason'), 'channel': last.get('channel'),
            'trees': list(releases.heads()), 'restarted': len(common.restarts) > restarts,
            'offered': (status['offered'] or {}).get('release', ''), 'trial': status['trial'],
            'channel_file': box.CHANNEL_FILE.exists(), 'release': status['release'],
            'software_line': [line['state'], line['reason']],
            'fetched': _offered_ref(releases) != offered_before,
        }

    return releases, update, happened, look


def _offered_ref(releases):
    """The commit the layer tree holds as the one it fetched to check, '' while it fetched none."""
    try:
        return releases.git(releases.layer_tree, 'rev-parse', '--verify', '--quiet', 'refs/viin-box/offered')
    except subprocess.CalledProcessError:
        return ''


def look_for_releases():
    """Start a box, have it look for a release in each case a channel can offer, and say what it did each time.

    Gives one record per case: the state and the channel of the look, the 'trees' the box runs
    after it, whether it 'restarted', the release 'offered', the 'trial', whether the 'channel_file'
    exists, the 'release' the box runs and the self-test line of its software. 'guard' says what
    the starts of a release that does not start did, 'promoted' what is left of a candidate box.
    """
    releases, update, happened, look = _box_that_looks()
    [core_0, core_1] = releases.core_commits

    happened['first'] = {'trees': list(releases.heads()), 'release': update.status()['release']}
    os.environ['GIT_CONFIG_GLOBAL'] = releases.unreachable
    look('unreachable')
    os.environ['GIT_CONFIG_GLOBAL'] = releases.reachable
    look('no_channel')
    releases.release(SERIES, None, release='without-pointer')
    look('no_pointer')
    releases.release(SERIES, '{ "release": ', release='not-json')
    look('not_json')
    releases.release(SERIES, box.pointer('1.1', core_1, series='17.0'), release='1.1')
    look('other_series')
    releases.release(SERIES, box.pointer('1.1', core_1, candidate=True), release='1.1')
    look('candidate_without_its_mark')
    releases.release(SERIES, box.pointer('rc.2', core_1), release='rc.2')
    look('candidate_on_the_series_channel')
    releases.release(SERIES, box.pointer('1.1', core_1, min_image='99.0'), release='1.1')
    look('newer_image')
    releases.release(SERIES, box.pointer('1.1', NOT_A_COMMIT_OF_CORE), release='1.1')
    look('core_commit_not_served')
    releases.release(SERIES, box.pointer('1.1', core_1), release='1.1', apply_fails=True)
    look('system_files_refused', apply=True, again=True)

    update.set_auto_update(False)
    happened['release_1_1'] = releases.release(SERIES, box.pointer('1.1', core_1), release='1.1')
    look('offered')
    happened['offered']['core_commit_on_the_card'] = bool(
        releases.git(releases.core_tree, 'cat-file', '-t', core_1) == 'commit')
    look('applied', apply=True, again=True)
    happened['applied']['system_files'] = releases.system_files_of('1.1')
    _start_on_the_release(update, releases)
    happened['good'] = {'trial': update.status()['trial'], 'release': update.status()['release']}

    # A release with which the box does not start: the system starts the box service three times.
    happened['release_1_2'] = releases.release(SERIES, box.pointer('1.2', core_0), release='1.2')
    look('trial', apply=True, again=True)
    starts = []
    for _start in range(3):
        box.run_guard()
        starts.append(list(releases.heads()))
    state = json.loads(box.STATE_FILE.read_text())
    happened['guard'] = {
        'trees_after_each_start': starts, 'bad': sorted(state.get('bad') or {}), 'trial': state.get('trial'),
        'system_files_of_1_1_again': releases.system_files_of('1.1'),
    }
    _box_runs_the_trees_of(update, releases)
    look('bad_release', apply=True, again=True)

    # A candidate box, then its channel gone.
    update.set_auto_update(True)
    box.CHANNEL_FILE.write_text(f'{CANDIDATE_CHANNEL}\n')
    releases.publish(CANDIDATE_CHANNEL, box.pointer('rc.2', core_0), release='rc.2')
    look('candidate_box', apply=False, again=True)
    look('candidate_box_applies', apply=True, again=True)
    _start_on_the_release(update, releases)
    releases.delete_branch(CANDIDATE_CHANNEL)
    happened['release_1_3'] = releases.release(SERIES, box.pointer('1.3', core_1), release='1.3')
    look('candidate_channel_gone', apply=True, again=True)
    _start_on_the_release(update, releases)
    status = update.status()
    happened['promoted'] = {
        'channel_file': box.CHANNEL_FILE.exists(), 'candidate_box': status['candidate_box'],
        'channel': status['channel'], 'release': status['release'], 'trial': status['trial'],
    }

    happened['promoted_trees'] = list(releases.heads())

    # A server on another series.
    server = box.ServerOfASeries(OTHER_SERIES)
    common.store_box_configuration(remote_server=server.url)
    releases.release(OTHER_SERIES, box.pointer('2.0', core_0, series=OTHER_SERIES), release='2.0')
    look('server_on_another_series', apply=False, again=True)
    releases.delete_branch(box.release_channel(OTHER_SERIES))
    releases.publish(OTHER_SERIES, box.pointer('2.1', core_0, series=OTHER_SERIES), release='2.1')
    look('server_on_a_series_without_a_branch', apply=False, again=True)
    happened['server_on_a_series_without_a_branch']['steps'] = [
        [step['channel'], step['state']] for step in update.status()['last_check']['steps']]
    return happened


def look_at_the_release_channel():
    """Start a box on release 1.0, publish releases to its channel in each way a channel can be bound, and look.

    A look is a scheduled one (the box decides itself whether to apply), as the box makes them on its own.
    Gives the record of each case, as `look_for_releases` does.
    """
    releases, update, happened, look = _box_that_looks()
    [core_0, core_1] = releases.core_commits
    update.set_auto_update(True)
    releases.name_release(SERIES, releases.first, '1.0')
    happened['first'] = {'trees': list(releases.heads()), 'release': update.status()['release']}

    # A commit merged to the development branch of the series is not a release.
    releases.publish(SERIES, box.pointer('1.1', core_1), release='1.1')
    look('merged_to_the_branch_of_the_series')

    # The tag of a release names another commit than the head of the channel.
    head = releases.release(SERIES, box.pointer('1.2', core_0), release='1.2', tag=False)
    releases.tag(SERIES, releases.first, '1.2')
    look('tag_names_another_commit')

    # A release is tagged with another name than its pointer gives.
    releases.release(SERIES, box.pointer('1.0', core_1), release='1.0', tag='1.1')
    look('tag_differs_from_the_pointer')

    # A head no tag names: nothing is fetched, and the next look applies the release once it is tagged.
    untagged = releases.release(SERIES, box.pointer('1.3', core_0), release='1.3', tag=False)
    look('untagged')
    look('untagged_again')
    releases.tag(SERIES, untagged, '1.3')
    look('tagged_later')
    happened['tagged_later']['untagged_head'] = untagged
    _start_on_the_release(update, releases)

    # Annotated and lightweight tags both name the release.
    annotated = releases.release(SERIES, box.pointer('1.4', core_1), release='1.4', annotated=True)
    look('annotated_tag')
    happened['annotated_tag']['head'] = annotated
    _start_on_the_release(update, releases)
    lightweight = releases.release(SERIES, box.pointer('1.5', core_0), release='1.5')
    look('lightweight_tag')
    happened['lightweight_tag']['head'] = lightweight
    _start_on_the_release(update, releases)

    # The channel moves back to an older release that is tagged; then the release it leaves is one that was undone.
    releases.name_release(SERIES, annotated)
    look('channel_moved_back')
    happened['channel_moved_back']['head'] = annotated
    _start_on_the_release(update, releases)
    releases.name_release(SERIES, lightweight)

    def undone(state):
        state['bad'] = {lightweight: {'release': '1.5', 'odoo_commit': core_0, 'at': '2026-10-15T00:00:00Z'}}

    update.change_state(undone)
    look('channel_moved_back_to_an_undone_release')
    happened['channel_moved_back_to_an_undone_release']['head'] = lightweight

    # The release of the pointer is not among the tags of the head, then the tag it is published under is added.
    wrongly_tagged = releases.release(SERIES, box.pointer('1.6', core_1), release='1.6', tag='1.6-final')
    look('wrong_tag_on_the_head')
    releases.tag(SERIES, wrongly_tagged, '1.6')
    look('right_tag_added_later')
    happened['right_tag_added_later']['head'] = wrongly_tagged
    happened['heads'] = [head, annotated, lightweight]
    return happened


def first_looks_of_a_box_at_its_start():
    """Start a box on release 1.0 while the release channel offers 1.1, then start it again, with and without
    a server, and say what each start did.

    Meant for `common.in_new_process`. One record per start: 'trees' and 'release' the box runs after it,
    'offered' (the release found and not applied), 'restarted', and the 'hosts' the box asked.
    """
    releases, update, happened, _look = _box_that_looks()
    [core_0, core_1] = releases.core_commits
    happened['first'] = {'trees': list(releases.heads())}
    happened['release_1_1'] = releases.release(SERIES, box.pointer('1.1', core_1), release='1.1')

    def start(case):
        restarts = len(common.restarts)
        with common.AskedHosts() as asked:
            _start_on_the_release(update, releases)
        status = update.status()
        happened[case] = {
            'trees': list(releases.heads()), 'release': status['release'],
            'offered': (status['offered'] or {}).get('release', ''), 'restarted': len(common.restarts) > restarts,
            'hosts': asked.hosts}

    update.set_auto_update(False)
    start('automatic_updates_off')
    update.set_auto_update(True)
    start('automatic_updates_on')

    server = common.FakeServer()
    common.store_box_configuration(remote_server=server.url)
    happened['release_1_2'] = releases.release(SERIES, box.pointer('1.2', core_0), release='1.2')
    start('with_a_server')
    return happened


class TestReleases(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        kept = {path: path.read_bytes() if path.exists() else None for path in (box.STATE_FILE, box.CHANNEL_FILE)}
        try:
            cls.happened = common.in_new_process(look_for_releases)
        finally:
            for path, content in kept.items():
                box._put_back(path, content)

    def assertBoxStays(self, case):
        """The look of `case` left both trees and the release the box runs as they were, and restarted nothing."""
        happened = self.happened[case]
        self.assertEqual(happened['trees'], self.happened['first']['trees'], case)
        self.assertEqual(happened['release'], self.happened['first']['release'], case)
        self.assertIs(happened['restarted'], False, case)

    def assertRefused(self, case, state):
        self.assertEqual(self.happened[case]['state'], state, self.happened[case])
        self.assertBoxStays(case)

    def test_box_that_cannot_reach_where_its_software_comes_from_stays_as_it_is(self):
        self.assertRefused('unreachable', 'unreachable')

    def test_channel_that_does_not_exist_moves_nothing(self):
        self.assertRefused('no_channel', 'no_channel')

    def test_head_without_a_pointer_moves_nothing(self):
        self.assertRefused('no_pointer', 'no_pointer')

    def test_pointer_that_is_not_one_moves_nothing(self):
        self.assertRefused('not_json', 'pointer_invalid')

    def test_pointer_of_another_series_moves_nothing(self):
        self.assertRefused('other_series', 'pointer_invalid')

    def test_pointer_marked_candidate_whose_release_does_not_say_so_moves_nothing(self):
        self.assertRefused('candidate_without_its_mark', 'pointer_invalid')

    def test_supported_box_refuses_a_candidate_pointer(self):
        self.assertRefused('candidate_on_the_series_channel', 'candidate_refused')

    def test_pointer_that_needs_a_newer_image_moves_nothing(self):
        self.assertRefused('newer_image', 'needs_new_image')

    def test_pointer_whose_core_commit_cannot_be_fetched_moves_nothing(self):
        self.assertRefused('core_commit_not_served', 'core_commit_unavailable')

    def test_release_whose_system_files_cannot_be_installed_leaves_the_box_on_its_release(self):
        self.assertRefused('system_files_refused', 'apply_failed')

    def test_self_test_names_the_state_of_a_look_that_moved_nothing(self):
        for case, line in (('no_channel', ['neutral', 'no_channel']),
                           ('other_series', ['warning', 'pointer_invalid']),
                           ('newer_image', ['warning', 'needs_new_image']),
                           ('core_commit_not_served', ['warning', 'core_commit_unavailable'])):
            with self.subTest(case=case):
                self.assertEqual(self.happened[case]['software_line'], line)

    def test_release_found_with_automatic_updates_off_is_offered_and_not_applied(self):
        offered = self.happened['offered']

        self.assertEqual((offered['state'], offered['offered']), ('available', '1.1'))
        self.assertTrue(offered['core_commit_on_the_card'])
        self.assertBoxStays('offered')

    def test_box_moves_to_the_pair_of_commits_its_channel_names_and_restarts(self):
        applied = self.happened['applied']

        self.assertEqual(applied['trees'], [self.happened['release_1_1'], self.happened['core_commits'][1]])
        self.assertTrue(applied['system_files'])
        self.assertTrue(applied['restarted'])
        self.assertEqual(applied['trial']['release'], '1.1')

    def test_release_that_runs_normally_after_its_start_is_marked_good(self):
        self.assertIsNone(self.happened['good']['trial'])
        self.assertEqual(self.happened['good']['release'], '1.1')

    def test_release_the_box_does_not_start_with_is_undone_at_the_third_start(self):
        guard = self.happened['guard']
        moved_to = self.happened['trial']['trees']
        back_to = self.happened['applied']['trees']

        self.assertEqual(guard['trees_after_each_start'], [moved_to, moved_to, back_to])
        self.assertIn(self.happened['release_1_2'], guard['bad'])
        self.assertIsNone(guard['trial'])
        self.assertTrue(guard['system_files_of_1_1_again'])

    def test_release_that_was_undone_is_never_tried_again(self):
        bad = self.happened['bad_release']

        self.assertEqual(bad['state'], 'bad_release')
        self.assertEqual(bad['trees'], self.happened['applied']['trees'])
        self.assertIs(bad['restarted'], False)

    def test_supported_box_never_gets_a_channel_of_its_own(self):
        supported = ('unreachable', 'no_channel', 'candidate_on_the_series_channel', 'offered', 'applied',
                     'bad_release')
        for case in supported:
            with self.subTest(case=case):
                self.assertEqual(self.happened[case]['channel'] or RELEASE_CHANNEL, RELEASE_CHANNEL)
                self.assertIs(self.happened[case]['channel_file'], False)

    def test_candidate_box_follows_its_candidate_channel_and_applies_a_commit_no_tag_names(self):
        candidate = self.happened['candidate_box']

        self.assertEqual(candidate['channel'], f'refs/heads/{CANDIDATE_CHANNEL}')
        self.assertEqual((candidate['state'], candidate['offered']), ('available', 'rc.2'))
        self.assertEqual(self.happened['candidate_box_applies']['state'], 'applied')

    def test_candidate_box_whose_channel_is_gone_becomes_a_supported_box_once_the_series_serves_a_release(self):
        gone = self.happened['candidate_channel_gone']
        promoted = self.happened['promoted']

        self.assertEqual(gone['state'], 'applied')
        self.assertEqual(gone['trees'], [self.happened['release_1_3'], self.happened['core_commits'][1]])
        self.assertEqual(promoted['release'], '1.3')
        self.assertIsNone(promoted['trial'])
        self.assertIs(promoted['channel_file'], False)
        self.assertIs(promoted['candidate_box'], False)
        self.assertEqual(promoted['channel'], RELEASE_CHANNEL)

    def test_box_whose_server_runs_another_series_is_offered_the_release_of_that_series(self):
        other = self.happened['server_on_another_series']

        self.assertEqual(other['channel'], OTHER_RELEASE_CHANNEL)
        self.assertEqual((other['state'], other['offered']), ('available', '2.0'))

    def test_box_whose_server_runs_a_series_without_a_branch_stays_on_its_own_series(self):
        other = self.happened['server_on_a_series_without_a_branch']

        self.assertEqual(other['channel'], RELEASE_CHANNEL)
        self.assertEqual(other['steps'][0], [OTHER_RELEASE_CHANNEL, 'no_channel'])
        self.assertEqual(other['trees'], self.happened['promoted_trees'])


class TestFirstLookAtStart(common.BoxStateCase):
    """A box without a server takes its first look for a release when it starts, not only at night."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        kept = {path: path.read_bytes() if path.exists() else None for path in (box.STATE_FILE, box.CHANNEL_FILE)}
        try:
            cls.happened = common.in_new_process(first_looks_of_a_box_at_its_start)
        finally:
            for path, content in kept.items():
                box._put_back(path, content)

    def test_start_of_a_box_without_a_server_offers_a_release_and_applies_none_when_updates_are_manual(self):
        start = self.happened['automatic_updates_off']

        self.assertEqual(start['offered'], '1.1')
        self.assertEqual(start['trees'], self.happened['first']['trees'])
        self.assertIs(start['restarted'], False)

    def test_start_of_a_box_without_a_server_applies_an_offered_release_when_updates_are_automatic(self):
        start = self.happened['automatic_updates_on']

        self.assertEqual(start['trees'][0], self.happened['release_1_1'])
        self.assertIs(start['restarted'], True)

    def test_first_look_of_a_box_without_a_server_contacts_nothing_but_where_the_software_comes_from(self):
        for case in ('automatic_updates_off', 'automatic_updates_on'):
            with self.subTest(case):
                contacted = set(self.happened[case]['hosts']) - {'127.0.0.1', 'localhost', '::1'}

                self.assertLessEqual(contacted, {common.SOFTWARE_PUBLISHER})

    def test_box_with_a_server_takes_no_second_look_of_its_own_at_its_start(self):
        start = self.happened['with_a_server']

        self.assertEqual(start['offered'], '')
        self.assertEqual(start['trees'], self.happened['automatic_updates_on']['trees'])


class TestRollBackGuard(common.BoxStateCase):
    """The guard is run as the system runs it, around the starts of the box service, after an update moved the box
    from release 1.0 to release 1.1."""

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.STATE_FILE)
        self.releases = releases = box.Releases(self.enterContext(tempfile.TemporaryDirectory(prefix='box-suite-')))
        [core_0, core_1] = releases.core_commits
        self.previous = list(releases.heads())
        target = releases.publish(SERIES, box.pointer('1.1', core_1), release='1.1')
        releases.git(releases.layer_tree, 'fetch', '--quiet', 'origin')
        releases.git(releases.layer_tree, 'reset', '--hard', '--quiet', target)
        releases.git(releases.core_tree, 'reset', '--hard', '--quiet', core_1)
        self.target = [target, core_1]
        self.trial = box.trial_after_update(releases, ('1.0', releases.first, core_0), ('1.1', target, core_1))

    def store(self, **state):
        box.STATE_FILE.write_text(json.dumps(state))

    def stored(self):
        return json.loads(box.STATE_FILE.read_text())

    def test_two_starts_without_the_good_mark_keep_the_release_on_trial(self):
        self.store(trial=self.trial)

        box.run_guard()
        box.run_guard()

        self.assertEqual(list(self.releases.heads()), self.target)
        self.assertEqual(self.stored()['trial']['starts'], 2)

    def test_third_start_without_the_good_mark_puts_back_the_pair_the_box_ran_and_marks_the_release_bad(self):
        self.store(trial=self.trial)

        for _start in range(3):
            box.run_guard()

        self.assertEqual(list(self.releases.heads()), self.previous)
        self.assertIn(self.target[0], self.stored().get('bad') or {})
        self.assertIsNone(self.stored()['trial'])
        self.assertTrue(self.releases.system_files_of('1.0'))

    def test_move_cut_short_is_undone_at_the_next_start_and_is_not_marked_bad(self):
        self.store(trial=dict(self.trial, phase='applying'))

        box.run_guard()

        self.assertEqual(list(self.releases.heads()), self.previous)
        self.assertNotIn(self.target[0], self.stored().get('bad') or {})
        self.assertIsNone(self.stored()['trial'])

    def test_restart_somebody_asked_for_counts_as_one_start(self):
        self.store(trial=self.trial)

        box.run_guard()
        for _restart in range(3):
            box.run_guard('--stopped', env={'SERVICE_RESULT': 'success'})
            box.run_guard()

        self.assertEqual(list(self.releases.heads()), self.target)
        self.assertEqual(self.stored()['trial']['starts'], 1)

class TestReleaseChannel(common.BoxStateCase):
    """The release channel of a series is the branch a published release moves: the box follows nothing else."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        kept = {path: path.read_bytes() if path.exists() else None for path in (box.STATE_FILE, box.CHANNEL_FILE)}
        try:
            cls.happened = common.in_new_process(look_at_the_release_channel)
        finally:
            for path, content in kept.items():
                box._put_back(path, content)

    def assertBoxStays(self, case):
        happened = self.happened[case]
        self.assertEqual(happened['trees'], self.happened['first']['trees'], case)
        self.assertEqual(happened['release'], self.happened['first']['release'], case)
        self.assertIs(happened['restarted'], False, case)

    def test_commit_merged_to_the_branch_of_the_series_moves_nothing_while_the_channel_is_unchanged(self):
        merged = self.happened['merged_to_the_branch_of_the_series']

        self.assertEqual(merged['state'], 'up_to_date')
        self.assertEqual(merged['channel'], RELEASE_CHANNEL)
        self.assertBoxStays('merged_to_the_branch_of_the_series')

    def test_channel_head_no_tag_names_is_not_fetched_and_not_remembered(self):
        for case in ('untagged', 'untagged_again'):
            with self.subTest(case=case):
                self.assertEqual(self.happened[case]['state'], 'release_untagged')
                self.assertIs(self.happened[case]['fetched'], False)
                self.assertBoxStays(case)

        later = self.happened['tagged_later']

        self.assertEqual(later['state'], 'applied')
        self.assertEqual(later['trees'], [later['untagged_head'], self.happened['core_commits'][0]])

    def test_self_test_does_not_warn_about_a_channel_no_release_is_published_on(self):
        self.assertEqual(self.happened['untagged']['software_line'], ['neutral', 'release_untagged'])

    def test_tag_that_names_another_commit_than_the_head_of_the_channel_is_no_release(self):
        self.assertEqual(self.happened['tag_names_another_commit']['state'], 'release_untagged')
        self.assertBoxStays('tag_names_another_commit')

    def test_pointer_that_names_another_release_than_the_tag_moves_nothing(self):
        differs = self.happened['tag_differs_from_the_pointer']

        self.assertEqual((differs['state'], differs['reason']), ('pointer_invalid', 'release_tag'))
        self.assertBoxStays('tag_differs_from_the_pointer')

    def test_annotated_and_lightweight_tags_both_lead_to_the_release(self):
        for case in ('annotated_tag', 'lightweight_tag'):
            with self.subTest(case=case):
                self.assertEqual(self.happened[case]['state'], 'applied')
                self.assertEqual(self.happened[case]['trees'][0], self.happened[case]['head'])

    def test_channel_moved_back_to_an_older_tagged_release_is_followed(self):
        back = self.happened['channel_moved_back']

        self.assertEqual(back['state'], 'applied')
        self.assertEqual(back['trees'][0], back['head'])

    def test_channel_moved_back_to_a_release_that_was_undone_is_refused(self):
        undone = self.happened['channel_moved_back_to_an_undone_release']

        self.assertEqual(undone['state'], 'bad_release')
        self.assertEqual(undone['trees'][0], self.happened['channel_moved_back']['trees'][0])
        self.assertIs(undone['restarted'], False)

    def test_release_the_tags_of_the_head_did_not_name_is_applied_by_the_next_scheduled_look_once_it_is_tagged(self):
        refused = self.happened['wrong_tag_on_the_head']
        later = self.happened['right_tag_added_later']

        self.assertEqual((refused['state'], refused['reason']), ('pointer_invalid', 'release_tag'))
        self.assertEqual(later['state'], 'applied')
        self.assertEqual(later['trees'][0], later['head'])


class TestStateFileSharedWithTheGuard(common.BoxCase):
    """The box software and the roll-back guard change the state file from two processes: neither loses the
    change of the other."""

    ON_TRIAL = {'trial': {'phase': 'applied', 'starts': 1}}

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.STATE_FILE)
        box.STATE_FILE.write_text(json.dumps(self.ON_TRIAL))

    @staticmethod
    def stopped_service_seen_by_the_guard():
        """The guard as the system runs it after the box service stopped by itself, not yet ended."""
        return subprocess.Popen(['python3', '-I', str(box.GUARD), '--stopped'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=dict(os.environ, SERVICE_RESULT='success'))

    def assertGuardIsWaiting(self, guard):
        with self.assertRaises(subprocess.TimeoutExpired):
            guard.wait(timeout=2)

    def test_guard_waits_for_a_process_that_holds_the_state_file_and_keeps_the_change_that_process_makes(self):
        lock = os.open(STATE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o660)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        stale = json.loads(box.STATE_FILE.read_text())
        guard = self.stopped_service_seen_by_the_guard()
        self.addCleanup(guard.wait, 60)

        self.assertGuardIsWaiting(guard)
        box.STATE_FILE.write_text(json.dumps(dict(stale, kept_by_the_holder=True)))
        fcntl.flock(lock, fcntl.LOCK_UN)

        self.assertEqual(guard.wait(60), 0)
        state = json.loads(box.STATE_FILE.read_text())
        self.assertIs(state['kept_by_the_holder'], True)
        self.assertIsNotNone(state['trial']['stopped_at'])

    def test_change_the_box_software_is_making_of_the_state_file_and_a_write_of_the_guard_both_survive(self):
        update = common.layer('update')
        reading, writing = threading.Event(), threading.Event()

        def slow_change(state):
            state['kept_by_the_software'] = True
            reading.set()
            writing.wait(60)

        software = threading.Thread(target=update.change_state, args=(slow_change,), daemon=True)
        software.start()
        self.assertTrue(reading.wait(30))
        guard = self.stopped_service_seen_by_the_guard()
        self.addCleanup(guard.wait, 60)

        self.assertGuardIsWaiting(guard)
        writing.set()
        software.join(60)

        self.assertEqual(guard.wait(60), 0)
        state = json.loads(box.STATE_FILE.read_text())
        self.assertIs(state['kept_by_the_software'], True)
        self.assertIsNotNone(state['trial']['stopped_at'])


class TestStateLockFileOfAnotherUser(common.BoxCase):
    """A process that runs as root opens the lock file a box user made in the shared temporary directory."""

    OPEN_AS_ROOT = (
        'import ast, importlib.machinery, importlib.util, os, sys\n'
        'source, lock = sys.argv[1], sys.argv[2]\n'
        'if sys.argv[3] == "guard":\n'
        '    spec = importlib.util.spec_from_loader("guard", importlib.machinery.SourceFileLoader("guard", source))\n'
        '    module = importlib.util.module_from_spec(spec)\n'
        '    spec.loader.exec_module(module)\n'
        '    module.LOCK_FILE = lock\n'
        '    descriptor = module.open_lock_file()\n'
        'else:\n'
        '    tree = ast.parse(open(source).read())\n'
        '    function = next(node for node in tree.body\n'
        '                    if isinstance(node, ast.FunctionDef) and node.name == "_open_lock_file")\n'
        '    scope = {"os": os, "STATE_LOCK_FILE": lock}\n'
        '    exec(compile(ast.Module([function], []), source, "exec"), scope)\n'
        '    descriptor = scope["_open_lock_file"]()\n'
        'os.close(descriptor)\n')

    def setUp(self):
        super().setUp()
        with open('/proc/sys/fs/protected_regular') as sysctl:
            if int(sysctl.read()) < 1:
                self.skipTest('the machine does not protect regular files in shared directories')
        descriptor, self.lock = tempfile.mkstemp(prefix='box-suite-state-', suffix='.lock', dir='/tmp')
        os.close(descriptor)
        self.addCleanup(os.unlink, self.lock)

    def opened_as_root(self, source, which):
        return subprocess.run(['sudo', '-n', 'python3', '-I', '-c', self.OPEN_AS_ROOT, str(source), self.lock, which],
                              capture_output=True, text=True, check=False, timeout=60)

    def test_guard_running_as_root_opens_a_lock_file_the_box_user_made(self):
        done = self.opened_as_root(box.GUARD, 'guard')

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_box_software_running_as_root_opens_a_lock_file_the_box_user_made(self):
        done = self.opened_as_root(common.LAYER_MODULE / 'update.py', 'software')

        self.assertEqual(done.returncode, 0, done.stderr)


class TestReleaseRulesOfTheGateAndOfTheBox(common.BoxCase):
    """A pointer the check before publishing accepts is one every box follows, and one it refuses no box applies."""

    CHECKER = box.REPOSITORY / 'box' / 'build' / 'check_release.sh'
    POINTERS = {
        'release of 40 characters': ('1' + 'a' * 39, '24.11', True),
        'release of 41 characters': ('1' + 'a' * 40, '24.11', False),
        'release that is a candidate': ('rc.1', '24.11', True),
        'release that ends in a candidate mark': ('1.0-rc.12', '24.11', True),
        'release with rc. in front of more text': ('rc.1-final', '24.11', False),
        'release with rc. and a letter after the number': ('1.0-rc.1x', '24.11', False),
        'release with letters before rc.': ('xrc.1', '24.11', False),
        'release with rc. in the middle': ('1-rc.1-2', '24.11', False),
        'plain release': ('1.0', '24.11', True),
        'image version of one number': ('1.0', '24', False),
        'image version of three numbers': ('1.0', '1.2.3', False),
        'image version of four digits': ('1.0', '1234.1234', True),
        'image version of five digits': ('1.0', '12345.1', False),
    }

    def gate_accepts(self, text):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as pointer:
            pointer.write(text)
            pointer.flush()
            return subprocess.run(['bash', str(self.CHECKER), 'pointer', pointer.name], capture_output=True,
                                  check=False, timeout=60).returncode == 0

    def box_accepts(self, text):
        update = common.layer('update')
        try:
            update._pointer(text.encode())
        except update._Stop:
            return False
        return True

    def test_gate_and_box_give_the_same_verdict_on_the_name_of_a_release_and_the_image_it_needs(self):
        common.start()
        for case, (release, min_image, accepted) in self.POINTERS.items():
            with self.subTest(case):
                text = box.pointer(release, NOT_A_COMMIT_OF_CORE, min_image=min_image)

                self.assertEqual((self.gate_accepts(text), self.box_accepts(text)), (accepted, accepted))

    def test_gate_alone_refuses_a_supported_release_not_starting_with_a_digit_and_a_supported_series_not_nn_n(self):
        common.start()
        # (release, series, the gate accepts it): the box takes every case below, it is more lenient than the gate.
        cases = {
            'supported release starting with a letter': ('v1.0', '18.0', False),
            'supported series of one digit': ('1.0', '8.0', False),
            'supported series of three digits': ('1.0', '118.0', False),
            'supported series with two digits after the dot': ('1.0', '18.10', False),
            'supported release and series of the usual shape': ('1.0', '18.0', True),
            'candidate whose release starts with a letter': ('rc.1', '18.0', True),
            'candidate of a series of one digit': ('rc.1', '8.0', True),
        }
        for case, (release, series, gate_accepts) in cases.items():
            with self.subTest(case):
                text = box.pointer(release, NOT_A_COMMIT_OF_CORE, series=series)

                self.assertEqual((self.gate_accepts(text), self.box_accepts(text)), (gate_accepts, True))
