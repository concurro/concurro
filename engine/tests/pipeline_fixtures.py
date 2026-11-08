"""Controller lifecycle fixtures; mixins contain helpers, never discovered tests."""

from copy import deepcopy
import shutil
from support import CONFIG, ROOT, TARGET
from controller import Controller
from fakes import FakeAgents, FakeGitHub

ISSUE = 7


class PipelineFixture:
    """Fresh transports and one durable controller transition per tick."""

    def setUp(self):
        """Create fresh transports and require manual plan approval."""
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh = FakeGitHub(self.config)
        self.agents = FakeAgents()

    def tick(self):
        """Reconstruct a controller, advance once, and return it for inspection."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(ISSUE)
        return controller

    def reach(self, stage, limit=30):
        """Advance to a stage within the caller's test bound."""
        for _ in range(limit):
            controller = self.tick()
            if controller.state["stage"] == stage:
                return controller
        self.fail("Did not reach " + stage)

    def persist(self, state):
        """Save intentionally altered evidence as though recovering a damaged state comment."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        controller.state = state
        controller.save()

    def loaded(self):
        """Reload the durable checkpoint without advancing work."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def load(self):
        """Keep the shorter loader name used by existing lifecycle tests."""
        return self.loaded()


class ManualPipelineFixture(PipelineFixture):
    """Manual tests drain planning/reviews per tick and return state, not a controller."""

    def tick(self):
        """Reconstruct the controller each time to prove state survives process exit."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.advance(ISSUE)
        while controller.state and controller.state["stage"] in ("plan", "plan_qc"):
            controller = Controller(self.gh, self.agents, self.config)
            controller.advance(ISSUE)
        return controller.state

    def ready(self):
        """Advance through plan, approval, publication and two independent reviews."""
        state = self.tick()
        self.gh.comment("/concurro approve " + state["plan_id"])
        for _ in range(4):
            state = self.tick()
        self.assertEqual(state["stage"], "gates")
        return state


class ConsensusFixture(PipelineFixture):
    """Enrolled consensus tests return state per tick and support scratch bundle roots."""

    def setUp(self):
        """Create a fresh enrolled task using fake transports only."""
        self.config = deepcopy(CONFIG)
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.agents = FakeAgents()
        self.bundle_root = None
        self.target_root = None

    def tick(self):
        """Reconstruct every controller to test durable attempts and vote recovery."""
        controller = Controller(self.gh, self.agents, self.config, self.bundle_root, self.target_root)
        controller.advance(ISSUE)
        return controller.state

    def reach(self, stage, limit=30):
        """Advance within a hard test bound until the requested stage is reached."""
        for _ in range(limit):
            state = self.tick()
            if state["stage"] == stage:
                return state
        self.fail("Did not reach " + stage)


def copy_roots(root):
    """Copy the engine bundle and the example target charter into one scratch root."""
    shutil.copytree(ROOT / "bundle", root / "bundle")
    shutil.copytree(TARGET / ".concurro", root / ".concurro")
