"""Resolved block notices retain history and recover safely across interrupted writes."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule
from pipeline_fixtures import PipelineFixture
from fakes import FakeAgents, FakeGitHub
from github import NotFound, Transient
from policy import Blocked, digest
import actions
import conversation


class BlockedNoticeTests(PipelineFixture, unittest.TestCase):
    """Exercise real checkpoints, command authorization and comment publication."""

    def setUp(self):
        """Start a small autonomous task with a published block at the plan stage."""
        self.config = deepcopy(CONFIG)
        self.config['autonomous'] = True
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        controller = self.tick()
        controller.suspend('blocked', 'Source could not be read. Retry after the transport recovers.', outboxes=False)
        self.notice = next(c for c in self.gh.comments if '\n### 🛑 Blocked\n' in c['body'])
        self.original = self.notice['body']

    def assert_resolved(self):
        """The reason stays quoted; instructions, heading emoji and event footer disappear."""
        lines = self.notice['body'].split('\n')
        self.assertEqual(lines[0], self.original.split('\n')[0])
        self.assertEqual(lines[1:4], ['> <del>', '>', '> ### Blocked'])
        self.assertEqual(lines[-3:], ['> </del>', '', '**This blocker has been resolved.**'])
        self.assertNotIn('🛑', self.notice['body'])
        restored = '\n'.join(line[2:] if line.startswith('> ') else '' for line in lines[4:-4])
        self.assertEqual(restored.strip('\n'), actions.explain(
            'Source could not be read. Retry after the transport recovers.', '').strip('\n'))
        self.assertNotIn(actions.BLOCKED_RETRY, self.notice['body'])
        self.assertNotIn('<sub>Event ', self.notice['body'])

    def test_retry_and_invalidate_resolve_without_a_new_notification(self):
        """Both supported resumptions edit the existing notice exactly once."""
        for command in ('retry', 'invalidate'):
            with self.subTest(command=command):
                if command == 'invalidate':
                    self.setUp()
                self.gh.comment('/concurro ' + command)
                count = len(self.gh.comments)
                controller = self.tick()
                self.assertNotEqual(controller.state['stage'], 'blocked')
                self.assert_resolved()
                self.assertFalse(controller.state.get('blocked_notices'))
                self.assertEqual(len(self.gh.comments), count)
                updated = self.notice['body']
                self.tick()
                self.assertEqual(self.notice['body'], updated)

    def test_still_blocked_unauthorized_retry_and_stop_do_not_claim_resumption(self):
        """Waiting or stopping is not a resolution."""
        self.tick()
        self.gh.comment('/concurro retry', 'outsider')
        self.assertEqual(self.tick().state['stage'], 'blocked')
        self.gh.comment('/concurro stop')
        self.assertEqual(self.tick().state['stage'], 'stopped')
        self.assertEqual(self.notice['body'], self.original)

    def test_closed_issue_does_not_claim_resumption(self):
        """Closing a blocked issue preserves its historical notice without a false success."""
        self.gh.issue['state'] = 'closed'
        self.assertEqual(self.tick().state['stage'], 'stopped')
        self.assertEqual(self.notice['body'], self.original)

    def test_legacy_notice_is_discovered_after_resumption(self):
        """Older checkpoints did not record Blocked comments; active watchers can still clean them up."""
        controller = self.tick()
        controller.state.pop('blocked_notices', None)
        controller.state.pop('blocked_notice_cursor', None)
        controller.state.update(stage='plan')
        controller.state.pop('reason', None)
        controller.checkpoint()
        self.tick()
        self.assert_resolved()

    def test_changed_notice_is_left_untouched_and_never_rediscovered(self):
        """An operator edit with the old heading cannot block work or be adopted on a later tick."""
        self.notice['body'] += '\nOperator annotation.'
        edited = self.notice['body']
        self.gh.comment('/concurro retry')
        controller = self.tick()
        self.assertNotEqual(controller.state['stage'], 'blocked')
        self.assertFalse(controller.state.get('blocked_notices'))
        self.assertEqual(self.notice['body'], edited)
        controller = self.tick()
        self.assertFalse(controller.state.get('blocked_notices'))
        self.assertEqual(self.notice['body'], edited)

    def test_deleted_notice_is_dropped_without_blocking(self):
        """Deletion before GET or between GET and PATCH does not interfere with resumption."""
        for missing_at in ('GET', 'PATCH'):
            with self.subTest(missing_at=missing_at):
                if missing_at == 'PATCH':
                    self.setUp()
                original = self.gh.repo
                path = f"issues/comments/{self.notice['id']}"

                def deleted(endpoint, method='GET', data=None):
                    if endpoint == path and method == missing_at:
                        self.gh.comments[:] = [c for c in self.gh.comments if c['id'] != self.notice['id']]
                        raise NotFound('comment deleted')
                    return original(endpoint, method, data)

                self.gh.comment('/concurro retry')
                with patch.object(self.gh, 'repo', side_effect=deleted):
                    controller = self.tick()
                self.assertNotEqual(controller.state['stage'], 'blocked')
                self.assertFalse(controller.state.get('blocked_notices'))
                self.assertFalse(self.tick().state.get('blocked_notices'))

    def test_changed_input_needed_request_still_blocks(self):
        """Advisory notice cleanup does not relax the Input-needed edit contract."""
        state = {'stage': 'plan', 'requests': [{'comment': self.notice['id'], 'body': 'original request',
                 'edit': {'state': 'intended', 'body': 'answered request'}}]}
        with self.assertRaisesRegex(Blocked, 'request comment differs'):
            actions.publish(state, self.gh, lambda: None)
        self.assertEqual(self.notice['body'], self.original)

    def test_lost_patch_response_recovers_without_a_second_patch(self):
        """The edit intent survives restart and an exact edited body confirms completion."""
        original, writes = self.gh.repo, []

        def lose(path, method='GET', data=None):
            result = original(path, method, data)
            if method == 'PATCH' and path == f"issues/comments/{self.notice['id']}":
                writes.append(path)
                raise Transient('lost response')
            return result

        self.gh.comment('/concurro retry')
        with patch.object(self.gh, 'repo', side_effect=lose):
            with self.assertRaises(Transient):
                self.tick()
            self.assert_resolved()
            controller = self.tick()
        self.assertEqual(len(writes), 1)
        self.assertFalse(controller.state.get('blocked_notices'))

    def test_new_block_after_resumption_gets_its_own_unresolved_notice(self):
        """Resolved history cannot hide a later failure, including one with the same reason."""
        self.gh.comment('/concurro retry')
        controller = self.tick()
        self.assert_resolved()
        controller.suspend('blocked', 'Source could not be read. Retry after the transport recovers.', outboxes=False)
        notices = [c for c in self.gh.comments if '\n### 🛑 Blocked\n' in c['body']]
        self.assertEqual(len(notices), 1)
        self.assertNotEqual(notices[0]['id'], self.notice['id'])
        self.assertEqual([r['comment'] for r in controller.state['blocked_notices']], [notices[0]['id']])

    def test_queued_notice_published_after_resume_is_resolved(self):
        """A pending POST may finish after the stage changes; its notice still receives the edit."""
        controller = self.tick()
        conversation.queue(controller.state, '🛑 Blocked', 'A second publication was delayed.')
        controller.state.update(stage='plan')
        controller.state.pop('reason', None)
        controller.save()
        notices = [c for c in self.gh.comments if '**This blocker has been resolved.**' in c['body']]
        self.assertEqual(len(notices), 2)
        self.assertFalse(controller.state['conversation']['pending'])
        self.assertFalse(controller.state.get('blocked_notices'))

    def test_discovery_ignores_user_imitation_and_other_notice_types(self):
        """Only exact App-authored event headers qualify for legacy recovery."""
        state = {}
        comments = [dict(self.notice, id=100, user={'login': 'outsider'}),
                    dict(self.notice, id=101, body=self.original.replace('🛑 Blocked', '🛑 Rejected')),
                    dict(self.notice, id=102, body=self.original.replace(conversation.MARKER, '<!-- other:')),
                    dict(self.notice, id=103, body=self.original.replace('🛑 Blocked', 'Blocked')),
                    dict(self.notice, id=104, body=self.original.replace('### 🛑 Blocked', '> ~~Blocked~~'))]
        actions.discover_blocked(state, comments, self.config['bot_login'])
        self.assertEqual([r['comment'] for r in state['blocked_notices']], [103])

    def test_lost_post_is_recovered_before_resolution(self):
        """A lost creation response cannot cause duplicates or conflict with the later edit."""
        state = {'issue': 7, 'stage': 'blocked'}
        conversation.queue(state, '🛑 Blocked', 'Publication was delayed.')
        durable = deepcopy(state)
        original, posts = self.gh.repo, []

        def lose(path, method='GET', data=None):
            result = original(path, method, data)
            if method == 'POST' and path == 'issues/7/comments':
                posts.append(result['id'])
                raise Transient('lost response')
            return result

        with patch.object(self.gh, 'repo', side_effect=lose):
            with self.assertRaises(Transient):
                conversation.publish(state, self.gh, self.config)
            durable['stage'] = 'plan'
            conversation.publish(durable, self.gh, self.config)
        self.assertEqual(len(posts), 1)
        actions.publish(durable, self.gh, lambda: None)
        edited = next(c for c in self.gh.comments if c['id'] == posts[0])
        self.assertIn('**This blocker has been resolved.**', edited['body'])
        self.assertFalse(durable['conversation']['pending'])
        self.assertFalse(conversation.publish(durable, self.gh, self.config))

    def test_failure_suspend_does_not_replay_comment_edits(self):
        """Failure reporting keeps its existing outbox isolation."""
        controller = self.tick()
        with patch.object(actions, 'publish', side_effect=AssertionError('must not replay edits')):
            controller.suspend('blocked', 'Still unavailable.', outboxes=False)
        self.assertEqual(self.notice['body'], self.original)

    def test_muted_history_renders_markdown_inside_the_strikethrough(self):
        """The strike spans paragraphs and collapsed reasons without exposing HTML as text."""
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            self.skipTest('markdown-it-py is needed for rendered Markdown assertions')
        self.gh.comment('/concurro retry')
        self.tick()
        rendered = MarkdownIt('commonmark', {'html': True}).render(self.notice['body'])
        self.assertIn('<blockquote>\n<del>\n<h3>Blocked</h3>', rendered)
        self.assertIn('<details><summary>Full reason</summary>', rendered)
        self.assertNotIn('<code>/concurro retry</code>', rendered)
        self.assertNotIn('<sub>', rendered)
        self.assertIn('</del>\n</blockquote>\n<p><strong>This blocker has been resolved.</strong></p>', rendered)

    def test_edit_intent_is_checkpointed_before_patch(self):
        """Resumption and original-body digests are durable before any edit; no body copies are stored."""
        state = {'stage': 'plan'}
        actions.blocked_posted(state, self.notice['id'], self.original)
        saved = []
        original = self.gh.repo

        def verify(path, method='GET', data=None):
            if method == 'PATCH':
                self.assertEqual(saved[-1]['blocked_notices'],
                                 [{'comment': self.notice['id'], 'digest': digest(self.original),
                                   'resolved_digest': digest(data['body'])}])
                self.assertEqual(saved[-1]['stage'], 'plan')
                self.assertEqual(data['body'], actions.blocked_resolution(self.original))
            return original(path, method, data)

        with patch.object(self.gh, 'repo', side_effect=verify):
            actions.publish(state, self.gh, lambda: saved.append(deepcopy(state)))
        self.assertEqual(saved[-1]['blocked_notices'], [])
        self.assert_resolved()

    def test_long_notices_use_the_same_checkpoint_space_as_short_notices(self):
        """Twenty roughly 5 KB reasons add only ID/digest records, independent of text length."""
        states = []
        for reason in ('Short.', 'Reason ' * 700):
            state = {'stage': 'blocked'}
            comments = [dict(self.notice, id=100 + i, body=self.original.split('\n', 2)[0] +
                             '\n### 🛑 Blocked\n\n' + reason) for i in range(20)]
            actions.discover_blocked(state, comments, self.config['bot_login'])
            self.assertEqual(len(state['blocked_notices']), 20)
            self.assertTrue(all(set(r) == {'comment', 'digest', 'resolved_digest'} for r in state['blocked_notices']))
            states.append(json.dumps(state))
        self.assertEqual(len(states[0]), len(states[1]))
        self.assertLess(len(states[1]), 4000)

    def test_terminal_tasks_do_not_discover_notice_records(self):
        """Merged and closed tasks neither retain old notice bodies nor start cleanup writes."""
        for stage in ('merged', 'observed_merged', 'closed'):
            with self.subTest(stage=stage):
                controller = self.tick()
                controller.state.update(stage=stage)
                controller.state.pop('blocked_notices', None)
                controller.state.pop('blocked_notice_cursor', None)
                controller.checkpoint()
                with patch.object(actions, 'discover_blocked', side_effect=AssertionError('terminal discovery')):
                    result = self.tick()
                self.assertNotIn('blocked_notices', result.state)
                self.assertNotIn('blocked_notice_cursor', result.state)
        state = {'stage': 'closed'}
        actions.discover_blocked(state, [self.notice], self.config['bot_login'])
        self.assertEqual(state, {'stage': 'closed'})

    def test_resolved_digest_covers_both_headings_and_manual_changes(self):
        """Original and resolved hashes identify both forms without storing either body."""
        for original in (self.original, self.original.replace('### 🛑 Blocked', '### Blocked')):
            with self.subTest(original=original):
                state = {}
                actions.blocked_posted(state, 10, original)
                record = state['blocked_notices'][0]
                resolved = actions.blocked_resolution(original)
                self.assertEqual(record['digest'], digest(original))
                self.assertEqual(record['resolved_digest'], digest(resolved))
                self.assertNotEqual(record['resolved_digest'], digest(resolved + '\nAn edit.'))

    def test_only_engine_suffixes_are_removed_from_the_reason(self):
        """Keep paragraphs and command mentions in reasons; remove only generated trailing instructions."""
        marker = self.original.split('\n')[0]
        reason = 'The `/concurro retry` attempt failed.\n\nSecond paragraph with an important detail.'
        for continuation in (actions.BLOCKED_RETRY, actions.BLOCKED_INVALIDATE):
            with self.subTest(continuation=continuation):
                original = marker + '\n### 🛑 Blocked\n\n' + reason + '\n\n' + continuation + '\n\n<sub>Event 12</sub>'
                resolved = actions.blocked_resolution(original)
                self.assertIn('> The `/concurro retry` attempt failed.\n>\n> Second paragraph', resolved)
                self.assertNotIn(continuation, resolved)
                self.assertNotIn('<sub>', resolved)
                self.assertTrue(resolved.endswith('**This blocker has been resolved.**'))
        self.assertIn('> Second paragraph', actions.blocked_resolution(marker + '\n### Blocked\n\n' + reason))
        original = marker + '\n### 🛑 Blocked\n\n' + reason + '\n\n' + actions.BLOCKED_INVALIDATE + '\n\n' + actions.BLOCKED_RETRY
        self.assertIn(actions.BLOCKED_INVALIDATE, actions.blocked_resolution(original),
                      'remove only the last instruction paragraph, not a matching paragraph inside the reason')

    def test_empty_reason_does_not_retain_continuation(self):
        """A notice without a reason retains just the muted heading and resolved message."""
        marker = self.original.split('\n')[0]
        original = marker + '\n### 🛑 Blocked\n\n' + actions.BLOCKED_RETRY + '\n\n<sub>Event 1</sub>'
        self.assertEqual(actions.blocked_resolution(original), marker +
                         '\n> <del>\n>\n> ### Blocked\n>\n> </del>\n\n**This blocker has been resolved.**')
