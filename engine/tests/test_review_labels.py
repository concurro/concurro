"""Review terminology changes presentation without changing authority or replay text."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule
import conversation
import dashboard_runs
import markup
from format_samples import qc_state


class ReviewLabelTests(unittest.TestCase):
    """Keep old protocol values and publication receipts stable."""

    def test_dashboard_labels_do_not_change_audit_rounds(self):
        for phase, label in (('cooperative', 'iterative'), ('fresh', 'fresh')):
            raw = '2 ' + phase
            self.assertEqual(dashboard_runs.display_round('Design review', raw, dashboard_runs.EMPTY), '2 ' + label)
            self.assertEqual(dashboard_runs.round_label('Design review', raw, dashboard_runs.EMPTY), raw)
        self.assertEqual(dashboard_runs.display_round('Code review', '2', dashboard_runs.EMPTY), '2')

    def test_new_reports_freeze_labels_without_rewriting_pending_reports(self):
        for kind in ('qc', 'review_rejection'):
            with self.subTest(kind=kind):
                state, source, details = qc_state()
                details.update(provider='claude', model='m', diagnostic={'detail': 'Invalid source'})
                original_details = deepcopy(details)
                old_meta = conversation.render_v2(state, kind, source, details)[2]
                self.assertEqual(old_meta[-1], 'cooperative')
                conversation.queue(state, None, source=source, kind=kind, details=details)
                event = state['conversation']['pending'][0]
                self.assertEqual(details, original_details)
                self.assertEqual(event['details']['phase'], 'cooperative')
                self.assertEqual(event['index']['round'], '2 cooperative')
                self.assertEqual(event['details']['display_phase'], 'iterative')
                with patch.object(markup, 'review_phase', side_effect=AssertionError('must use stored label')):
                    self.assertEqual(conversation.event_body(state, event), '')
                    new_meta = conversation.render_v2(state, kind, source, event['details'])[2]
                    self.assertEqual(new_meta[-1], 'iterative')
                    self.assertEqual(conversation.render_v2(state, kind, source, original_details)[2], old_meta)
