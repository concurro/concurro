"""Design-review policy adapter for the shared single-owner review coordinator."""

from copy import deepcopy

import design_eligibility
import review_batch
import review_timing
import sessions
from policy import Blocked, SessionFailure, UncertainOutcome, approval_binding, design_roster, digest


def run(controller):
    """Execute one cooperative or fresh design phase without exposing sibling votes."""
    if len(controller.state['design_votes']) == len(design_roster(controller.config)):
        controller.finish_consensus()
        controller.save()
        return
    review_batch.run(Design(controller))


class Design:
    """Durable member records and serial application reuse existing controller policy."""

    def __init__(self, controller):
        """Bind one phase to its current authority and configured reviewer roster."""
        self.controller = controller
        self.transport = controller.agents
        self.contexts = {}
        self.binding = approval_binding(controller.state, controller.config)
        self.roster = design_roster(controller.config)

    @property
    def batch(self):
        """Always use the current state copy after validated report application."""
        return self.controller.state['review_batch']

    def select(self, key):
        """Select one canonical member for existing operation and attribution helpers."""
        self.batch['active'] = key
        self.controller.consuming = None
        return self.batch['members'][key]

    def open(self):
        """Freeze independent contexts before reserving or applying any member result."""
        c = self.controller
        c.require_current_bundle()
        prior = c.state.get('review_batch', {})
        if prior.get('binding') != self.binding:
            if c.state.get('conversation', {}).get('pending'):
                c.save()
            c.state['review_batch'] = {'binding': self.binding, 'stage': 'plan_qc', 'members': {}}
        c.state['review_batch_version'] = 1
        keys = []
        for identity in self.roster:
            key = identity['id']
            if key in c.state['design_votes']:
                continue
            keys.append(key)
            member = self.batch['members'].setdefault(key, {'identity': identity, 'failures': 0})
            self.select(key)
            _, context = c.quality_context('plan_qc', identity)
            self.contexts[key] = deepcopy(context)
            operation = member.get('operation', {})
            # An unfinished previous invocation is uncertain; never poll its old turn ID.
            if (operation.get('status') in ('started', 'uncertain') or
                    operation.get('status') == 'completed' and 'response' not in operation):
                # Reuse the journal, as serial recovery does. A real SessionFailure rotates it.
                operation['status'] = 'uncertain'
        self.batch.pop('active', None)
        return keys

    def retained(self, key):
        """Only a result saved by the controller can bypass a new charged attempt."""
        operation = self.batch['members'][key].get('operation', {})
        return operation.get('status') == 'completed' and 'response' in operation

    def valid(self):
        """Phase transitions or changed authority stop every old batch result."""
        c = self.controller
        return (c.state['stage'] == 'plan_qc' and approval_binding(c.state, c.config) == self.binding)

    def needs_attempt(self, key):
        """Accepted votes and saved results never re-enter admission."""
        return (self.valid() and key not in self.controller.state['design_votes']
                and not self.retained(key))

    def finished(self):
        """The policy's roster, not transport completion order, determines completeness."""
        return all(m['id'] in self.controller.state['design_votes'] for m in self.roster)

    def reserve(self, keys, initial):
        """Charge the whole initial roster atomically; retries charge one member at a time."""
        c = self.controller
        if not keys:
            return {}
        self.transport.check_owner()
        if not self.valid():
            raise Blocked('review binding changed before admission')
        if c.state['calls'] + len(keys) > c.config['max_model_calls']:
            raise Blocked('model-call budget cannot admit the review roster' if initial else 'model-call budget exhausted')
        prepared, reservations = {}, []
        for key in keys:
            member = self.select(key)
            provider = member['identity']['provider']
            context = deepcopy(self.contexts[key])
            repair = member.get('review_repair')
            if repair:
                context['review_validation_feedback'] = {**repair['feedback'],
                    'instruction': design_eligibility.CORRECTION_INSTRUCTION}
            context = c.prepare_context(provider, 'plan_qc', context)
            if member.get('operation', {}).get('status') == 'consumed':
                member['failures'] = 0
            operation = c.operation(provider, 'plan_qc', context)
            context.update(operation['contract'])
            if operation.get('status') == 'completed':
                operation['status'] = 'uncertain'
            if operation.get('status') in ('started', 'uncertain') and operation['uncertain'] + 1 >= 2:
                raise Blocked('model call outcome was uncertain twice; operator reconciliation required')
            session = sessions.prepare(c.state, c.config, provider, 'plan_qc', context)
            design_eligibility.prepare(c.state, context, session, operation)
            credential = self.transport.credential(provider, 'plan_qc', {'repository': c.config['repository'], 'issue': c.state['issue']})
            request = {**context, 'controller_credential': credential.record()}
            if session:
                request['controller_session'] = session
            prepared[key] = self.transport.prepare(provider, 'plan_qc', request)
            reservations.append((key, context, session, credential))
        # Preparation can fail without spending any calls or sending a request.
        for key, context, session, credential in reservations:
            member = self.select(key)
            operation = member['operation']
            c.begin_attempt(operation, context)
            member.update(session=session, status='queued')
            operation['owner_generation'] = getattr(self.transport, 'generation', None)
            c.state['calls'] += 1
            operation['call'] = c.state['calls']
            used = c.state.setdefault('credentials', {}).setdefault(credential.id,
                {**{k: v for k, v in credential.record().items() if k != 'id'}, 'calls': 0})
            used['calls'] += 1
            self.contexts[key] = context
        self.batch.pop('active', None)
        c.save()
        return prepared

    def status(self, key, status):
        """Publish only actual status transitions, never every observation sweep."""
        member = self.batch['members'][key]
        if member.get('status') != status:
            member['status'] = status
            self.batch.pop('active', None)
            self.controller.save()

    def complete(self, key, turn, result, timing):
        """Durably retain a full result before allowing verdict application."""
        c = self.controller
        self.transport.check_owner()
        if not self.valid():
            raise Blocked('review binding changed before result retention')
        member = self.select(key)
        observed = review_timing.normalize(timing)
        if observed:
            member['operation']['timing'] = observed
        response = self.transport.decode(turn, result)
        session = member.get('session')
        if session:
            current = c.state['sessions'][session['role']]
            current['turn'] += 1
            current['completed'] = {'call': member['operation']['call'],
                                    'request': digest(self.contexts[key]), 'response': digest(response)}
        member['status'] = 'complete'
        c.complete(member['operation'], response)
        c.consuming = None

    def apply(self, key):
        """Use the same validator, objection ledger and publication path as serial review."""
        c = self.controller
        self.transport.check_owner()
        member = self.select(key)
        context = deepcopy(self.contexts[key])
        context.update(member['operation']['contract'])
        report = c.recover(member['operation'])
        c.apply_quality_report('plan_qc', member['identity']['provider'], member['identity'], context, report)
        member = self.select(key)
        member.pop('review_draft', None)  # apply/repair publishes the archive before returning
        if member.get('review_repair'):
            member['status'] = 'correcting'
        else:
            member['status'] = 'complete'
        c.consuming = None

    def failed(self, key, error):
        """Isolate explicit uncertain outcomes and session failures within their existing bounds."""
        c = self.controller
        member = self.select(key)
        operation = member['operation']
        if operation.get('status') == 'completed' and 'response' in operation:
            raise error
        operation['status'] = 'uncertain' if getattr(error, 'uncertain', False) else 'failed'
        if isinstance(error, UncertainOutcome):
            # Fresh reviewers have no transcript to restart. Admission checks the existing
            # uncertain-call allowance before charging a replacement attempt.
            member['status'] = 'retrying'
            c.save()
            return
        session = member.get('session')
        if isinstance(error, SessionFailure) and session:
            design_eligibility.restart(c.state, session)
            sessions.restart(c.state, session)
            member['failures'] = member.get('failures', 0) + 1
            member['status'] = 'correcting'
            c.save()
            if member['failures'] < 2:
                return
            raise Blocked('provider session failed twice; a fresh session is prepared for retry') from None
        raise error

    def close(self):
        """Clear only the runtime cursor; preserve saved results and charges on failure."""
        c = self.controller
        batch = c.state.get('review_batch')
        if not batch:
            return
        batch.pop('active', None)
        c.consuming = None
        # Keep terminal records for retry; a successful phase no longer needs them.
        if (c.state['stage'] in ('implement', 'planned', 'plan') or
                c.state['stage'] == 'plan_qc' and approval_binding(c.state, c.config) != self.binding):
            c.state.pop('review_batch', None)
            c.checkpoint()
